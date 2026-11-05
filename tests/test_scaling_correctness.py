"""分支审查发现的集成缺陷：过期写回、原子入队、取消、并发、迁移与跨节点文件。"""
from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from uuid import uuid4

import pytest

from mindex.shared.backends import open_context
from mindex.shared.config import Config


@pytest.fixture
def pg_config(tmp_path, monkeypatch):
    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("需要隔离 MINDEX_TEST_POSTGRES_DSN")
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg
    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    with psycopg.connect(dsn) as db:
        migrate_pg(db)
    return Config(data_dir=tmp_path / "node-a", database={"backend": "postgres"},
                  queue={"backend": "postgres"}, locks={"backend": "postgres"})


@pytest.fixture
def s3_config(pg_config, monkeypatch):
    endpoint = os.environ.get("MINDEX_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("需要隔离 MINDEX_TEST_S3_ENDPOINT")
    import boto3
    access = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
    secret = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", access)
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", secret)
    client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access,
                          aws_secret_access_key=secret, region_name="us-east-1")
    bucket = f"mindex-review-{uuid4().hex[:12]}"
    client.create_bucket(Bucket=bucket)
    pg_config.storage.backend = "s3"
    pg_config.storage.s3.endpoint_url = endpoint
    pg_config.storage.s3.bucket = bucket
    pg_config.storage.s3.min_free_gb = 0
    yield pg_config
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        if page.get("Contents"):
            client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": x["Key"]} for x in page["Contents"]]})
    client.delete_bucket(Bucket=bucket)


def _enqueue(context, kind="check", **kwargs):
    from mindex.shared.db.queue_pg import PgTaskQueue
    queue = PgTaskQueue(context.library_id)
    with context.store.connect() as db:
        return queue.enqueue(db, kind, {}, **kwargs)


@pytest.mark.requires_postgres
def test_ledger_and_enqueue_roll_back_together(pg_config, monkeypatch):
    from mindex.shared.db.queue_pg import PgTaskQueue
    from mindex.shared.queue_dispatch import ledger_transaction

    store = open_context(pg_config).store
    original = PgTaskQueue.enqueue
    def crash(self, db, *args, **kwargs):
        original(self, db, *args, **kwargs)
        assert db.execute("SELECT count(*) FROM task_queue").fetchone()[0] == 1
        raise RuntimeError("模拟账本和入队提交前崩溃")
    monkeypatch.setattr(PgTaskQueue, "enqueue", crash)
    with pytest.raises(RuntimeError):
        with ledger_transaction(store, "source_tasks", "t") as db:
            db.execute("INSERT INTO source_tasks(id,source_id,work,status,config,options)"
                       " VALUES('t','s','shots','queued','{}','{}')")
    assert store.one("SELECT count(*) AS n FROM task_queue")["n"] == 0
    assert store.one("SELECT count(*) AS n FROM source_tasks")["n"] == 0


@pytest.mark.requires_postgres
def test_expired_task_cannot_renew_without_reclaim(pg_config):
    from mindex.shared.db.queue_pg import PgTaskQueue
    context = open_context(pg_config)
    queue = PgTaskQueue(context.library_id)
    _enqueue(context)
    with context.store.connect() as db:
        task = queue.claim(db, "old", ["check"], ttl=.05)
    time.sleep(.08)
    with context.store.connect() as db:
        assert not queue.renew(db, task)
        assert not queue.complete(db, task)


@pytest.mark.requires_postgres
def test_expired_worker_cannot_write_business_state(pg_config):
    from mindex.worker_runtime import Worker
    context = open_context(pg_config)
    _enqueue(context)
    def stale(config, task):
        time.sleep(.12)
        open_context(config).store.execute("INSERT INTO settings(key,value) VALUES('stale','forbidden')")
    worker = Worker(pg_config, queues=["check"], once=True, task_ttl=.05, renew_interval=10,
                    extra_handlers={"check": stale})
    assert worker.run() == 1
    assert context.store.one("SELECT value FROM settings WHERE key='stale'") is None
    assert context.store.one("SELECT status FROM task_queue")["status"] != "succeeded"


@pytest.mark.requires_postgres
def test_worker_concurrency_runs_two_handlers_together(pg_config):
    from mindex.worker_runtime import Worker
    context = open_context(pg_config)
    _enqueue(context, dedupe_key="one")
    _enqueue(context, dedupe_key="two")
    barrier = threading.Barrier(2)
    calls = []
    def concurrent(config, task):
        barrier.wait(timeout=5)
        calls.append(task.id)
    worker = Worker(pg_config, queues=["check"], concurrency=2, idle_exit=.05,
                    extra_handlers={"check": concurrent})
    assert worker.run() == 2
    assert len(calls) == 2
    assert {r["status"] for r in context.store.rows("SELECT status FROM task_queue")} == {"succeeded"}


