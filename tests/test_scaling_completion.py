"""闭合计划：运行准入、物理资源占用与冷节点身份边界。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shared.backends import open_context
from shared.config import Config
from shared.external_calls import ReviewRequired
from shared.gpu_resources import GpuResources, required_groups


def _task(number):
    return SimpleNamespace(id=number, attempt_id=f"attempt-{number}", kind="source")


def test_cluster_requires_explicit_resource_declaration(tmp_path):
    config = Config(data_dir=tmp_path)
    with pytest.raises(ValueError, match="物理资源组"):
        required_groups(config, "source", cluster=True)
    config.gpu.cpu_queues = ["source"]
    assert required_groups(config, "source", cluster=True) == []


def test_physical_groups_share_capacity_and_unknown_is_not_reclaimed(tmp_path):
    config = Config(
        data_dir=tmp_path,
        gpu={
            "groups": {
                "physical-0": {
                    "capacity": 1,
                    "queues": ["source", "dialogue"],
                    "services": ["embedding", "asr"],
                }
            }
        },
    )
    with open_context(config) as context:
        resources = GpuResources(context.store)
        assert resources.acquire(config, _task(1))
        assert not resources.acquire(config, _task(2))
        resources.finish(_task(1), certain=False)
        with pytest.raises(ReviewRequired, match="未知远端执行"):
            resources.acquire(config, _task(2))
        row = resources.pending()[0]
        with pytest.raises(ValueError, match="核对依据"):
            resources.resolve(row["id"], evidence="")
        resources.resolve(row["id"], evidence="隔离模型服务确认 request 已终止")
        assert resources.acquire(config, _task(2))
        resources.finish(_task(2), certain=True)
        assert resources.pending() == []


def test_resource_capacity_mismatch_cannot_override_existing_declaration(tmp_path):
    config = Config(data_dir=tmp_path, gpu={"groups": {"physical": {"capacity": 1, "queues": ["source"]}}})
    with open_context(config) as context:
        resources = GpuResources(context.store)
        assert resources.acquire(config, _task(1))
        config.gpu.groups["physical"].capacity = 2
        with pytest.raises(ValueError, match="容量"):
            resources.acquire(config, _task(2))
        assert len(resources.pending()) == 1


def test_protocol_and_derived_index_admission(tmp_path):
    from models.adapters import ModelError
    from shared.admission import validate_index, validate_protocol

    with open_context(Config(data_dir=tmp_path)) as context:
        validate_protocol(context.store)
        context.store.execute("UPDATE settings SET value='future' WHERE key='runtime.protocol'")
        with pytest.raises(RuntimeError, match="运行协议"):
            validate_protocol(context.store)
        with pytest.raises(ModelError, match="全快照"):
            validate_index(context.store, {"id": "old", "spec": {"format": 1}})
        with pytest.raises(ModelError, match="派生数据"):
            validate_index(context.store, {"id": "new", "spec": {"format": 2}})


def test_cache_identity_changes_when_same_directory_is_rebound(tmp_path):
    config = Config(data_dir=tmp_path)
    with open_context(config) as first:
        original = first.store.cache_identity
        first.store.execute("UPDATE settings SET value='replacement-library' WHERE key='library.id'")
    with open_context(config) as second:
        assert second.store.cache_identity != original
        identity = second.store.cache_identity
        second.store.backend_identity = "different-remote-backends"
        assert second.store.cache_identity != identity


@pytest.fixture
def pg_config(tmp_path, monkeypatch):
    import os

    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("需要隔离 MINDEX_TEST_POSTGRES_DSN")
    import psycopg

    from shared.db.migrate_pg import migrate_pg

    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    with psycopg.connect(dsn) as db:
        migrate_pg(db)
    return Config(
        data_dir=tmp_path / "node-a",
        runtime_overrides=False,
        database={"backend": "postgres"},
        queue={"backend": "postgres"},
        locks={"backend": "postgres"},
    )


@pytest.mark.requires_postgres
def test_pg_resource_occupancy_survives_task_ttl_and_reclaim(pg_config):
    import time

    from shared.db.queue_pg import PgTaskQueue

    pg_config.gpu = type(pg_config.gpu).model_validate(
        {"groups": {"physical": {"capacity": 1, "queues": ["source", "dialogue"]}}}
    )
    with open_context(pg_config) as context:
        queue = PgTaskQueue(context.library_id)
        with context.store.connect() as db:
            queue.enqueue(db, "source", {})
            task = queue.claim(db, "old", ["source"], ttl=0.03)
        resources = GpuResources(context.store)
        assert resources.acquire(pg_config, task)
        time.sleep(0.05)
        with context.store.connect() as db:
            assert queue.reclaim_expired(db) == 1
        assert resources.pending()[0]["state"] == "unknown"
        with pytest.raises(ReviewRequired):
            resources.acquire(pg_config, _task(999))
        pending = resources.pending()[0]
        resources.resolve(pending["id"], evidence="隔离远端服务已停止该请求，保留服务日志")
        assert resources.acquire(pg_config, _task(999))


@pytest.mark.requires_postgres
def test_cli_prepare_uses_same_persistent_source_ledger(pg_config, video_file, tmp_path):
    import threading

    import yaml
    from typer.testing import CliRunner
    from worker_runtime import Worker

    from cli import app
    from shared.storage import Store

    config_file = tmp_path / "node.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(pg_config.data_dir),
                "runtime_overrides": False,
                "database": {"backend": "postgres"},
                "queue": {"backend": "postgres"},
                "locks": {"backend": "postgres"},
            }
        )
    )
    worker = Worker(pg_config, queues=["source"], idle_exit=15)
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        result = CliRunner().invoke(app, ["--config", str(config_file), "prepare", str(video_file)])
        assert result.exit_code == 0, result.output + repr(result.exception)
        with Store.open(pg_config) as store:
            rows = store.rows("SELECT status,work FROM source_tasks")
            assert rows == [{"status": "complete", "work": "shots"}]
            assert store.one("SELECT prepared FROM sources")["prepared"] == 1
            assert store.one("SELECT status FROM task_queue")["status"] == "succeeded"
    finally:
        worker.stop()
        thread.join(20)
    assert not thread.is_alive()
