"""MCP 导出：工具清单与注解、策略强制、streamable-http 令牌、stdio 往返（与 dsh 命名约束兼容）。"""

from __future__ import annotations

import asyncio
import json
import re
import sys

import pytest
from conftest import legacy_job
from fastapi.testclient import TestClient

pytest.importorskip("mcp")

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from mindex.agent.loopback import MindexClient
from mindex.agent.mcp_server import build_server, exported, server_for_app
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.studio import studio
from mindex.studio import workspace as ws


@pytest.fixture
def job(config):
    job_id = legacy_job(config, "红色开场。蓝色结尾。")["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id="shot-a1daad59aa", text="红色开场。", query="红色", estimated_seconds=1),
        studio.Shot(id="shot-925281d5bb", text="蓝色结尾。", query="蓝色", estimated_seconds=1),
    ])
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id


def run(coro):
    return asyncio.run(coro)


def text(result) -> str:
    return "".join(block.text for block in result.content)


async def session_calls(server, calls):
    async with create_connected_server_and_client_session(server) as client:
        listing = await client.list_tools()
        return listing, [await client.call_tool(name, args) for name, args in calls]


def test_tools_annotations_and_read_policy(config, job):
    app = create_app(config)
    server = server_for_app(app, app.state.runtime_config, app.state.store)
    merge = {"job_id": job, "operations": [{"op": "merge", "shots": ["#1", "#2"]}]}
    listing, (jobs, blocked, gap) = run(session_calls(server, [
        ("list_jobs", {}), ("edit_shots", merge),
        ("report_capability_gap", {"request": "调粗拆分粒度", "reason": "没有参数"}),
    ]))
    tools = {tool.name: tool for tool in listing.tools}
    # 导出集合等于注册表中 mcp=True 的工具；ask_user_question、navigate、load_tools 不导出。
    exported = {s.name for s in build_registry().all() if s.mcp}
    assert not {"ask_user_question", "navigate", "load_tools"} & set(tools) and set(tools) == exported
    for name, tool in tools.items():
        public = f"mcp__mindex__{name}"
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", public)  # dsh publicToolName 不会改写
        assert tool.inputSchema["type"] == "object"
    assert tools["list_jobs"].annotations.readOnlyHint is True
    assert tools["delete_job"].annotations.destructiveHint is True
    assert tools["edit_shots"].annotations.readOnlyHint is False and not tools["edit_shots"].annotations.destructiveHint
    assert not jobs.isError and job in text(jobs) and jobs.structuredContent["items"][0]["id"] == job
    assert blocked.isError and "agent.mcp.policy=read" in text(blocked)
    assert not gap.isError
    assert TestClient(app).get("/api/agent/gaps").json()["items"][0]["request"] == "调粗拆分粒度"


def test_write_policy_allows_write_not_danger(config, job):
    app = create_app(config)
    server = server_for_app(app, app.state.runtime_config, app.state.store, policy="write")
    merge = {"job_id": job, "operations": [{"op": "merge", "shots": ["#1", "#2"]}]}
    _, (merged, delete) = run(session_calls(server, [("edit_shots", merge), ("delete_job", {"job_id": job})]))
    assert not merged.isError and "2 → 1" in text(merged)
    assert delete.isError and "danger" in text(delete)
    assert TestClient(app).get(f"/api/studio/jobs/{job}").status_code == 200


def test_get_shot_details_mcp_matches_builtin(config, job):
    app = create_app(config)
    registry = build_registry()
    spec = registry.get("get_shot_details")
    arguments = {"job_id": job, "refs": ["#2", "a1daad59"]}

    async def builtin():
        client = MindexClient(app)
        try:
            return await spec.execute(spec.parse(arguments), ToolContext(client=client))
        finally:
            await client.aclose()

    expected = run(builtin())
    expected_structured = json.loads(json.dumps(expected.value, default=str))
    for policy in ("read", "write", "all"):
        published = []
        server = build_server(registry, lambda: MindexClient(app), policy=policy,
                              publish=lambda kind, data, sink=published: sink.append((kind, data)))
        listing, (ok, bad, conflict) = run(session_calls(server, [
            ("get_shot_details", arguments),
            ("get_shot_details", {"job_id": job, "limit": 0}),
            ("get_shot_details", {"job_id": job, "refs": ["#1"], "offset": 0}),
        ]))
        tool = next(t for t in listing.tools if t.name == "get_shot_details")
        assert tool.inputSchema == spec.parameters()
        assert tool.annotations.readOnlyHint is True and tool.annotations.destructiveHint is False
        assert not ok.isError, text(ok)
        assert text(ok) == expected.render()
        assert ok.structuredContent == expected_structured
        assert [kind for kind, _ in published] == ["focus"]  # 只有成功调用发布定位
        published.clear()
        assert bad.isError and conflict.isError
        assert published == []


