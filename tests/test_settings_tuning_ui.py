"""Real settings form layout and persistence for duration tuning."""

import os
import subprocess

import pytest

from api import create_app
from test_agent_panel_ui import ROOT, playwright_available, serve


@pytest.mark.slow
@pytest.mark.skipif(not playwright_available(), reason="Requires Node and Playwright")
def test_tuning_controls_wrap_without_artificial_rows(config):
    server, thread, url = serve(create_app(config))
    try:
        result = subprocess.run(
            ["node", "tests/settings_tuning_ui.cjs"],
            cwd=ROOT,
            env={**os.environ, "MINDEX_TUNING_URL": url},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
