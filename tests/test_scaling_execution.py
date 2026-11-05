"""索引持久链与调度周期：检查真实账本、发布指针及跨节点重放，而非只检查 HTTP。"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient

from mindex.admin.source_tasks import IndexTasks
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.shared.config import Config
from mindex.shared.storage import Store
from mindex.topics import profiles, scheduler
from mindex.topics.models import ProfileBody, Schedule
from mindex.topics.runner import TopicRuns


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
    return Config(data_dir=tmp_path / "api", database={"backend": "postgres"},
                  queue={"backend": "postgres"}, locks={"backend": "postgres"})


def index_runner(config):
    store = Store.open(config)
    return IndexTasks(RuntimeConfig(config, store), store)


def ready(store, index_id="v"):
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES(?,'{}','ready')", (index_id,))


def test_named_timezone_without_system_zone_database():
    import zoneinfo

    original = zoneinfo.TZPATH
    try:
        zoneinfo.reset_tzpath([])  # 模拟缺少系统 IANA 数据的安装环境。
        zone = zoneinfo.ZoneInfo.no_cache("Asia/Shanghai")
        assert datetime(2026, 11, 5, tzinfo=zone).utcoffset().total_seconds() == 8 * 3600
    finally:
        zoneinfo.reset_tzpath(original)


def wait_task(client, task_id):
    for _ in range(200):
        task = client.get(f"/api/index-tasks/{task_id}").json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(.02)
    pytest.fail("索引任务未按时结束")


def test_local_index_task_rest_and_cli(config, tmp_path):
    from typer.testing import CliRunner

    from mindex.api import create_app
    from mindex.cli import app

    ready(Store.open(config))
    with TestClient(create_app(config)) as client:
        response = client.post("/api/index-tasks", json={"action": "lexical", "index_id": "v"})
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        task = wait_task(client, task_id)
        assert task["status"] == "complete", task
        assert task["result"]["indexes"][0]["index_id"] == "v"
        assert "config" not in task and "queue_attempt_id" not in task
        assert client.get("/api/index-tasks?limit=1").json()["items"][0]["id"] == task_id
        assert client.post(f"/api/index-tasks/{task_id}/retry").status_code == 409
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {config.data_dir}\n")
    out = CliRunner().invoke(app, ["--config", str(path), "indexes", "tasks", "show", task_id])
    assert out.exit_code == 0, out.output
    assert '"complete"' in out.output and task_id in out.output


def test_scheduler_uses_library_timezone_and_period_replay(config, monkeypatch):
    store = Store.open(config)
    runtime = RuntimeConfig(config, store)
    runs = TopicRuns(runtime, store)
    # 保留 queued 账本，以便直接核对周期占位；不执行网络挖掘。
    monkeypatch.setattr(runs, "launch", lambda task_id, config, lock: lock.release())
    profile = profiles.create_profile(store, ProfileBody(name="财经", domain="金融",
        schedule=Schedule(enabled=True, time="08:00")))["id"]
    now = datetime(2026, 11, 5, 0, 1, tzinfo=ZoneInfo("UTC"))
    sched = scheduler.TopicScheduler(store, runs, timezone="Asia/Shanghai", clock=lambda: now)
    ids = sched.tick()
    assert len(ids) == 1  # 节点为 UTC，当地库时间已到 08:01。
    assert sched.tick() == []
    stamp = now.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    again = runs.start_scheduled(profile, period="2026-11-05", ordinal=1,
                                 trigger="catchup", created_at=stamp)
    assert again["id"] == ids[0]
    store.execute("UPDATE topic_runs SET status='failed' WHERE id=?", (ids[0],))
    second = sched.tick()
    assert len(second) == 1 and second != ids
    assert runs.start_scheduled(profile, period="2026-11-05", ordinal=1,
                                trigger="schedule", created_at=stamp)["id"] == ids[0]
    store.execute("UPDATE topic_runs SET status='failed' WHERE id=?", (second[0],))
    third = sched.tick()
    store.execute("UPDATE topic_runs SET status='failed' WHERE id=?", (third[0],))
    assert sched.tick() == []
    assert len(store.rows("SELECT * FROM scheduled_runs")) == 3
    assert scheduler.status(store, now=now)["now"].endswith("+08:00")
    with pytest.raises(ValueError, match="时区"):
        scheduler.TopicScheduler(store, runs, timezone="UTC")
    profiles.delete_profile(store, profile)
    assert store.rows("SELECT * FROM scheduled_runs") == []


def test_remote_mcp_does_not_open_local_library(config, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from mindex.agent import mcp_server
    from mindex.cli import app
    from mindex.settings import runtime_settings

    def unexpected(*args, **kwargs):
        raise AssertionError("远程 stdio 不得打开本地库或会话")

    monkeypatch.setattr(runtime_settings, "apply_runtime_overrides", unexpected)
    monkeypatch.setattr(Store, "open", unexpected)
    monkeypatch.setattr(mcp_server, "build_server", lambda *args, **kwargs: object())

    async def serve(server):
        pass

    monkeypatch.setattr(mcp_server, "serve_stdio", serve)
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {config.data_dir}\n")
    out = CliRunner().invoke(app, ["--config", str(path), "mcp", "--base-url", "http://127.0.0.1:1111"])
    assert out.exit_code == 0, (out.output, out.exception)
    assert not config.data_dir.exists()
    runtime = SimpleNamespace(base=config)
    with pytest.raises(RuntimeError, match="业务 API"):
        mcp_server.server_for_app(None, runtime, SimpleNamespace(pg=True))


@pytest.mark.requires_postgres
def test_publication_lock_is_held_through_terminal_commit(pg_config, tmp_path):
    from mindex.shared.db.fencing import publication_scope
    from mindex.worker_runtime import Worker

    runner = index_runner(pg_config)
    ready(runner.store, "first")
    ready(runner.store, "second")
    first = runner.start({"action": "lexical", "index_id": "first"})
    runner.start({"action": "lexical", "index_id": "second"})
    registered, release, waiting, inherited = (threading.Event() for _ in range(4))
    seen, errors = [], []

    def handler(config, task):
        store = Store.open(config)
        if task.ledger_id != first["id"]:
            waiting.set()
        with publication_scope(store, config=config):
            if task.ledger_id == first["id"]:
                store.activate("first")
            else:
                seen.append(store.active_index()["id"])
                inherited.set()
                store.activate("second")
        if task.ledger_id == first["id"]:
            registered.set()
            assert release.wait(6)

    def run(config):
        try:
            Worker(config, queues=["index"], once=True, extra_handlers={"index": handler}).run()
        except BaseException as exc:  # noqa: BLE001 — 把执行线程异常交回主测试线程
            errors.append(exc)

    a = threading.Thread(target=run, args=(pg_config,))
    b = threading.Thread(target=run, args=(pg_config.model_copy(update={"data_dir": tmp_path / "b"}),))
    a.start()
    try:
        assert registered.wait(5)
        b.start()
        assert waiting.wait(5)
        assert not inherited.wait(.15)
    finally:
        release.set()
        a.join(8)
        if b.ident is not None:
            b.join(8)
    assert not a.is_alive() and not b.is_alive() and not errors, errors
    assert seen == ["first"]
    assert runner.store.active_index()["id"] == "second"
    assert {r["status"] for r in runner.store.rows("SELECT status FROM index_tasks")} == {"complete"}


@pytest.mark.requires_postgres
def test_person_publication_and_release_activation_use_index_queue(pg_config):
    from mindex.worker_runtime import Worker

    runner = index_runner(pg_config)
    ready(runner.store)
    task = runner.start({"action": "person-publish", "index_id": "v"})
    Worker(pg_config, queues=["index"], once=True).run()
    publication = runner.get(task["id"])
    assert publication["status"] == "complete", publication
    release_id = publication["result"]["release_id"]
    assert runner.store.active_index()["search_release_id"] == release_id
    runner.store.execute("DELETE FROM settings WHERE key IN ('active','active.release')")
    task = runner.start({"action": "activate-release", "release_id": release_id})
    assert runner.store.active_index() is None
    Worker(pg_config, queues=["index"], once=True).run()
    assert runner.get(task["id"])["status"] == "complete"
    assert runner.store.active_index()["search_release_id"] == release_id


@pytest.mark.requires_postgres
def test_index_api_queue_cancel_retry_and_real_lexical_worker(pg_config, tmp_path):
    from mindex.api import create_app
    from mindex.worker_runtime import Worker

    runner = index_runner(pg_config)
    ready(runner.store)
    with TestClient(create_app(pg_config)) as client:
        task = client.post("/api/index-tasks", json={"action": "lexical", "index_id": "v"}).json()
        assert task["status"] == "queued"
        assert runner.store.one("SELECT kind,status FROM task_queue") == {"kind": "index", "status": "pending"}
        assert client.post(f"/api/index-tasks/{task['id']}/cancel").json()["status"] == "cancelled"
        assert client.post(f"/api/index-tasks/{task['id']}/retry").json()["status"] == "queued"
    other = pg_config.model_copy(update={"data_dir": tmp_path / "worker"})
    assert Worker(other, queues=["index"], once=True).run() == 1
    assert runner.get(task["id"])["status"] == "complete"
    assert runner.store.one("SELECT status FROM task_queue")["status"] == "succeeded"
    assert runner.store.one("SELECT segments FROM lexical_index_meta WHERE index_id='v'")["segments"] == 0


@pytest.mark.requires_postgres
def test_worker_waits_for_submission_lock_instead_of_failing(pg_config):
    from mindex.worker_runtime import Worker

    runner = index_runner(pg_config)
    ready(runner.store)
    task = runner.start({"action": "lexical", "index_id": "v"})
    lock = runner.row_lock({"id": task["id"]})
    errors = []

    def run():
        try:
            Worker(pg_config, queues=["index"], once=True).run()
        except BaseException as exc:  # noqa: BLE001 — 把执行线程异常交回主测试线程
            errors.append(exc)

    thread = threading.Thread(target=run)
    lock.acquire()
    try:
        thread.start()
        for _ in range(100):
            if runner.get(task["id"])["status"] == "running":
                break
            time.sleep(.02)
        assert runner.get(task["id"])["status"] == "running"
        time.sleep(.1)
        assert runner.store.one("SELECT attempts,status FROM task_queue") == {"attempts": 1, "status": "running"}
    finally:
        lock.release()
        thread.join(8)
    assert not thread.is_alive() and not errors, errors
    assert runner.get(task["id"])["status"] == "complete"


@pytest.mark.requires_postgres
@pytest.mark.parametrize("outcome", ["success", "thread", "failure", "cancel", "expired", "lease-expired", "rebound", "invalid-publication"])
def test_publication_is_invisible_until_terminal_transaction(pg_config, outcome):
    from mindex.shared.db.fencing import publication_scope
    from mindex.worker_runtime import Worker

    runner = index_runner(pg_config)
    ready(runner.store)
    task = runner.start({"action": "lexical", "index_id": "v"})
    entered, release = threading.Event(), threading.Event()
    errors = []

    def handler(config, claimed):
        store = Store.open(config)

        def publish():
            with publication_scope(store, config=config):
                store.activate("missing" if outcome == "invalid-publication" else "v", execution=claimed.execution)

        if outcome == "thread":
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(1) as pool:
                pool.submit(claimed.execution.run, publish).result(timeout=5)
        else:
            publish()
        entered.set()
        assert release.wait(6)
        if outcome == "failure":
            raise ValueError("模拟发布登记后执行失败")

    worker = Worker(pg_config, queues=["index"], once=True, task_ttl=.8 if outcome == "expired" else 30,
                    renew_interval=10, extra_handlers={"index": handler})

    def run():
        try:
            worker.run()
        except BaseException as exc:  # noqa: BLE001 — 把执行线程异常交回主测试线程
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        assert runner.store.active_index() is None
        assert runner.get(task["id"])["status"] == "running"
        if outcome == "cancel":
            runner.cancel(task["id"])
        if outcome == "expired":
            time.sleep(1)
        if outcome == "lease-expired":
            runner.store.execute("DELETE FROM lock_holders WHERE name='index-write'")
        if outcome == "rebound":
            runner.store.execute("UPDATE index_tasks SET queue_attempt_id='new-attempt' WHERE id=?", (task["id"],))
    finally:
        release.set()
        thread.join(8)
    assert not thread.is_alive() and not errors, errors
    row = runner.store.one("SELECT status FROM task_queue")
    if outcome in {"success", "thread"}:
        assert runner.store.active_index()["id"] == "v"
        assert runner.get(task["id"])["status"] == "complete" and row["status"] == "succeeded"
    else:
        assert runner.store.active_index() is None
        assert runner.get(task["id"])["status"] != "complete" and row["status"] != "succeeded"
    # 失败/取消/失联都必须释放 attempt 持有的发布锁。
    from mindex.shared import lock_backend
    with lock_backend.named(runner.store, "index-write", exclusive=True, timeout=0):
        pass


@pytest.mark.requires_postgres
def test_real_visual_index_build_dispatches_without_caller_lock(pg_config, video_file, monkeypatch):
    from conftest import FixtureAnalyzer

    from mindex.models import adapters
    from mindex.processing.indexing import IndexBuilder
    from mindex.processing.pipeline import Pipeline
    from mindex.worker_runtime import Worker

    embedder = FixtureEmbedder(pg_config)
    Pipeline(pg_config, analyzer=FixtureAnalyzer(embedder)).prepare(video_file)
    frozen_model = pg_config.embedding.model
    monkeypatch.setattr(adapters, "make_embedder", lambda config: FixtureEmbedder(config))
    result, errors = [], []

    def submit():
        try:
            result.append(IndexBuilder(pg_config).build(mode="visual", activate=True))
        except BaseException as exc:  # noqa: BLE001 — 把执行线程异常交回主测试线程
            errors.append(exc)

    thread = threading.Thread(target=submit)
    thread.start()
    try:
        runner = index_runner(pg_config)
        for _ in range(100):
            if runner.store.one("SELECT id FROM index_tasks"):
                break
            time.sleep(.02)
        pg_config.embedding.model = "changed-after-submission"
        assert Worker(pg_config, queues=["index"], once=True).run() == 1
    finally:
        thread.join(15)
    assert not thread.is_alive() and not errors, errors
    assert runner.store.active_index()["id"] == result[0]
    assert runner.store.active_index()["spec"]["space"]["model"] == frozen_model
    assert runner.store.one("SELECT status FROM index_tasks")["status"] == "complete"


@pytest.mark.requires_postgres
def test_scheduler_leader_rotation_replays_one_period_across_directories(pg_config, tmp_path, monkeypatch):
    from mindex.shared.db.locks_pg import PgLockProvider
    from mindex.shared.db.queue_pg import PgTaskQueue

    a, b = Store.open(pg_config), Store.open(pg_config.model_copy(update={"data_dir": tmp_path / "b"}))
    pid = profiles.create_profile(a, ProfileBody(name="财经", domain="金融",
        schedule=Schedule(enabled=True, time="08:00")))["id"]
    runs_a, runs_b = TopicRuns(RuntimeConfig(pg_config, a), a), TopicRuns(RuntimeConfig(pg_config, b), b)
    instant = datetime(2026, 11, 5, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    sa = scheduler.TopicScheduler(a, runs_a, timezone="Asia/Shanghai", clock=lambda: instant,
        leader=PgLockProvider(a.backend, a.library_id))
    sb = scheduler.TopicScheduler(b, runs_b, timezone="Asia/Shanghai", clock=lambda: instant,
        leader=PgLockProvider(b.backend, b.library_id))
    lease = sa._leader.lease(scheduler.LEADER_NAME, wait=0)
    try:
        assert sb.tick() == []
    finally:
        lease.release()
    ids = sa.tick()
    assert len(ids) == 1
    assert sb.tick() == []
    replay = runs_b.start_scheduled(pid, period="2026-11-05", ordinal=1,
                                    trigger="catchup", created_at=instant.isoformat())
    assert replay["id"] == ids[0]
    assert len(a.rows("SELECT * FROM task_queue")) == len(a.rows("SELECT * FROM scheduled_runs")) == 1
    # 周期、账本、队列任一写入失败，都不允许留下其他两项。
    a.execute("UPDATE topic_runs SET status='failed' WHERE id=?", (ids[0],))
    original = PgTaskQueue.enqueue

    def crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("模拟周期入队崩溃")

    monkeypatch.setattr(PgTaskQueue, "enqueue", crash)
    with pytest.raises(RuntimeError, match="周期入队"):
        runs_b.start_scheduled(pid, period="2026-11-05", ordinal=2,
                               trigger="schedule", created_at=instant.isoformat())
    assert len(a.rows("SELECT * FROM task_queue")) == len(a.rows("SELECT * FROM scheduled_runs")) == 1
    assert len(a.rows("SELECT * FROM topic_runs")) == 1


@pytest.mark.requires_postgres
def test_v18_upgrade_and_nonempty_execution_tables_export(pg_config, tmp_path):
    import psycopg

    from mindex.maintenance.pg_export import export_pg_to_sqlite
    from mindex.shared.db.migrate_pg import migrate_pg

    with psycopg.connect(os.environ["MINDEX_TEST_POSTGRES_DSN"]) as db:
        before = db.execute("SELECT version,checksum FROM schema_migrations WHERE version<=18 ORDER BY version").fetchall()
        db.execute("DROP TABLE studio_events; DROP TABLE uploaded_assets; DROP TABLE upload_parts; DROP TABLE upload_sessions; DROP TABLE external_calls; DROP TABLE studio_tasks; DROP TABLE directory_heads; DROP TABLE directory_manifests; DROP TABLE scheduled_runs; DROP TABLE index_tasks; DELETE FROM schema_migrations WHERE version>=19")
        db.execute("INSERT INTO settings(key,value) VALUES('upgrade-marker','preserve')")
        assert migrate_pg(db) == 24
        assert db.execute("SELECT version,checksum FROM schema_migrations WHERE version<=18 ORDER BY version").fetchall() == before
    store = Store.open(pg_config)
    assert store.one("SELECT value FROM settings WHERE key='upgrade-marker'")["value"] == "preserve"
    ready(store)
    index = index_runner(pg_config).start({"action": "lexical", "index_id": "v"})
    pid = profiles.create_profile(store, ProfileBody(name="财经", domain="金融"))["id"]
    topic = TopicRuns(RuntimeConfig(pg_config, store), store).start_scheduled(pid, period="2026-11-05", ordinal=1,
        trigger="schedule", created_at="2026-11-05T09:00:00+00:00")
    path = tmp_path / "export.sqlite3"
    report = export_pg_to_sqlite(store, path)
    restored = Store(path)
    assert report["tables"]["index_tasks"]["rows"] == report["tables"]["scheduled_runs"]["rows"] == 1
    assert restored.one("SELECT id,status FROM index_tasks") == {"id": index["id"], "status": "queued"}
    assert restored.one("SELECT run_id FROM scheduled_runs")["run_id"] == topic["id"]
    assert restored.rows("PRAGMA foreign_key_check") == []
