"""StudioExecutor：worker 异常记录、旧执行与构建的进程内互斥、取消事件与哨兵、终态写入失败。"""

from __future__ import annotations

import logging
import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from filelock import Timeout

from api import create_app
from conftest import FixtureStoryboarder
from shared import locks
from studio import executor, studio
from studio import workspace as ws
from studio.executor import BUSY, StudioExecutor
from test_studio_api import create_legacy_job
from test_studio_cancel import wait_until
from test_studio_columns import request_body
from test_studio_workspace import setup_workspace, wait_build


def lock_free(config) -> bool:
    lock = locks.named(config.data_dir, "studio")
    try:
        lock.acquire()
    except Timeout:
        return False
    lock.release()
    return True


def submit_build(client, config, jid, w, p):
    return client.post(
        f"/api/studio/jobs/{jid}/plans/{p['id']}/regenerate-column",
        json=request_body(config, jid, w, p, "tts"),
    )


def test_lock_path_matches_studio_lock_path(config):
    assert locks.path(config.data_dir, "studio") == studio.lock_path(config)


# ---------------------------------------------------------------- (a) 构建记录缺失


def test_build_worker_logs_original_error_when_record_deleted(config, monkeypatch, caplog):
    jid, w, p = setup_workspace(config)

    def broken(cfg, job_id, build_id, **_):
        fresh = ws.load(cfg, job_id)
        fresh["builds"] = [b for b in fresh["builds"] if b["id"] != build_id]  # 运行中构建记录被删除
        ws.persist(cfg, job_id, fresh)
        raise RuntimeError("原始构建错误")

    monkeypatch.setattr(ws, "run_build", broken)
    client = TestClient(create_app(config))
    with caplog.at_level(logging.INFO):
        response = submit_build(client, config, jid, w, p)
        assert response.status_code == 202, response.text
        bid = response.json()["id"]
        wait_until(lambda: True if lock_free(config) else None)

    failures = [r for r in caplog.records if f"构建 {bid} 失败" in r.getMessage() and r.exc_info]
    assert len(failures) == 1
    assert isinstance(failures[0].exc_info[1], RuntimeError)
    assert str(failures[0].exc_info[1]) == "原始构建错误"
    assert any("记录已不存在" in r.getMessage() for r in caplog.records)
    assert not any("再次出错" in r.getMessage() for r in caplog.records)  # 终态处理本身没有出错
    assert all(b["id"] != bid for b in ws.load(config, jid)["builds"])
    assert not executor.cancel_sentinel(config, "build", jid, bid).exists()


def test_build_worker_records_failed_state(config, monkeypatch):
    jid, w, p = setup_workspace(config)

    def broken(*_, **__):
        raise RuntimeError("节点之外的失败")

    monkeypatch.setattr(ws, "run_build", broken)
    client = TestClient(create_app(config))
    response = submit_build(client, config, jid, w, p)
    assert response.status_code == 202, response.text
    done = wait_build(client, jid)["builds"][-1]
    assert done["status"] == "failed" and done["error"] == "节点之外的失败" and done["finished_at"]
    wait_until(lambda: True if lock_free(config) else None)


# ---------------------------------------------------------------- (b) 两路执行互斥


