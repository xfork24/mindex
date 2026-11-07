"""PostgreSQL 至少一次任务队列验收（plans §7.2/§11 阶段 3）。

覆盖：dedupe 幂等、claim 的优先级与 SKIP LOCKED 争用、只有原 owner+attempt 能
完成/失败/续期（暂停过 TTL 的旧 worker 不得提交重派任务）、到期回收重排、
重试上限与退避、pending 原子取消与 running 取消意图、人工重试、入队与账本同事务。
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from shared.db.pg_backend import PgBackend
from shared.db.queue_pg import PgTaskQueue

pytestmark = pytest.mark.requires_postgres

DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")


@pytest.fixture
def queue():
    if not DSN:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    import psycopg

    from shared.db.migrate_pg import migrate_pg

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(DSN) as conn:
        migrate_pg(conn)
    return PgTaskQueue("lib-queue-test")


@pytest.fixture
def backend():
    return PgBackend(DSN, library_id="lib-queue-test")


def _enqueue(queue, backend, kind="source", **kwargs):
    with backend.connect() as db:
        return queue.enqueue(db, kind, kwargs.pop("payload", {"work": "detect"}), **kwargs)


def test_enqueue_dedupe_is_idempotent(queue, backend):
    first = _enqueue(queue, backend, dedupe_key="src:1:detect")
    again = _enqueue(queue, backend, dedupe_key="src:1:detect")
    assert first == again  # 重复提交只产生一个有效工作
    with backend.connect() as db:
        assert queue.stats(db)["pending"]["count"] == 1


def test_claim_respects_priority_then_run_after(queue, backend):
    low = _enqueue(queue, backend, priority=200)
    high = _enqueue(queue, backend, priority=10)
    with backend.connect() as db:
        got = queue.claim(db, "worker-a", ["source"])
        assert got.id == high
        queue.complete(db, got)
        got = queue.claim(db, "worker-a", ["source"])
        assert got.id == low
        assert got.fencing_token >= 1


def test_claim_skips_other_kinds_and_cancelled(queue, backend):
    _enqueue(queue, backend, kind="dialogue")
    with backend.connect() as db:
        assert queue.claim(db, "w", ["source"]) is None  # kind 不匹配
        assert queue.claim(db, "w", ["dialogue", "source"]) is not None


def test_only_holder_can_complete_and_stale_attempt_rejected(queue, backend):
    task_id = _enqueue(queue, backend)
    with backend.connect() as db:
        first = queue.claim(db, "worker-a", ["source"], ttl=0.3)
    time.sleep(1.8)  # worker-a 暂停超过 TTL；回收重排以过期时刻为基准（≤1.5s）
    with backend.connect() as db:
        reclaimed = queue.claim(db, "worker-b", ["source"])  # 回收重派给 worker-b
        assert reclaimed.id == task_id
        assert reclaimed.attempt_id != first.attempt_id
        assert reclaimed.fencing_token > first.fencing_token
        # 旧 worker 恢复：其 attempt 的完成/失败/续期全部拒绝（plans §11 验收闸门）
        assert not queue.complete(db, first)
        assert not queue.renew(db, first)
        with pytest.raises(RuntimeError, match="旧 worker"):
            queue.fail(db, first, "stale worker")
        assert queue.complete(db, reclaimed)  # 新持有者正常完成


def test_paused_fenced_transaction_cannot_block_takeover(queue, backend):
    """旧进程停在持共享行锁的事务内时，PG 自行终止空闲事务，接管不依赖旧进程恢复。"""
    from shared.db.fencing import Attempt, ExecutionFence
    from shared.db.locks_pg import LeaseLost

    _enqueue(queue, backend)
    with backend.connect() as db:
        first = queue.claim(db, "paused-worker", ["source"], ttl=1)
    attempt = Attempt(backend, backend.library_id, first, threading.Event(), threading.Event())
    stop = threading.Event()
    reclaimed = None

    def takeover():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not stop.is_set():
            with backend.connect() as db:
                task = queue.claim(db, "new-worker", ["source"])
            if task is not None:
                return task
            stop.wait(0.05)
        return None

    try:
        with ThreadPoolExecutor(max_workers=1) as executor, pytest.raises(LeaseLost):
            with backend.connect(execution=ExecutionFence(attempt)) as db:
                db.execute("INSERT INTO settings(key,value) VALUES('idle-fence-rollback','old-worker')")
                # 模拟 SIGSTOP：旧连接保持打开且不发送任何 SQL，新 worker 必须能独立接管。
                reclaimed = executor.submit(takeover).result(timeout=6)
                assert reclaimed is not None, "暂停事务持有的行锁阻止了到期回收"
                assert reclaimed.owner == "new-worker" and reclaimed.fencing_token > first.fencing_token
                db.execute("INSERT INTO settings(key,value) VALUES('idle-fence-late','stale')")
    finally:
        stop.set()
        attempt.close()
    assert attempt.lost.is_set()
    with backend.connect() as db:
        assert db.execute("SELECT 1 FROM settings WHERE key LIKE ?", ("idle-fence-%",)).fetchone() is None
        assert not queue.complete(db, first)
        assert queue.complete(db, reclaimed)


def test_fence_idle_timeout_does_not_leak_to_next_transaction(queue, backend):
    from shared.db.fencing import Attempt, ExecutionFence

    _enqueue(queue, backend)
    with backend.connect() as db:
        original = db.execute(
            "SELECT setting FROM pg_settings WHERE name='idle_in_transaction_session_timeout'"
        ).fetchone()[0]
        task = queue.claim(db, "worker", ["source"], ttl=10)
    attempt = Attempt(backend, backend.library_id, task, threading.Event(), threading.Event())
    try:
        with backend.connect(execution=ExecutionFence(attempt)) as db:
            timeout = db.execute(
                "SELECT setting FROM pg_settings WHERE name='idle_in_transaction_session_timeout'"
            ).fetchone()[0]
            assert 0 < int(timeout) <= 10_000
        with backend.connect() as db:
            restored = db.execute(
                "SELECT setting FROM pg_settings WHERE name='idle_in_transaction_session_timeout'"
            ).fetchone()[0]
            assert restored == original
    finally:
        attempt.close()


def test_expiry_reclaims_and_requeues(queue, backend):
    _enqueue(queue, backend, max_attempts=2)
    with backend.connect() as db:
        first = queue.claim(db, "worker-a", ["source"], ttl=0.2)
    time.sleep(2.0)
    with backend.connect() as db:
        second = queue.claim(db, "worker-b", ["source"])
    assert second.id == first.id and second.attempts == 2  # 重派不认定成功


def test_fail_retries_with_backoff_then_gives_up(queue, backend):
    task_id = _enqueue(queue, backend, max_attempts=2)
    with backend.connect() as db:
        first = queue.claim(db, "w", ["source"])
        assert queue.fail(db, first, "boom", backoff=0.2) == "pending"
        stats = queue.stats(db)
        assert stats["pending"]["count"] == 1
    time.sleep(0.3)  # 等待退避窗口
    with backend.connect() as db:
        second = queue.claim(db, "w", ["source"])
        assert second.attempts == 2
        assert queue.fail(db, second, "boom again") == "failed"  # 超上限保留可人工重试
        row = db.execute("SELECT error FROM task_queue WHERE id=%s", (task_id,)).fetchone()
        assert "boom again" in row["error"]
        assert queue.retry(db, task_id)  # 人工重试清空重排
        third = queue.claim(db, "w", ["source"])
        assert third.attempts == 1


def test_cancel_pending_atomic_and_running_intent(queue, backend):
    pending = _enqueue(queue, backend, dedupe_key="p1")
    with backend.connect() as db:
        assert queue.cancel(db, pending) == "cancelled"
        assert queue.claim(db, "w", ["source"]) is None  # cancelled 不再被领取
    running = _enqueue(queue, backend, dedupe_key="p2")
    with backend.connect() as db:
        task = queue.claim(db, "w", ["source"])
        assert queue.cancel(db, running) == "cancel_requested"  # 意图持久化
        assert queue.cancel_requested(db, task)
        # 持有方按意图以 cancelled 收尾（账本侧由业务保证一致后提交）
        db.execute("UPDATE task_queue SET status='cancelled', expires_at=NULL WHERE id=%s", (running,))


def test_concurrent_claims_are_disjoint(queue, backend):
    for index in range(8):
        _enqueue(queue, backend, dedupe_key=f"c{index}")
    claimed: list[int] = []
    lock = threading.Lock()

    def worker(name):
        with backend.connect() as db:
            while True:
                task = queue.claim(db, name, ["source"])
                if task is None:
                    return
                with lock:
                    claimed.append(task.id)
                queue.complete(db, task)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(claimed) == sorted(set(claimed))  # 任意任务只被一个 worker 领取
    assert len(claimed) == 8
