"""文案创作区域的 UI 控制逻辑，用真实 studio.js 代码和 DOM 桩运行，不依赖浏览器。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the UI controller checks")
def test_script_section_controller():
    result = subprocess.run(
        ["node", "tests/studio_script_ui.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
