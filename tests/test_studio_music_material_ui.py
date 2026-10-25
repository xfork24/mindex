"""分镜渲染阶段列出背景音乐素材、成片面板列出混入的背景音乐段落：用真实创作页脚本和桩运行，不依赖浏览器。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the UI controller checks")
def test_music_material_controller():
    result = subprocess.run(
        ["node", "tests/studio_music_material_unit.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
