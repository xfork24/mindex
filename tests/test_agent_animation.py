"""智能助手的 AI 动画暴露（需求 14）：edit_shots 的 animation 来源、run_node(animate)、get/edit_animation_source
（预览确认、静态校验、查找替换）、分镜详情中的动画状态与录制媒体、动画生成角色的档案工具。

动画生成模型用确定性的 FakeAnimator，录制用 FakeCapture（ffmpeg 纯色画面），绝不调用真实 LLM 或 Chromium。
"""

from __future__ import annotations

import pytest
from conftest import FixtureSpeaker
from fastapi.testclient import TestClient
from test_agent_aigc import content, read, wait_build
from test_agent_studio_recall import run
from test_studio_animation import FakeAnimator, FakeCapture, anim, page, shot  # noqa: F401 — anim 为 fixture
from test_studio_sources import setup

from mindex.agent.registry import ToolError
from mindex.agent.tools import build_registry
from mindex.agent.tools.common import PlanView
from mindex.agent.tools.shot_details import render_detail, shot_detail
from mindex.api import create_app

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb"]


@pytest.fixture
def agent_job(anim):  # noqa: F811
    job_id, _, _ = setup(anim, [shot(SHOTS[0], "红色开场。"), shot(SHOTS[1], "增长曲线收尾。")])
    animator, capture = FakeAnimator(), FakeCapture()
    with TestClient(create_app(anim, speaker=FixtureSpeaker(anim), animator=animator,
                               animation_capture=capture)) as client:
        yield anim, client, job_id, animator


def as_animation(client, config, job_id, brief="折线图逐年上升，标注 2024 年峰值"):
    return run(client, config, "edit_shots", {"job_id": job_id, "operations": [
        {"op": "update", "shot": "#2", "fields": {"source": {"kind": "animation", "brief": brief}}}]})


def test_registry_places_source_tools_in_studio_detail():
    registry = build_registry()
    assert registry.get("get_animation_source").domain == "studio_detail"
    assert registry.get("get_animation_source").risk == "read"
    edit = registry.get("edit_animation_source")
    assert edit.domain == "studio_detail" and edit.risk == "write" and edit.preview is not None and edit.mcp
    assert len([s for s in registry.all() if s.domain == "studio"]) <= 28


def test_edit_shots_sets_animation_source_and_explains_rebuild(agent_job):
    config, client, job_id, _ = agent_job
    preview, _, _ = as_animation(client, config, job_id)
    change = next(c for c in preview.changes if "画面来源" in c["label"])
    assert change["before"] == "素材召回（默认）" and "AI 动画" in change["after"] and "折线图" in change["after"]
    warning = next(w for w in preview.warnings if "AI 动画" in w)
    assert "生成动画" in warning and "录制" in warning
    assert content(client, job_id, SHOTS[1])["source"] == {"kind": "animation", "brief": "折线图逐年上升，标注 2024 年峰值"}
    with pytest.raises(ToolError, match="不支持字段"):
        run(client, config, "edit_shots", {"job_id": job_id, "operations": [
            {"op": "update", "shot": "#2", "fields": {"source": {"kind": "animation", "prompt": "x"}}}]})
    _, summary, _ = read(client, config, "get_workspace_summary", {"job_id": job_id})
    assert "AI 动画 画面说明" in summary.text and "生成动画：" in summary.text


