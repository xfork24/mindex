"""设置页「索引版本」的 UI 控制逻辑，用真实 settings.js 和 DOM 桩运行，不依赖浏览器。"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the UI controller checks")
def test_settings_release_section():
    result = subprocess.run(
        ["node", "tests/settings_releases_ui.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
