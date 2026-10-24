"""智能助手面板浏览器回归：进程内启动带规则化模型的隔离服务，再用 Playwright 驱动真实页面。

需要 node 与 Playwright（可用 MINDEX_PLAYWRIGHT_MODULE 指定模块路径），缺失时跳过。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def playwright_available() -> bool:
    if shutil.which("node") is None:
        return False
    module = os.environ.get("MINDEX_PLAYWRIGHT_MODULE", "playwright")
    probe = subprocess.run(["node", "-e", f"require({module!r})"], capture_output=True, cwd=ROOT, check=False)
    return probe.returncode == 0


def serve(app):
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture
def media_server(tmp_path):
    from serve_agent_fixture import build_app

    app, job_id = build_app(tmp_path / "data", media=True)
    server, thread, url = serve(app)
    yield url, job_id
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def agent_server(tmp_path):
    from serve_agent_fixture import build_app

    app, job_id = build_app(tmp_path / "data")
    server, thread, url = serve(app)
    yield url, job_id
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.skipif(not playwright_available(), reason="需要 node 与 Playwright")
@pytest.mark.parametrize("scenario", ["basic", "approval", "cross", "query", "external", "nofollow", "navigate", "markdown",
                                      "resize"])
def test_agent_panel(agent_server, scenario):
    url, job_id = agent_server
    result = subprocess.run(
        ["node", "tests/agent_panel_ui.cjs"],
        cwd=ROOT,
        env={**os.environ, "MINDEX_AGENT_URL": url, "MINDEX_AGENT_JOB": job_id, "MINDEX_AGENT_SCENARIO": scenario},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not playwright_available(), reason="需要 node 与 Playwright")
def test_agent_markdown(agent_server):
    url, _ = agent_server
    result = subprocess.run(
        ["node", "tests/agent_markdown_ui.cjs"],
        cwd=ROOT,
        env={**os.environ, "MINDEX_AGENT_URL": url},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not playwright_available(), reason="需要 node 与 Playwright")
def test_agent_media(media_server):
    url, job_id = media_server
    result = subprocess.run(
        ["node", "tests/agent_panel_ui.cjs"],
        cwd=ROOT,
        env={**os.environ, "MINDEX_AGENT_URL": url, "MINDEX_AGENT_JOB": job_id, "MINDEX_AGENT_SCENARIO": "media"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