def test_get_shot_details_invalid_args_do_not_publish(config, job):
    app = create_app(config)
    published = []
    server = build_server(build_registry(), lambda: MindexClient(app), policy="all",
                          publish=lambda kind, data: published.append((kind, data)))
    _, results = run(session_calls(server, [
        ("get_shot_details", {"job_id": job, "refs": []}),
        ("get_shot_details", {"job_id": job, "refs": ["  "]}),
        ("get_shot_details", {"job_id": job, "unknown": 1}),
        ("get_shot_details", {"job_id": job, "limit": 101}),
    ]))
    assert all(result.isError for result in results)
    assert published == []


def test_streamable_http_requires_token(config, job, monkeypatch):
    monkeypatch.setenv("MINDEX_MCP_TOKEN", "mcp-secret")
    config.agent.mcp.http_enabled = True
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
    with TestClient(create_app(config, web_token="web-secret")) as client:
        assert client.post("/mcp", json=initialize, headers=headers).status_code == 401
        assert client.post("/mcp", json=initialize, headers={**headers, "Authorization": "Bearer wrong"}).status_code == 401
        auth = {**headers, "Authorization": "Bearer mcp-secret"}
        response = client.post("/mcp", json=initialize, headers=auth)
        assert response.status_code == 200, response.text
        listing = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=auth)
        payload = next(json.loads(line[5:]) for line in listing.text.splitlines() if line.startswith("data:"))
        assert "list_jobs" in {tool["name"] for tool in payload["result"]["tools"]}
        # 网页接口仍受访问令牌保护。
        assert client.get("/api/agent/sessions").status_code == 401

    config.agent.mcp.http_enabled = True
    monkeypatch.delenv("MINDEX_MCP_TOKEN")
    with pytest.raises(ValueError, match="MINDEX_MCP_TOKEN"):
        create_app(config)


def test_domain_filter_hot_reload_and_async_hooks(config, job):
    app = create_app(config)
    registry = build_registry()
    holder = {"domains": ["settings"]}
    gaps = []

    async def profile():
        return None

    async def record(sid, request, reason, suggestion):
        gaps.append(request)
        return {"id": 7}

    server = build_server(registry, lambda: MindexClient(app), policy="read", agent_profile=profile,
                          record_gap=record, domains=lambda: holder["domains"])

    async def go():
        async with create_connected_server_and_client_session(server) as client:
            first = {tool.name for tool in (await client.list_tools()).tools}
            blocked = await client.call_tool("list_jobs", {})
            unknown = await client.call_tool("no_such_tool", {})
            hidden = await client.call_tool("navigate", {})
            gap = await client.call_tool("report_capability_gap", {"request": "远程记录", "reason": "测试"})
            holder["domains"] = None  # 配置变更热生效
            second = {tool.name for tool in (await client.list_tools()).tools}
            jobs = await client.call_tool("list_jobs", {})
            return first, blocked, unknown, hidden, gap, second, jobs

    first, blocked, unknown, hidden, gap, second, jobs = run(go())
    settings = {spec.name for spec in registry.all() if spec.mcp and spec.domain == "settings"}
    assert first == settings | {"report_capability_gap"}
    assert blocked.isError and "未在当前 MCP 导出范围内" in text(blocked)
    assert unknown.isError and "未知工具" in text(unknown)
    assert hidden.isError and "未知工具" in text(hidden)  # mcp=False 的工具不暴露存在性
    assert not gap.isError and "#7" in text(gap) and gaps == ["远程记录"]
    assert "list_jobs" in second and not jobs.isError and job in text(jobs)


def test_server_for_app_domains_override(config, job):
    app = create_app(config)
    config.agent.mcp.domains = ["settings"]
    server = server_for_app(app, app.state.runtime_config, app.state.store, domains=["studio"])
    listing, (jobs,) = run(session_calls(server, [("list_jobs", {})]))
    names = {tool.name for tool in listing.tools}
    assert "list_jobs" in names and "report_capability_gap" in names and not jobs.isError


