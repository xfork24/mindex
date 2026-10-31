"""桌面外壳：纯逻辑（node --test desktop/test）与真实 Electron 冒烟（tests/desktop_smoke.cjs）。

未安装 desktop/ 的 npm 依赖（cd desktop && npm ci）时跳过。``MINDEX_SMOKE_APP`` 指定已安装应用的可执行文件时，
冒烟测试针对该应用运行（例如 /Applications/Mindex.app/Contents/MacOS/Mindex）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
modules = DESKTOP / "node_modules"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
needs_electron = pytest.mark.skipif(
    not (modules / "electron").is_dir() or not (modules / "playwright-core").is_dir(),
    reason="需要 desktop/ 的 npm 依赖（cd desktop && npm ci）",
)


@needs_node
def test_desktop_unit():
    files = [str(path) for path in sorted((DESKTOP / "test").glob("*.test.js"))]
    result = subprocess.run(["node", "--test", *files], cwd=DESKTOP, capture_output=True, text=True,
                            timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@needs_node
@needs_electron
def test_desktop_smoke():
    result = subprocess.run(["node", "tests/desktop_smoke.cjs"], cwd=ROOT, capture_output=True, text=True,
                            timeout=600, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "desktop smoke ok" in result.stdout
