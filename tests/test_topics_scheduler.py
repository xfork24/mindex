"""选题挖掘 · 每日定时与启动补跑：纯函数、注入时钟的调度、跨进程锁、随应用启停、状态接口、CLI 与智能助手工具。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from topics_fixtures import RSS, ScriptedTopicLlm, install, topic_embedder
from typer.testing import CliRunner

from mindex import cli
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.shared import locks
from mindex.shared.storage import Store
from mindex.topics import profiles, scheduler
from mindex.topics.models import ProfileBody, Schedule
from mindex.topics.runner import TopicRuns

FEED = "https://news.example.com/feed.xml"
DAY = datetime(2026, 10, 26, 0, 0).astimezone()


def at(hour, minute=0):
    return DAY.replace(hour=hour, minute=minute)


def run(trigger="schedule", status="complete"):
    return {"trigger": trigger, "status": status}


def test_due_rules():
    on = Schedule(enabled=True, time="08:00")
    assert scheduler.due(Schedule(enabled=False), at(9), [], startup=False) is None
    assert scheduler.due(on, at(7, 59), [], startup=False) is None
    assert scheduler.due(on, at(8), [], startup=False) == "schedule"
    assert scheduler.due(on, at(10), [], startup=True) == "catchup"
    assert scheduler.due(on, at(10), [run()], startup=False) is None
    assert scheduler.due(on, at(10), [run("catchup", "running")], startup=False) is None
    assert scheduler.due(on, at(10), [run(status="failed")], startup=False) == "schedule"  # 失败后再试
    assert scheduler.due(on, at(10), [run(status="failed")] * 3, startup=False) is None  # 每天最多 3 次
    assert scheduler.due(on, at(10), [run("manual")], startup=False) == "schedule"  # 手动运行不算定时
    assert scheduler.next_run(on, at(7), []).startswith("2026-10-26T08:00")
    assert scheduler.next_run(on, at(9), [run()]).startswith("2026-10-27T08:00")
    assert scheduler.next_run(on, at(9), []) == at(9).isoformat(timespec="seconds")  # 到期未运行：马上
    assert scheduler.next_run(Schedule(), at(9), []) is None


class FakeRuns:
    def __init__(self, store, busy=False):
        self.store, self.busy, self.started = store, busy, []

    def start(self, profile_id, options=None, *, trigger="manual"):
        if self.busy:
            raise HTTPException(409, "已有选题挖掘任务在运行，请稍后启动")
        run_id = uuid.uuid4().hex
        self.store.execute("INSERT INTO topic_runs(id,source_id,trigger,status,config,options,created_at) "
                           "VALUES(?,?,?,?,?,?,?)", (run_id, profile_id, trigger, "complete", "{}", "{}",
                                                     self.now().isoformat()))
        self.started.append((profile_id, trigger))
        return {"id": run_id}


def test_tick_with_injected_clock(config):
    store = Store(config.data_dir)
    on = profiles.create_profile(store, ProfileBody(name="财经", domain="金融",
                                                    schedule=Schedule(enabled=True, time="08:00")))["id"]
    profiles.create_profile(store, ProfileBody(name="关闭", domain="历史"))
    clock = {"now": at(9)}
    runs = FakeRuns(store, busy=True)
    runs.now = lambda: clock["now"]
    sched = scheduler.TopicScheduler(store, runs, clock=lambda: clock["now"])
    assert sched.tick() == []  # 启动时补跑，但有任务在运行：跳过，下次再试
    runs.busy = False
    assert len(sched.tick()) == 1 and runs.started == [(on, "schedule")]
    assert sched.tick() == []  # 当天已成功，不重复
    clock["now"] = at(7) + timedelta(days=1)
    assert sched.tick() == []  # 第二天还没到时刻
    clock["now"] = at(8, 1) + timedelta(days=1)
    sched.tick()
    assert runs.started[-1] == (on, "schedule") and len(runs.started) == 2
    fresh = scheduler.TopicScheduler(store, FakeRuns(store), clock=lambda: at(10) + timedelta(days=2))
    fresh.runs.now = lambda: at(10) + timedelta(days=2)
    fresh.tick()
    assert fresh.runs.started == [(on, "catchup")]  # 服务启动后的第一次检查：补跑


def test_scheduler_runs_with_app_and_respects_lock(config, monkeypatch):
    install(monkeypatch, {FEED: RSS})
    store = Store(config.data_dir)
    pid = profiles.create_profile(store, ProfileBody(name="财经", domain="金融",
                                                    schedule=Schedule(enabled=True, time="00:00", count=1)))["id"]
    from mindex.topics import datasources

    datasources.create_source(store, pid, datasources.SourceBody(kind="rss", name="要闻", config={"url": FEED}))
    config.topics.scheduler.interval_seconds = 5
    app = create_app(config, topic_embedder(config), topic_llm=ScriptedTopicLlm(), scheduler=True)
    with locks.named(config.data_dir, "topics"):  # 另一进程正在挖掘：启动补跑跳过
        with TestClient(app) as client:
            time.sleep(0.3)
            assert client.get(f"/api/topics/profiles/{pid}/runs").json()["items"] == []
            assert client.get("/api/topics/schedule").json()["scheduler"] is True
    assert app.state.topic_scheduler._thread is None  # 随应用停止
    app = create_app(config, topic_embedder(config), topic_llm=ScriptedTopicLlm(), scheduler=True)
    with TestClient(app) as client:
        for _ in range(200):
            items = client.get(f"/api/topics/profiles/{pid}/runs").json()["items"]
            if items and items[0]["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        assert items[0]["trigger"] == "catchup" and items[0]["status"] == "complete", items
        status = client.get("/api/topics/schedule").json()
        item = next(i for i in status["items"] if i["profile_id"] == pid)
        assert item["today"] == {"runs": 1, "cards": 2} and item["last_run"]["trigger"] == "catchup"
        tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
        assert item["next_run"].startswith(tomorrow)
    # 测试与内嵌应用缺省不启动调度器
    plain = create_app(config)
    with TestClient(plain) as client:
        assert client.get("/api/topics/schedule").json()["scheduler"] is False
    assert plain.state.topic_scheduler is None
    TopicRuns(None, store)  # 不影响已完成的任务
    assert store.one("SELECT COUNT(*) AS n FROM topic_runs WHERE status='complete'")["n"] == 1


def test_schedule_cli_and_tool(config, tmp_path):
    store = Store(config.data_dir)
    profiles.create_profile(store, ProfileBody(name="财经", domain="金融", schedule=Schedule(enabled=True, time="23:59")))
    profiles.create_profile(store, ProfileBody(name="关闭", domain="历史"))
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"data_dir: {config.data_dir}\n", encoding="utf-8")
    result = json.loads(CliRunner().invoke(cli.app, ["--config", str(yaml_path), "topics", "schedule", "status"]).output)
    assert result["scheduler"] is True and "mindex serve" in result["note"]
    assert [i["enabled"] for i in result["items"]] == [True, False]
    app = create_app(config)

    async def call():
        mindex = MindexClient(app)
        try:
            spec = build_registry().get("get_topic_schedule")
            return await spec.execute(spec.parse({}), ToolContext(client=mindex))
        finally:
            await mindex.aclose()

    out = asyncio.run(call())
    assert "没有运行定时调度" in out.text and "每天 23:59" in out.text and "未开启每日定时" in out.text


@pytest.mark.parametrize("value", ["24:00", "8:00", "08:60", "abc"])
def test_schedule_time_validation(value):
    with pytest.raises(ValueError):
        Schedule(time=value)
