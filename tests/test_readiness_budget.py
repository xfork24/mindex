"""真实通道耗尽与慢依赖必须在总期限内失败，不影响 liveness。"""

from __future__ import annotations

import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from api import create_app
from test_scaling_correctness import pg_config as pg_config


@pytest.mark.requires_postgres
@pytest.mark.parametrize("control", [False, True])
def test_readiness_distinguishes_exhausted_query_and_control_pool(pg_config, control):
    pg_config.server.readiness_timeout_seconds = 0.6
    app = create_app(pg_config)
    with TestClient(app) as client, ExitStack() as held:
        pool = app.state.store.backend.pool(control=control)
        count = (
            pg_config.database.postgres.control_pool_max if control else pg_config.database.postgres.pool_max
        )
        for _ in range(count):
            held.enter_context(pool.connection())
        start = time.monotonic()
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert time.monotonic() - start < 1.5
        checks = {item["check"]: item["ok"] for item in response.json()["checks"]}
        assert checks["database.query"] is control
        assert checks["database.control"] is not control
        assert client.get("/api/live").json() == {"alive": True}
    with TestClient(create_app(pg_config)) as client:
        assert client.get("/api/ready").status_code == 200


def test_sse_last_event_id_reconnect_preserves_persistent_identity(config):
    from agent.llm.scripted import ScriptedLlm

    with TestClient(create_app(config, agent_llm=ScriptedLlm())) as client:
        feed = client.app.state.agent_runtime.feed
        first = feed.publish("mutation", {"domain": "studio"})
        second = feed.publish("focus", {"page": "settings"})
        result = client.get("/api/agent/changes?follow=false", headers={"Last-Event-ID": str(first["seq"])})
        assert f"id: {second['seq']}\n" in result.text
        assert f"id: {first['seq']}\n" not in result.text
        assert (
            client.get("/api/agent/changes?follow=false", headers={"Last-Event-ID": "invalid"}).status_code
            == 422
        )
        assert (
            client.get(
                "/api/agent/changes?follow=false&after=0", headers={"Last-Event-ID": "invalid"}
            ).status_code
            == 200
        )
