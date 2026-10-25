"""Run the actual UI controller against a small DOM adapter, without a browser dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the UI controller checks")
def test_fixed_window_controller():
    result = subprocess.run(
        ["node", "tests/studio_window_ui.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
