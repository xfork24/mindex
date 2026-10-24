"""素材领域工具的补充测试（真实 FFmpeg 素材 + 进程内回环 + Fixture 嵌入）。

基础覆盖见 test_agent_library_browse.py / test_agent_library_tasks.py / test_agent_task_watch.py；
这里补齐：字段透传、隐藏后检索被过滤、报告结构、GPU 锁冲突 409 的转述、
经会话运行时的任务终态 followup、重试状态限制、上传的后缀与大小限制。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from agent_helpers import new_session, send, wait_idle, wait_pending
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.shared.processing_lock import ProcessingLock, SharedFileLock
from mindex.shared.storage import Store

REF = "attachment://" + "b" * 32
SRT = "1\n00:00:00,000 --> 00:00:01,000\n你好\n".encode()
TERMINAL = {"complete", "failed", "cancelled", "interrupted"}


@pytest.fixture
def prepared(config, video_file):
    source_id = Pipeline(config).prepare(video_file)
    return config, source_id


@pytest.fixture
def indexed(prepared):
    config, source_id = prepared
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot", "moment"), activate=True)
    return config, source_id, embedder


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


def gpu_held(config) -> SharedFileLock:
    """模拟另一个进程（例如 CLI 处理）持有 GPU 独占锁。"""
    return SharedFileLock(config.data_dir / "pipeline.lock", exclusive=True).acquire()


def wait_status(app, kind, task_id, statuses, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = run_tool(app, "get_task_status", {"kind": kind, "task_id": task_id})
        if out.value["status"] in statuses:
            return out
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 未进入 {statuses}")


def retry_when_unlocked(app, kind, task_id, timeout=10):
    """上一轮 worker 线程在置终态后才释放任务锁；锁释放前重试会得到 409。"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return run_tool(app, "retry_task", {"kind": kind, "task_id": task_id})
        except ToolError as exc:
            if "正在运行" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.05)


# ---------- 字段透传与隐藏过滤 ----------

