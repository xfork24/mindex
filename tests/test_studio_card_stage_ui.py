"""分镜卡片的阶段模型（可打开的阶段、阶段状态、卡片显示的阶段）：用真实创作页脚本代码运行，不依赖浏览器。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the UI controller checks")
def test_card_stage_model():
    result = subprocess.run(
        ["node", "tests/studio_card_stage_unit.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
