"""分镜工作台固定五列、召回列统一显示画面阶段：用真实创作页脚本代码和桩运行，不依赖浏览器。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the UI controller checks")
def test_picture_column_controller():
    result = subprocess.run(
        ["node", "tests/studio_picture_column_unit.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
