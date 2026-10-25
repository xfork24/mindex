"""创作细节工具补充测试（任务 18.4，需求 15.1/15.3/15.5/15.6/15.7）。

基础用例见 ``test_agent_studio_detail_basic.py``；这里补齐：

- diagnose_plan 转述缺字体与「旁白已变化」原因；
- run_column 在输入变化时被拒并提示重新预览（经运行时确认卡与直接调用两条路径）、构建运行中被拒；
- 两条人工选片路径经运行时会话端到端（preview_selection 直接执行，confirm_selection 需确认卡）；
- revision 变化、预览缓存丢失、跨会话作用域都要求重新预览；未配音时的确认路径；构建运行中选片工具被拒；
- preview_version 对真实选片版本与渲染版本生成可播放的预览。

全部使用 conftest 的隔离数据目录、Fixture 适配器与真实 FFmpeg 素材。
"""

from __future__ import annotations

import asyncio
import re
import threading

import pytest
import test_studio_window
from agent_helpers import new_session, result_text, results, send, wait_idle, wait_pending
from conftest import FixtureSpeaker
from fastapi.testclient import TestClient
from test_agent_studio_cancel import call_tool
from test_studio_columns import request_body
from test_studio_window import speech
from test_studio_workspace import setup_workspace, wait_build

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.agent.tools.studio_detail import SELECTION_PREVIEWS
from mindex.api import create_app
from mindex.studio import studio
from mindex.studio import workspace as ws

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行

selection_job = test_studio_window.refinement  # 真实素材 + 召回候选池（10 个 1 秒镜头）


# ------------------------------------------------------------ 助手
def call_as(app, name, arguments, *, session_id=None, steps=("preview", "execute")):
    """与 call_tool 相同，但指定会话作用域（选片预览按会话隔离）。"""
    spec = build_registry().get(name)

    async def go():
        client = MindexClient(app)
        ctx = ToolContext(client=client, session_id=session_id)
        try:
            args = spec.parse(arguments)
            return [await getattr(spec, step)(args, ctx) for step in steps]
        finally:
            await client.aclose()

    return asyncio.run(go())


def approve(client, sid, name):
    pending = wait_pending(client, sid, "approval")
    assert pending["name"] == name, pending
    client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})
    return pending


def named_results(events) -> list[tuple[str, dict]]:
    names = {e["data"]["callId"]: e["data"]["name"] for e in events if e["type"] == "tool/call"}
    return [(names[r["message"]["toolCallId"]], r) for r in results(events)]


def last_token(request) -> str:
    text = request.messages[-1]["content"][0]["text"]
    return re.search(r"preview_token=(\w+)", text).group(1)


def page(jid):
    return {"page": "studio", "job_id": jid}


class Blocking(FixtureSpeaker):
    """第一次配音时阻塞，制造一个运行中的构建。"""

    def __init__(self, config):
        super().__init__(config)
        self.entered, self.release = threading.Event(), threading.Event()

    def speak(self, text, voice=None):
        self.entered.set()
        assert self.release.wait(20)
        return super().speak(text, voice)


def start_blocking_build(config, app, jid, pid):
    client = TestClient(app)
    w = ws.load(config, jid)
    response = client.post(f"/api/studio/jobs/{jid}/plans/{pid}/regenerate-column",
                           json=request_body(config, jid, w, ws.plan(w, pid), "tts"))
    assert response.status_code == 202, response.text
    return client, response.json()["id"]


def add_tts(config, jid, sid="a"):
    """为分镜登记一个与当前输入一致的配音版本（不调用配音服务）并选用。"""
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    spec = ws.specification(config, jid, w, p, "tts", sid)
    vid = ws.add_version(config, jid, w, "tts", sid, inputs=spec["inputs"], parameters=spec["parameters"],
                         signature=spec["signature"])
    p["shots"][sid]["tts"] = vid
    ws.persist(config, jid, w)
    return vid


