"""桌面外壳使用的 ``GET /api/health``：轻量、受令牌保护、报告运行中的任务。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi.testclient import TestClient

from mindex.api import app_version, create_app
from mindex.shared.background_tasks import _RUNNERS
from mindex.studio.executor import _EXECUTORS

TOKEN = "desktop-health-token"


def test_health_shape_and_auth(config):
    client = TestClient(create_app(config, web_token=TOKEN))
    assert client.get("/api/health").status_code == 401
    response = client.get("/api/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["pid"] == os.getpid()
    assert body["version"] == app_version() and body["version"] != "0+unknown"
    assert Path(body["data_dir"]) == config.data_dir
    assert body["busy"] is False and body["tasks"] == []


def test_health_reports_studio_and_background_tasks(config):
    client = TestClient(create_app(config))
    target = config.data_dir.resolve()
    executor = next(e for e in list(_EXECUTORS) if Path(e.base.data_dir).resolve() == target)
    runner = next(r for r in list(_RUNNERS) if r.store.root == target and r.thread_prefix == "source")
    release = threading.Event()
    studio_thread = executor.start("health-test", release.wait)
    worker = threading.Thread(target=release.wait, daemon=True)
    runner.threads["fake"] = worker
    worker.start()
    try:
        body = client.get("/api/health").json()
        assert body["busy"] is True and body["tasks"] == ["studio", "source"]
    finally:
        release.set()
        worker.join(5)
        runner.threads.pop("fake", None)
        studio_thread.join(5)
    assert client.get("/api/health").json()["busy"] is False


def test_health_ignores_other_data_dirs(config, tmp_path):
    other = config.model_copy(update={"data_dir": tmp_path / "other-data"})
    create_app(other)
    target = other.data_dir.resolve()
    executor = next(e for e in list(_EXECUTORS) if Path(e.base.data_dir).resolve() == target)
    release = threading.Event()
    thread = executor.start("other", release.wait)
    try:
        assert TestClient(create_app(config)).get("/api/health").json()["busy"] is False
    finally:
        release.set()
        thread.join(5)
