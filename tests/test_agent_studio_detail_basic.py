"""创作细节工具基础测试（任务 18.2/18.3）：diagnose_plan、get_version_lineage、suggest_shot_query、
get_material_digest、preview_version、run_column 与人工选片三件套，经进程内回环调用真实接口。"""

from __future__ import annotations

import asyncio

import pytest
import test_studio_window
from conftest import FixtureSpeaker, FixtureStoryboarder
from fastapi.testclient import TestClient
from test_agent_studio_cancel import call_tool
from test_studio_window import speech
from test_studio_workspace import setup_workspace, wait_build

from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.studio import workspace as ws

READ = ("diagnose_plan", "get_version_lineage", "suggest_shot_query", "get_material_digest", "preview_version")
selection_job = test_studio_window.refinement  # 复用选片窗口测试的真实素材与召回候选 fixture


def test_specs_domain_and_risk():
    registry = build_registry()
    for name in READ:
        spec = registry.get(name)
        assert (spec.domain, spec.risk, spec.mcp) == ("studio_detail", "read", True)
    column = registry.get("run_column")
    assert (column.domain, column.risk) == ("studio_detail", "danger") and column.preview is not None


def test_diagnose_lineage_and_suggestion(config):
    jid, _w, p = setup_workspace(config)
    storyboarder = FixtureStoryboarder(config)
    app = create_app(config, storyboarder=storyboarder)
    [output] = call_tool(app, "diagnose_plan", {"job_id": jid}, steps=("execute",))
    assert output.value["plan_id"] == p["id"] and "inputs_by_stage" not in output.value
    assert "无硬性问题" in output.text and "构建预检" in output.text
    assert "缺少画面意象" in output.text  # 两个分镜都没有 visual：软性建议

    content = p["shots"]["a"]["content"]
    [lineage] = call_tool(app, "get_version_lineage", {"job_id": jid, "version": content[:8]}, steps=("execute",))
    assert lineage.value["root"] == content and "【当前选用】" in lineage.text
    with pytest.raises(ToolError, match="找不到版本"):
        call_tool(app, "get_version_lineage", {"job_id": jid, "version": "zzzzzzzz"}, steps=("execute",))

    [suggestion] = call_tool(app, "suggest_shot_query", {"job_id": jid, "shot": "#1", "visual": "红色 幕布 拉开"},
                             steps=("execute",))
    assert suggestion.value == {"shot_id": "a", "query": "红色 幕布 画面", "current_query": "红色"}
    assert "edit_shots" in suggestion.text
    # 只是建议：分镜未被修改。
    w = ws.load(config, jid)
    assert ws.version(w, ws.plan(w, p["id"])["shots"]["a"]["content"])["data"]["query"] == "红色"
    with pytest.raises(ToolError, match="HTTP 422.*画面意象"):
        call_tool(app, "suggest_shot_query", {"job_id": jid, "shot": "a"}, steps=("execute",))
    with pytest.raises(ToolError, match="找不到分镜"):
        call_tool(app, "suggest_shot_query", {"job_id": jid, "shot": "#9"}, steps=("execute",))

    [digest] = call_tool(app, "get_material_digest", {"job_id": jid}, steps=("execute",))
    assert digest.value["empty_reason"] and "摘要为空" in digest.text

    with pytest.raises(ToolError, match="只有选片与渲染版本可以预览") as exc:
        call_tool(app, "preview_version", {"job_id": jid, "version": content}, steps=("execute",))
    assert exc.value.code == "unsupported_version"


def test_run_column_previews_submits_and_tracks(config):
    jid, _w, p = setup_workspace(config)
    speaker = FixtureSpeaker(config)
    app = create_app(config, speaker=speaker)
    client = TestClient(app)
    preview, output = call_tool(app, "run_column", {"job_id": jid, "stage": "tts", "force": True})
    assert "配音" in preview.summary and "重新生成" in preview.summary
    labels = [c["label"] for c in preview.changes]
    assert any(label.startswith("- #1 a：尚未生成") for label in labels)
    assert any("付费" in w for w in preview.warnings)
    build_id = output.value["build_id"]
    assert output.watch_build == {"job_id": jid, "build_id": build_id, "plan_id": p["id"]}
    assert output.meta["kind"] == "build" and output.mutation["job_id"] == jid
    done = wait_build(client, jid)["builds"][-1]
    assert done["id"] == build_id and done["status"] == "complete" and done["force"]
    assert [n["key"] for n in done["nodes"]] == ["tts:a", "tts:b"] and speaker.calls == 2

    # 执行/复用：节点已是最新，预览说明复用，执行不再调用配音服务。
    preview, output = call_tool(app, "run_column", {"job_id": jid, "stage": "tts"})
    assert any("预计复用已有版本" in c["label"] for c in preview.changes)
    done = wait_build(client, jid)["builds"][-1]
    assert done["status"] == "complete" and done["force"] is False and speaker.calls == 2


def test_run_column_rejects_changed_inputs_and_unavailable_stage(config):
    jid, _w, p = setup_workspace(config)
    app = create_app(config, speaker=FixtureSpeaker(config))
    client = TestClient(app)

    # 渲染依赖选片与配音：尚未生成时预览按服务端 column_nodes 规则说明会被阻塞。
    [preview] = call_tool(app, "run_column", {"job_id": jid, "stage": "render"}, steps=("preview",))
    assert preview.value == {"blocked": ["a", "b"]}
    assert any("2 个分镜的依赖缺失或失败" in w for w in preview.warnings)
    assert any(c["label"].startswith("- #1 a：将被阻塞") for c in preview.changes)

    # 预览之后方案被修改：执行时服务端拒绝，原因原样转述并提示重新预览。
    spec = build_registry().get("run_column")

    async def go():
        loop_client = MindexClient(app)
        ctx = ToolContext(client=loop_client)
        try:
            args = spec.parse({"job_id": jid, "stage": "tts"})
            await spec.preview(args, ctx)
            w = ws.load(config, jid)
            plan = ws.plan(w, p["id"])
            plan["options"]["duck_gain"] = round(plan["options"].get("duck_gain", 0.2) / 2, 3)
            ws.persist(config, jid, w)
            await spec.execute(args, ctx)
        finally:
            await loop_client.aclose()

    with pytest.raises(ToolError, match="已变化.*重新调用 run_column"):
        asyncio.run(go())
    assert not [b for b in client.get(f"/api/studio/jobs/{jid}/workspace").json()["builds"]]