@pytest.mark.requires_postgres
def test_running_cancel_crosses_node_directories(pg_config, tmp_path):
    from mindex.shared.queue_dispatch import insert_ledger
    from mindex.shared.task_cancel import registry_for
    from mindex.shared.task_progress import request_task_cancel
    from mindex.worker_runtime import Worker
    context = open_context(pg_config)
    insert_ledger(context.store, "source_tasks", "cancel-me",
                  "INSERT INTO source_tasks(id,source_id,work,status,config,options) VALUES(?,?,?,?,?,?)",
                  ("cancel-me", "s", "shots", "queued", "{}", "{}"))
    started = threading.Event()
    def running(config, task):
        started.set()
        for _ in range(300):
            registry_for(config.data_dir).check(task.ledger_id)
            time.sleep(.01)
        raise AssertionError("取消未传播")
    worker = Worker(pg_config, queues=["source"], once=True, renew_interval=.03,
                    extra_handlers={"source": running})
    thread = threading.Thread(target=worker.run)
    thread.start()
    assert started.wait(5)
    other = pg_config.model_copy(update={"data_dir": tmp_path / "node-b"})
    request_task_cancel(open_context(other).store, "source_tasks", "cancel-me")
    thread.join(6)
    assert not thread.is_alive()
    assert context.store.one("SELECT status FROM source_tasks")["status"] == "cancelled"
    assert context.store.one("SELECT status FROM task_queue")["status"] == "cancelled"


@pytest.mark.requires_postgres
def test_maintenance_blocks_other_thread_writes_and_releases_after_error(pg_config):
    from mindex.shared.db.maintenance_gate import MaintenanceBusy, maintenance_gate
    store = open_context(pg_config).store
    errors = []
    def write():
        try:
            store.execute("INSERT INTO settings(key,value) VALUES('during-maintenance','bad')")
        except MaintenanceBusy:
            errors.append("blocked")
    with pytest.raises(RuntimeError), maintenance_gate(store, name="故障测试"):
        thread = threading.Thread(target=write)
        thread.start()
        thread.join(5)
        assert errors == ["blocked"]
        assert store.one("SELECT 1 AS n")["n"] == 1  # 只读继续
        raise RuntimeError("模拟维护事务异常")
    store.execute("INSERT INTO settings(key,value) VALUES('after-maintenance','ok')")


@pytest.mark.requires_postgres
@pytest.mark.requires_s3
def test_checkpoint_reads_latest_remote_revision_even_with_local_cache(s3_config, tmp_path):
    from mindex.shared import checkpoints
    first = open_context(s3_config).store
    second = open_context(s3_config.model_copy(update={"data_dir": tmp_path / "node-b"})).store
    checkpoints.save_text(first, first.root / "plans/a.json", '{"revision":1}')
    assert checkpoints.read_text(second, second.root / "plans/a.json") == '{"revision":1}'
    checkpoints.save_text(first, first.root / "plans/a.json", '{"revision":2}')
    assert checkpoints.read_text(second, second.root / "plans/a.json") == '{"revision":2}'
    assert len(list(first.media.iter_prefix("objects/"))) == 2  # 旧对象仍不可变，等待 GC 宽限


@pytest.mark.requires_postgres
@pytest.mark.requires_s3
def test_media_pointer_holds_pin_and_preserves_consumer_error(s3_config, tmp_path):
    from mindex.shared import files

    first = open_context(s3_config).store
    path = first.root / "clips/a.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"authoritative-media")
    files.publish(first, path)
    other = open_context(s3_config.model_copy(update={"data_dir": tmp_path / "node-b"}))
    with pytest.raises(FileNotFoundError, match="消费方错误"):
        with other.store.materialize_media("clips/a.mp4") as cached:
            assert cached.read_bytes() == b"authoritative-media"
            assert other.staging.usage()["pinned"] == len(b"authoritative-media")
            raise FileNotFoundError("消费方错误")
    assert other.staging.usage()["pinned"] == 0
    entry = files.pointer(first, "clips/a.mp4")
    first.execute("UPDATE shared_files SET sha256=? WHERE path='clips/a.mp4'", ("0" * 64,))
    assert not other.store.media_available("clips/a.mp4")
    with pytest.raises(ValueError, match="元数据库指针"), other.store.materialize_media("clips/a.mp4"):
        pass
    assert other.staging.usage()["pinned"] == 0
    first.execute("UPDATE shared_files SET sha256=? WHERE path='clips/a.mp4'", (entry["sha256"],))
    # 墓碑优先于仍存在的节点文件，不能把旧缓存继续作为权威媒体。
    files.delete_tree(first, "clips/")
    with pytest.raises(FileNotFoundError), first.materialize_media("clips/a.mp4"):
        pass
    node_only = first.root / "sources/node-only.mp4"
    node_only.parent.mkdir(parents=True)
    node_only.write_bytes(b"stale-node-file")
    assert not first.media_available("sources/node-only.mp4")


