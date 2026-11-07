"""独立进程冷启动关键入口，避免顺序导入掩盖循环与脚本自举缺失。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_isolated(code, argument, tmp_path):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", code, argument],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "module",
    [
        "worker.runtime",
        "worker.handlers",
        "studio.tasks",
        "studio.executor",
        "studio.workspace",
        "api",
        "cli",
    ],
)
def test_entrypoint_imports_in_a_fresh_process(module, tmp_path):
    run_isolated(
        "import importlib, sys; import mindex; importlib.import_module(sys.argv[1]); "
        "assert not any(name.startswith('mindex.') for name in sys.modules)",
        module,
        tmp_path,
    )


@pytest.mark.parametrize(
    "script",
    ["scaling-capacity.py", "verify-scaling.py", "verify-scaling-recovery.py", "profile_person_review.py"],
)
def test_standalone_scripts_bootstrap_short_imports(script, tmp_path):
    run_isolated(
        "import runpy, sys; runpy.run_path(sys.argv[1]); import shared.config; "
        "assert not any(name.startswith('mindex.') for name in sys.modules)",
        str(ROOT / "scripts" / script),
        tmp_path,
    )