# ------------------------------------------------------------ 人工选片（任务 18.3）
def test_selection_specs():
    registry = build_registry()
    for name in ("get_selection_candidates", "preview_selection"):
        spec = registry.get(name)
        assert (spec.domain, spec.risk) == ("studio_detail", "read")
    confirm = registry.get("confirm_selection")
    assert (confirm.domain, confirm.risk) == ("studio_detail", "write") and confirm.preview is not None
    preview = registry.get("preview_selection")
    with pytest.raises(ToolError, match="区间方式需要同时给 start 与 end"):
        preview.parse({"shot": "a", "source_id": "s", "start": 1})
    with pytest.raises(ToolError, match="区间方式不使用 candidate"):
        preview.parse({"shot": "a", "source_id": "s", "start": 1, "end": 2, "candidate": "#1"})


def test_selection_requires_speech(selection_job):
    config, jid, _w, _p, _ = selection_job
    app = create_app(config)
    [listing] = call_tool(app, "get_selection_candidates", {"job_id": jid, "shot": "#1"}, steps=("execute",))
    assert listing.value["ready"] is False and "先为该分镜配音" in listing.value["reason"]
    assert [row["candidate_id"] for row in listing.value["candidates"]] == ["shot-0", "shot-1", "shot-2", "shot-8"]
    assert listing.value["tts"] is None and "#2 shot-1" in listing.text
    with pytest.raises(ToolError, match="尚无成功的配音") as exc:
        call_tool(app, "preview_selection", {"job_id": jid, "shot": "a", "candidate": "#2"}, steps=("execute",))
    assert exc.value.code == "tts_missing"


def test_window_selection_preview_and_confirm(selection_job):
    config, jid, w, p, _ = selection_job
    speech(config, jid, w, p)
    ws.persist(config, jid, w)
    app = create_app(config)
    client = TestClient(app)

    [listing] = call_tool(app, "get_selection_candidates", {"job_id": jid, "shot": "a"}, steps=("execute",))
    assert listing.value["ready"] is True and listing.value["window"]["duration"] > 0
    assert listing.images and listing.images[0] == "/api/admin/segments/shot-0/thumbnail"

    [preview] = call_tool(app, "preview_selection", {"job_id": jid, "shot": "a", "candidate": "#2"},
                          steps=("execute",))
    assert preview.value["anchor_changed"] and preview.mutation["job_id"] == jid
    assert preview.value["anchor_ref"]["candidate_id"] == "shot-1"
    fresh = ws.load(config, jid)
    assert ws.plan(fresh, p["id"])["anchors"]["a"]["candidate_id"] == "shot-1"
    assert client.get(preview.media[0]["url"]).status_code == 200
    token = preview.value["preview_token"]
    assert token in preview.text

    # 起点越界：说明合法范围。
    with pytest.raises(ToolError, match="超出合法范围") as exc:
        call_tool(app, "preview_selection", {"job_id": jid, "shot": "a", "start": 999}, steps=("execute",))
    assert exc.value.code == "out_of_range"

    before = ws.plan(ws.load(config, jid), p["id"])["shots"]["a"].get("select")
    confirm_preview, output = call_tool(app, "confirm_selection", {"preview_token": token[:10]})
    assert "人工选片" in confirm_preview.summary and confirm_preview.changes[0]["before"] == "无"
    fresh = ws.load(config, jid)
    selected = ws.plan(fresh, p["id"])["shots"]["a"]["select"]
    assert selected != before and output.value["select_version"] == selected
    item = ws.version(fresh, selected)
    assert item["data"]["manual"] and item["data"]["anchor_ref"]["candidate_id"] == "shot-1"
    assert output.mutation == {"domain": "studio", "job_id": jid, "plan_id": p["id"]}
    # 已确认的预览不能重复确认。
    with pytest.raises(ToolError, match="重新调用 preview_selection") as exc:
        call_tool(app, "confirm_selection", {"preview_token": token})
    assert exc.value.code == "preview_stale"


def test_range_selection_rejects_after_plan_change(selection_job):
    config, jid, w, p, store = selection_job
    speech(config, jid, w, p)
    ws.persist(config, jid, w)
    app = create_app(config)
    source = store.one("SELECT id FROM sources")["id"]
    [preview] = call_tool(app, "preview_selection",
                          {"job_id": jid, "shot": "a", "source_id": source, "start": 3, "end": 5},
                          steps=("execute",))
    assert preview.value["mode"] == "range" and preview.value["range"]["source_id"] == source
    assert not preview.value["anchor_changed"] and preview.mutation is None

    w = ws.load(config, jid)
    plan = ws.plan(w, p["id"])
    plan["options"]["duck_gain"] = round(plan["options"].get("duck_gain", 0.2) / 2, 3)
    ws.persist(config, jid, w)
    with pytest.raises(ToolError, match="方案已变化.*重新调用 preview_selection") as exc:
        call_tool(app, "confirm_selection", {"preview_token": preview.value["preview_token"]})
    assert exc.value.code == "preview_stale"
