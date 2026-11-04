"""maintenance gate 与 agent-gateway 路由（plans §8/§9 阶段 5）。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from mindex.shared.config import Config
from mindex.shared.db.maintenance_gate import MaintenanceBusy, maintenance_gate
from mindex.shared.storage import Store


def test_gate_is_noop_on_local_form(tmp_path):
    store = Store(tmp_path / "data")
    with maintenance_gate(store, name="clean") as info:
        assert info["mode"] == "file-locks"  # 本地沿用文件锁语义（零行为变化）


def test_gate_mutual_exclusion_on_pg(tmp_path, monkeypatch):
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg
    from mindex.shared.storage import PgStore

    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(dsn) as conn:
        migrate_pg(conn)
    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    store = PgStore(Config(data_dir=tmp_path / "data", database={"backend": "postgres"}))
    with maintenance_gate(store, name="clean") as info:
        assert info["mode"] == "pg-advisory"
        # 并发进入的第二维护请求快速失败（不排队）
        with pytest.raises(MaintenanceBusy, match="稍后再试"):
            with maintenance_gate(store, name="purge"):
                pass
    with maintenance_gate(store, name="after"):  # 释放后可再次进入
        pass


def test_cluster_api_stubs_agent_routes(tmp_path, monkeypatch):
    """cluster 业务 API 的 /api/agent 指向单实例网关，明确 503 而非假数据降级。"""
    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", "mindex")
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", "mindex-test")
    from mindex.api import create_app

    config = Config(
        data_dir=tmp_path / "data",
        database={"backend": "postgres"}, vector={"backend": "chroma_server"},
        storage={"backend": "s3"}, queue={"backend": "postgres"}, locks={"backend": "postgres"},
        agent={"gateway_url": "https://agent.internal"},
    )
    app = create_app(config)
    assert getattr(app.state, "agent_runtime", None) is None  # 不建独立会话运行时
    with TestClient(app) as client:
        response = client.get("/api/agent/status")
        assert response.status_code == 503
        assert "agent-gateway" in response.json()["detail"]
        assert response.json()["gateway_url"] == "https://agent.internal"
        health = client.get("/api/health").json()
        assert health["backends"]["form"] == "cluster"