@pytest.mark.requires_postgres
@pytest.mark.requires_s3
def test_studio_workspace_cas_is_shared_across_directories(s3_config, tmp_path):
    from mindex.studio.workspace import versions
    second = s3_config.model_copy(update={"data_dir": tmp_path / "node-b"})
    job = "20260101-120000-abcdef"
    workspace = {"plans": {}, "versions": {}, "builds": {}, "active_plan": None}
    versions.persist(s3_config, job, dict(workspace))
    baseline = versions.current_revision(second, job)
    versions.persist(s3_config, job, {**workspace, "active_plan": "new"}, expected_revision=baseline)
    with pytest.raises(versions.RevisionConflict):
        versions.persist(second, job, {**workspace, "active_plan": "stale"}, expected_revision=baseline)
    assert versions.current_revision(second, job) == versions.current_revision(s3_config, job)


@pytest.mark.requires_postgres
@pytest.mark.requires_s3
def test_s3_rejects_false_digest_and_concurrent_overwrite(s3_config):
    media = open_context(s3_config).media
    with pytest.raises(ValueError, match="摘要"):
        media.put_immutable("invalid", io.BytesIO(b"data"), sha256="0" * 64)
    barrier, results = threading.Barrier(2), []
    def put(data):
        barrier.wait(timeout=5)
        try:
            media.put_immutable("race", io.BytesIO(data), sha256=hashlib.sha256(data).hexdigest())
            results.append("ok")
        except ValueError:
            results.append("conflict")
    threads = [threading.Thread(target=put, args=(data,)) for data in (b"first", b"second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(results) == ["conflict", "ok"]


@pytest.mark.requires_postgres
def test_outbox_retry_after_ack_failure_does_not_lose_or_duplicate(pg_config, tmp_path, monkeypatch):
    from mindex.agent.changes import ChangeFeed
    from mindex.agent.outbox_bridge import OutboxBridge
    from mindex.shared.db import agent_outbox
    store = open_context(pg_config).store
    agent_outbox.publish(store, "mutation", {"domain": "search"})
    feed = ChangeFeed(tmp_path / "gateway")
    bridge = OutboxBridge(store, feed)
    original = agent_outbox.acknowledge
    def fail(*args):
        raise RuntimeError("模拟事件落盘后 PG ack 失败")
    monkeypatch.setattr(agent_outbox, "acknowledge", fail)
    assert bridge.drain_once() == 0
    assert len(feed.after(0)) == 1
    monkeypatch.setattr(agent_outbox, "acknowledge", original)
    # 新建网关模拟重启，必须依赖持久回执去重。
    restarted = OutboxBridge(store, ChangeFeed(tmp_path / "gateway"))
    assert restarted.drain_once() == 1
    assert len(feed.after(0)) == 1
    assert restarted.drain_once() == 0


def test_local_migration_resume_activate_and_reject_nonempty(tmp_path):
    from mindex.maintenance.migration import activate_bundle, export_bundle, load_bundle, verify_bundle
    origin = Config(data_dir=tmp_path / "origin")
    open_context(origin).store.execute("INSERT INTO settings(key,value) VALUES('example','1')")
    bundle = tmp_path / "bundle"
    export_bundle(origin, bundle)
    target = Config(data_dir=tmp_path / "target")
    assert load_bundle(target, bundle)["consistent"]
    assert load_bundle(target, bundle)["consistent"]
    assert activate_bundle(target, bundle)["activated"]
    assert verify_bundle(target, bundle)["consistent"]
    with pytest.raises(ValueError, match="已激活"):
        load_bundle(target, bundle)
    existing = Config(data_dir=tmp_path / "existing")
    open_context(existing).store.execute("INSERT INTO settings(key,value) VALUES('keep','valuable')")
    with pytest.raises(ValueError, match="非空"):
        load_bundle(existing, bundle)
    assert open_context(existing).store.one("SELECT value FROM settings WHERE key='keep'")["value"] == "valuable"


@pytest.mark.requires_postgres
def test_pg_migration_roundtrip_and_nonempty_protection(pg_config, tmp_path):
    from mindex.maintenance.migration import activate_bundle, export_bundle, load_bundle
    origin = Config(data_dir=tmp_path / "origin")
    store = open_context(origin).store
    store.execute("INSERT INTO runs(id,stage,status) VALUES(42,'test','complete')")
    bundle = tmp_path / "bundle"
    export_bundle(origin, bundle)
    assert load_bundle(pg_config, bundle)["consistent"]
    assert load_bundle(pg_config, bundle)["consistent"]
    assert activate_bundle(pg_config, bundle)["activated"]
    target = open_context(pg_config).store
    with target.connect() as db:
        assert db.execute("INSERT INTO runs(stage,status) VALUES('new','complete')").lastrowid == 43
    with pytest.raises(ValueError, match="已激活"):
        load_bundle(pg_config, bundle)
    assert target.one("SELECT count(*) AS n FROM runs")["n"] == 2


def test_staging_pinned_cache_counts_towards_quota(tmp_path):
    from mindex.shared.backends.staging import StagingArea, StagingFull
    staging = StagingArea(tmp_path, quota_bytes=100)
    path = tmp_path / "active"
    path.write_bytes(b"x" * 80)
    staging.adopt(path)
    staging.pin(path)
    with pytest.raises(StagingFull):
        staging.reserve(30)
    with pytest.raises(ValueError):
        staging.reserve(-1)
