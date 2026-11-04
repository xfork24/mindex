"""底座验收：部署拒绝降级、连接预算、追加升级及显式/跨线程 fencing。"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mindex.shared.backends import open_context
from mindex.shared.config import Config, PostgresConfig


@pytest.mark.parametrize("backend,value", [
    ("vector", "chroma_server"), ("storage", "s3"), ("locks", "postgres"), ("queue", "postgres"),
])
def test_invalid_shared_combination_does_not_create_local_database(tmp_path, backend, value):
    config = Config(data_dir=tmp_path / "node", **{backend: {"backend": value}})
    with pytest.raises(RuntimeError, match="需要 database.backend=postgres"):
        open_context(config)
    assert not config.data_dir.exists()


def test_explicit_cluster_never_falls_back(tmp_path):
    config = Config(data_dir=tmp_path / "node", server={"form": "cluster"})
    with pytest.raises(RuntimeError, match="server.form=cluster"):
        open_context(config)
    assert not config.data_dir.exists()


def test_expected_library_identity_is_checked(tmp_path):
    config = Config(data_dir=tmp_path / "node")
    identity = open_context(config).library_id
    config.server.library_id = "wrong-library"
    with pytest.raises(RuntimeError, match="library_id"):
        open_context(config)
    config.server.library_id = identity
    assert open_context(config).library_id == identity


@pytest.mark.parametrize("values", [
    {"pool_min": 9}, {"pool_max": 9}, {"control_pool_max": 3}, {"checkout_timeout": 0},
])
def test_pool_configuration_is_validated(values):
    with pytest.raises(ValueError):
        PostgresConfig(**values)


def test_pg_warmup_does_not_load_snapshot_or_people_maps(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mindex.persons.persons import Persons
    from mindex.processing import indexing
    from mindex.search import warmup
    from mindex.shared.storage import Store

    def unexpected(*args, **kwargs):
        pytest.fail("PG 预热不能读取全量快照、人物证据或向量映射")

    fake = SimpleNamespace(pg=True, root=tmp_path, library_id="test",
                           active_index=lambda: {"id": "v", "status": "ready", "spec": {"format": 2}},
                           snapshots_shared=unexpected)
    monkeypatch.setattr(Store, "open", lambda _: fake)
    monkeypatch.setattr(Persons, "published_evidence", unexpected)
    monkeypatch.setattr(Persons, "published_entities", unexpected)
    indexing.VectorIndex(tmp_path, "v", create=True)
    monkeypatch.setattr(indexing.VectorIndex, "_representation_map", unexpected)
    monkeypatch.setattr(warmup, "retire_superseded", lambda *a: None)
    warmup.warm_up(Config(data_dir=tmp_path), startup=False)


@pytest.mark.requires_postgres
def test_snapshot_cache_isolates_library_identity_at_the_same_node_path(pg):
    from uuid import uuid4

    from mindex.shared.storage import Store, dumps

    first = Store.open(pg)
    first.execute("INSERT INTO index_versions(id,spec,status) VALUES('same','{}','ready')")
    first.execute("INSERT INTO index_members VALUES('same','segment',?,1)", (dumps({"id": "first"}),))
    assert first.snapshots_shared("same") == [{"id": "first"}]
    # 同一节点目录连接另一库，行数与文本长度完全一致，签名不能替代库身份。
    first.execute("UPDATE settings SET value=? WHERE key='library.id'", (str(uuid4()),))
    first.execute("UPDATE index_members SET snapshot=? WHERE index_id='same'", (dumps({"id": "other"}),))
    second = Store.open(pg)
    assert second.path == first.path and second.library_id != first.library_id
    assert second.snapshot_signature("same") == first.snapshot_signature("same")
    assert second.snapshots_shared("same") == [{"id": "other"}]


@pytest.fixture
def pg(tmp_path, monkeypatch):
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
    return Config(data_dir=tmp_path / "node", database={"backend": "postgres"},
                  queue={"backend": "postgres"}, locks={"backend": "postgres"})


@pytest.mark.requires_postgres
def test_append_upgrade_preserves_v15_data_and_identity(pg):
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg
    from mindex.shared.db.schema_pg import BASELINE_DDL

    dsn = os.environ["MINDEX_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn) as db:
        db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        db.execute(BASELINE_DDL)
        db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)")
        db.execute("INSERT INTO schema_migrations VALUES(15,%s)",
                   (hashlib.sha256(BASELINE_DDL.encode()).hexdigest(),))
        db.execute("INSERT INTO settings(key,value) VALUES('library.id','old-library'),('preserve','value')")
        # 基线若被重跑，会尝试创建此索引并撞上同名表。
        db.execute("DROP INDEX segments_source; CREATE TABLE segments_source(marker TEXT)")
        assert migrate_pg(db) == 18
        assert migrate_pg(db) == 18
        assert db.execute("SELECT value FROM settings WHERE key='library.id'").fetchone()[0] == "old-library"
        assert db.execute("SELECT value FROM settings WHERE key='preserve'").fetchone()[0] == "value"
        assert db.execute("SELECT count(*) FROM shared_files").fetchone()[0] == 0


@pytest.mark.requires_postgres
def test_changed_migration_checksum_and_future_schema_are_rejected(pg):
    import psycopg

    from mindex.shared.db.errors import SchemaMismatch
    from mindex.shared.db.migrate_pg import migrate_pg

    dsn = os.environ["MINDEX_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn) as db:
        db.execute("UPDATE schema_migrations SET checksum='wrong' WHERE version=16")
    with psycopg.connect(dsn) as db, pytest.raises(SchemaMismatch, match="校验值"):
        migrate_pg(db)
    with psycopg.connect(dsn) as db:
        db.execute("INSERT INTO schema_migrations(version,checksum) VALUES(99,'future')")
    with psycopg.connect(dsn) as db, pytest.raises(SchemaMismatch, match="高于"):
        migrate_pg(db)


@pytest.mark.requires_postgres
def test_query_pool_exhaustion_does_not_block_control_pool(pg):
    from psycopg.conninfo import make_conninfo
    from psycopg_pool import PoolTimeout

    from mindex.shared.db.pg_backend import PgBackend

    backend = PgBackend(make_conninfo(os.environ["MINDEX_TEST_POSTGRES_DSN"], application_name="pool-budget"),
                        library_id="test", pool_min=1, pool_max=1, control_pool_max=1,
                        connection_budget=2, checkout_timeout=.05, control_checkout_timeout=.1)
    try:
        with backend.connect():
            with pytest.raises(PoolTimeout), backend.connect():
                pass
            with backend.connect(control=True) as db:
                assert db.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(RuntimeError, match="控制连接"), backend.connect(control=True) as db:
                db.execute("INSERT INTO settings(key,value) VALUES('bypass','forbidden')")
            for sql in ("INSERT OR REPLACE INTO settings(key,value) VALUES('bypass','forbidden')",
                        'UPDATE "settings" SET value=\'forbidden\'', "DROP SCHEMA public CASCADE"):
                with pytest.raises(RuntimeError, match="控制连接"), backend.connect(control=True) as db:
                    db.execute(sql)
        assert backend.pool().min_size == 1
        assert backend.pool(control=True).max_size == 1
    finally:
        backend.pool().close()
        backend.pool(control=True).close()


def _attempt(context, ttl=.15):
    from mindex.shared.db.fencing import Attempt
    from mindex.shared.db.queue_pg import PgTaskQueue

    backend = context.store.backend
    queue = PgTaskQueue(context.library_id)
    with backend.connect() as db:
        queue.enqueue(db, "test", {})
        task = queue.claim(db, "old-worker", ["test"], ttl=ttl)
    return Attempt(backend, context.library_id, task, threading.Event(), threading.Event())


@pytest.mark.requires_postgres
def test_explicit_commit_rejects_expired_attempt(pg):
    from mindex.shared.db.fencing import executing
    from mindex.shared.db.locks_pg import LeaseLost

    context = open_context(pg)
    attempt = _attempt(context)
    with pytest.raises(LeaseLost), executing(attempt), context.store.connect() as db:
        db.execute("INSERT INTO settings(key,value) VALUES('stale-commit','forbidden')")
        time.sleep(.2)
        db.commit()
    assert context.store.one("SELECT value FROM settings WHERE key='stale-commit'") is None


@pytest.mark.requires_postgres
def test_explicit_fence_survives_thread_and_context_boundary(pg):
    from mindex.shared.db.fencing import ExecutionFence, executing
    from mindex.shared.db.locks_pg import LeaseLost

    context = open_context(pg)
    with executing(_attempt(context)):
        fence = ExecutionFence.capture()
    time.sleep(.2)
    def stale():
        with context.store.backend.connect(execution=fence) as db:
            db.execute("INSERT INTO settings(key,value) VALUES('child','forbidden')")
    with ThreadPoolExecutor(max_workers=1) as pool, pytest.raises(LeaseLost):
        pool.submit(stale).result()
    assert context.store.one("SELECT value FROM settings WHERE key='child'") is None


@pytest.mark.requires_postgres
def test_processing_pool_carries_fence_to_child_writes(pg):
    from mindex.shared.db.fencing import FencedThreadPoolExecutor, executing
    from mindex.shared.db.locks_pg import LeaseLost

    context = open_context(pg)
    def stale():
        time.sleep(.2)
        context.store.execute("INSERT INTO settings(key,value) VALUES('pool-child','forbidden')")
    with executing(_attempt(context)), FencedThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(LeaseLost):
            pool.submit(stale).result()
    assert context.store.one("SELECT value FROM settings WHERE key='pool-child'") is None


@pytest.mark.requires_postgres
@pytest.mark.parametrize("cancel,attempts,expected", [(False, 3, "failed"), (True, 1, "cancelled"), (False, 1, "queued")])
def test_reclaimed_task_updates_ledger_and_clears_attempt(pg, cancel, attempts, expected):
    from mindex.shared.db.queue_pg import PgTaskQueue
    from mindex.shared.queue_dispatch import insert_ledger

    context = open_context(pg)
    store, queue = context.store, PgTaskQueue(context.library_id)
    insert_ledger(store, "source_tasks", "expired", "INSERT INTO source_tasks(id,source_id,work,status,config,options)"
                  " VALUES(?,?,?,?,?,?)", ("expired", "s", "shots", "queued", "{}", "{}"))
    with store.connect() as db:
        task = queue.claim(db, "stale", ["source"])
    row = store.one("SELECT * FROM source_tasks WHERE id='expired'")
    assert row["queue_attempt_id"] == task.attempt_id
    assert row["queue_fencing_token"] == task.fencing_token
    with store.connect() as db:
        db.execute("UPDATE task_queue SET attempts=%s,cancel_requested=%s,expires_at=clock_timestamp()-interval '1 second'",
                   (attempts, cancel))
        assert queue.reclaim_expired(db) == 1
    row = store.one("SELECT * FROM source_tasks WHERE id='expired'")
    assert row["status"] == expected
    assert row["queue_attempt_id"] is None
    assert row["error"]