def test_stdio_roundtrip(config, job, tmp_path):
    isolated = tmp_path / "isolated.yaml"  # 不读取开发机的 config.local.yaml
    isolated.write_text("{}\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from mindex.cli import app; app()", "--config", str(isolated),
              "--data-dir", str(config.data_dir), "mcp"],
    )

    async def go():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            info = await session.initialize()
            assert info.serverInfo.name == "mindex"
            result = await session.call_tool("get_workspace_summary", {"job_id": job})
            return result

    result = asyncio.run(asyncio.wait_for(go(), timeout=60))
    assert not result.isError and "shot-a1daad59" in text(result)


def test_remote_gap_endpoint_and_hooks(config, job):
    from mindex.agent.mcp_server import RemoteAgentProfile, remote_gap_recorder
    from mindex.settings.runtime_settings import put_setting

    app = create_app(config)
    http = TestClient(app)
    assert http.post("/api/agent/gaps", json={"request": "", "reason": "x"}).status_code == 422
    created = http.post("/api/agent/gaps", json={"request": "远程缺口", "reason": "没有工具"})
    assert created.status_code == 201 and created.json()["request"] == "远程缺口"
    assert created.json()["session_id"] is None

    now = [0.0]

    async def go():
        client = MindexClient(app)
        try:
            item = await remote_gap_recorder(client)(None, "经 HTTP", "测试", "")
            profile = RemoteAgentProfile(client, clock=lambda: now[0])
            first = await profile()
            # 档案指针变化后：缓存期内（10 秒）返回旧值，过期后刷新。
            put_setting(app.state.store, "active.profile.agent", "42")
            cached = await profile()
            now[0] = 11.0
            refreshed = await profile()
            return item, first, cached, refreshed
        finally:
            await client.aclose()

    item, first, cached, refreshed = run(go())
    assert item["request"] == "经 HTTP" and item["id"] > created.json()["id"]
    assert first is None and cached is None and refreshed == 42


def test_server_for_app_follows_config_domains_hot(config, job):
    """未显式给出 domains 时，同一会话内每次 list_tools/call_tool 都读取最新的 agent.mcp.domains。"""
    app = create_app(config)
    runtime_config = app.state.runtime_config
    registry = build_registry()
    server = server_for_app(app, runtime_config, app.state.store)

    def names(domains):
        return {spec.name for spec in exported(registry, domains)}

    async def go():
        async with create_connected_server_and_client_session(server) as client:
            seen = []
            for domains in (["settings"], ["studio", "search"], None):
                runtime_config.base.agent.mcp.domains = domains
                listing = {tool.name for tool in (await client.list_tools()).tools}
                jobs = await client.call_tool("list_jobs", {})
                seen.append((domains, listing, jobs))
            return seen

    first, second, third = run(go())
    assert first[1] == names(["settings"]) and "list_jobs" not in first[1]
    assert first[2].isError and "未在当前 MCP 导出范围内" in text(first[2])
    assert second[1] == names(["studio", "search"]) and "search_media" in second[1]
    assert not second[2].isError and job in text(second[2])
    assert third[1] == names(None) and len(third[1]) > len(second[1]) and not third[2].isError


def test_stdio_domain_option_overrides_config(config, job, tmp_path):
    """``mindex mcp --domain`` 端到端：覆盖 yaml 中的 agent.mcp.domains。"""
    isolated = tmp_path / "isolated.yaml"
    isolated.write_text("agent:\n  mcp:\n    domains: [studio]\n")
    registry = build_registry()

    def params(*extra):
        return StdioServerParameters(
            command=sys.executable,
            args=["-c", "from mindex.cli import app; app()", "--config", str(isolated),
                  "--data-dir", str(config.data_dir), "mcp", *extra],
        )

    async def go(server_params):
        async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listing = {tool.name for tool in (await session.list_tools()).tools}
            return listing, await session.call_tool("list_jobs", {})

    from_config, jobs = asyncio.run(asyncio.wait_for(go(params()), timeout=60))
    assert from_config == {spec.name for spec in exported(registry, ["studio"])}
    assert not jobs.isError and job in text(jobs)

    overridden, blocked = asyncio.run(asyncio.wait_for(go(params("--domain", "search")), timeout=60))
    search = {spec.name for spec in registry.all() if spec.mcp and spec.domain == "search"}
    assert search and overridden == search | {"report_capability_gap"}
    assert blocked.isError and "未在当前 MCP 导出范围内" in text(blocked)


