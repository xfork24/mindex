"""上下文退出、失败清理与共享资源的所有权边界。"""
from __future__ import annotations

import gc
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from mindex.shared.backends import open_context
from mindex.shared.backends.context import resource_scope
from mindex.shared.config import Config
from mindex.shared.db.pg_backend import PgBackend
from mindex.shared.storage import Store


def test_store_owns_context_and_close_is_idempotent(tmp_path):
    config = Config(data_dir=tmp_path)
    store = Store.open(config)
    gc.collect()
    assert store.one("SELECT 1 AS ok")["ok"] == 1
    store.close()
    store.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        store.one("SELECT 1")
    with Store.open(config) as reopened:
        assert reopened.library_id == store.library_id
    assert reopened._backend_context.closed


def test_staging_owners_close_independently(tmp_path):
    config = Config(data_dir=tmp_path, storage={"staging_quota_gb": 0.01})
    first, second = open_context(config), open_context(config)
    assert first.staging is not second.staging
    owner_lock = FileLock(first.staging._owner_lock.lock_file)
    first.close()
    with owner_lock.acquire(timeout=0):
        pass
    second.staging.reserve(100)
    second.staging.release(100)
    second.close()


def test_scope_cleans_up_on_failure(tmp_path):
    with pytest.raises(ValueError):
        with resource_scope():
            store = Store.open(Config(data_dir=tmp_path))
            raise ValueError("中断")
    assert store._backend_context.closed


def test_failed_context_open_releases_pg_backend(tmp_path, monkeypatch):
    from mindex.shared import storage

    fake = Mock(library_id="actual-library", root=tmp_path)
    monkeypatch.setattr(storage, "PgStore", Mock(return_value=fake))
    with pytest.raises(RuntimeError, match="library_id"):
        open_context(Config(data_dir=tmp_path, database={"backend": "postgres"},
                            server={"library_id": "00000000-0000-0000-0000-000000000001"}))
    fake.backend.close.assert_called_once()


def test_shared_pg_pools_survive_other_owner_close(monkeypatch):
    from mindex.shared.db import pg_backend

    dsn = "lifecycle-isolated-test"
    pool = Mock()
    first = PgBackend(dsn, library_id="a")
    second = PgBackend(dsn, library_id="b")
    monkeypatch.setitem(pg_backend._POOLS, (dsn, False), pool)
    first.close()
    first.close()
    pool.close.assert_not_called()
    assert second.pool() is pool
    second.close()
    pool.close.assert_called_once()
    assert (dsn, False) not in pg_backend._POOLS
    with pytest.raises(RuntimeError, match="已关闭"):
        second.pool()


def test_api_shutdown_closes_after_inner_lifespan(tmp_path):
    from mindex.api import create_app

    app = create_app(Config(data_dir=tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/live").status_code == 200
        assert not app.state.backends.closed
    assert app.state.backends.closed


def test_cli_scope_closes_opened_stores(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import mindex.settings.runtime_settings as settings
    from mindex.cli import app

    opened = []

    def overrides(config):
        opened.append(Store.open(config))
        return config

    monkeypatch.setattr(settings, "apply_runtime_overrides", overrides)
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"data_dir: {tmp_path / 'data'}\n")
    result = CliRunner().invoke(app, ["--config", str(config_file), "indexes", "list"])
    assert result.exit_code == 0, result.output
    assert opened and all(store._backend_context.closed for store in opened)


def test_worker_closes_context_even_on_failure():
    from mindex.worker_runtime import Worker

    worker = Worker.__new__(Worker)
    worker.context = Mock()
    worker._run = Mock(side_effect=RuntimeError("控制通道断开"))
    with pytest.raises(RuntimeError, match="控制通道"):
        worker.run()
    worker.context.close.assert_called_once()


def test_borrowed_media_keeps_context_alive(tmp_path):
    import weakref

    context = open_context(Config(data_dir=tmp_path, storage={"staging_quota_gb": 0.01}))
    reference = weakref.ref(context)
    media = context.media
    owner = FileLock(context.staging._owner_lock.lock_file)
    del context
    gc.collect()
    assert reference() is not None
    assert not reference().closed
    del media
    gc.collect()
    assert reference() is None
    with owner.acquire(timeout=0):
        pass


def test_gc_during_other_staging_accounting_never_reenters_ledger(tmp_path):
    import weakref

    config = Config(data_dir=tmp_path, storage={"staging_quota_gb": 0.01})
    first, second = open_context(config), open_context(config)
    reference = weakref.ref(first)
    with second.staging._accounting():
        del first
        gc.collect()
        assert reference() is None
    second.staging.reserve(100)
    second.staging.release(100)
    assert second.staging.usage()["reserved"] == 0
    second.close()


def test_api_construction_failure_closes_opened_context(tmp_path, monkeypatch):
    import mindex.api as api

    contexts = []

    def opened(config):
        context = open_context(config)
        contexts.append(context)
        return context

    monkeypatch.setattr(api, "open_context", opened)
    monkeypatch.setattr(api, "RuntimeConfig", Mock(side_effect=ValueError("构造中断")))
    with pytest.raises(ValueError, match="构造中断"):
        api.create_app(Config(data_dir=tmp_path))
    assert contexts[0].closed