def test_legacy_run_rejected_while_build_running(config, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def blocking(*_, **__):
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(ws, "run_build", blocking)
    jid, w, p = setup_workspace(config)
    client = TestClient(create_app(config))
    client.config = config
    legacy = create_legacy_job(client, "红色首先出现。随后蓝色的天空调转。")
    try:
        assert submit_build(client, config, jid, w, p).status_code == 202
        assert entered.wait(5)
        rejected = client.post(f"/api/studio/jobs/{legacy}/run")
        assert rejected.status_code == 409 and rejected.json()["detail"] == BUSY
        again = submit_build(client, config, jid, ws.load(config, jid), p)
        assert again.status_code == 409 and again.json()["detail"] == BUSY
    finally:
        release.set()
    wait_until(lambda: True if lock_free(config) else None)
    assert client.get(f"/api/studio/jobs/{legacy}").json()["job"]["execution"]["status"] != "running"


def test_build_rejected_while_legacy_run_running(config):
    entered, release = threading.Event(), threading.Event()

    class Slow(FixtureStoryboarder):
        def split(self, text, vocabulary=None):
            entered.set()
            assert release.wait(10)
            return super().split(text, vocabulary)

    jid, w, p = setup_workspace(config)
    client = TestClient(create_app(config, storyboarder=Slow(config)))
    client.config = config
    legacy = create_legacy_job(client, "红色首先出现。随后蓝色的天空调转。")
    try:
        assert client.post(f"/api/studio/jobs/{legacy}/steps/split").status_code == 202
        assert entered.wait(5)
        rejected = submit_build(client, config, jid, w, p)
        assert rejected.status_code == 409 and rejected.json()["detail"] == BUSY
    finally:
        release.set()

    def finished():
        job = client.get(f"/api/studio/jobs/{legacy}").json()["job"]
        return job if job["execution"]["status"] != "running" else None

    assert wait_until(finished)["execution"]["status"] == "complete"
    assert all(b["status"] != "running" for b in ws.load(config, jid)["builds"])


def test_alive_thread_blocks_reserve_even_without_file_lock(config):
    """进程内互斥不依赖文件锁：只要执行线程存活，reserve/start 都返回 409。"""
    runner = StudioExecutor(config)
    release = threading.Event()
    with runner.reserve() as lock:
        runner.start("t-block", lambda: release.wait(10), lock=lock)
    try:
        assert runner.busy()
        for attempt in (runner.reserve().__enter__, lambda: runner.start("t-other", lambda: None)):
            with pytest.raises(HTTPException) as raised:
                attempt()
            assert raised.value.status_code == 409 and raised.value.detail == BUSY
    finally:
        release.set()
    wait_until(lambda: True if not runner.busy() else None)
    assert lock_free(config)
    with runner.reserve() as lock:  # 线程结束后可以再次启动
        runner.start("t-next", lambda: None, lock=lock)
    wait_until(lambda: True if lock_free(config) and not runner.busy() else None)


def test_file_lock_held_elsewhere_returns_409(config):
    runner = StudioExecutor(config)
    with locks.named(config.data_dir, "studio"), pytest.raises(HTTPException) as raised:
        with runner.mutation():
            pass
    assert raised.value.status_code == 409 and raised.value.detail == BUSY


def test_start_failure_runs_cleanup_and_releases_lock(config, monkeypatch, caplog):
    runner = StudioExecutor(config)
    cleaned = []

    def refuse(self):
        raise RuntimeError("无法创建线程")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="无法创建线程"):
        with runner.reserve() as lock:
            runner.start("t-fail", lambda: None, lock=lock, on_failure=cleaned.append)
    assert [str(e) for e in cleaned] == ["无法创建线程"]
    assert not runner.busy() and lock_free(config)
    assert any("启动失败" in r.getMessage() and r.exc_info for r in caplog.records)


# ---------------------------------------------------------------- (c) 取消：事件与哨兵