def remote_like_server(app):
    """远程 stdio 模式的服务组装（与 ``mindex mcp --base-url`` 相同）；回环客户端用进程内 app 代替 HTTP。"""
    from mindex.agent.mcp_server import RemoteAgentProfile, remote_gap_recorder

    client = MindexClient(app)
    return build_server(build_registry(), lambda: client, policy="all",
                        agent_profile=RemoteAgentProfile(client), record_gap=remote_gap_recorder(client))


def test_remote_mode_protects_agent_own_profile(config):
    app = create_app(config)
    http = TestClient(app)

    def create(role, name, model):
        response = http.post("/api/settings/profiles", json={
            "role": role, "name": name, "provider": "custom",
            "config": {"backend": "openai", "base_url": "http://llm.test/v1", "model": model}})
        assert response.status_code in (200, 201), response.text
        return response.json()["id"]

    own = create("agent", "助手 A", "a")
    assert http.post(f"/api/settings/profiles/{own}/activate").status_code == 200
    other_agent = create("agent", "助手 B", "b")
    writer = create("writer", "文案", "w")

    _, (update_own, switch_agent, update_writer, activate_writer) = run(session_calls(remote_like_server(app), [
        ("update_model_profile", {"profile_id": own, "config": {"model": "changed"}}),
        ("activate_model_profile", {"profile_id": other_agent}),
        ("update_model_profile", {"profile_id": writer, "name": "文案 2"}),
        ("activate_model_profile", {"profile_id": writer}),
    ]))
    assert update_own.isError and "不能修改智能助手当前正在使用的档案" in text(update_own)
    assert switch_agent.isError and "不能在会话中切换智能助手自身的模型档案" in text(switch_agent)
    assert not update_writer.isError, text(update_writer)
    assert not activate_writer.isError, text(activate_writer)

    roles = {r["role"]: r for r in http.get("/api/settings?probe_embedding=false").json()["roles"]}
    assert roles["agent"]["active_profile_id"] == own
    own_profile = next(p for p in roles["agent"]["profiles"] if p["id"] == own)
    assert own_profile["config"]["model"] == "a"
    assert roles["writer"]["active_profile_id"] == writer


def test_remote_mode_records_capability_gap(config):
    app = create_app(config)
    _, (gap,) = run(session_calls(remote_like_server(app), [
        ("report_capability_gap", {"request": "远程模式缺口", "reason": "没有对应工具", "suggestion": "新增工具"}),
    ]))
    assert not gap.isError, text(gap)
    items = TestClient(app).get("/api/agent/gaps").json()["items"]
    item = next(i for i in items if i["request"] == "远程模式缺口")
    assert item["reason"] == "没有对应工具" and item["session_id"] is None
    assert f"#{item['id']}" in text(gap)


def test_mcp_cli_rejects_unknown_domain(config, tmp_path):
    from typer.testing import CliRunner

    from mindex.cli import app as cli

    isolated = tmp_path / "isolated.yaml"
    isolated.write_text("{}\n")
    result = CliRunner().invoke(cli, ["--config", str(isolated), "--data-dir", str(config.data_dir),
                                      "mcp", "--domain", "studio", "--domain", "nope"])
    assert result.exit_code != 0 and "未知领域：nope" in result.output


def test_real_http_client_initializes_lists_and_reads(config, monkeypatch):
    import httpx
    from mcp.client.streamable_http import streamablehttp_client
    from test_agent_panel_ui import serve
    monkeypatch.setenv('MINDEX_MCP_TOKEN', 'http-fixture-token')
    config.agent.mcp.http_enabled = True
    server, thread, url = serve(create_app(config))

    async def exercise():
        async with streamablehttp_client(url + '/mcp', headers={'Authorization': 'Bearer http-fixture-token'}) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert 'list_local_services' in names
                result = await session.call_tool('list_local_services', {})
                assert not result.isError, result
        async with httpx.AsyncClient() as http:
            assert (await http.post(url + '/mcp', json={})).status_code == 401
    try:
        asyncio.run(exercise())
    finally:
        server.should_exit = True
        thread.join(timeout=10)
