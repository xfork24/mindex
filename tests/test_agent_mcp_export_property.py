"""MCP 导出过滤的性质测试：导出集合与参考公式一致，list_tools 实时反映当前 domains 配置。"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session

from agent.mcp_server import build_server, exported
from agent.tools import build_registry
from shared.agent_domains import LOADABLE_DOMAINS

REGISTRY = build_registry()
NEVER_EXPORTED = {"load_tools", "navigate", "ask_user_question"}

# None（全部导出）或 LOADABLE_DOMAINS 的非空子集
domain_configs = st.one_of(
    st.none(),
    st.lists(st.sampled_from(LOADABLE_DOMAINS), min_size=1, max_size=len(LOADABLE_DOMAINS), unique=True),
)


def reference(domains) -> set[str]:
    return {
        s.name
        for s in REGISTRY.all()
        if s.mcp and (domains is None or s.domain in domains or s.name == "report_capability_gap")
    }


def test_registry_has_the_never_exported_tools():
    # 保证排除断言不是空洞的：这三个工具确实在注册表中，只是不导出
    names = {s.name for s in REGISTRY.all()}
    assert NEVER_EXPORTED <= names
    assert "report_capability_gap" in names


# Feature: agent-capability-expansion, Property 4: MCP 导出过滤
@settings(max_examples=100, deadline=None)
@given(domains=domain_configs)
def test_exported_matches_reference_formula(domains):
    """**Validates: Requirements 2.1, 2.2**"""
    names = [s.name for s in exported(REGISTRY, domains)]
    assert len(names) == len(set(names))
    assert set(names) == reference(domains)
    assert not set(names) & NEVER_EXPORTED
    assert "report_capability_gap" in names


# Feature: agent-capability-expansion, Property 4: MCP 导出过滤
@settings(max_examples=100, deadline=None)
@given(sequence=st.lists(domain_configs, min_size=1, max_size=4))
def test_list_tools_reflects_current_domains(sequence):
    """**Validates: Requirements 2.1, 2.2**

    同一个服务、同一个会话内依次切换 domains（可变 holder），list_tools 每次都等于参考集合。
    """
    holder = {"domains": None}
    server = build_server(REGISTRY, lambda: None, policy="read", domains=lambda: holder["domains"])

    async def listings():
        seen = []
        async with create_connected_server_and_client_session(server) as client:
            for domains in sequence:
                holder["domains"] = domains
                seen.append({tool.name for tool in (await client.list_tools()).tools})
        return seen

    for domains, names in zip(sequence, asyncio.run(listings()), strict=True):
        assert names == reference(domains)
        assert not names & NEVER_EXPORTED