@pytest.mark.parametrize(
    ("kind", "key", "relative"), [("build", "b1", "builds/b1.cancel"), ("steps", None, "steps.cancel")]
)
def test_cancel_event_and_sentinel_both_observed(config, kind, key, relative):
    jid = studio.create_job(config, "大纲")["id"]
    sentinel = studio.job_dir(config, jid) / relative
    assert executor.cancel_sentinel(config, kind, jid, key) == sentinel
    runner = StudioExecutor(config)
    event = runner.cancel_event(kind, jid, key)
    assert runner.cancel_event(kind, jid, key) is event
    assert not runner.cancelled(kind, jid, key)

    # 只有哨兵（另一进程发起）：进程内事件未置位也视为已取消。
    executor.request_cancel(config, kind, jid, key)
    assert sentinel.exists() and not event.is_set()
    assert runner.cancelled(kind, jid, key) and executor.cancel_requested(config, kind, jid, key)
    executor.clear_cancel(config, kind, jid, key)
    assert not runner.cancelled(kind, jid, key)

    # 只有事件（本进程，哨兵被提前删除）：同样视为已取消。
    event.set()
    assert runner.cancelled(kind, jid, key) and not sentinel.exists()
    assert executor.cancel_requested(config, kind, jid, key, event)
    event.clear()

    # 执行器请求取消：两者同时写入；clear 同时注销事件与删除哨兵，幂等。
    runner.request_cancel(kind, jid, key)
    assert event.is_set() and sentinel.exists() and runner.cancelled(kind, jid, key)
    runner.clear(kind, jid, key)
    runner.clear(kind, jid, key)
    assert not sentinel.exists() and not runner.cancelled(kind, jid, key)
    assert runner.cancel_event(kind, jid, key) is not event


def test_cancel_sentinel_rejects_unknown_kind(config):
    jid = studio.create_job(config, "大纲")["id"]
    with pytest.raises(ValueError, match="未知的取消类型"):
        executor.cancel_sentinel(config, "other", jid)
    with pytest.raises(ValueError, match="未知的取消类型"):
        executor.cancel_sentinel(config, "build", jid)  # 构建取消必须指明构建


# ---------------------------------------------------------------- (d) 写终态失败


def test_on_error_failure_logs_both_and_releases_lock(config, caplog):
    lock = locks.named(config.data_dir, "studio").acquire()
    finished = []

    def body():
        raise RuntimeError("原始错误")

    def on_error(exc):
        raise OSError(f"写入终态失败（{exc}）")

    with caplog.at_level(logging.ERROR):
        StudioExecutor.run_guarded(
            body, message="测试任务失败", on_error=on_error, finally_=lambda: finished.append(True), lock=lock
        )

    records = [r for r in caplog.records if r.name == "studio.executor"]
    assert [r.getMessage() for r in records] == ["测试任务失败", "测试任务失败；写入失败终态时再次出错"]
    assert isinstance(records[0].exc_info[1], RuntimeError)
    assert isinstance(records[1].exc_info[1], OSError)
    assert finished == [True]
    assert not lock.is_locked and lock_free(config)


def test_finally_failure_still_releases_lock(config, caplog):
    lock = locks.named(config.data_dir, "studio").acquire()

    def finish():
        raise OSError("收尾失败")

    with caplog.at_level(logging.ERROR):
        executor.run_guarded(lambda: None, message="测试任务", finally_=finish, lock=lock)
    assert [r.getMessage() for r in caplog.records if r.name == "studio.executor"] == [
        "测试任务；执行收尾时出错"
    ]
    assert lock_free(config)


def test_build_worker_terminal_write_failure_keeps_original_error(config, monkeypatch, caplog):
    """API 构建 worker：失败终态写入本身出错时，原始异常与写入错误都记录，锁照常释放。"""
    jid, w, p = setup_workspace(config)
    real_load = ws.load
    state = {"armed": False}

    def broken(*_, **__):
        state["armed"] = True
        raise RuntimeError("原始构建错误")

    def load(cfg, job_id):
        if state["armed"]:
            raise OSError("工作台读取失败")
        return real_load(cfg, job_id)

    monkeypatch.setattr(ws, "run_build", broken)
    monkeypatch.setattr(ws, "load", load)
    client = TestClient(create_app(config))
    with caplog.at_level(logging.ERROR):
        assert submit_build(client, config, jid, w, p).status_code == 202
        wait_until(lambda: True if lock_free(config) else None)
    errors = [(r.getMessage(), type(r.exc_info[1]).__name__) for r in caplog.records if r.exc_info]
    assert any(m.endswith("失败") and name == "RuntimeError" for m, name in errors)
    assert any("再次出错" in m and name == "OSError" for m, name in errors)
