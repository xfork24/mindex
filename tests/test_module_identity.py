"""短导入必须覆盖安装入口与动态装配，避免类、注册表和 monkeypatch 出现双身份。"""

import ast
import importlib
import tomllib
from pathlib import Path

import mindex
import pytest

from worker import handlers

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("kind", sorted(handlers._KIND_TABLES))
def test_worker_runner_uses_canonical_module(config, monkeypatch, kind):
    _, name, class_name = handlers._KIND_TABLES[kind]
    assert not name.startswith("mindex.")
    module = importlib.import_module(name)
    marker = object()
    monkeypatch.setattr(module, class_name, lambda runtime, store: marker)
    assert handlers._build_runner(kind, config) is marker


@pytest.mark.parametrize(
    "entry,module",
    [
        ("mindex", "cli"),
        ("mindex-person-server", "persons.person_server"),
        ("mindex-model-server", "models.model_server"),
    ],
)
def test_console_entries_bootstrap_canonical_modules(monkeypatch, entry, module):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package, function = project["project"]["scripts"][entry].split(":")
    assert package == "mindex"
    calls = []
    # 入口委托不添加参数，保持 Typer/argparse 读取 sys.argv 的行为。
    monkeypatch.setattr(
        importlib.import_module(module), "app" if module == "cli" else "main", lambda: calls.append(entry)
    )
    getattr(mindex, function)()
    assert calls == [entry]


def test_python_sources_do_not_import_qualified_submodules():
    problems = []
    for directory in ("backend/src", "tests", "scripts", "eval"):
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                if any(name.startswith("mindex.") for name in modules):
                    problems.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not problems, f"请在包自举后使用短导入：{problems}"
