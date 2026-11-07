"""持续检查业务 Store 构造，防止共享后端配置静默回落 SQLite。"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def test_database_construction_boundaries():
    path = Path(__file__).resolve().parents[1] / "scripts/check-scaling-boundaries.py"
    spec = importlib.util.spec_from_file_location("scaling_boundaries", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.violations() == []
