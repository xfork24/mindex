"""素材浏览、整理与报告工具的冒烟测试（真实 FFmpeg 处理的测试素材 + 进程内回环）。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent.loopback import MindexClient
from agent.registry import ToolContext, ToolError
from agent.tools import build_registry
from api import create_app
from processing.pipeline import Pipeline


@pytest.fixture
def prepared(config, video_file):
    Pipeline(config).prepare(video_file)
    return config, None


def run_tool(app, name, arguments, *, preview=False):
    spec = build_registry().get(name)

    async def go():
        client = MindexClient(app)
        try:
            handler = spec.preview if preview else spec.execute
            return await handler(spec.parse(arguments), ToolContext(client=client))
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_report_endpoint(prepared):
    config, _ = prepared
    with TestClient(create_app(config)) as client:
        source_id = client.get("/api/admin/sources").json()["items"][0]["id"]
        report = client.get(f"/api/admin/sources/{source_id}/report")
        assert report.status_code == 200, report.text
        assert report.json()["planned_shots"] >= 1 and report.json()["configuration_matches"] is True
        assert client.get("/api/admin/sources/missing/report").status_code == 404


def test_browse_and_curation_tools(prepared):
    config, _ = prepared
    app = create_app(config)
    with TestClient(app) as client:
        source_id = client.get("/api/admin/sources").json()["items"][0]["id"]

        source = run_tool(app, "get_source", {"source_id": source_id})
        assert "分镜：" in source.text and "对白：" in source.text and "人物：" in source.text
        assert source.focus == {"page": "admin", "source_id": source_id}
        assert source.media and source.media[0]["url"] == f"/api/admin/sources/{source_id}/playlist"

        segments = run_tool(app, "list_segments", {"source_id": source_id, "page_size": 5})
        first = segments.value["items"][0]
        assert segments.value["total"] >= 1 and len(segments.media) <= 5
        assert first["id"] in segments.text

        details = run_tool(app, "get_segment_details", {"segment_id": first["id"]})
        assert "ASR" in details.text and "LLM 分析" in details.text
        assert details.focus == {"page": "admin", "source_id": source_id, "segment_id": first["id"]}

        report = run_tool(app, "get_source_report", {"source_id": source_id})
        assert "计划镜头" in report.text and "布局与当前配置一致" in report.text

        args = {"target": "source", "id": source_id, "hidden": True, "notes": "测试备注"}
        preview = run_tool(app, "update_curation", args, preview=True)
        assert "隐藏后该内容不会出现在检索结果中" in preview.warnings
        assert any("一并隐藏" in w for w in preview.warnings)
        assert {c["label"] for c in preview.changes} == {"隐藏", "备注"}

        out = run_tool(app, "update_curation", {"target": "segment", "id": first["id"], "favorite": True})
        assert out.mutation["domain"] == "library" and out.value["changed"] == {"favorite": True}
        segment = client.get(f"/api/admin/segments/{first['id']}").json()
        # 只提交给出的字段：收藏生效，备注与隐藏保持原值。
        assert segment["favorite"] is True and segment["hidden"] is False and segment["notes"] == ""

        with pytest.raises(ToolError):
            run_tool(app, "update_curation", {"target": "segment", "id": first["id"]})