# ------------------------------------------------------------ diagnose_plan
def test_diagnose_plan_relays_font_and_narration_reasons(config, tmp_path):
    jid, _w, p = setup_workspace(config)
    add_tts(config, jid, "a")
    w = ws.load(config, jid)
    plan = ws.plan(w, p["id"])
    shots = [studio.Shot.model_validate(ws.version(w, plan["shots"][sid]["content"])["data"])
             for sid in ws.ordered_shots(w, plan)]
    shots[0] = shots[0].model_copy(update={"text": "红色的开场。"})
    ws.set_board(config, jid, w, plan, shots)
    plan["options"]["burn"] = True
    ws.persist(config, jid, w)
    missing = tmp_path / "missing.ttf"
    config.studio.subtitle.font_file = str(missing)

    [output] = call_tool(create_app(config), "diagnose_plan", {"job_id": jid}, steps=("execute",))
    assert f"字体渲染：字幕字体文件不存在：{missing}" in output.text
    assert "- 配音 #1 a：已过期（旁白已变化）" in output.text
    assert "文案覆盖：" in output.text  # 改写旁白后分镜不再按顺序覆盖原文
    [node] = output.value["nodes"]
    assert node["state"] == "stale" and node["reasons"] == ["旁白已变化"]
    assert output.focus["shots"] == ["a", "b"]  # 两镜都缺画面意象（软性建议），a 另有过期节点


# ------------------------------------------------------------ run_column
def test_run_column_rejected_when_narration_changes_after_preview(config):
    """经运行时：确认卡展示之后用户在页面改了旁白，确认执行被服务端拒绝，助手收到“重新预览”提示。"""
    jid, _w, p = setup_workspace(config)
    llm = ScriptedLlm([
        [call("load_tools", domains=["studio_detail"])],
        [call("run_column", job_id=jid, stage="tts")],
        [say("方案已变化，我重新预览。")],
    ])
    app = create_app(config, speaker=FixtureSpeaker(config), agent_llm=llm)
    with TestClient(app) as client:
        sid = new_session(client)
        send(client, sid, "整列配音", context=page(jid))
        pending = wait_pending(client, sid, "approval")
        assert pending["name"] == "run_column" and pending["risk"] == "danger" and not pending["allowGrant"]
        w = ws.load(config, jid)
        plan = ws.plan(w, p["id"])
        shots = [studio.Shot.model_validate(ws.version(w, plan["shots"][s]["content"])["data"])
                 for s in ws.ordered_shots(w, plan)]
        ws.set_board(config, jid, w, plan, [shots[0].model_copy(update={"text": "红色的开场。"}), shots[1]])
        ws.persist(config, jid, w)
        client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})
        events = wait_idle(client, sid)
        builds = client.get(f"/api/studio/jobs/{jid}/workspace").json()["builds"]
    name, result = named_results(events)[-1]
    assert name == "run_column" and result["message"]["isError"]
    text = result_text(result)
    assert "HTTP 422" in text and "方案已变化" in text and "重新调用 run_column" in text
    assert builds == []


def test_run_column_rejects_outdated_shot_inputs(config):
    """revision 未变但提交的逐镜输入已过期：服务端的“分镜 X 输入版本已变化”原样转述并提示重新预览。"""
    jid, _w, _p = setup_workspace(config)
    app = create_app(config, speaker=FixtureSpeaker(config))
    spec = build_registry().get("run_column")

    async def go():
        client = MindexClient(app)
        ctx = ToolContext(client=client)
        try:
            args = spec.parse({"job_id": jid, "stage": "tts"})
            await spec.preview(args, ctx)
            _job, _pid, body = ctx.extra["planned"]["column"]
            body["inputs_by_shot"]["a"] = {"content": "0" * 32}
            await spec.execute(args, ctx)
        finally:
            await client.aclose()

    with pytest.raises(ToolError, match="分镜 a 输入版本已变化.*重新调用 run_column") as exc:
        asyncio.run(go())
    assert exc.value.code == "http_422"
    assert ws.load(config, jid)["builds"] == []


