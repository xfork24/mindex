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

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402

from mindex.agent.mcp_server import server_for_app  # noqa: E402
from mindex.api import create_app  # noqa: E402
from mindex.studio import studio  # noqa: E402
from mindex.studio import studio_workspace as ws  # noqa: E402


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
    assert "ask_user_question" not in tools and len(tools) == 28
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
