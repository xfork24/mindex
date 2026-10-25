"""包依赖方向：按包递归扫描 ``backend/src`` 的全部模块（含函数内的延迟导入）。

- 顶层模块（``api.py``、``cli*.py``）合称 ``app``，是最上层的装配与入口层。
- shared 最底层；models 只能用 shared；processing 只能用 models、shared（``ALLOWED``，严格）。
- 其余上层包之间现存的互相引用冻结在 ``ALLOWED_UPPER``：这些是既有债务，不得新增边；
  去掉某条依赖后同步从白名单删除（白名单与实际不一致同样失败）。
- maintenance 不得依赖 CLI 层（``app``）。
- agent 只依赖 shared 与 ``AGENT_EXTERNAL_MODULES`` 中逐模块列出的少量接口。
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "backend/src"
APP = "app"

ALLOWED = {
    "shared": set(),
    "models": {"shared"},
    "processing": {"models", "shared"},
}

# 既有债务（2026-10 冻结）：上层包之间的现存依赖。不得新增；消除后从这里删除。
ALLOWED_UPPER = {
    "admin": {"diagnostics", "maintenance", "models", "persons", "processing", "search", "shared"},
    "agent": {"processing", "settings", "shared", "studio", "topics"},
    APP: {"admin", "agent", "diagnostics", "maintenance", "models", "persons", "processing", "search",
          "settings", "shared", "studio", "topics"},
    "diagnostics": {"models", "processing", "search", "shared"},
    "maintenance": {"agent", "models", "persons", "processing", "shared"},
    "persons": {"admin", "maintenance", "models", "settings", "shared"},
    "search": {"admin", "models", "persons", "processing", "settings", "shared"},
    "settings": {"agent", "models", "persons", "shared"},
    "studio": {"admin", "models", "persons", "search", "settings", "shared"},
    # 选题挖掘（M2）：只向下依赖；studio 不反向依赖 topics（写作约束快照在 shared/writing_profile.py）。
    "topics": {"admin", "models", "search", "shared", "studio"},
}

# 任何情况下都不允许的边（白名单也不能放开）。
FORBIDDEN = {
    "maintenance": {APP},
}

# agent 在 shared 之外允许导入的具体模块（既有接口，逐模块冻结）。
AGENT_EXTERNAL_MODULES = {
    "mindex.processing.subtitle_uploads",
    "mindex.settings.runtime_settings",
    "mindex.studio.studio",
    "mindex.studio.studio_models",
    "mindex.studio.workspace",
    # 选题工具的参数模型（ID 格式、权重与定时），与 studio_models 同类。
    "mindex.topics.common",
    "mindex.topics.models",
}


def _source_unit(src: Path, path: Path) -> str:
    parts = path.relative_to(src).parts
    return parts[0] if len(parts) > 1 else APP


def _module_exists(src: Path, dotted: str) -> bool:
    rel = Path(*dotted.split(".")[1:])
    return (src / rel).with_suffix(".py").is_file() or (src / rel / "__init__.py").is_file()


def imported_modules(src: Path, path: Path) -> set[str]:
    """``path`` 导入的 mindex 模块全名；``from 包 import 子模块`` 解析到子模块。"""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("mindex."))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module != "mindex" and not node.module.startswith("mindex."):
                continue
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                found.add(candidate if _module_exists(src, candidate) else node.module)
    return {name for name in found if name != "mindex"}


def _target_unit(src: Path, module: str) -> str:
    head = module.split(".")[1]
    return head if (src / head).is_dir() else APP


def package_edges(src: Path) -> dict[str, set[str]]:
    """包级依赖边（不含指向自身的边）。"""
    edges: dict[str, set[str]] = defaultdict(set)
    for path in sorted(src.rglob("*.py")):
        unit = _source_unit(src, path)
        edges.setdefault(unit, set())
        for module in imported_modules(src, path):
            target = _target_unit(src, module)
            if target != unit:
                edges[unit].add(target)
    return dict(edges)


def violations(src: Path, allowed: dict[str, set[str]], forbidden: dict[str, set[str]],
               agent_modules: set[str]) -> list[str]:
    """返回全部违规描述；空列表表示通过。"""
    problems = []
    for path in sorted(src.rglob("*.py")):
        unit = _source_unit(src, path)
        rel = path.relative_to(src)
        if unit not in allowed:
            problems.append(f"{rel}：包 {unit} 没有依赖规则，请在测试中登记")
            continue
        for module in sorted(imported_modules(src, path)):
            target = _target_unit(src, module)
            if target == unit:
                continue
            if target in forbidden.get(unit, set()):
                problems.append(f"{rel} -> {module}：{unit} 禁止依赖 {target}")
            elif target not in allowed[unit]:
                problems.append(f"{rel} -> {module}：新增了 {unit} -> {target} 的依赖")
            elif unit == "agent" and target != "shared" and module not in agent_modules:
                problems.append(f"{rel} -> {module}：agent 只能导入 shared 与白名单模块")
    return problems


def _rules() -> dict[str, set[str]]:
    return {**ALLOWED, **ALLOWED_UPPER}


def test_package_dependencies_follow_rules():
    problems = violations(SRC, _rules(), FORBIDDEN, AGENT_EXTERNAL_MODULES)
    assert not problems, "\n".join(problems)


def test_upper_whitelist_matches_current_edges():
    """白名单只冻结现存的边：依赖消除后须同步删除，避免白名单悄悄变宽。"""
    edges = package_edges(SRC)
    stale = {pkg: sorted(allowed - edges.get(pkg, set())) for pkg, allowed in ALLOWED_UPPER.items()}
    stale = {pkg: extra for pkg, extra in stale.items() if extra}
    assert not stale, f"白名单中已不存在的依赖，请删除：{stale}"
    used = set()
    for path in (SRC / "agent").rglob("*.py"):
        used |= {m for m in imported_modules(SRC, path) if _target_unit(SRC, m) not in {"agent", "shared"}}
    assert AGENT_EXTERNAL_MODULES == used, f"agent 白名单与实际不一致：{sorted(AGENT_EXTERNAL_MODULES ^ used)}"


def test_rules_cover_exactly_existing_packages():
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and any(p.rglob("*.py"))} | {APP}
    assert set(ALLOWED).isdisjoint(ALLOWED_UPPER)
    assert set(_rules()) == packages
    assert all(not (FORBIDDEN[pkg] & _rules()[pkg]) for pkg in FORBIDDEN)


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_checker_reports_new_violations(tmp_path):
    src = tmp_path / "src"
    for package in ("shared", "models", "maintenance", "agent", "studio"):
        _write(src, f"{package}/__init__.py")
    _write(src, "cli.py")
    _write(src, "studio/studio.py")
    _write(src, "studio/other.py")
    _write(src, "shared/util.py")
    rules = {"shared": set(), "models": {"shared"}, "maintenance": {"shared"},
             "agent": {"shared", "studio"}, "studio": {"shared"}, APP: {"shared", "models"}}
    forbidden = {"maintenance": {APP}}
    _write(src, "models/ok.py", "from mindex.shared.util import x\n")
    assert violations(src, rules, forbidden, {"mindex.studio.studio"}) == []

    # 嵌套子包里的函数内导入也要被发现。
    _write(src, "models/deep/inner.py", "def f():\n    from mindex.studio import studio\n")
    _write(src, "maintenance/tool.py", "import mindex.cli\n")
    _write(src, "agent/tools/x.py", "from mindex.studio import studio, other\n")
    _write(src, "newpkg/mod.py", "")
    problems = violations(src, rules, forbidden, {"mindex.studio.studio"})
    assert any("models/deep/inner.py -> mindex.studio.studio" in p and "新增" in p for p in problems)
    assert any("maintenance/tool.py -> mindex.cli" in p and "禁止" in p for p in problems)
    assert any("agent/tools/x.py -> mindex.studio.other" in p for p in problems)
    assert not any("agent/tools/x.py -> mindex.studio.studio" in p for p in problems)
    assert any("newpkg/mod.py" in p and "没有依赖规则" in p for p in problems)
    assert len(problems) == 4
    assert package_edges(src)["models"] == {"shared", "studio"}