def test_run_column_and_diagnose_while_build_running(config):
    jid, _w, p = setup_workspace(config)
    speaker = Blocking(config)
    app = create_app(config, speaker=speaker)
    client, bid = start_blocking_build(config, app, jid, p["id"])
    try:
        assert speaker.entered.wait(10)
        with pytest.raises(ToolError, match=f"构建 {bid} 正在执行") as exc:
            call_tool(app, "run_column", {"job_id": jid, "stage": "tts"}, steps=("preview",))
        assert exc.value.code == "build_running" and exc.value.value == {"build_id": bid}
        with pytest.raises(ToolError, match="正在执行") as exc:
            call_tool(app, "run_column", {"job_id": jid, "stage": "tts", "force": True}, steps=("execute",))
        assert exc.value.code == "build_running"
        [diagnosis] = call_tool(app, "diagnose_plan", {"job_id": jid}, steps=("execute",))
        assert diagnosis.value["running_build"] == bid and f"构建 {bid} 正在执行" in diagnosis.text
    finally:
        speaker.release.set()
    assert wait_build(client, jid)["builds"][-1]["status"] == "complete"


# ------------------------------------------------------------ 人工选片：运行时端到端
def test_window_and_range_selection_via_runtime(selection_job):
    config, jid, w, p, store = selection_job
    speech(config, jid, w, p, "a")
    speech(config, jid, w, p, "b")
    ws.persist(config, jid, w)
    source = store.one("SELECT id FROM sources")["id"]
    llm = ScriptedLlm([
        [call("load_tools", domains=["studio_detail"])],
        [call("get_selection_candidates", job_id=jid, shot="#1")],
        [call("preview_selection", job_id=jid, shot="#1", candidate="#2")],
        lambda request: [call("confirm_selection", preview_token=last_token(request))],
        [call("preview_selection", job_id=jid, shot="#2", source_id=source, start=3, end=5)],
        lambda request: [call("confirm_selection", preview_token=last_token(request))],
        [say("两个分镜的选片都已确认。")],
    ])
    app = create_app(config, agent_llm=llm)
    with TestClient(app) as client:
        sid = new_session(client)
        send(client, sid, "第一镜换成第二个候选，第二镜用 3 到 5 秒", context=page(jid))
        first = approve(client, sid, "confirm_selection")
        assert first["risk"] == "write" and first["allowGrant"]
        assert "人工选片" in first["preview"]["summary"] and "定长窗口" in first["preview"]["changes"][0]["after"]
        assert {"label": "参考候选", "after": "shot-1"} in first["preview"]["changes"]
        second = approve(client, sid, "confirm_selection")
        assert "任意区间" in second["preview"]["changes"][0]["after"]
        events = wait_idle(client, sid)

    named = named_results(events)
    assert [n for n, _ in named] == ["load_tools", "get_selection_candidates", "preview_selection",
                                     "confirm_selection", "preview_selection", "confirm_selection"]
    assert not any(r["message"].get("isError") for _, r in named), [result_text(r) for _, r in named]
    # preview_selection 是 read：直接执行，没有确认卡；只有两次 confirm_selection 弹出确认。
    asked = [e["data"]["name"] for e in events if e["type"] == "plugin:mindex/approval-asked"]
    assert asked == ["confirm_selection", "confirm_selection"]
    assert "已确认分镜 #1 a 的人工选片" in result_text(named[3][1])
    assert "已确认分镜 #2 b 的人工选片" in result_text(named[5][1])

    fresh = ws.load(config, jid)
    plan = ws.plan(fresh, p["id"])
    window = ws.version(fresh, plan["shots"]["a"]["select"])
    assert window["status"] == "complete" and window["data"]["manual"]
    assert window["data"]["anchor_ref"]["candidate_id"] == "shot-1"
    assert plan["anchors"]["a"]["candidate_id"] == "shot-1"
    # 区间方式：确认后人工区间写入 plan.ranges，并按该区间生成新的选片版本。
    ranged = plan["ranges"]["b"]
    assert ranged["source_id"] == source and ranged["start"] == pytest.approx(3)
    assert ranged["end"] <= 5 + 1e-6
    chosen = ws.version(fresh, plan["shots"]["b"]["select"])
    assert chosen["status"] == "complete" and chosen["data"]["manual"]
    video = chosen["data"]["event"]["video"]
    assert video["source_id"] == source and video["start"] == pytest.approx(3)
    # 确认后预览从缓存移除。
    assert not [k for k in SELECTION_PREVIEWS._items if k[0] == sid]


