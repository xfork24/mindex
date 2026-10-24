"""素材任务与上传工具的基本测试（真实 FFmpeg 处理的测试素材 + 进程内回环）。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.processing.pipeline import Pipeline
from mindex.shared.storage import Store

REF = "attachment://" + "a" * 32
SRT = "1\n00:00:00,000 --> 00:00:01,000\n你好\n".encode()


@pytest.fixture
def prepared(config, video_file):
    Pipeline(config).prepare(video_file)
    return config, video_file


def run_tool(app, name, arguments, *, preview=False, attachment=None):
    spec = build_registry().get(name)

    async def go():
        client = MindexClient(app, resolve_attachment=(lambda ref: attachment) if attachment else None)
        try:
            handler = spec.preview if preview else spec.execute
            return await handler(spec.parse(arguments), ToolContext(client=client))
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_specs_risk_domain_and_arguments():
    registry = build_registry()
    risks = {name: registry.get(name).risk for name in (
        "start_source_task", "get_task_status", "retry_task", "cancel_task", "upload_source", "upload_subtitle")}
    assert risks == {"start_source_task": "danger", "get_task_status": "read", "retry_task": "write",
                     "cancel_task": "write", "upload_source": "write", "upload_subtitle": "write"}
    for name in risks:
        spec = registry.get(name)
        assert spec.domain == "library" and spec.mcp
        assert (spec.preview is not None) == (spec.risk != "read")
    # 不开放删除素材（物理删除）。
    assert not any("delete_source" in s.name for s in registry.all())

    start = registry.get("start_source_task")
    with pytest.raises(ToolError) as exc:
        start.parse({"source_id": "s", "work": "shots", "method": "asr"})
    assert exc.value.code == "invalid_arguments" and "method" in str(exc.value)
    with pytest.raises(ToolError):
        start.parse({"source_id": "s", "work": "publish"})
    from mindex.agent.tools.library_tasks import task_body

    assert task_body(start.parse({"source_id": "s", "work": "publish", "target": "analysis", "scope": "extra"})) == \
        {"work": "analysis", "scope": "extra"}
    assert task_body(start.parse({"source_id": "s", "work": "dialogue", "method": "subtitle",
                                  "subtitle_file": "f" * 64})) == {"method": "subtitle", "subtitle_file": "f" * 64}
    assert task_body(start.parse({"source_id": "s", "work": "shots"})) == {}


def test_task_status_cancel_and_preview(prepared):
    config, _ = prepared
    app = create_app(config)
    with TestClient(app) as client:
        source_id = client.get("/api/admin/sources").json()["items"][0]["id"]
        store = Store(config.data_dir)
        store.execute("INSERT INTO source_tasks(id,source_id,work,status,config,options) "
                      "VALUES('q1',?,'analysis','queued','{}','{}')", (source_id,))
        store.execute("INSERT INTO dialogue_tasks(id,source_id,status,config,options) "
                      "VALUES('d1',?,'running','{}','{}')", (source_id,))

        preview = run_tool(app, "start_source_task", {"source_id": source_id, "work": "analysis", "publish": True},
                           preview=True)
        assert any("GPU" in w for w in preview.warnings) and any("子任务" in w for w in preview.warnings)
        assert {c["label"] for c in preview.changes} == {"工作", "完成后自动发布检索"}

        status = run_tool(app, "get_task_status", {"kind": "source", "task_id": "q1"})
        assert "排队中" in status.text and status.value["work"] == "analysis"
        assert status.focus == {"page": "admin", "source_id": source_id}

        assert "已取消" in run_tool(app, "cancel_task", {"kind": "source", "task_id": "q1"}, preview=True).changes[0]["after"]
        out = run_tool(app, "cancel_task", {"kind": "source", "task_id": "q1"})
        assert out.value["status"] == "cancelled" and "任务已取消" in out.text
        assert out.mutation["domain"] == "library"

        status = run_tool(app, "get_task_status", {"kind": "source", "task_id": "q1"})
        assert "已取消" in status.text and "retry_task(kind=source)" in status.text

        # 非 queued/running 再取消：409 原样转述服务端原因。
        with pytest.raises(ToolError) as exc:
            run_tool(app, "cancel_task", {"kind": "source", "task_id": "q1"})
        assert exc.value.code == "http_409" and "只有排队中或运行中的任务可以取消" in str(exc.value)

        # 运行中的任务只设置取消标记。
        running = run_tool(app, "cancel_task", {"kind": "dialogue", "task_id": "d1"})
        assert running.value["status"] == "running" and "已请求取消" in running.text
        assert running.value["work"] == "dialogue"

        retry = run_tool(app, "retry_task", {"kind": "dialogue", "task_id": "d1"}, preview=True)
        assert any("服务端会拒绝" in w for w in retry.warnings)


def test_upload_source_and_subtitle(prepared):
    config, video_file = prepared
    app = create_app(config)
    with TestClient(app) as client:
        source_id = client.get("/api/admin/sources").json()["items"][0]["id"]

        out = run_tool(app, "upload_source", {"attachment": REF},
                       attachment=(video_file.name, video_file, "video/mp4"))
        assert out.value == {"source_id": source_id, "duplicate": True}
        assert out.focus == {"page": "admin", "source_id": source_id}

        sub = run_tool(app, "upload_subtitle", {"source_id": source_id, "attachment": REF},
                       attachment=("ep1.srt", SRT, "application/x-subrip"))
        assert len(sub.value["id"]) == 64 and f"subtitle_file={sub.value['id']}" in sub.text
        assert sub.mutation == {"domain": "library", "source_id": source_id, "subtitle_id": sub.value["id"]}

        with pytest.raises(ToolError) as exc:
            run_tool(app, "upload_subtitle", {"source_id": source_id, "attachment": REF},
                     attachment=("notes.txt", b"hello", "text/plain"))
        assert exc.value.code == "http_422"

        # 附件无法解析：不发送目标请求，转为 attachment_invalid。
        with pytest.raises(ToolError) as exc:
            run_tool(app, "upload_source", {"attachment": REF})
        assert exc.value.code == "attachment_invalid"
