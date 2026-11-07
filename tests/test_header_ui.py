"""顶栏导航与界面语言的浏览器回归：进程内启动隔离服务（真实素材、索引与 fixture 模型），
再用 Playwright 驱动真实页面。

两个脚本都直接驱动真实页面：header_navigation_ui.cjs 检查顶栏五个页面 × 两种语言 × 桌面/窄屏的
导航顺序、选中态与可见性（完整像素布局矩阵手动执行）；i18n_ui.cjs 检查语言切换不丢输入、不触发额外请求、
窄屏不溢出。

需要 node 与 Playwright（可用 MINDEX_PLAYWRIGHT_MODULE 指定模块路径），缺失时跳过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from test_agent_panel_ui import ROOT, playwright_available, serve

needs_browser = pytest.mark.skipif(not playwright_available(), reason="需要 node 与 Playwright")


@pytest.fixture
def ui_tmpdir():
    # seed 素材的 fixture 拒绝 /tmp 之外的路径（防误种真实素材库）；
    # macOS 上 pytest 的 tmp_path 在 /private/var 下，须显式落在 /tmp。
    root = Path(tempfile.mkdtemp(prefix="mindex-header-ui-", dir="/tmp"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def header_server(ui_tmpdir):
    from serve_agent_fixture import build_app

    app, _job_id = build_app(ui_tmpdir / "data", media=True)
    server, thread, url = serve(app)
    yield url, ui_tmpdir
    server.should_exit = True
    thread.join(timeout=10)


@needs_browser
@pytest.mark.slow
def test_header_navigation_layout(header_server):
    url, tmpdir = header_server
    result = subprocess.run(
        ["node", "tests/header_navigation_ui.cjs"],
        cwd=ROOT,
        env={**os.environ, "MINDEX_HEADER_URL": url, "MINDEX_HEADER_OUTPUT": str(tmpdir / "header")},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@needs_browser
@pytest.mark.slow
def test_i18n_language_and_layout(header_server):
    url, _tmpdir = header_server
    result = subprocess.run(
        ["node", "tests/i18n_ui.cjs"],
        cwd=ROOT,
        env={**os.environ, "MINDEX_I18N_TEST_URL": url},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
