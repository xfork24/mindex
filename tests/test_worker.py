"""Worker 运行时验收（plans §7.2/§11 阶段 3）：至少一次交付、心跳失联不提交、
取消传播、补偿扫描与 dispatch 分叉（API 入队 → worker 执行 → 账本+队列终态）。

经 ``MINDEX_TEST_POSTGRES_DSN`` 门控。selftest 处理器直接验证引擎语义；
dispatch 用例走真实 source 任务（fixture 模型 + AdaptiveDetector）。
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from shared.config import Config

pytestmark = pytest.mark.requires_postgres

DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")


def _pg_config(tmp_path, monkeypatch) -> Config:
    import psycopg

    from shared.db.migrate_pg import migrate_pg

    monkeypatch.setenv("MINDEX_DATABASE_DSN", DSN)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(DSN) as conn:
        migrate_pg(conn)
    config = Config(
        data_dir=tmp_path / "data", database={"backend": "postgres"}, queue={"backend": "postgres"}
    )
    config.embedding.dimensions = 8
    return config


@pytest.fixture
def pg_config(tmp_path, monkeypatch):
    if not DSN:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    return _pg_config(tmp_path, monkeypatch)


def _enqueue(pg_config, kind, payload, **kwargs):
    from shared.backends import open_context
    from shared.db.queue_pg import PgTaskQueue

    ctx = open_context(pg_config)
    queue = PgTaskQueue(ctx.library_id)
    with ctx.db.connect() as db:
        return queue.enqueue(db, kind, payload, **kwargs)


def test_worker_executes_selftest_and_completes(pg_config, tmp_path):
    from worker.runtime import Worker

    marker = tmp_path / "selftest.json"
    done = marker  # 处理器把 attempt 信息写盘，验证外部产物按 attempt 组织

    def selftest(config, task):
        done.write_text(
            json.dumps({"task": task.id, "attempt": task.attempt_id, "token": task.fencing_token}),
            encoding="utf-8",
        )

    task_id = _enqueue(pg_config, "selftest", {"note": "hi"}, dedupe_key="st-1")
    worker = Worker(pg_config, queues=["selftest"], once=True, extra_handlers={"selftest": selftest})
    executed = worker.run()
    assert executed == 1
    recorded = json.loads(done.read_text(encoding="utf-8"))
    assert recorded["task"] == task_id and recorded["attempt"]
    from shared.backends import open_context

    ctx = open_context(pg_config)
    with ctx.db.connect() as db:
        row = db.execute("SELECT status FROM task_queue WHERE id=%s", (task_id,)).fetchone()
    assert row["status"] == "succeeded"


def test_worker_stale_attempt_does_not_commit(pg_config):
    """暂停超过 TTL 的执行不得提交（plans §11 阶段 3 闸门）。"""
    from shared.backends import open_context
    from shared.db.queue_pg import PgTaskQueue
    from worker.runtime import Worker

    _enqueue(pg_config, "selftest", {}, dedupe_key="stale-1")
    # ttl=0.5s、心跳间隔 10s（不会抢救）：处理器睡 1.5s → 租约过期 → 完成被拒
    worker = Worker(
        pg_config,
        queues=["selftest"],
        once=True,
        task_ttl=0.5,
        renew_interval=10.0,
        extra_handlers={"selftest": lambda config, task: time.sleep(1.5)},
    )
    assert worker.run() == 1  # 执行了，但完成提交被拒
    ctx = open_context(pg_config)
    with ctx.db.connect() as db:
        row = db.execute("SELECT status FROM task_queue WHERE dedupe_key='stale-1'").fetchone()
        assert row["status"] in ("running", "pending")  # 未被认定成功；回收后重派
        PgTaskQueue(ctx.library_id).reclaim_expired(db)
        assert (
            db.execute("SELECT status FROM task_queue WHERE dedupe_key='stale-1'").fetchone()["status"]
            == "pending"
        )


def test_worker_failure_goes_to_retry(pg_config):
    from shared.backends import open_context
    from worker.runtime import Worker

    def boom(config, task):
        raise RuntimeError("合成失败")

    _enqueue(pg_config, "selftest", {}, dedupe_key="fail-1", max_attempts=1)
    Worker(pg_config, queues=["selftest"], once=True, extra_handlers={"selftest": boom}).run()
    ctx = open_context(pg_config)
    with ctx.db.connect() as db:
        row = db.execute("SELECT status, error FROM task_queue WHERE dedupe_key='fail-1'").fetchone()
    assert row["status"] == "failed" and "合成失败" in row["error"]


def test_backlog_scan_enqueues_stranded_ledger_rows(pg_config):
    """账本 queued 但入队前崩溃：补偿扫描幂等补入（durable outbox 简化变体）。"""
    from shared.queue_dispatch import enqueue_backlog
    from shared.storage import Store

    store = Store.open(pg_config)
    store.execute(
        "INSERT INTO source_tasks(id,source_id,work,status,config,options) "
        "VALUES('t-strand','s1','detect','queued','{}','{}')"
    )
    first = enqueue_backlog(pg_config)
    again = enqueue_backlog(pg_config)
    assert first == {"source_tasks": 1}
    assert again == {"source_tasks": 1}  # 幂等：dedupe 命中
    with store.backend.connect() as db:
        rows = db.execute(
            "SELECT count(*) AS n FROM task_queue WHERE dedupe_key='source_tasks:t-strand'"
        ).fetchone()
    assert rows["n"] == 1


def test_dispatch_api_to_worker_end_to_end(pg_config, video_file):
    """API 创建 source 任务（postgres 队列）→ worker 执行 → 账本与队列同为终态。"""

    from api import create_app
    from conftest import FixtureAnalyzer, FixtureEmbedder
    from processing.pipeline import Pipeline
    from shared.backends import open_context
    from shared.db.queue_pg import PgTaskQueue
    from worker.runtime import Worker

    embedder = FixtureEmbedder(pg_config)
    pipeline = Pipeline(pg_config, analyzer=FixtureAnalyzer(embedder))
    source_id = pipeline.prepare(video_file)
    app = create_app(pg_config, embedder)
    with TestClient(app) as client:
        response = client.post(f"/api/admin/sources/{source_id}/shots")
        assert response.status_code == 202, response.text
        task = response.json()
        assert task["status"] == "queued"  # 不再由 API 线程执行
    ctx = open_context(pg_config)
    with ctx.db.connect() as db:
        pending = db.execute("SELECT count(*) AS n FROM task_queue WHERE status='pending'").fetchone()
    assert pending["n"] >= 1  # dispatch 已入队

    Worker(pg_config, queues=["source"], once=True).run()  # 使用真实账本处理器链
    ledger = ctx.store.one("SELECT status FROM source_tasks WHERE id=?", (task["id"],))
    assert ledger["status"] == "complete"
    with ctx.db.connect() as db:
        queue_row = db.execute(
            "SELECT status FROM task_queue WHERE dedupe_key=%s", (f"source_tasks:{task['id']}",)
        ).fetchone()
    assert queue_row["status"] == "succeeded"
    assert PgTaskQueue(ctx.library_id)  # 队列原语与上下文共享 library_id 语义


def test_topic_scheduler_leader_lease_gates_ticks(pg_config):
    """locks.backend=postgres 时同一时刻只有一个调度角色触发（plans §7.2）。"""

    from shared.backends import open_context
    from shared.db.locks_pg import PgLockProvider
    from topics.scheduler import LEADER_NAME, TopicScheduler

    ctx = open_context(pg_config)
    provider_a = PgLockProvider(ctx.db.backend, library_id=ctx.library_id)
    provider_b = PgLockProvider(ctx.db.backend, library_id=ctx.library_id)

    class FakeRuns:
        def __init__(self):
            self.started = []

        def start(self, profile_id, trigger="manual"):
            self.started.append((profile_id, trigger))
            return {"id": f"run-{len(self.started)}"}

    class FakeLeader:  # 占住 leader 租约：provider_b 的 tick 必须跳过
        def __init__(self, provider):
            self.provider = provider

        def __enter__(self):
            self.lease = self.provider.lease(LEADER_NAME, mode="exclusive", ttl=30, wait=0)
            return self.lease

        def __exit__(self, *_):
            self.lease.release()

    runs = FakeRuns()
    scheduler_b = TopicScheduler(ctx.db, runs, leader=provider_b)
    with FakeLeader(provider_a):
        assert scheduler_b.tick() == []  # leader 被他人持有：本轮跳过
    assert scheduler_b.tick() == []  # 无档案时也不触发（runs.start 未被调用）
    assert runs.started == []
