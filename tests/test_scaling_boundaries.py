"""持续检查业务 Store 构造，防止共享后端配置静默回落 SQLite。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ["shared.storage", "mindex.shared.storage"])
def test_checker_rejects_direct_store_construction(tmp_path, module):
    path = Path(__file__).resolve().parents[1] / "scripts/check-scaling-boundaries.py"
    spec = importlib.util.spec_from_file_location("scaling_boundaries", path)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    (tmp_path / "business.py").write_text(
        f"from {module} import Store as LocalStore\nLocalStore('data')\n", encoding="utf-8"
    )
    assert any("业务入口直构 LocalStore" in problem for problem in checker.violations(tmp_path))


def test_database_construction_boundaries():
    path = Path(__file__).resolve().parents[1] / "scripts/check-scaling-boundaries.py"
    spec = importlib.util.spec_from_file_location("scaling_boundaries", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.violations() == []