# ------------------------------------------------------------ 人工选片：要求重新预览
def test_window_confirm_rejected_after_revision_change(selection_job):
    config, jid, w, p, _ = selection_job
    speech(config, jid, w, p)
    ws.persist(config, jid, w)
    app = create_app(config)
    [preview] = call_as(app, "preview_selection", {"job_id": jid, "shot": "a", "candidate": "#3"},
                        session_id="s-rev", steps=("execute",))
    token = preview.value["preview_token"]
    before = ws.plan(ws.load(config, jid), p["id"])["shots"]["a"].get("select")

    fresh = ws.load(config, jid)
    plan = ws.plan(fresh, p["id"])
    plan["options"]["duck_gain"] = round(plan["options"].get("duck_gain", 0.2) / 2, 3)
    ws.persist(config, jid, fresh)
    with pytest.raises(ToolError, match="方案已变化.*重新调用 preview_selection") as exc:
        call_as(app, "confirm_selection", {"preview_token": token}, session_id="s-rev", steps=("preview",))
    assert exc.value.code == "preview_stale"
    # 跳过预览直接执行（缓存仍在）：服务端同样拒绝，选片不变。
    with pytest.raises(ToolError, match="重新调用 preview_selection") as exc:
        call_as(app, "confirm_selection", {"preview_token": token}, session_id="s-rev", steps=("execute",))
    assert exc.value.code == "preview_stale"
    assert ws.plan(ws.load(config, jid), p["id"])["shots"]["a"].get("select") == before


def test_preview_scope_and_lost_cache_require_new_preview(selection_job):
    config, jid, w, p, store = selection_job
    speech(config, jid, w, p)
    ws.persist(config, jid, w)
    app = create_app(config)
    source = store.one("SELECT id FROM sources")["id"]
    [preview] = call_as(app, "preview_selection",
                        {"job_id": jid, "shot": "a", "source_id": source, "start": 2, "end": 4},
                        session_id="s-one", steps=("execute",))
    token = preview.value["preview_token"]

    # 其他会话（以及 MCP 作用域）不能确认这个预览。
    for scope in ("s-two", None):
        with pytest.raises(ToolError, match="来自其他会话") as exc:
            call_as(app, "confirm_selection", {"preview_token": token}, session_id=scope, steps=("preview",))
        assert exc.value.code == "preview_stale"
    # 本会话可以（只做预览校验，不执行）。
    [confirm] = call_as(app, "confirm_selection", {"preview_token": token}, session_id="s-one", steps=("preview",))
    assert confirm.value["preview_token"] == token

    # 服务重启 / 缓存过期：进程内缓存丢失后提示重新预览。
    SELECTION_PREVIEWS._items.clear()
    with pytest.raises(ToolError, match="已过期.*重新调用 preview_selection") as exc:
        call_as(app, "confirm_selection", {"preview_token": token}, session_id="s-one")
    assert exc.value.code == "preview_stale"

    # TTL 过期同样按缓存丢失处理。
    [preview] = call_as(app, "preview_selection",
                        {"job_id": jid, "shot": "a", "source_id": source, "start": 2, "end": 4},
                        session_id="s-one", steps=("execute",))
    ttl = SELECTION_PREVIEWS.ttl
    SELECTION_PREVIEWS.ttl = -1
    try:
        with pytest.raises(ToolError, match="已过期") as exc:
            call_as(app, "confirm_selection", {"preview_token": preview.value["preview_token"]},
                    session_id="s-one", steps=("preview",))
    finally:
        SELECTION_PREVIEWS.ttl = ttl
    assert exc.value.code == "preview_stale"
    assert ws.plan(ws.load(config, jid), p["id"])["shots"]["a"].get("select") is None


