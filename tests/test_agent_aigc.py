"""智能助手的 AIGC 暴露（需求 14）：edit_shots 的 aigc 字段与费用提示、附件作首帧参考图、run_node(generate)、
分镜详情中的生成状态与生成画面媒体、视频生成角色的档案工具。

视频生成服务一律用确定性的 FakeVideoGenerator，绝不调用真实付费接口。
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.registry import ToolError
from agent.tools.common import PlanView
from agent.tools.media import shot_media
from agent.tools.shot_details import render_detail, shot_detail
from api import create_app
from conftest import FixtureSpeaker
from test_agent_studio_recall import attachment, run
from test_studio_aigc import SECRET, FakeVideoGenerator, aigc, aigc_shot, shot  # noqa: F401 — aigc 为 fixture
from test_studio_sources import setup

WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
}


@pytest.fixture
def agent_job(aigc):  # noqa: F811
    job_id, _, _ = setup(aigc, [shot("shot-a1daad59aa", "红色开场。"), shot("shot-925281d5bb", "蓝色结尾。")])
    fake = FakeVideoGenerator()
    with TestClient(create_app(aigc, speaker=FixtureSpeaker(aigc), video_generator=fake)) as client:
        yield aigc, client, job_id, fake


def read(client, config, name, arguments):
    """只读工具：没有预览，直接执行；返回 (None, output, 请求列表)，与 run 的返回形状一致。"""

    from agent.attachments import AttachmentStore
    from agent.loopback import MindexClient
    from agent.registry import ToolContext
    from agent.tools import build_registry

    async def go():
        loop = MindexClient(
            client.app,
            resolve_attachment=AttachmentStore(config.data_dir).resolver(session_id=None, owner="mcp"),
        )
        try:
            spec = build_registry().get(name)
            return await spec.execute(
                spec.parse(arguments), ToolContext(client=loop, page={"page": "studio"})
            )
        finally:
            await loop.aclose()

    return None, asyncio.run(go()), []


def content(client, job_id, sid):
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    p = data["plans"][data["active_plan"]]
    return data["versions"][p["shots"][sid]["content"]]["data"]


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "red").save(buffer, "PNG")
    return buffer.getvalue()


def wait_build(client, job_id, timeout=120):

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
        if data["builds"] and data["builds"][-1]["status"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("构建未结束")


def test_edit_shots_sets_aigc_fields_and_warns_cost(agent_job):
    config, client, job_id, _ = agent_job
    preview, output, _ = run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {
                    "op": "update",
                    "shot": "#2",
                    "fields": {
                        "source": {"kind": "aigc", "prompt": "极光雪山", "negative": "模糊", "seed": 42}
                    },
                }
            ],
        },
    )
    change = next(c for c in preview.changes if "画面来源" in c["label"])
    assert change["before"] == "素材召回（默认）"
    assert "AI 生成视频" in change["after"] and "极光雪山" in change["after"] and "种子 42" in change["after"]
    warning = next(w for w in preview.warnings if "AI 生成视频" in w)
    assert "可能产生费用" in warning and "生成画面" in warning and "选窗" in warning
    assert content(client, job_id, "shot-925281d5bb")["source"] == {
        "kind": "aigc",
        "prompt": "极光雪山",
        "negative": "模糊",
        "image_id": None,
        "seed": 42,
    }
    assert "AI 生成视频" in output.text
    with pytest.raises(ToolError, match="不支持字段"):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [
                    {
                        "op": "update",
                        "shot": "#2",
                        "fields": {"source": {"kind": "aigc", "media_id": "a" * 64}},
                    }
                ],
            },
        )
    # 服务端校验：参考图必须已上传。
    with pytest.raises(ToolError, match="参考图片缺失"):
        run(
            client,
            config,
            "edit_shots",
            {
                "job_id": job_id,
                "operations": [
                    {
                        "op": "update",
                        "shot": "#2",
                        "fields": {"source": {"kind": "aigc", "image_id": "c" * 64}},
                    }
                ],
            },
        )


def test_attach_image_sets_aigc_reference_with_single_confirmation(agent_job):
    config, client, job_id, _ = agent_job
    run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {
                    "op": "update",
                    "shot": "#2",
                    "fields": {"source": {"kind": "aigc", "prompt": "极光雪山", "seed": 7}},
                }
            ],
        },
    )
    ref = attachment(client, png_bytes(), "首帧.png", "image/png")
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    pid = data["active_plan"]
    preview, output, sent = run(
        client,
        config,
        "attach_query_media",
        {"job_id": job_id, "shot": "#2", "attachment": ref, "purpose": "source"},
    )
    assert "AI 生成视频" in preview.summary and "一次确认" in preview.summary
    assert "参考图 首帧.png" in preview.changes[1]["after"]
    assert any("可能产生费用" in w for w in preview.warnings)
    writes = [entry for entry in sent if entry[0] != "GET"]
    assert writes == [
        ("POST", f"/api/studio/jobs/{job_id}/plans/{pid}/query-media"),
        ("PATCH", f"/api/studio/jobs/{job_id}/plans/{pid}"),
    ]
    source = content(client, job_id, "shot-925281d5bb")["source"]
    assert source == {
        "kind": "aigc",
        "prompt": "极光雪山",
        "negative": None,
        "image_id": output.value["media_id"],
        "seed": 7,
    }


def test_run_node_generate_previews_cost_and_shows_generated_media(agent_job):
    config, client, job_id, fake = agent_job
    with pytest.raises(ToolError, match="不是 aigc"):
        run(client, config, "run_node", {"job_id": job_id, "stage": "generate", "shot": "#1"}, execute=False)
    run(
        client,
        config,
        "edit_shots",
        {
            "job_id": job_id,
            "operations": [
                {
                    "op": "update",
                    "shot": "#2",
                    "fields": {"source": {"kind": "aigc", "prompt": "极光雪山", "seed": 42}},
                }
            ],
        },
    )
    run(client, config, "run_node", {"job_id": job_id, "stage": "tts", "shot": "#2"})
    assert wait_build(client, job_id)["builds"][-1]["status"] == "complete"

    preview, _, _ = run(
        client, config, "run_node", {"job_id": job_id, "stage": "generate", "shot": "#2"}, execute=False
    )
    assert preview.summary.startswith("执行单个节点：生成画面")
    assert any("可能产生费用" in w for w in preview.warnings)
    from agent.tools import build_registry

    assert build_registry().get("run_node").risk == "danger"

    # 生成前：详情显示生成状态缺失；构建预检提示生成费用。
    view = PlanView(client.get(f"/api/studio/jobs/{job_id}/workspace").json())
    detail = shot_detail(view, view.order[1], 2)
    assert detail["generation"] == {
        "available": False,
        "status": "missing",
    } and "生成画面：missing" in render_detail(detail)
    assert shot_detail(view, view.order[0], 1)["generation"] is None
    build_preview, _, _ = run(client, config, "run_build", {"job_id": job_id}, execute=False)
    assert any("1 个 aigc 分镜" in w and "可能产生费用" in w for w in build_preview.warnings)

    run(client, config, "run_node", {"job_id": job_id, "stage": "generate", "shot": "#2"})
    data = wait_build(client, job_id)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    [request] = fake.requests
    assert request.prompt == "极光雪山" and request.seed == 42

    _, output, _ = read(client, config, "get_shot_details", {"job_id": job_id, "refs": ["#2"]})
    assert "生成画面：版本" in output.text and "提示词「极光雪山」" in output.text
    detail = output.value["results"][0]["detail"]
    assert detail["stages"]["generate"]["status"] == "complete"
    assert detail["generation"]["available"] and detail["generation"]["prompt"] == "极光雪山"
    assert output.media and all(m["url"].startswith("/api/") for m in output.media)
    [video] = [m for m in output.media if "生成画面" in m["title"]]
    assert video["kind"] == "video" and client.get(video["url"]).status_code == 200
    view = PlanView(client.get(f"/api/studio/jobs/{job_id}/workspace").json())
    assert any("生成画面" in m["title"] for m in shot_media(view, view.order[1]) if m)
    _, versions, _ = read(
        client, config, "list_versions", {"job_id": job_id, "shot": "#2", "stage": "generate"}
    )
    assert versions.media and versions.media[0]["kind"] == "video"
    _, summary, _ = read(client, config, "get_workspace_summary", {"job_id": job_id})
    assert "AI 生成视频" in summary.text and "生成画面" in summary.text
    assert SECRET not in output.text + summary.text


def test_video_generator_profile_tools_validate_comfyui_and_activate(agent_job):
    config, client, _, _ = agent_job
    with pytest.raises(ToolError, match="缺少 prompt"):
        run(
            client,
            config,
            "create_model_profile",
            {
                "role": "video_generator",
                "name": "本机 ComfyUI",
                "provider": "self",
                "config": {
                    "backend": "comfyui",
                    "base_url": "http://127.0.0.1:8188",
                    "comfyui_workflow": WORKFLOW,
                    "comfyui_mapping": {"seed": "3.inputs.seed"},
                },
            },
            execute=False,
        )
    with pytest.raises(ToolError, match="界面格式"):
        run(
            client,
            config,
            "create_model_profile",
            {
                "role": "video_generator",
                "name": "本机 ComfyUI",
                "provider": "self",
                "config": {
                    "backend": "comfyui",
                    "comfyui_workflow": {"nodes": [], "links": []},
                    "comfyui_mapping": {"prompt": "6.inputs.text"},
                },
            },
            execute=False,
        )
    preview, output, _ = run(
        client,
        config,
        "create_model_profile",
        {
            "role": "video_generator",
            "name": "本机 ComfyUI",
            "provider": "self",
            "config": {
                "backend": "comfyui",
                "base_url": "http://127.0.0.1:8188",
                "fps": 8,
                "durations": [2, 4],
                "comfyui_workflow": WORKFLOW,
                "comfyui_mapping": {"prompt": "6.inputs.text", "seed": "3.inputs.seed"},
            },
        },
    )
    workflow = next(c for c in preview.changes if c["label"] == "comfyui_workflow")
    assert workflow["after"].startswith("2 个节点（摘要 ")
    assert any("不发起生成" in w and "可能产生费用" in w for w in preview.warnings)
    pid = output.value["profile_id"]
    stored = next(
        p
        for r in client.get("/api/settings").json()["roles"]
        if r["role"] == "video_generator"
        for p in r["profiles"]
        if p["id"] == pid
    )
    assert stored["config"]["comfyui_mapping"] == {"prompt": "6.inputs.text", "seed": "3.inputs.seed"}
    assert stored["config"]["fps"] == 8

    with pytest.raises(ToolError, match="没有节点 9"):
        run(
            client,
            config,
            "update_model_profile",
            {"profile_id": pid, "config": {"comfyui_mapping": {"prompt": "9.inputs.text"}}},
            execute=False,
        )
    preview, _, _ = run(
        client, config, "update_model_profile", {"profile_id": pid, "config": {"max_wait": 600}}
    )
    assert preview.changes == [{"label": "max_wait", "before": None, "after": 600.0}]

    preview, _, _ = run(client, config, "activate_model_profile", {"profile_id": pid})
    assert any("可能产生费用" in w for w in preview.warnings)
    role = next(r for r in client.get("/api/settings").json()["roles"] if r["role"] == "video_generator")
    assert role["label"] == "视频生成" and role["active_profile_id"] == pid
