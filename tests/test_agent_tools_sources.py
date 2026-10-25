"""第三方工具源的智能助手 / MCP 与 CLI 入口（spec m3-post-production 需求 18.4、19、22）：登记为 danger、
mcp-stdio 登记不导出到 MCP、密钥只收 secret:// 引用、edit_shots 设置 tool 来源与 ``mindex studio tools`` 命令组。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_agent_aigc import read
from test_agent_studio_recall import run
from test_studio_sources import setup, shot, small  # noqa: F401 — small 是夹具
from tool_stub_server import StubServer
from typer.testing import CliRunner

from mindex import cli_studio
from mindex.agent.mcp_server import exported
from mindex.agent.registry import ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.studio import workspace as ws

TOOLS = [{"name": "make_video", "output": "video"}, {"name": "make_image", "output": "image"}]


@pytest.fixture
def agent(small, tmp_path):  # noqa: F811
    job_id, _, _ = setup(small, [shot("a", "红色开场。")])
    with StubServer(tmp_path) as stub, TestClient(create_app(small)) as client:
        yield small, client, job_id, stub


def test_risk_and_mcp_export():
    registry = build_registry()
    assert registry.get("save_tool_source").risk == "danger"
    assert registry.get("save_stdio_tool_source").risk == "danger"
    assert registry.get("list_tool_sources").risk == "read"
    names = {spec.name for spec in exported(registry)}
    assert {"list_tool_sources", "test_tool_source", "save_tool_source"} <= names
    assert "save_stdio_tool_source" not in names


def test_save_test_and_use_tool_source(agent):
    config, client, job_id, stub = agent
    with pytest.raises(ToolError, match="secret://"):
        run(client, config, "save_tool_source", {"source_id": "stub", "name": "替身", "protocol": "http",
                                                 "http": {"base_url": stub.url}, "tools": TOOLS,
                                                 "secrets": {"token": "plain-text"}})
    preview, output, _ = run(client, config, "save_tool_source", {
        "source_id": "stub", "name": "替身", "protocol": "http",
        "http": {"base_url": stub.url, "tools_path": "/tools", "poll_interval": 0.05}, "tools": TOOLS})
    assert "发送给该服务" in " ".join(preview.warnings)
    assert output.value["id"] == "stub"
    listed = read(client, config, "list_tool_sources", {})[1]
    assert listed.value["items"][0]["tools"][0]["name"] == "make_video"
    _, tested, _ = run(client, config, "test_tool_source", {"source_id": "stub"})
    assert tested.value["ok"] and "make_video" in tested.text
    with pytest.raises(ToolError):
        run(client, config, "save_tool_source", {"source_id": "x", "name": "x", "protocol": "http", "tools": TOOLS})
    preview, _, _ = run(client, config, "save_stdio_tool_source", {
        "source_id": "local", "name": "本机", "stdio": {"command": "/usr/bin/true"}, "tools": TOOLS})
    assert "本机执行：/usr/bin/true" in preview.warnings[0]
    run(client, config, "edit_shots", {"job_id": job_id, "operations": [{"op": "update", "shot": "#1", "fields": {
        "source": {"kind": "tool", "tool_id": "stub", "tool": "make_image", "output": "image",
                   "arguments": {"prompt": "{{text}}"}}}}]})
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    source = ws.version(w, p["shots"]["a"]["content"])["data"]["source"]
    assert source == {"kind": "tool", "tool_id": "stub", "tool": "make_image", "arguments": {"prompt": "{{text}}"},
                      "output": "image", "motion": "zoom-in", "strength": 0.3}


def test_cli_tools_commands(agent, monkeypatch):
    config, _, job_id, stub = agent
    monkeypatch.setattr(cli_studio, "make_app", lambda cfg: create_app(cfg))
    monkeypatch.setenv("STUB_TOKEN", "Bearer cli-secret")

    def invoke(*args):
        return CliRunner().invoke(cli_studio.app, list(args), obj=config)

    assert invoke("tools", "add", "gen", "--tool", "a").exit_code != 0  # 缺少连接
    added = invoke("tools", "add", "gen", "--http", stub.url, "--tool", "make_video:video",
                   "--tool", "make_image:image:upload", "--header", "Authorization=secret:token",
                   "--secret", "token=STUB_TOKEN", "--json")
    assert added.exit_code == 0, added.output
    item = json.loads(added.stdout)
    assert item["missing_secrets"] == [] and item["tools"][1]["allow_upload"] is True
    assert "cli-secret" not in added.stdout
    assert "gen\t" in invoke("tools", "list").stdout
    shown = invoke("tools", "show", "gen").stdout
    assert "token" in shown and "cli-secret" not in shown
    tested = invoke("tools", "test", "gen")
    assert tested.exit_code == 0 and "连接成功" in tested.stdout
    used = invoke("shot", "set", job_id, "#1", "--tool", "gen/make_video", "--tool-args", '{"prompt": "{{visual}}"}',
                  "--json")
    assert used.exit_code == 0, used.output
    assert json.loads(used.stdout)["source"]["tool_id"] == "gen"
    assert invoke("tools", "delete", "gen", "--yes").exit_code == 2
    assert invoke("shot", "set", job_id, "#1", "--tool", "bad").exit_code != 0