def test_browse_fields_pass_through(prepared):
    config, source_id = prepared
    app = create_app(config)
    with TestClient(app) as client:
        rest = client.get(f"/api/admin/sources/{source_id}", params={"detail": "work"}).json()
        source = run_tool(app, "get_source", {"source_id": source_id})
        assert {k: source.value[k] for k in ("id", "name", "media", "hidden", "favorite", "notes")} == \
            {k: rest[k] for k in ("id", "name", "media", "hidden", "favorite", "notes")}
        assert set(source.value["work"]) == {"shots", "dialogue", "analysis", "persons"}
        for name, work in source.value["work"].items():
            for key in ("status", "can_start", "start_reason", "can_publish", "result"):
                if key in rest["work"][name]:
                    assert work[key] == rest["work"][name][key], (name, key)

        # list_segments 的 kind / visibility / 分页参数原样透传。
        rest_shots = client.get(f"/api/admin/sources/{source_id}/segments",
                                params={"kind": "shot", "page": 1, "page_size": 50}).json()
        shots = run_tool(app, "list_segments", {"source_id": source_id, "page_size": 50})
        assert [r["id"] for r in shots.value["items"]] == [s["id"] for s in rest_shots["items"]]
        assert shots.value["total"] == rest_shots["total"] >= 1
        page2 = run_tool(app, "list_segments", {"source_id": source_id, "page_size": 1, "page": 2})
        if rest_shots["total"] >= 2:
            assert [r["id"] for r in page2.value["items"]] == [rest_shots["items"][1]["id"]]
        assert page2.value["page"] == 2 and page2.value["page_size"] == 1

        moments = run_tool(app, "list_segments", {"source_id": source_id, "kind": "moment", "page_size": 50})
        assert moments.value["total"] >= 1 and "精细片段" in moments.text
        first = moments.value["items"][0]["id"]
        details = run_tool(app, "get_segment_details", {"segment_id": first})
        rest_details = client.get(f"/api/admin/segments/{first}/details").json()
        parent = details.value["segment"]["parent_id"]
        assert details.value["segment"]["id"] == first and parent == rest_details["segment"]["parent_id"]
        assert details.value["asr"]["status"] == rest_details["asr"]["status"]
        assert details.value["llm"]["status"] == rest_details["llm"]["status"]
        assert details.value["person"]["status"] == rest_details["person"]["status"]
        assert details.value["truncated"] is False

        # parent_id 只列出该镜头下的精细片段。
        children = run_tool(app, "list_segments",
                            {"source_id": source_id, "kind": "moment", "parent_id": parent, "page_size": 50})
        assert first in {r["id"] for r in children.value["items"]}
        assert children.value["total"] <= moments.value["total"]
        for row in children.value["items"][:3]:
            assert client.get(f"/api/admin/segments/{row['id']}").json()["parent_id"] == parent

        # update_curation 字段到 REST：显示名、备注生效；空显示名恢复原名。
        original = rest["name"]
        out = run_tool(app, "update_curation",
                       {"target": "source", "id": source_id, "display_name": "测试显示名", "notes": "备注一"})
        assert out.value["changed"] == {"display_name": "测试显示名", "notes": "备注一"}
        assert out.mutation == {"domain": "library", "source_id": source_id, "target": "source", "id": source_id}
        after = client.get(f"/api/admin/sources/{source_id}").json()
        assert after["name"] == "测试显示名" and after["custom_name"] == "测试显示名" and after["notes"] == "备注一"
        assert after["favorite"] is False and after["hidden"] is False
        assert run_tool(app, "get_source", {"source_id": source_id}).value["notes"] == "备注一"
        run_tool(app, "update_curation", {"target": "source", "id": source_id, "display_name": ""})
        restored = client.get(f"/api/admin/sources/{source_id}").json()
        assert restored["name"] == original and restored["notes"] == "备注一"


def test_hidden_segment_filtered_from_search(indexed):
    config, source_id, embedder = indexed
    app = create_app(config, embedder)
    with TestClient(app) as client:
        query = {"text": "绿色 草地", "granularity": "shot", "limit": 20}
        before = run_tool(app, "search_media", query)
        ids = [r["segment_id"] for r in before.value["results"]]
        assert ids, before.text
        target = ids[0]

        preview = run_tool(app, "update_curation", {"target": "segment", "id": target, "hidden": True}, preview=True)
        assert "隐藏后该内容不会出现在检索结果中" in preview.warnings
        run_tool(app, "update_curation", {"target": "segment", "id": target, "hidden": True})
        assert client.get(f"/api/admin/segments/{target}").json()["hidden"] is True

        after = run_tool(app, "search_media", query)
        assert target not in {r["segment_id"] for r in after.value["results"]}
        assert target not in after.text
        # 片段列表按 visibility 区分。
        visible = run_tool(app, "list_segments", {"source_id": source_id, "page_size": 50})
        hidden = run_tool(app, "list_segments", {"source_id": source_id, "visibility": "hidden", "page_size": 50})
        assert target not in {r["id"] for r in visible.value["items"]}
        assert [r["id"] for r in hidden.value["items"]] == [target] and hidden.value["items"][0]["hidden"]
        assert "隐藏" in hidden.text

        # 取消隐藏后恢复出现在检索结果中。
        run_tool(app, "update_curation", {"target": "segment", "id": target, "hidden": False})
        again = run_tool(app, "search_media", query)
        assert target in {r["segment_id"] for r in again.value["results"]}


# ---------- 素材报告 ----------

