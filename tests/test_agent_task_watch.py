"""后台任务跟踪：ToolOutput.watch_task 登记跟踪、终态 followup、重启续跟踪，以及目标规范化与通知文本。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agent.llm.scripted import ScriptedLlm, call, say
from agent.registry import ToolOutput, ToolRegistry, ToolSpec
from agent.tools import meta
from agent.watcher import BuildWatcher, TaskWatcher, watch_target
from agent_helpers import new_session, send, wait_idle
from api import create_app
from conftest import FixtureEmbedder
from shared.storage import Store


class Empty(BaseModel):
    pass


def make_app(config, llm):
    app = create_app(config, FixtureEmbedder(config), agent_llm=llm)
    app.state.agent_runtime.watcher.interval = 0.05
    return app


def probe_registry(target: dict) -> ToolRegistry:
    async def execute(args, ctx) -> ToolOutput:
        return ToolOutput({"task_id": target["task_id"]}, text="已提交任务", watch_task=target)

    return ToolRegistry([*meta.specs(), ToolSpec("watch_probe", "meta", "提交任务", Empty, "read", execute)])


def wait_type(client, sid, kind, count=1, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = client.get(f"/api/agent/sessions/{sid}/log").json()["items"]
        if sum(e["type"] == kind for e in items) >= count:
            return items
        time.sleep(0.05)
    raise AssertionError(f"没有出现 {kind}")


def followups(events):
    return [
        e["data"]["content"][0]["text"]
        for e in events
        if e["type"] == "user/message" and e["data"]["source"]["kind"] == "followup"
    ]


def test_watch_task_followup_on_cancelled(config):
    target = {"kind": "source", "task_id": "task-a", "source_id": "src-1", "work": "analysis"}
    llm = ScriptedLlm([[call("watch_probe")], [say("已提交，结束后告诉你。")], [say("任务已取消。")]])
    app = make_app(config, llm)
    with TestClient(app) as client:
        app.state.agent_runtime.registry = probe_registry(target)
        store = Store(config.data_dir)
        store.execute(
            "INSERT INTO source_tasks(id,source_id,work,status,config,options) VALUES(?,?,?,?,?,?)",
            ("task-a", "src-1", "analysis", "running", "{}", "{}"),
        )
        sid = new_session(client)
        send(client, sid, "分析这个素材")
        events = wait_idle(client, sid)
        assert next(e for e in events if e["type"] == "plugin:mindex/task-watch")["data"] == target
        # 仍在运行：没有结束事件。
        time.sleep(0.2)
        assert not any(
            e["type"] == "plugin:mindex/task-finished"
            for e in client.get(f"/api/agent/sessions/{sid}/log").json()["items"]
        )
        store.execute(
            "UPDATE source_tasks SET status='cancelled',error='已取消',progress=? WHERE id='task-a'",
            ('{"stage":"analyze","completed":2,"total":5}',),
        )
        wait_type(client, sid, "plugin:mindex/task-finished")
        events = wait_idle(client, sid)
    finished = next(e for e in events if e["type"] == "plugin:mindex/task-finished")["data"]
    assert finished["status"] == "cancelled" and finished["error"] == "已取消"
    assert (
        finished["kind"] == "source" and finished["work"] == "analysis" and finished["progress"]["total"] == 5
    )
    texts = followups(events)
    assert len(texts) == 1
    assert "素材任务 task-a（素材 src-1，工作 分析）已结束，状态：cancelled" in texts[0]
    assert "retry_task(kind=source)" in texts[0] and "最后进度" in texts[0]
    assert sum(e["type"] == "turn/start" for e in events) == 2
    # 构建卡片事件不受影响。
    assert not any(e["type"].startswith("plugin:mindex/build-") for e in events)


def test_restart_resumes_unfinished_task_watches(config):
    first = make_app(config, ScriptedLlm())
    with TestClient(first) as client:
        sid = new_session(client)
        store = Store(config.data_dir)
        store.execute(
            "INSERT INTO person_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
            ("person-1", "src-1", "running", "{}", "{}"),
        )
        store.execute(
            "INSERT INTO dialogue_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
            ("dialogue-1", "src-1", "complete", "{}", "{}"),
        )
        sessions = first.state.agent_runtime.store
        sessions.append(sid, "plugin:mindex/task-watch", {"kind": "person", "task_id": "person-1"})
        sessions.append(sid, "plugin:mindex/task-watch", {"kind": "dialogue", "task_id": "dialogue-1"})
        sessions.append(
            sid,
            "plugin:mindex/task-finished",
            {"kind": "dialogue", "task_id": "dialogue-1", "status": "complete"},
        )

    # 重启时运行中的人物任务被恢复为 interrupted；已结束的对白跟踪不再补发。
    with TestClient(make_app(config, ScriptedLlm([[say("人物任务中断了，可以重试。")]]))) as client:
        client.get("/api/agent/sessions")
        wait_type(client, sid, "plugin:mindex/task-finished", count=2)
        events = wait_idle(client, sid)
    finished = [e["data"] for e in events if e["type"] == "plugin:mindex/task-finished"]
    assert len(finished) == 2 and finished[-1]["task_id"] == "person-1"
    assert finished[-1]["status"] == "interrupted" and finished[-1]["source_id"] == "src-1"
    texts = followups(events)
    assert len(texts) == 1 and "人物任务 person-1（素材 src-1）已结束，状态：interrupted" in texts[0]


def test_missing_task_reports_unknown(config):
    llm = ScriptedLlm([[call("watch_probe")], [say("已提交")], [say("任务不存在")]])
    app = make_app(config, llm)
    with TestClient(app) as client:
        app.state.agent_runtime.registry = probe_registry({"kind": "dialogue", "task_id": "nope"})
        sid = new_session(client)
        send(client, sid, "跟踪")
        wait_type(client, sid, "plugin:mindex/task-finished")
        events = wait_idle(client, sid)
    finished = next(e for e in events if e["type"] == "plugin:mindex/task-finished")["data"]
    assert finished["status"] == "unknown" and "找不到对白任务" in finished["error"]


def test_watch_target_normalization_and_texts():
    assert BuildWatcher is TaskWatcher
    build = {"job_id": "j", "build_id": "b", "plan_id": "p"}
    assert watch_target(build) == build
    assert watch_target({"kind": "build", **build}) == build
    assert watch_target({"kind": "person", "task_id": "t", "work": None, "extra": 1}) == {
        "kind": "person",
        "task_id": "t",
    }
    for bad in ({"kind": "other", "task_id": "t"}, {"kind": "source"}, {"job_id": "j"}):
        with pytest.raises(ValueError):
            watch_target(bad)

    summary = TaskWatcher.task_summary(
        {"kind": "source", "task_id": "t"},
        {
            "id": "t",
            "source_id": "s",
            "work": "shots",
            "status": "complete",
            "result": {"big": "x" * 10000},
            "publication": {"status": "queued", "task_ids": ["p1", "p2"]},
            "progress": {},
        },
    )
    assert "result" not in summary and summary["work"] == "shots"
    text = TaskWatcher.followup_text(summary)
    assert "素材任务 t（素材 s，工作 分镜）已结束，状态：complete" in text
    assert "已链式创建发布任务：p1, p2" in text and "最后进度" not in text
    assert TaskWatcher.followup_text({"job_id": "j", "build_id": "b", "status": "cancelled"}).startswith(
        "[系统通知] 构建 b（任务 j）已结束，状态：cancelled"
    )
