"""包依赖方向：按包递归扫描 ``backend/src`` 的全部模块（含函数内的延迟导入）。

- 装配/入口层:``api``（REST 装配 + 上传）、``cli/`` 包、``server``（gateway/desktop_entry）、
  ``worker``（后台执行）。互相引用属既有形态,侧向不做模块级冻结。
- shared 最底层；models 只能用 shared；processing 只能用 models、shared（``ALLOWED``，严格）。
- 其余上层包之间现存的互相引用冻结在 ``ALLOWED_UPPER``：这些是既有债务，不得新增边；
  去掉某条依赖后同步从白名单删除（白名单与实际不一致同样失败）。
- 上层包之间的侧向依赖再冻结到模块级（``UPPER_MODULES``，2026-11）：已登记包对内新增模块导入
  须显式登记，消除后同步删除。指向 shared/models/processing 的下层依赖随开发自然增长，不冻结；
  app 与 cli 是装配层、agent 另由 ``AGENT_EXTERNAL_MODULES`` 逐模块约束，均不入册。
- maintenance 不得依赖 CLI 层（``app`` 与 ``cli``）。
- agent 只依赖 shared 与 ``AGENT_EXTERNAL_MODULES`` 中逐模块列出的少量接口。
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "backend/src"
# 包内组件统一短名导入(shared.media 等,见 plans/code-style-normalization-plan.md 原始需求 2)。
FIRST_PARTY = {
    "admin",
    "agent",
    "api",
    "cli",
    "diagnostics",
    "maintenance",
    "models",
    "persons",
    "processing",
    "search",
    "server",
    "settings",
    "shared",
    "studio",
    "topics",
    "worker",
}
APP = "api"  # 顶层装配单元归包后即 api(REST 装配与上传)

ALLOWED = {
    "shared": set(),
    "models": {"shared"},
    "processing": {"models", "shared"},
}

# 既有债务（2026-10 冻结）：上层包之间的现存依赖。不得新增；消除后从这里删除。
ALLOWED_UPPER = {
    "admin": {"diagnostics", "maintenance", "models", "persons", "processing", "search", "shared"},
    "agent": {"processing", "settings", "shared", "studio", "topics"},
    "cli": {
        "admin",
        "agent",
        APP,
        "diagnostics",
        "worker",
        "maintenance",
        "models",
        "persons",
        "processing",
        "search",
        "settings",
        "shared",
        "studio",
        "topics",
    },
    "api": {
        "admin",
        "agent",
        "models",
        "persons",
        "search",
        "server",
        "settings",
        "shared",
        "studio",
        "topics",
    },
    "diagnostics": {"agent", "models", "processing", "search", "shared", "studio", "topics"},
    "server": {"cli", "settings"},
    "worker": {"models", "settings", "shared", "studio"},
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
    "maintenance": {"api", "cli"},
}

# agent 在 shared 之外允许导入的具体模块（既有接口，逐模块冻结）。
AGENT_EXTERNAL_MODULES = {
    "processing.subtitle_uploads",
    "settings.runtime_settings",
    "studio.studio",
    "studio.models",
    # 可管理的平台 ID 校验及既有方案的规则快照解析，与 studio_models 同类的领域接口。
    "studio.platforms",
    "studio.workspace",
    # 选题工具的参数模型（ID 格式、权重与定时），与 studio_models 同类。
    "topics.common",
    "topics.models",
}

# 侧向依赖 = 源与目标都是上层包（shared/models/processing 是设计的下层，app 与 cli 是装配层，
# agent 另有逐模块约束）。从 ALLOWED_UPPER 推导，供 upper_module_edges 过滤。
_LOWER_UNITS = {"shared", "models", "processing"}
# 装配/入口层(app→api、cli、server、worker)不冻结侧向依赖;agent 另有逐模块约束。
_ENTRY_UNITS = {APP, "api", "cli", "server", "worker", "agent"}
UPPER_PAIRS = {
    (unit, target)
    for unit, targets in ALLOWED_UPPER.items()
    if unit not in _ENTRY_UNITS
    for target in targets
    if target not in _LOWER_UNITS
}

# 侧向包对内实际导入的模块全名（2026-11 冻结，共 22 对 33 条）：新增侧向模块导入须显式登记，
# 消除后同步删除；与 ALLOWED_UPPER 同样做精确相等校验。已知局限：同一已登记模块被新文件导入
# 不报警（广度冻结而非扇出冻结），需要时再收紧。
UPPER_MODULES: dict[str, dict[str, frozenset[str]]] = {
    "admin": {
        "diagnostics": frozenset({"diagnostics.film"}),
        "maintenance": frozenset({"maintenance.purge"}),
        "persons": frozenset({"persons.person_names", "persons.persons"}),
        "search": frozenset({"search.search", "search.warmup"}),
    },
    "diagnostics": {
        "agent": frozenset({"agent.dsh_runtime"}),
        "search": frozenset({"search.search"}),
        "studio": frozenset(
            {
                "studio.animation_capture",
                "studio.beats",
                "studio.brands",
                "studio.platforms",
                "studio.script_templates",
                "studio.render",
                "studio.tools",
                "studio.tools.artifacts",
                "studio.tools.mcp",
                "studio.workspace.handlers.overlay",
            }
        ),
        "topics": frozenset({"topics.sources.calendar"}),
    },
    "maintenance": {
        "agent": frozenset({"agent.attachments"}),
        "persons": frozenset({"persons.persons"}),
    },
    "persons": {
        "admin": frozenset({"admin.source_tasks"}),
        "maintenance": frozenset({"maintenance.clean"}),
        "settings": frozenset({"settings.runtime_settings"}),
    },
    "search": {
        "admin": frozenset({"admin.curation"}),
        "persons": frozenset({"persons.person_names", "persons.person_processing", "persons.persons"}),
        "settings": frozenset({"settings.runtime_settings"}),
    },
    "settings": {
        "agent": frozenset({"agent.dsh_runtime", "agent.llm.factory", "agent.llm.types"}),
        "persons": frozenset({"persons.person_processing"}),
    },
    "studio": {
        "admin": frozenset({"admin.curation"}),
        "persons": frozenset({"persons.person_names", "persons.person_processing", "persons.persons"}),
        "search": frozenset({"search.search"}),
        "settings": frozenset({"settings.runtime_settings"}),
    },
    "topics": {
        "admin": frozenset({"admin.curation"}),
        "search": frozenset({"search.search"}),
        "studio": frozenset(
            {
                "studio.material_digest",
                "studio.script_templates",
                "studio.studio",
                "studio.workspace",
            }
        ),
    },
}


def _source_unit(src: Path, path: Path) -> str | None:
    parts = path.relative_to(src).parts
    if len(parts) == 1:
        # backend/src/__init__.py:包自举(查询根目录修正),无依赖方向;其他顶层文件视为装配层
        return None if parts[0] == "__init__.py" else APP
    return parts[0]


def _module_exists(src: Path, dotted: str) -> bool:
    rel = Path(*dotted.split("."))
    return (src / rel).with_suffix(".py").is_file() or (src / rel / "__init__.py").is_file()


def _absolute_module(src: Path, path: Path, node: ast.ImportFrom) -> str | None:
    """ImportFrom 语句的绝对模块名;相对导入按文件包深度换算,越界或非本包返回 None。"""
    if node.level == 0:
        if node.module and node.module.split(".")[0] in FIRST_PARTY:
            return node.module
        return None
    parts = list(path.relative_to(src).parts[:-1])
    up = node.level - 1
    if up > len(parts):
        return None
    base = parts[: len(parts) - up]
    return ".".join(base + (node.module.split(".") if node.module else []))


def imported_modules(src: Path, path: Path) -> set[str]:
    """``path`` 导入的 mindex 模块全名(绝对与相对导入都识别);``from 包 import 子模块`` 解析到子模块。"""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.split(".")[0] in FIRST_PARTY)
        elif isinstance(node, ast.ImportFrom):
            absolute = _absolute_module(src, path, node)
            if absolute is None:
                continue
            for alias in node.names:
                candidate = f"{absolute}.{alias.name}"
                found.add(candidate if _module_exists(src, candidate) else absolute)
    # 旧语义只剔除裸 mindex 根包;短名下裸包导入(如 import cli)就是包级边,全部保留。
    return found


def _target_unit(src: Path, module: str) -> str:
    head = module.split(".")[0]
    return head if (src / head).is_dir() else APP


def package_edges(src: Path) -> dict[str, set[str]]:
    """包级依赖边（不含指向自身的边）。"""
    edges: dict[str, set[str]] = defaultdict(set)
    for path in sorted(src.rglob("*.py")):
        unit = _source_unit(src, path)
        if unit is None:
            continue
        edges.setdefault(unit, set())
        for module in imported_modules(src, path):
            target = _target_unit(src, module)
            if target != unit:
                edges[unit].add(target)
    return dict(edges)


def upper_module_edges(src: Path, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], set[str]]:
    """每个（源包, 目标上层包）侧向对实际导入的模块全名。"""
    edges: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in sorted(src.rglob("*.py")):
        unit = _source_unit(src, path)
        if unit is None:
            continue
        for module in imported_modules(src, path):
            target = _target_unit(src, module)
            if target != unit and (unit, target) in pairs:
                edges[(unit, target)].add(module)
    return edges


def violations(
    src: Path,
    allowed: dict[str, set[str]],
    forbidden: dict[str, set[str]],
    agent_modules: set[str],
    upper_modules: dict[str, dict[str, frozenset[str]]],
) -> list[str]:
    """返回全部违规描述；空列表表示通过。"""
    problems = []
    for path in sorted(src.rglob("*.py")):
        unit = _source_unit(src, path)
        if unit is None:
            continue  # backend/src/__init__.py:包自举
        rel = path.relative_to(src)
        if unit not in allowed:
            problems.append(f"{rel}：包 {unit} 没有依赖规则，请在测试中登记")
            continue
        for module in sorted(imported_modules(src, path)):
            target = _target_unit(src, module)
            if target == unit:
                continue
            frozen = upper_modules.get(unit, {}).get(target)
            if target in forbidden.get(unit, set()):
                problems.append(f"{rel} -> {module}：{unit} 禁止依赖 {target}")
            elif target not in allowed[unit]:
                problems.append(f"{rel} -> {module}：新增了 {unit} -> {target} 的依赖")
            elif unit == "agent" and target != "shared" and module not in agent_modules:
                problems.append(f"{rel} -> {module}：agent 只能导入 shared 与白名单模块")
            elif frozen is not None and module not in frozen:
                problems.append(
                    f"{rel} -> {module}：新增了 {unit} -> {target} 的模块级侧向依赖，"
                    f"请消除或在 UPPER_MODULES 登记"
                )
    return problems


def _rules() -> dict[str, set[str]]:
    return {**ALLOWED, **ALLOWED_UPPER}


def test_package_dependencies_follow_rules():
    problems = violations(SRC, _rules(), FORBIDDEN, AGENT_EXTERNAL_MODULES, UPPER_MODULES)
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
    assert AGENT_EXTERNAL_MODULES == used, (
        f"agent 白名单与实际不一致：{sorted(AGENT_EXTERNAL_MODULES ^ used)}"
    )


def test_upper_module_whitelist_matches_current_edges():
    """侧向模块白名单与实际边精确相等：包对与模块都要双向一致。"""
    actual = upper_module_edges(SRC, UPPER_PAIRS)
    declared = {(unit, target) for unit, targets in UPPER_MODULES.items() for target in targets}
    assert declared == set(actual), (
        f"UPPER_MODULES 的包对与实际侧向边不一致：{sorted(declared ^ set(actual))}"
    )
    for (unit, target), modules in sorted(actual.items()):
        difference = sorted(UPPER_MODULES[unit][target] ^ modules)
        assert not difference, f"{unit} -> {target} 模块白名单与实际不一致：{difference}"


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
    _write(src, "cli/__init__.py")
    _write(src, "studio/studio.py")
    _write(src, "studio/other.py")
    _write(src, "shared/util.py")
    rules = {
        "shared": set(),
        "models": {"shared"},
        "maintenance": {"shared"},
        "agent": {"shared", "studio"},
        "studio": {"shared"},
        "cli": {"shared"},
        APP: {"shared", "models"},
    }
    forbidden = {"maintenance": {"api", "cli"}}
    _write(src, "models/ok.py", "from shared.util import x\n")
    assert violations(src, rules, forbidden, {"studio.studio"}, {}) == []

    # 嵌套子包里的函数内导入也要被发现。
    _write(src, "models/deep/inner.py", "def f():\n    from studio import studio\n")
    _write(src, "maintenance/tool.py", "import cli\n")
    _write(src, "agent/tools/x.py", "from studio import studio, other\n")
    _write(src, "newpkg/mod.py", "")
    problems = violations(src, rules, forbidden, {"studio.studio"}, {})
    assert any("models/deep/inner.py -> studio.studio" in p and "新增" in p for p in problems)
    assert any("maintenance/tool.py -> cli" in p and "禁止" in p for p in problems)
    assert any("agent/tools/x.py -> studio.other" in p for p in problems)
    assert not any("agent/tools/x.py -> studio.studio" in p for p in problems)
    assert any("newpkg/mod.py" in p and "没有依赖规则" in p for p in problems)
    assert len(problems) == 4
    assert package_edges(src)["models"] == {"shared", "studio"}


def test_checker_reports_new_lateral_module_imports(tmp_path):
    """侧向包对内新增模块导入被单独报告；已登记模块与未冻结的包对不受影响。"""
    src = tmp_path / "src"
    for package in ("shared", "studio", "persons"):
        _write(src, f"{package}/__init__.py")
    _write(src, "persons/persons.py")
    _write(src, "persons/other.py")
    _write(src, "shared/util.py")
    rules = {"shared": set(), "studio": {"shared", "persons"}, "persons": {"shared"}}
    upper = {"studio": {"persons": frozenset({"persons.persons"})}}
    _write(src, "studio/ok.py", "from persons import persons\n")
    _write(src, "studio/downward.py", "from shared.util import x\n")
    assert violations(src, rules, {}, set(), upper) == []

    _write(src, "studio/extra.py", "import persons.other\n")
    problems = violations(src, rules, {}, set(), upper)
    assert any("studio/extra.py -> persons.other" in p and "模块级侧向依赖" in p for p in problems)
    assert len(problems) == 1
