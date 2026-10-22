"""包依赖方向：底层包不得导入上层功能包（含函数内的延迟导入）。

shared 最底层；models 只能用 shared；processing 只能用 models、shared。
上层功能包之间目前仍有互相引用，不在此限制。
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "backend/src"

ALLOWED = {
    "shared": {"shared"},
    "models": {"models", "shared"},
    "processing": {"processing", "models", "shared"},
}


def imported_packages(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names = []
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        for name in names:
            parts = name.split(".")
            if parts[0] == "mindex" and len(parts) > 1:
                found.add(parts[1])
    return found


def test_lower_layers_do_not_import_upper_packages():
    violations = []
    for package, allowed in ALLOWED.items():
        for path in sorted((SRC / package).glob("*.py")):
            extra = imported_packages(path) - allowed
            if extra:
                violations.append(f"{path.relative_to(SRC)} -> {sorted(extra)}")
    assert not violations, "\n".join(violations)


def test_boundary_rules_cover_existing_packages():
    assert set(ALLOWED) <= {path.name for path in SRC.iterdir() if path.is_dir()}