def test_source_report_structure(prepared):
    config, source_id = prepared
    app = create_app(config)
    with TestClient(app) as client:
        rest = client.get(f"/api/admin/sources/{source_id}/report").json()
        report = run_tool(app, "get_source_report", {"source_id": source_id})
        assert set(report.value) == {"source_id", "name", "prepared", "planned_shots", "planned_moments",
                                     "materialized", "shot_seconds", "configuration_matches", "missing_count",
                                     "missing", "runs"}
        for key in ("source_id", "name", "prepared", "planned_shots", "planned_moments", "materialized",
                    "shot_seconds", "configuration_matches"):
            assert report.value[key] == rest[key], key
        stats = report.value["shot_seconds"]
        assert set(stats) == {"min", "median", "p95", "max", "under_one_second"}
        assert stats["min"] <= stats["median"] <= stats["p95"] <= stats["max"]
        assert report.value["prepared"] is True and report.value["materialized"]["shot"] >= 1
        assert report.value["missing_count"] == 0 and "缺失文件：无" in report.text
        assert all(set(r) == {"stage", "status", "message"} for r in report.value["runs"])
        assert report.focus == {"page": "admin", "source_id": source_id}

        # 删除一个镜头缩略图：报告列出缺失明细。
        store = Store(config.data_dir)
        segment = store.segments(source_id, "shot")[0]
        store.resolve_media(segment["thumbnail"]).unlink()
        broken = run_tool(app, "get_source_report", {"source_id": source_id})
        assert broken.value["missing_count"] == 1
        assert broken.value["missing"][0]["id"] == segment["id"] and broken.value["missing"][0]["field"] == "thumbnail"
        assert f"缺失文件 1 个：{segment['id']} thumbnail" in broken.text

        with pytest.raises(ToolError) as exc:
            run_tool(app, "get_source_report", {"source_id": "missing"})
        assert exc.value.code == "http_404"


# ---------- GPU 锁冲突 ----------

def test_gpu_lock_conflict_is_relayed(prepared):
    config, source_id = prepared
    app = create_app(config)
    with TestClient(app):
        store = Store(config.data_dir)
        held = gpu_held(config)
        try:
            for work in ("analysis", "shots"):
                with pytest.raises(ToolError) as exc:
                    run_tool(app, "start_source_task", {"source_id": source_id, "work": work})
                assert exc.value.code == "http_409"
                assert "GPU 独占锁或该素材同类任务正在运行，请稍后启动" in str(exc.value)
                assert exc.value.value["status"] == 409
            with pytest.raises(ToolError) as exc:
                run_tool(app, "start_source_task", {"source_id": source_id, "work": "dialogue"})
            assert exc.value.code == "http_409" and "GPU 独占锁" in str(exc.value)
        finally:
            held.release()
        # 同一素材的同类任务锁被占用（另一进程正在分析）。
        with ProcessingLock(config, source_id=source_id, work="analysis"):
            with pytest.raises(ToolError) as exc:
                run_tool(app, "start_source_task", {"source_id": source_id, "work": "analysis"})
            assert exc.value.code == "http_409" and "同类任务正在运行" in str(exc.value)
        # 冲突时不创建任务记录。
        assert store.rows("SELECT id FROM source_tasks") == []
        assert store.rows("SELECT id FROM dialogue_tasks") == []


# ---------- 重试状态限制 ----------

