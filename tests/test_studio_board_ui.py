"""分镜工作台（顶部分镜条、阶段浮层、下方阶段详情、卡片产物）真实浏览器回归。

进程内启动隔离服务（三镜、真实素材与一次完整构建），再用 Playwright 驱动真实页面；
需要 node 与 Playwright（可用 MINDEX_PLAYWRIGHT_MODULE 指定模块路径），缺失时跳过。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from test_agent_panel_ui import playwright_available, serve

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def built_studio(tmp_path):
    from serve_agent_fixture import build_app

    app, job_id = build_app(tmp_path / "data", media=True)
    server, thread, url = serve(app)
    yield url, job_id
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.skipif(not playwright_available(), reason="需要 node 与 Playwright")
def test_storyboard_strip_in_browser(built_studio, tmp_path):
    url, job_id = built_studio
    result = subprocess.run(
        ["node", "tests/studio_board_ui.cjs"],
        cwd=ROOT,
        env={
            **os.environ,
            "MINDEX_STUDIO_URL": url,
            "MINDEX_STUDIO_JOB": job_id,
            "MINDEX_BOARD_OUTPUT": os.environ.get("MINDEX_BOARD_OUTPUT", str(tmp_path / "screens")),
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
