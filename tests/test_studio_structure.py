"""创作工作台包结构：按职责拆分的模块各自不超过 700 行，旧单文件模块不保留转发层。"""

from __future__ import annotations

from pathlib import Path

STUDIO = Path(__file__).resolve().parents[1] / "backend/src/studio"
LIMIT = 700


def test_workspace_modules_stay_small():
    files = sorted((STUDIO / "workspace").glob("*.py"))
    assert {path.stem for path in files} >= {
        "__init__", "persistence", "versions", "plans", "signatures", "preflight", "builder", "details",
        "stages",
    }
    handlers = sorted((STUDIO / "workspace/handlers").glob("*.py"))
    assert {path.stem for path in handlers} >= {
        "__init__", "script", "split", "recall", "tts", "select", "render", "compose",
    }
    files += handlers
    sizes = {path.name: len(path.read_text(encoding="utf-8").splitlines()) for path in files}
    assert all(size <= LIMIT for size in sizes.values()), {k: v for k, v in sizes.items() if v > LIMIT}


def test_legacy_single_module_removed():
    assert not (STUDIO / "studio_workspace.py").exists()
