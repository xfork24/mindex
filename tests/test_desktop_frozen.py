"""冻结运行兼容：入口的 ``-m`` 分发，以及“以自身源码哈希作指纹”的模块都登记为源码打包。"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "desktop" / "sidecar" / "mindex.spec"


def run_entry(*args):
    return subprocess.run(
        [sys.executable, "-m", "mindex.server.desktop_entry", *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
        check=False,
    )


def test_entry_dispatches_module_and_cli(tmp_path):
    module = run_entry("-m", "json.tool", "--help")
    assert module.returncode == 0 and "json.tool" in module.stdout
    cli = run_entry("--help")
    assert cli.returncode == 0 and "serve" in cli.stdout
    bad = run_entry("-m")
    assert bad.returncode != 0 and "用法" in bad.stderr


def test_entry_passes_arguments_to_module(tmp_path):
    target = tmp_path / "echo_args.py"
    target.write_text("import sys, json\nprint(json.dumps(sys.argv[1:]))\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, mindex; sys.path.insert(0, sys.argv[1]); from server.desktop_entry import main; "
            "main(['-m', 'echo_args', 'a b', '--flag'])",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '["a b", "--flag"]'


def self_hashing_modules() -> set[str]:
    """源码里出现 ``content_hash(Path(__file__))`` 的模块：冻结后必须以 .py 源码打包，
    哈希才与源码运行相同。
    """
    found = set()
    package = ROOT / "backend" / "src"
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "content_hash"
                and node.args
                and ast.unparse(node.args[0]) == "Path(__file__)"
            ):
                found.add("mindex." + ".".join(path.relative_to(package).with_suffix("").parts))
    return found


def test_self_hashing_modules_are_collected_as_source():
    modules = self_hashing_modules()
    assert {"mindex.models.llama_cpp", "mindex.models.model_server"} <= modules
    text = SPEC.read_text(encoding="utf-8")
    declared = set(re.findall(r'"([\w.]+)":\s*"py"', text))
    required = modules | {name.removeprefix("mindex.") for name in modules}
    assert required <= declared, f"mindex.spec 的 SOURCE_MODULES 缺少：{sorted(required - declared)}"


def test_launcher_uses_bootstrap_and_packaged_entry():
    result = subprocess.run(
        [sys.executable, str(ROOT / "desktop/sidecar/launcher.py"), "--help"],
        cwd=ROOT.parent,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "serve" in result.stdout


def test_desktop_check_module_entry():
    result = run_entry("-m", "mindex.diagnostics.desktop_check")
    assert result.returncode != 0 and "用法" in result.stderr
    assert "No module named" not in result.stderr
