"""创作取消与删除方案工具：cancel_build（构建 / 旧任务步骤执行）与 delete_plan，经进程内回环调用真实接口。"""

from __future__ import annotations

import asyncio
import threading

import pytest
from agent_helpers import new_session
from conftest import FixtureSpeaker, FixtureStoryboarder
from fastapi.testclient import TestClient
from test_studio_api import create_legacy_job
from test_studio_cancel import wait_until
from test_studio_columns import request_body
from test_studio_workspace import setup_workspace, wait_build

from mindex.agent.llm.scripted import ScriptedLlm
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.agent.tools.studio_build import watched
from mindex.api import create_app
from mindex.studio import studio
from mindex.studio import studio_workspace as ws


def call_tool(app, name, arguments, *, steps=("preview", "execute")):
    """按顺序调用 preview / execute，共享同一个 ToolContext（与运行时确认流程一致）。"""
    spec = build_registry().get(name)

    async def go():
        client = MindexClient(app)
        ctx = ToolContext(client=client)
        try:
            args = spec.parse(arguments)
            return [await getattr(spec, step)(args, ctx) for step in steps]
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_specs_risk_and_preview():
    registry = build_registry()
    cancel, delete = registry.get("cancel_build"), registry.get("delete_plan")
    assert (cancel.risk, cancel.domain) == ("write", "studio") and cancel.preview is not None and cancel.mcp
    assert (delete.risk, delete.domain) == ("danger", "studio") and delete.preview is not None and delete.mcp
    assert "plan_id" in delete.input_model.model_json_schema()["required"]


def test_cancel_running_build_previews_and_cancels(config):
    entered, release = threading.Event(), threading.Event()

    class Blocking(FixtureSpeaker):
        def speak(self, text, voice=None):
            entered.set()
            assert release.wait(10)
            return super().speak(text, voice)

    jid, w, p = setup_workspace(config)
    app = create_app(config, speaker=Blocking(config))
    client = TestClient(app)
    response = client.post(f"/api/studio/jobs/{jid}/plans/{p['id']}/regenerate-column",
                           json=request_body(config, jid, w, p, "tts"))
    assert response.status_code == 202, response.text
    bid = response.json()["id"]
    try:
        assert entered.wait(5)
        preview, output = call_tool(app, "cancel_build", {"job_id": jid})
    finally:
        release.set()
    assert bid in preview.summary and "照常完成" in preview.warnings[0]
    changes = {c["label"]: c.get("after") for c in preview.changes}
    assert changes["执行中节点（照常完成）"] == "配音（分镜 a）" and changes["将标为已取消的节点"] == 1
    assert preview.focus["build_id"] == bid and preview.focus["section"] == "log"
    assert output.value == {"build_id": bid, "status": "cancelling"}
    assert output.watch_build == {"job_id": jid, "build_id": bid, "plan_id": p["id"]}
    assert output.mutation["domain"] == "studio" and output.mutation["job_id"] == jid

    done = wait_build(client, jid)["builds"][-1]
    nodes = {n["key"]: n for n in done["nodes"]}
    assert done["status"] == "cancelled" and nodes["tts:a"]["status"] == "complete"
    assert nodes["tts:b"]["status"] == "blocked" and nodes["tts:b"]["error"] == "已取消"

    # 已结束的构建：预览即拒绝；省略 build_id 时说明没有可取消的执行。
    with pytest.raises(ToolError, match="不在执行中") as exc:
        call_tool(app, "cancel_build", {"job_id": jid, "build_id": bid}, steps=("preview",))
    assert exc.value.code == "not_running"
    with pytest.raises(ToolError, match="没有正在执行的构建或步骤") as exc:
        call_tool(app, "cancel_build", {"job_id": jid}, steps=("execute",))
    assert exc.value.code == "not_running"
    with pytest.raises(ToolError, match="找不到构建执行"):
        call_tool(app, "cancel_build", {"job_id": jid, "build_id": "missing"}, steps=("preview",))
    with pytest.raises(ToolError, match="HTTP 404"):
        call_tool(app, "cancel_build", {"job_id": "missing"}, steps=("preview",))


def test_cancel_legacy_step_execution(config):
    entered, release = threading.Event(), threading.Event()

    class Slow(FixtureStoryboarder):
        def split(self, text, vocabulary=None):
            entered.set()
            assert release.wait(10)
            return super().split(text, vocabulary)

    app = create_app(config, storyboarder=Slow(config))
    client = TestClient(app)
    client.config = config
    jid = create_legacy_job(client, "红色首先出现。随后蓝色的天空调转。")
    assert client.post(f"/api/studio/jobs/{jid}/run").status_code == 202
    try:
        assert entered.wait(5)
        preview, output = call_tool(app, "cancel_build", {"job_id": jid})
    finally:
        release.set()
    changes = {c["label"]: c["after"] for c in preview.changes}
    assert "步骤执行" in preview.summary and changes["当前步骤（照常完成）"] == "拆分镜"
    assert "召回" in changes["不再执行的步骤"]
    assert output.value == {"job_id": jid, "status": "cancelling"} and output.watch_build is None

    def finished():
        job = client.get(f"/api/studio/jobs/{jid}").json()["job"]
        return job if job["execution"]["status"] != "running" else None

    job = wait_until(finished)
    assert job["execution"]["status"] == "cancelled" and job["steps"]["recall"]["status"] == "pending"
    assert not (studio.job_dir(config, jid) / "steps.cancel").exists()


