"""Theme cycle, persistence and i18n labels across pages."""
import os
import subprocess

import pytest
from test_agent_panel_ui import ROOT, playwright_available, serve

from mindex.api import create_app


@pytest.mark.slow
@pytest.mark.skipif(not playwright_available(), reason="Requires Node and Playwright")
def test_theme_toggle_cycles_and_persists(config):
    server, thread, url = serve(create_app(config))
    try:
        result = subprocess.run(['node', 'tests/theme_ui.cjs'], cwd=ROOT,
                                env={**os.environ, 'MINDEX_THEME_URL': url}, capture_output=True,
                                text=True, timeout=60, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
