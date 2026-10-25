"""BackgroundTaskRunner 基类：生命周期、终态、取消、重试与线程表清理。"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest
from fastapi import HTTPException
from filelock import Timeout

from mindex.shared.background_tasks import BackgroundTaskRunner, public_task
from mindex.shared.storage import Store
from mindex.shared.task_cancel import registry_for

HELD: set[str] = set()


class FakeLock:
    def __init__(self, key):
        self.key, self.owned = key, False

    def acquire(self):
        if self.key in HELD:
            raise Timeout(self.key)
        HELD.add(self.key)
        self.owned = True
        return self

    def release(self):
        if self.owned:
            HELD.discard(self.key)
            self.owned = False


class FakeTasks(BackgroundTaskRunner):
    # 记在 mindex.* 名下，使 task_logs 把失败日志写入进度。
    __module__ = "mindex.fake_tasks"
    table = "dialogue_tasks"
    label = "示例任务"
    busy = "示例任务正在运行"
    not_retryable = "只有失败、中断或已取消的任务可以重试"
    thread_prefix = "fake"

    def __init__(self, store, behaviour):
        self.behaviour = behaviour
        self.executed = []
        super().__init__(None, store)

    def lock(self, source_id):
        return FakeLock(source_id)

    def start(self, source_id, task_id):
        def create():
            self.store.execute(
                "INSERT INTO dialogue_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
                (task_id, source_id, "queued", "{}", "{}"),
            )
            return task_id, {"frozen": True}

        return self.submit(self.lock(source_id), create)

    def restore(self, row):
        return {"frozen": json.loads(row["config"])}

    def execute(self, task_id, row, config, save):
        self.executed.append(task_id)
        self.behaviour(task_id, save)
        save("result", {"done": task_id})


def settled(manager, task_id):
    for _ in range(1000):
        value = manager.get(task_id)
        if value["status"] not in {"queued", "running"} and task_id not in manager.threads:
            return value
        time.sleep(0.01)
    pytest.fail("后台任务未结束")


@pytest.fixture
def store(tmp_path):
    HELD.clear()
    yield Store(tmp_path / "data")
    HELD.clear()


def test_start_runs_to_complete_and_cleans_up(store):
    manager = FakeTasks(store, lambda task_id, save: save("progress", {"stage": "work"}))
    started = manager.start("s", "t1")
    assert started["id"] == "t1" and "config" not in started
    value = settled(manager, "t1")
    assert value["status"] == "complete" and value["result"] == {"done": "t1"}
    assert value["progress"]["stage"] == "work"
    assert manager.threads == {} and not HELD


def test_failure_marks_failed_and_logs(store, caplog):
    def boom(task_id, save):
        raise RuntimeError("模拟崩溃")

    manager = FakeTasks(store, boom)
    with caplog.at_level(logging.ERROR, logger="mindex.fake_tasks"):
        manager.start("s", "t2")
        value = settled(manager, "t2")
    assert value["status"] == "failed" and value["error"] == "模拟崩溃"
    record = next(r for r in caplog.records if r.name == "mindex.fake_tasks")
    assert record.exc_info and "示例任务 t2 失败" in record.getMessage()
    assert "模拟崩溃" in value["progress"]["latest_log"]
    assert manager.threads == {} and not HELD


def test_long_error_is_truncated(store):
    def boom(task_id, save):
        raise ValueError("x" * 5000)

    manager = FakeTasks(store, boom)
    manager.start("s", "t3")
    assert len(settled(manager, "t3")["error"]) == 2000


def test_cancel_queued_never_executes(store):
    manager = FakeTasks(store, lambda task_id, save: None)
    store.execute("INSERT INTO dialogue_tasks(id,source_id,status,config,options) VALUES('q','s','queued','{}','{}')")
    assert manager.cancel("q")["status"] == "cancelled"
    lock = manager.lock("s").acquire()
    manager.run("q", {}, lock)
    value = manager.get("q")
    assert value["status"] == "cancelled" and value["error"] == "已取消" and value["result"] is None
    assert manager.executed == [] and not HELD
    assert not registry_for(store.root).requested("q")


def test_cancel_running_then_retry_cancelled(store):
    entered, proceed = threading.Event(), threading.Event()

    def wait(task_id, save):
        entered.set()
        proceed.wait(5)
        save("progress", {"stage": "after"})  # 进度上报即取消检查点

    manager = FakeTasks(store, wait)
    manager.start("s", "t4")
    assert entered.wait(5)
    with pytest.raises(HTTPException) as busy:
        manager.start("s", "other")
    assert busy.value.status_code == 409 and busy.value.detail == "示例任务正在运行，请稍后启动"
    assert manager.cancel("t4")["status"] == "running"
    proceed.set()
    value = settled(manager, "t4")
    assert value["status"] == "cancelled" and value["error"] == "已取消"
    assert manager.threads == {} and not HELD

    # 已取消可重试：沿用冻结配置并重新执行。
    manager.behaviour = lambda task_id, save: None
    retried = manager.retry("t4")
    assert retried["status"] in {"queued", "running", "complete"}
    value = settled(manager, "t4")
    assert value["status"] == "complete" and value["error"] is None
    assert manager.executed == ["t4", "t4"] and manager.threads == {} and not HELD


def test_retry_and_cancel_conflicts(store):
    manager = FakeTasks(store, lambda task_id, save: None)
    manager.start("s", "t5")
    settled(manager, "t5")
    with pytest.raises(HTTPException) as conflict:
        manager.retry("t5")
    assert conflict.value.status_code == 409 and conflict.value.detail == FakeTasks.not_retryable
    assert not HELD  # 拒绝重试也释放锁
    with pytest.raises(HTTPException) as conflict:
        manager.cancel("t5")
    assert conflict.value.status_code == 409 and conflict.value.detail == "只有排队中或运行中的任务可以取消"
    for call in (manager.get, manager.retry, manager.cancel):
        with pytest.raises(HTTPException) as missing:
            call("nope")
        assert missing.value.status_code == 404 and missing.value.detail == "找不到示例任务"

    # 检索更新失败的已完成任务可以重试；锁被占用时 409。
    store.execute("UPDATE dialogue_tasks SET publication=? WHERE id='t5'", (json.dumps({"status": "failed"}),))
    HELD.add("s")
    with pytest.raises(HTTPException) as busy:
        manager.retry("t5")
    assert busy.value.detail == "示例任务正在运行，请稍后重试"
    HELD.clear()
    manager.retry("t5")
    assert settled(manager, "t5")["status"] == "complete"


def test_init_recovers_live_rows(store):
    store.execute("INSERT INTO dialogue_tasks(id,source_id,status,config,options) VALUES('r','s','running','{}','{}')")
    manager = FakeTasks(store, lambda task_id, save: None)
    assert manager.get("r")["status"] == "interrupted"


def test_public_task_drops_config_and_decodes_json():
    row = {"id": "a", "config": "{}", "options": '{"x": 1}', "progress": "{}", "result": None,
           "publication": '{"status": "not_requested"}', "error": None}
    assert public_task(row) == {"id": "a", "options": {"x": 1}, "progress": {}, "result": None,
                                "publication": {"status": "not_requested"}, "error": None}
