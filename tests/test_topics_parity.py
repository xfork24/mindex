"""选题挖掘的入口一致性：``topics/capabilities.py`` 登记的每项能力都要同时提供
REST 路由、``mindex topics`` CLI 命令、智能助手工具（MCP 同名导出）与页面区块，缺任一项即失败。"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from mindex.agent.mcp_server import exported
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.cli_topics import app as topics_app
from mindex.topics.capabilities import CAPABILITIES

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"
REGISTRY = build_registry()


@pytest.fixture(scope="module")
def routes(tmp_path_factory):
    from mindex.shared.config import Config

    app = create_app(Config(data_dir=tmp_path_factory.mktemp("parity") / "data"))

    def flat(items):
        for route in items:
            nested = getattr(route, "original_router", None)  # FastAPI 惰性包含的子路由
            if nested is not None:
                yield from flat(nested.routes)
            else:
                yield route

    return {(method, route.path) for route in flat(app.routes) for method in getattr(route, "methods", None) or ()}


def cli_commands() -> set[str]:
    found = set()

    def walk(command, prefix: str):
        if hasattr(command, "commands"):  # Typer 可能使用自带的 click，不用 isinstance 判断命令组
            for name, child in command.commands.items():
                walk(child, f"{prefix} {name}".strip())
        else:
            found.add(prefix)

    walk(typer.main.get_command(topics_app), "")
    return found


def test_capability_names_unique():
    names = [c.name for c in CAPABILITIES]
    assert len(names) == len(set(names))
    tools = [t for c in CAPABILITIES for t in c.tools]
    assert len(tools) == len(set(tools))


@pytest.mark.parametrize("capability", CAPABILITIES, ids=lambda c: c.name)
def test_every_capability_has_all_entries(capability, routes):
    for method, path in capability.rest:
        assert (method, path) in routes, f"{capability.label}：缺少 REST {method} {path}"
    commands = cli_commands()
    for command in capability.cli:
        assert command in commands, f"{capability.label}：缺少 CLI mindex topics {command}"
    mcp_names = {spec.name for spec in exported(REGISTRY)}
    for name in capability.tools:
        spec = REGISTRY.get(name)
        assert spec is not None, f"{capability.label}：缺少智能助手工具 {name}"
        assert spec.domain == "topics" or name.endswith("job_profile"), name
        assert name in mcp_names, f"{capability.label}：工具 {name} 未导出给 MCP"
    html = (FRONTEND / capability.page_file).read_text(encoding="utf-8")
    assert f'data-capability="{capability.page}"' in html, f"{capability.label}：页面缺少区块 {capability.page}"


def test_all_topics_tools_are_registered_capabilities():
    """topics 领域的每个工具都必须出现在对照表里（反向检查，防止只加了工具忘了其他入口）。"""
    listed = {t for c in CAPABILITIES for t in c.tools}
    assert {s.name for s in REGISTRY.all() if s.domain == "topics"} <= listed


def test_all_topics_routes_are_registered_capabilities(routes):
    listed = {route for c in CAPABILITIES for route in c.rest}
    actual = {(m, p) for m, p in routes if p.startswith("/api/topics") and m not in {"HEAD", "OPTIONS"}}
    assert actual <= listed, sorted(actual - listed)
