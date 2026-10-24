"""智能助手工具层：注册表、回环客户端（含访问令牌）、短 ID 解析与 Studio 只读工具。"""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import legacy_job
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from mindex import cli
from mindex.agent.ids import resolve_shot, resolve_version, short
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import NAME_PATTERN, ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.studio import studio
from mindex.studio import studio_workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-925281d6cc"]


@pytest.fixture
def board(config):
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    texts = ["红色开场。", "蓝色中段。", "绿色结尾。"]
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.5) for sid, text in zip(SHOTS, texts)
    ])
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id


def run_tool(app, name, arguments, page=None):
    registry = build_registry()

    async def go():
        client = MindexClient(app)
        try:
            spec = registry.get(name)
            return await spec.execute(spec.parse(arguments), ToolContext(client=client, page=page or {}))
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_registry_names_and_schemas():
    registry = build_registry()
    assert len(registry) <= 30
    for spec in registry.all():
        assert NAME_PATTERN.match(spec.name) and len(f"mcp__mindex__{spec.name}") <= 64
        schema = spec.schema()
        assert schema["parameters"]["type"] == "object"
        assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(schema)
        assert spec.risk in {"read", "write", "danger"}
    assert {s.name for s in registry.restrict(["studio"]).all()} >= {"list_jobs", "get_workspace_summary"}


def test_cli_lists_tools(tmp_path):
    result = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path), "agent", "tools", "--names-only"])
    assert result.exit_code == 0, result.output
    assert "list_jobs\tread\tstudio" in result.output
    full = CliRunner().invoke(cli.app, ["--data-dir", str(tmp_path), "agent", "tools", "--domain", "studio"])
    assert json.loads(full.output)[0]["parameters"]["type"] == "object"


def test_id_resolution():
    assert resolve_shot(SHOTS, "a1daad59")["shot_id"] == SHOTS[0]
    assert resolve_shot(SHOTS, "shot-a1daad59")["shot_id"] == SHOTS[0]
    assert resolve_shot(SHOTS, SHOTS[2])["index"] == 3
    assert resolve_shot(SHOTS, "#2")["shot_id"] == SHOTS[1]
    assert resolve_shot(SHOTS, "#9")["status"] == "missing"
    ambiguous = resolve_shot(SHOTS, "925281d")
    assert ambiguous["status"] == "ambiguous" and len(ambiguous["candidates"]) == 2
    assert resolve_shot(SHOTS, "ffff")["status"] == "missing"
    versions = {"abcdef12" + "0" * 24: {"stage": "tts", "shot_id": "x"}, "abcdef13" + "0" * 24: {"stage": "recall"}}
    assert resolve_version(versions, "abcdef1")["status"] == "ambiguous"
    assert resolve_version(versions, "abcdef1", stage="tts")["status"] == "ok"
    assert short("shot-a1daad59aa") == "shot-a1daad59" and short("a" * 32) == "a" * 8


def test_loopback_with_web_token(config):
    app = create_app(config, web_token="secret-token")
    outside = TestClient(app)
    assert outside.get("/api/studio/jobs").status_code == 401
    assert outside.get("/api/studio/jobs", headers={"x-mindex-internal": "forged"}).status_code == 401

    async def inside():
        client = MindexClient(app)
        try:
            return await client.get("/api/studio/jobs")
        finally:
            await client.aclose()

    result = asyncio.run(inside())
    assert result == {"ok": True, "status": 200, "data": {"items": []}}


def test_studio_read_tools(config, board):
    app = create_app(config)
    listing = run_tool(app, "list_jobs", {})
    assert board in listing.render()

    summary = run_tool(app, "get_workspace_summary", {}, page={"job_id": board})
    text = summary.render()
    assert "shot-a1daad59" in text and SHOTS[0] in text and "revision=" in text
    assert summary.value["shots"][1]["shot_id"] == SHOTS[1]
    assert summary.value["plan"]["revision"] == TestClient(app).get(
        f"/api/studio/jobs/{board}/workspace").json()["revision"]

    resolved = run_tool(app, "resolve_shots", {"job_id": board, "refs": ["a1daad59", "925281d", "#3"]})
    statuses = [r["status"] for r in resolved.value["results"]]
    assert statuses == ["ok", "ambiguous", "ok"]
    assert "请向用户确认" in resolved.render()

    preflight = run_tool(app, "get_preflight", {"job_id": board})
    assert preflight.value["counts"]["rebuild"] >= 1 and preflight.value["token"]

    versions = run_tool(app, "list_versions", {"job_id": board, "stage": "content", "shot": "#1"})
    assert versions.value["items"][0]["selected"] is True

    with pytest.raises(ToolError, match="没有找到构建记录"):
        run_tool(app, "get_build_status", {"job_id": board})
    with pytest.raises(ToolError, match="未指定创作任务"):
        run_tool(app, "get_workspace_summary", {})
    with pytest.raises(ToolError, match="HTTP 404"):
        run_tool(app, "get_workspace_summary", {"job_id": "20990101-000000-abcdef"})
    with pytest.raises(ToolError, match="工具参数无效"):
        build_registry().get("list_jobs").parse({"limit": 0})