def test_watched_skips_duplicate_build_card(config):
    app = create_app(config, agent_llm=ScriptedLlm())
    with TestClient(app) as client:
        sid = new_session(client)
        runtime = app.state.agent_runtime
        ctx = ToolContext(client=None, session_id=sid, extra={"runtime": runtime})
        assert not watched(ctx, "b1")
        runtime.store.append(sid, "plugin:mindex/build-watch", {"job_id": "j", "build_id": "b1"})
        assert watched(ctx, "b1") and not watched(ctx, "b2")
        assert not watched(ToolContext(client=None), "b1")  # MCP：没有会话，照常交给调用方查询


def test_delete_plan_preview_and_execute(config):
    jid, _w, p = setup_workspace(config)
    app = create_app(config, storyboarder=FixtureStoryboarder(config))
    client = TestClient(app)
    base = f"/api/studio/jobs/{jid}"

    with pytest.raises(ToolError, match="至少保留一个方案") as exc:
        call_tool(app, "delete_plan", {"job_id": jid, "plan_id": p["id"]}, steps=("preview",))
    assert exc.value.code == "last_plan"
    with pytest.raises(ToolError, match="HTTP 422.*至少保留一个方案"):
        call_tool(app, "delete_plan", {"job_id": jid, "plan_id": p["id"]}, steps=("execute",))

    second = client.post(f"{base}/plans", json={"name": "副本", "copy_from": p["id"]}).json()["active_plan"]
    third = client.post(f"{base}/plans", json={"name": "三号", "copy_from": p["id"]}).json()["active_plan"]
    with pytest.raises(ToolError, match="方案不存在") as exc:
        call_tool(app, "delete_plan", {"job_id": jid, "plan_id": "missing"}, steps=("preview",))
    assert exc.value.code == "missing_plan"
    before = ws.load(config, jid)

    # 删除当前方案：预览说明删除后切到其余方案中最新创建的一个（second）。
    preview, output = call_tool(app, "delete_plan", {"job_id": jid, "plan_id": third})
    changes = {c["label"]: c for c in preview.changes}
    assert "三号" in preview.summary and changes["是否当前方案"]["after"] == "是"
    assert second in changes["删除后的当前方案"]["after"] and "副本" in changes["删除后的当前方案"]["after"]
    assert "版本与文件保留" in preview.warnings[0] and "mindex clean" in preview.warnings[0]
    assert output.value["active_plan"] == second and output.value["deleted_plan"] == third
    assert output.mutation == {"domain": "studio", "job_id": jid, "plan_id": second, "deleted_plan": third}

    # 删除非当前方案：当前方案不变。
    preview, output = call_tool(app, "delete_plan", {"job_id": jid, "plan_id": p["id"]})
    changes = {c["label"]: c for c in preview.changes}
    assert changes["是否当前方案"]["after"] == "否" and second in changes["删除后的当前方案"]["after"]
    assert output.value["active_plan"] == second and output.value["plans"] == [second]
    after = ws.load(config, jid)
    assert after["versions"].keys() == before["versions"].keys() and after["builds"] == before["builds"]


def test_delete_plan_rejected_while_build_running(config):
    entered, release = threading.Event(), threading.Event()

    class Blocking(FixtureSpeaker):
        def speak(self, text, voice=None):
            entered.set()
            assert release.wait(10)
            return super().speak(text, voice)

    jid, _w, p = setup_workspace(config)
    app = create_app(config, speaker=Blocking(config))
    client = TestClient(app)
    base = f"/api/studio/jobs/{jid}"
    other = client.post(f"{base}/plans", json={"name": "副本", "copy_from": p["id"]}).json()["active_plan"]
    client.post(f"{base}/plans/{p['id']}/activate")
    w = ws.load(config, jid)
    response = client.post(f"{base}/plans/{p['id']}/regenerate-column",
                           json=request_body(config, jid, w, ws.plan(w, p["id"]), "tts"))
    assert response.status_code == 202, response.text
    try:
        assert entered.wait(5)
        snapshot = set(ws.load(config, jid)["plans"])
        with pytest.raises(ToolError, match="有构建正在执行") as exc:
            call_tool(app, "delete_plan", {"job_id": jid, "plan_id": other}, steps=("preview",))
        assert exc.value.code == "build_running"
        with pytest.raises(ToolError, match="HTTP 409"):
            call_tool(app, "delete_plan", {"job_id": jid, "plan_id": other}, steps=("execute",))
    finally:
        release.set()
    wait_build(client, jid)
    assert set(ws.load(config, jid)["plans"]) == snapshot == {p["id"], other}