def test_retry_status_rules(prepared):
    config, source_id = prepared
    app = create_app(config, FixtureEmbedder(config))
    with TestClient(app):
        # 持有 GPU 锁：发布任务停在等待准入（queued），此时可以取消。
        held = gpu_held(config)
        try:
            started = run_tool(app, "start_source_task", {"source_id": source_id, "work": "publish", "target": "shots"})
            task_id = started.value["id"]
            assert started.watch_task == {"kind": "source", "task_id": task_id, "source_id": source_id,
                                          "work": "publish"}
            assert started.value["status"] == "queued"
            cancelled = run_tool(app, "cancel_task", {"kind": "source", "task_id": task_id})
            assert cancelled.value["status"] == "cancelled"
            status = wait_status(app, "source", task_id, {"cancelled"})
            assert "retry_task(kind=source)" in status.text

            # cancelled 可重试：重新排队，仍在等待 GPU。
            retried = retry_when_unlocked(app, "source", task_id)
            assert retried.value["id"] == task_id and retried.value["status"] in {"queued", "running"}
            assert retried.watch_task["task_id"] == task_id and "结束时会通知你" in retried.text
        finally:
            held.release()
        done = wait_status(app, "source", task_id, TERMINAL)
        assert done.value["status"] == "complete", done.text

        # 已完成的任务不可重试：预览提示，执行时 409 原样转述。
        preview = run_tool(app, "retry_task", {"kind": "source", "task_id": task_id}, preview=True)
        assert any("服务端会拒绝" in w for w in preview.warnings)
        with pytest.raises(ToolError) as exc:
            retry_when_unlocked(app, "source", task_id)
        assert exc.value.code == "http_409" and "任务可以重试" in str(exc.value)

        # 对白、人物任务按类别路由到各自的重试接口。
        store = Store(config.data_dir)
        store.execute("INSERT INTO dialogue_tasks(id,source_id,status,config,options) VALUES('dc',?,'complete','{}','{}')",
                      (source_id,))
        store.execute("INSERT INTO person_tasks(id,source_id,status,config,options) VALUES('pc',?,'complete','{}','{}')",
                      (source_id,))
        with pytest.raises(ToolError) as exc:
            run_tool(app, "retry_task", {"kind": "dialogue", "task_id": "dc"})
        assert exc.value.code == "http_409" and "只有失败或中断的任务可以重试" in str(exc.value)
        with pytest.raises(ToolError) as exc:
            run_tool(app, "retry_task", {"kind": "person", "task_id": "pc"})
        assert exc.value.code == "http_409" and "仅失败或中断任务可以重试" in str(exc.value)
        with pytest.raises(ToolError) as exc:
            run_tool(app, "retry_task", {"kind": "person", "task_id": "nope"})
        assert exc.value.code == "http_404"


# ---------- 运行时端到端：终态 followup ----------

def finished_events(client, sid, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = client.get(f"/api/agent/sessions/{sid}/log").json()["items"]
        if any(e["type"] == "plugin:mindex/task-finished" for e in items):
            return items
        time.sleep(0.05)
    raise AssertionError("任务结束事件没有出现")


def test_start_source_task_followup_on_completion(prepared):
    config, source_id = prepared
    llm = ScriptedLlm([[call("start_source_task", source_id=source_id, work="publish", target="shots")],
                       [say("已提交发布任务，完成后告诉你。")], [say("发布完成，可以检索了。")]])
    app = create_app(config, FixtureEmbedder(config), agent_llm=llm)
    app.state.agent_runtime.watcher.interval = 0.05
    with TestClient(app) as client:
        sid = new_session(client)
        send(client, sid, "把分镜发布到检索", context={"page": "admin", "source_id": source_id})
        pending = wait_pending(client, sid, "approval")
        assert pending["name"] == "start_source_task" and pending["risk"] == "danger"
        client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})
        finished_events(client, sid)
        events = wait_idle(client, sid, timeout=60)
        task_id = next(e for e in events if e["type"] == "plugin:mindex/task-watch")["data"]["task_id"]
        rest = client.get(f"/api/admin/source-tasks/{task_id}").json()

    assert rest["status"] == "complete"
    watch = next(e for e in events if e["type"] == "plugin:mindex/task-watch")["data"]
    assert watch == {"kind": "source", "task_id": task_id, "source_id": source_id, "work": "publish"}
    finished = [e["data"] for e in events if e["type"] == "plugin:mindex/task-finished"]
    assert len(finished) == 1 and finished[0]["status"] == "complete" and finished[0]["task_id"] == task_id
    followups = [e["data"]["content"][0]["text"] for e in events
                 if e["type"] == "user/message" and e["data"]["source"]["kind"] == "followup"]
    assert len(followups) == 1
    assert f"素材任务 {task_id}（素材 {source_id}" in followups[0] and "状态：complete" in followups[0]
    # followup 触发了第二轮，模型收到了通知文本。
    assert sum(e["type"] == "turn/start" for e in events) == 2
    assert any(f"素材任务 {task_id}" in str(m) for m in llm.requests[-1].messages)


