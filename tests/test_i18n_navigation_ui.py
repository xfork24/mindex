"""所有页面的首屏语言、导航与动态提示浏览器回归。"""
import os
import subprocess
from pathlib import Path

import pytest
from test_agent_panel_ui import playwright_available, serve

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not playwright_available(), reason='需要 node 与 Playwright')
def test_navigation_and_dynamic_messages_do_not_flash_chinese(tmp_path):
    from serve_agent_fixture import build_app

    app, _ = build_app(tmp_path / 'data')
    server, thread, url = serve(app)
    try:
        result = subprocess.run(
            ['node', 'tests/i18n_navigation_ui.cjs'], cwd=ROOT,
            env={**os.environ, 'MINDEX_I18N_TEST_URL': url},
            capture_output=True, text=True, timeout=120, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
