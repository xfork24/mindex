"""在常规回归中执行检索筛选与创作素材目录的 Node 控制器测试。"""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 运行前端控制器测试")
@pytest.mark.parametrize("script", [
    "studio_catalogue_loading_unit.cjs",
    "search_cascading_filters_unit.cjs",
])
def test_filter_controller(script):
    result = subprocess.run(
        ["node", f"tests/{script}"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