def test_confirm_requires_speech(selection_job):
    """预览之后配音被撤下（例如回退到无配音的状态）：确认前即提示先配音，不提交。"""
    config, jid, w, p, _ = selection_job
    speech(config, jid, w, p)
    ws.persist(config, jid, w)
    app = create_app(config)
    [preview] = call_as(app, "preview_selection", {"job_id": jid, "shot": "#1", "candidate": "#1"},
                        session_id="s-tts", steps=("execute",))
    fresh = ws.load(config, jid)
    ws.plan(fresh, p["id"])["shots"]["a"].pop("tts")
    ws.persist(config, jid, fresh)
    with pytest.raises(ToolError, match="尚无成功的配音.*run_column stage=tts") as exc:
        call_as(app, "confirm_selection", {"preview_token": preview.value["preview_token"]},
                session_id="s-tts", steps=("preview",))
    assert exc.value.code == "tts_missing"
    [listing] = call_tool(app, "get_selection_candidates", {"job_id": jid, "shot": "a"}, steps=("execute",))
    assert listing.value["ready"] is False and listing.value["tts"] is None
    assert "尚无成功的配音" in listing.value["reason"] and "选片未就绪" in listing.text


def test_selection_tools_rejected_while_build_running(selection_job):
    config, jid, w, p, _ = selection_job
    speech(config, jid, w, p)
    ws.persist(config, jid, w)
    speaker = Blocking(config)
    app = create_app(config, speaker=speaker)
    [preview] = call_as(app, "preview_selection", {"job_id": jid, "shot": "a", "candidate": "#2"},
                        session_id="s-busy", steps=("execute",))
    client, bid = start_blocking_build(config, app, jid, p["id"])
    try:
        assert speaker.entered.wait(10)
        attempts = [
            ("get_selection_candidates", {"job_id": jid, "shot": "a"}, ("execute",)),
            ("preview_selection", {"job_id": jid, "shot": "a", "candidate": "#1"}, ("execute",)),
            ("confirm_selection", {"preview_token": preview.value["preview_token"]}, ("preview",)),
        ]
        for name, arguments, steps in attempts:
            with pytest.raises(ToolError, match=f"构建 {bid} 正在执行.*cancel_build") as exc:
                call_as(app, name, arguments, session_id="s-busy", steps=steps)
            assert exc.value.code == "build_running", name
    finally:
        speaker.release.set()
    assert wait_build(client, jid)["builds"][-1]["status"] == "complete"


# ------------------------------------------------------------ preview_version
def test_preview_version_for_real_selection_and_render(selection_job):
    config, jid, w, p, _ = selection_job
    speech(config, jid, w, p)
    selected = ws.execute(config, jid, w, p, "select", "a")
    rendered = ws.execute(config, jid, w, p, "render", "a")
    ws.persist(config, jid, w)
    app = create_app(config)
    client = TestClient(app)

    [output] = call_tool(app, "preview_version", {"job_id": jid, "version": selected[:8]}, steps=("execute",))
    assert output.value["version_id"] == selected and "带配音的选片片段" in output.text
    assert output.media[0]["kind"] == "video" and output.media[0]["url"] == output.value["url"]
    response = client.get(output.value["url"])
    assert response.status_code == 200 and response.content[4:8] == b"ftyp"
    assert output.focus["shots"] == ["a"] and output.focus["stage"] == "select"

    [output] = call_tool(app, "preview_version", {"job_id": jid, "version": rendered}, steps=("execute",))
    assert output.value["version_id"] == rendered and "渲染结果" in output.text
    response = client.get(output.value["url"])
    assert response.status_code == 200 and response.content[4:8] == b"ftyp"
    assert output.focus["stage"] == "render"