# ---------- 上传的后缀与大小限制 ----------

def test_upload_suffix_and_size_limits(prepared, video_file, monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.delenv("MINDEX_MCP_TOKEN", raising=False)
    config, source_id = prepared
    app = create_app(config)
    with TestClient(app) as client:
        store = Store(config.data_dir)
        # 素材：非视频内容被探测拒绝（422），不登记。
        with pytest.raises(ToolError) as exc:
            run_tool(app, "upload_source", {"attachment": REF}, attachment=("notes.txt", b"not a video", "text/plain"))
        assert exc.value.code == "http_422"
        assert len(store.sources()) == 1

        # 素材：超过 source_upload_max_bytes 返回 413，临时文件被清理。
        config.source_upload_max_bytes = 10
        with pytest.raises(ToolError) as exc:
            run_tool(app, "upload_source", {"attachment": REF}, attachment=(video_file.name, video_file, "video/mp4"))
        assert exc.value.code == "http_413" and "上传文件过大" in str(exc.value)
        assert not list((config.data_dir / "sources" / ".uploads").iterdir())
        assert len(store.sources()) == 1

        # 字幕：不支持的后缀在预览阶段即拒绝（预览读取真实附件元数据）；执行时服务端 422 转述。
        for name in ("notes.txt", "subtitle", "movie.srt.mp4"):
            ref = client.post("/api/agent/attachments", files={"file": (name, SRT, "text/plain")}).json()["id"]
            with pytest.raises(ToolError) as exc:
                run_tool(app, "upload_subtitle", {"source_id": source_id, "attachment": ref}, preview=True)
            assert exc.value.code == "invalid_arguments" and "不是支持的字幕格式" in str(exc.value)
        ref = client.post("/api/agent/attachments", files={"file": ("ep1.ASS", SRT, "text/plain")}).json()["id"]
        preview = run_tool(app, "upload_subtitle", {"source_id": source_id, "attachment": ref}, preview=True)
        assert preview.changes[0]["after"] == "ep1.ASS"
        from mindex.agent.tools.library_tasks import subtitle_suffix_error

        assert subtitle_suffix_error("notes.txt") and subtitle_suffix_error(None)
        assert all(subtitle_suffix_error(f"ep1{s}") is None for s in (".srt", ".ASS", ".ssa", ".vtt", ".sub", ".smi",
                                                                       ".sami"))
        with pytest.raises(ToolError) as exc:
            run_tool(app, "upload_subtitle", {"source_id": source_id, "attachment": REF},
                     attachment=("ep1.doc", SRT, "application/msword"))
        assert exc.value.code == "http_422" and "不支持的字幕文件格式" in str(exc.value)

        # 字幕：超过上限返回 413。
        with monkeypatch.context() as patch:
            patch.setattr("mindex.processing.subtitle_uploads.SUBTITLE_MAX_BYTES", 10)
            with pytest.raises(ToolError) as exc:
                run_tool(app, "upload_subtitle", {"source_id": source_id, "attachment": REF},
                         attachment=("ep1.srt", SRT, "application/x-subrip"))
        assert exc.value.code == "http_413" and "上传文件过大" in str(exc.value)
        ok = run_tool(app, "upload_subtitle", {"source_id": source_id, "attachment": REF},
                      attachment=("ep1.vtt", "WEBVTT\n\n00:00.000 --> 00:01.000\n你好\n".encode(), "text/vtt"))
        assert len(ok.value["id"]) == 64
