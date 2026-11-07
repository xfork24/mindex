"""maintenance gate 与 agent-gateway 路由（plans §8/§9 阶段 5）。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from shared.config import Config
from shared.db.maintenance_gate import MaintenanceBusy, maintenance_gate
from shared.storage import Store


def test_gate_is_noop_on_local_form(tmp_path):
    store = Store(tmp_path / "data")
    with maintenance_gate(store, name="clean") as info:
        assert info["mode"] == "file-locks"  # 本地沿用文件锁语义（零行为变化）


def test_gate_mutual_exclusion_on_pg(tmp_path, monkeypatch):
    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    import psycopg

    from shared.db.migrate_pg import migrate_pg
    from shared.storage import PgStore

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


def test_cluster_api_proxies_agent_without_local_session_state(tmp_path, monkeypatch):
    """业务 API 转发给单实例网关，保留用户认证并剥离本机内部凭证。"""
    import httpx

    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", "mindex")
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", "mindex-test")
    from api import create_app

    received = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"via":"gateway","path":"/api/agent/status"}'

    def gateway(request):
        received.append(request)
        return httpx.Response(200, stream=Body(), headers={"content-type": "application/json"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(gateway)),
    )

    config = Config(
        data_dir=tmp_path / "data",
        database={"backend": "postgres"},
        vector={"backend": "chroma_server"},
        storage={"backend": "s3"},
        queue={"backend": "postgres"},
        locks={"backend": "postgres"},
        agent={"gateway_url": "https://agent.internal"},
    )
    app = create_app(config)
    assert getattr(app.state, "agent_runtime", None) is None  # 不建独立会话运行时
    with TestClient(app) as client:
        response = client.get(
            "/api/agent/status",
            headers={"Authorization": "Bearer user-token", "X-Mindex-Internal": "local-only"},
        )
        assert response.status_code == 200
        assert response.json() == {"via": "gateway", "path": "/api/agent/status"}
        assert received[0].headers["authorization"] == "Bearer user-token"
        assert "x-mindex-internal" not in received[0].headers
        assert received[0].headers["x-mindex-gateway-hop"] == "1"
        health = client.get("/api/health").json()
        assert health["backends"]["form"] == "cluster"
    assert not (config.data_dir / "agent.sqlite3").exists()
