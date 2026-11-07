"""智能助手工具层：注册表、回环客户端（含访问令牌）、短 ID 解析与 Studio 只读工具。"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import cli
from agent.ids import resolve_shot, resolve_version, short
from agent.loopback import MindexClient
from agent.registry import NAME_PATTERN, ToolContext, ToolError
from agent.tools import build_registry
from api import create_app
from conftest import legacy_job
from studio import studio
from studio import workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-925281d6cc"]


@pytest.fixture
def board(config):
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    texts = ["红色开场。", "蓝色中段。", "绿色结尾。"]
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.5)
            for sid, text in zip(SHOTS, texts)
        ],
    )
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


def module_spec_count() -> int:
    """各工具模块 ``specs()`` 的总数（自动发现 mindex.agent.tools 下声明了 specs 的模块）。"""
    import importlib
    import pkgutil

    from agent import tools as package

    total = 0
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        if callable(getattr(module, "specs", None)):
            total += len(module.specs())
    return total


def test_registry_names_and_schemas():
    registry = build_registry()
    # 与各工具模块实际声明的数量一致（结构断言：新增工具无需同步修改）。
    assert len(registry) == len(registry.all()) == module_spec_count()
    shot_details = [s for s in registry.all() if s.name == "get_shot_details"]
    assert len(shot_details) == 1
    assert (
        shot_details[0].domain == "studio" and shot_details[0].risk == "read" and shot_details[0].mcp is True
    )
    for spec in registry.all():
        assert NAME_PATTERN.match(spec.name) and len(f"mcp__mindex__{spec.name}") <= 64
        schema = spec.schema()
        assert schema["parameters"]["type"] == "object"
        assert "$ref" not in json.dumps(schema) and "$defs" not in json.dumps(schema)
        assert spec.risk in {"read", "write", "danger"}
    assert {s.name for s in registry.restrict(["studio"]).all()} >= {"list_jobs", "get_workspace_summary"}


def test_registry_domain_filter_and_domain_tools():
    from agent.domains import DOMAIN_INFO, PAGE_DOMAIN
    from agent.registry import ToolSpec
    from shared.agent_domains import AGENT_DOMAINS, LOADABLE_DOMAINS

    assert LOADABLE_DOMAINS == AGENT_DOMAINS[1:] and "meta" not in LOADABLE_DOMAINS
    assert set(DOMAIN_INFO) == set(LOADABLE_DOMAINS)
    assert set(PAGE_DOMAIN.values()) <= set(LOADABLE_DOMAINS)
    registry = build_registry()
    spec = registry.all()[0]
    with pytest.raises(ValueError):
        ToolSpec(
            name="x",
            domain="unknown",
            description="",
            input_model=spec.input_model,
            risk="read",
            execute=spec.execute,
        )
    grouped = registry.domain_tools()
    assert sorted(n for names in grouped.values() for n in names) == sorted(s.name for s in registry.all())
    for domain, names in grouped.items():
        assert names == [s.name for s in registry.all() if s.domain == domain]
    assert len(registry.schemas()) == len(registry)
    only = {s["name"] for s in registry.schemas(domains={"meta"})}
    assert only == set(grouped.get("meta", []))
    assert registry.schemas(domains=()) == []
    studio_first = registry.schemas(first=("studio",), domains={"meta", "studio"})
    assert {s["name"] for s in studio_first} == set(grouped.get("meta", [])) | set(grouped.get("studio", []))
    assert studio_first[0]["name"] in grouped["studio"]


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
    versions = {
        "abcdef12" + "0" * 24: {"stage": "tts", "shot_id": "x"},
        "abcdef13" + "0" * 24: {"stage": "recall"},
    }
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
    assert (
        summary.value["plan"]["revision"]
        == TestClient(app).get(f"/api/studio/jobs/{board}/workspace").json()["revision"]
    )

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


TRUNCATION_NOTE = "（旁白与画面意象截断到 60 字、查询截断到 30 字；完整内容请用 get_shot_details 读取）"


def test_system_prompt_requires_reading_full_narration():
    """Validates: Requirements 6.1, 6.6"""
    from agent.prompts import SYSTEM_PROMPT

    assert "get_shot_details" in SYSTEM_PROMPT
    for phrase in [
        "拆分（split）",
        "合并（merge）",
        "update 修改旁白（text）",
        "完整旁白",
        "截断摘要",
        "get_workspace_summary",
        "resolve_shots",
        "按顺序切分",
        "重新调用 get_shot_details 读取当前旁白后再提交",
    ]:
        assert phrase in SYSTEM_PROMPT, phrase


def test_tool_descriptions_mention_truncation_and_detail_tool():
    """Validates: Requirements 6.2, 6.3"""
    from agent.tools.studio_write import ShotOperation

    registry = build_registry()
    summary = registry.get("get_workspace_summary").description
    assert "旁白与画面意象截断到 60 字" in summary and "查询截断到 30 字" in summary
    assert "get_shot_details" in summary
    edit = registry.get("edit_shots").description
    assert "split" in edit and "get_shot_details" in edit and "按顺序切分" in edit and "忽略空白" in edit
    parts = ShotOperation.model_fields["parts"].description
    assert "get_shot_details" in parts and "按顺序切分" in parts and "忽略空白" in parts


def test_board_text_truncation_note(config, board):
    """Validates: Requirements 6.2"""
    from agent.tools.common import PlanView, board_text

    data = (
        TestClient(create_app(config))
        .get(f"/api/studio/jobs/{board}/workspace", params={"compact": "true"})
        .json()
    )
    text = board_text(PlanView(data))
    assert "get_shot_details" in text
    assert text.splitlines()[-1] == TRUNCATION_NOTE
    empty = PlanView({"active_plan": "p1", "plans": {"p1": {"name": "空方案"}}, "versions": {}, "job": {}})
    empty_text = board_text(empty)
    assert empty_text == "（当前方案还没有分镜）"
    assert "截断" not in empty_text and "get_shot_details" not in empty_text