def test_run_node_animate_then_read_edit_source_and_details(agent_job):
    config, client, job_id, animator = agent_job
    with pytest.raises(ToolError, match="不是 animation"):
        run(client, config, "run_node", {"job_id": job_id, "stage": "animate", "shot": "#1"}, execute=False)
    as_animation(client, config, job_id)
    with pytest.raises(ToolError, match="还没有动画源码"):
        read(client, config, "get_animation_source", {"job_id": job_id, "shot": "#2"})
    run(client, config, "run_node", {"job_id": job_id, "stage": "tts", "shot": "#2"})
    assert wait_build(client, job_id)["builds"][-1]["status"] == "complete"
    preview, _, _ = run(client, config, "run_node", {"job_id": job_id, "stage": "animate", "shot": "#2"},
                        execute=False)
    assert preview.summary.startswith("执行单个节点：生成动画")
    assert any("动画生成模型" in w and "人工修改" in w for w in preview.warnings)
    build_preview, _, _ = run(client, config, "run_build", {"job_id": job_id}, execute=False)
    assert any("1 个 animation 分镜" in w for w in build_preview.warnings)
    run(client, config, "run_node", {"job_id": job_id, "stage": "animate", "shot": "#2"})
    data = wait_build(client, job_id)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    assert animator.calls[-1]["brief"] == "折线图逐年上升，标注 2024 年峰值"

    _, source, _ = read(client, config, "get_animation_source", {"job_id": job_id, "shot": "#2"})
    original = page("折线图逐年上升，标注 2024 年峰值#1")
    assert source.value["origin"] == "generated" and source.value["chars"] == len(original)
    assert original in source.text and "模型生成" in source.text and "尚未按此源码录制" in source.text
    assert source.value["preview_url"].endswith("/animation.html")
    assert source.focus["stage"] == "animate"
    _, paged, _ = read(client, config, "get_animation_source", {"job_id": job_id, "shot": "#2", "limit": 200})
    assert paged.value["has_more"] and paged.value["next_offset"] == 200 and "offset=200" in paged.text

    # 查找替换：不唯一或找不到时拒绝；违规源码在预览阶段就被服务端校验拦下，不出确认卡。
    with pytest.raises(ToolError, match="没有找到"):
        run(client, config, "edit_animation_source", {"job_id": job_id, "shot": "#2", "replacements": [
            {"find": "不存在的片段", "replace": "x"}]}, execute=False)
    with pytest.raises(ToolError, match="出现了 2 次"):
        run(client, config, "edit_animation_source", {"job_id": job_id, "shot": "#2", "replacements": [
            {"find": "requestAnimationFrame", "replace": "x"}]}, execute=False)
    with pytest.raises(ToolError, match="未通过安全检查.*fetch"):
        run(client, config, "edit_animation_source", {"job_id": job_id, "shot": "#2", "replacements": [
            {"find": "</body>", "replace": "<script>fetch('/api/x')</script></body>"}]}, execute=False)
    versions_before = len(client.get(f"/api/studio/jobs/{job_id}/workspace").json()["versions"])
    preview, output, sent = run(client, config, "edit_animation_source", {"job_id": job_id, "shot": "#2",
                                                                          "replacements": [
        {"find": "background:#204080", "replace": "background:#802040"}]})
    labels = {c["label"]: c["after"] for c in preview.changes}
    assert labels["源码大小"] == f"{len(original)} → {len(original)} 字符" and labels["静态校验"].startswith("通过")
    assert any("重新录制" in w for w in preview.warnings) and any("不会被重新生成覆盖" in w for w in preview.warnings)
    patches = [entry for entry in sent if entry[0] == "PATCH"]
    assert len(patches) == 2 and patches[0][1].endswith(f"/shots/{SHOTS[1]}/animation")  # dry_run + 正式提交
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    assert len(data["versions"]) == versions_before + 1
    vid = data["plans"][data["active_plan"]]["shots"][SHOTS[1]]["animate"]
    assert output.value["animate_version"] == vid and data["versions"][vid]["data"]["origin"] == "manual"
    assert output.mutation["domain"] == "studio"
    edited = client.get(f"/api/studio/jobs/{job_id}/versions/{vid}/animation.html").text
    assert "background:#802040" in edited

    # 只重新录制该镜：渲染以人工源码为准，不调用动画生成模型。
    calls = len(animator.calls)
    run(client, config, "run_node", {"job_id": job_id, "stage": "render", "shot": "#2"})
    assert wait_build(client, job_id)["builds"][-1]["status"] == "complete"
    assert len(animator.calls) == calls

    _, output, _ = read(client, config, "get_shot_details", {"job_id": job_id, "refs": ["#2"]})
    detail = output.value["results"][0]["detail"]
    assert detail["animation"]["origin"] == "manual" and detail["animation"]["recording"]["current"]
    assert "动画源码：版本" in output.text and "人工修改" in output.text and "动画录制：渲染版本" in output.text
    assert "/animation.html" in output.text
    [video] = [m for m in output.media if "动画录制" in m["title"]]
    assert video["kind"] == "video" and client.get(video["url"]).status_code == 200
    view = PlanView(client.get(f"/api/studio/jobs/{job_id}/workspace").json())
    assert shot_detail(view, view.order[0], 1)["animation"] is None
    assert "动画源码" not in render_detail(shot_detail(view, view.order[0], 1))

    # 整份替换同样可用；非 animation 分镜拒绝。
    run(client, config, "edit_animation_source", {"job_id": job_id, "shot": "#2", "html": page("整份替换")})
    assert "整份替换" in read(client, config, "get_animation_source", {"job_id": job_id, "shot": "#2"})[1].text
    with pytest.raises(ToolError, match="不是 AI 动画"):
        run(client, config, "edit_animation_source", {"job_id": job_id, "shot": "#1", "html": page("x")},
            execute=False)


def test_animator_profile_tools(agent_job):
    config, client, _, _ = agent_job
    preview, output, _ = run(client, config, "create_model_profile", {
        "role": "animator", "name": "动画 DeepSeek", "provider": "deepseek",
        "config": {"backend": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                   "max_tokens": 12000, "max_html_kb": 300, "retries": 1, "max_capture_seconds": 90}})
    pid = output.value["profile_id"]
    preview, _, _ = run(client, config, "activate_model_profile", {"profile_id": pid})
    assert any("激活即启用动画生成" in w for w in preview.warnings)
    role = next(r for r in client.get("/api/settings").json()["roles"] if r["role"] == "animator")
    assert role["label"] == "动画生成" and role["active_profile_id"] == pid
    assert role["effective"]["max_capture_seconds"] == 90 and role["effective"]["enabled"] is True
    _, presets, _ = read(client, config, "list_provider_presets", {"role": "animator"})
    assert {row["provider"] for row in presets.value["presets"]} >= {"deepseek", "anthropic", "zhipu", "aliyun"}
