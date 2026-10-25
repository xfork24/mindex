"""选题挖掘 · 从选题建创作任务、账号档案进入文案与同步：REST、CLI、智能助手工具与签名回归。"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from conftest import FixtureWriter
from fastapi.testclient import TestClient
from topics_fixtures import RSS, ScriptedTopicLlm, install, topic_embedder
from typer.testing import CliRunner

from mindex import cli
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.models.writer import PROFILE_HEADER, QwenWriter
from mindex.shared import locks
from mindex.shared.storage import Store
from mindex.studio import script_templates as st
from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.topics import create_job as topic_jobs

FEED = "https://news.example.com/feed.xml"


@pytest.fixture
def env(config, monkeypatch):
    install(monkeypatch, {FEED: RSS})
    writer = FixtureWriter(config, vary=True)
    app = create_app(config, topic_embedder(config), writer=writer, topic_llm=ScriptedTopicLlm())
    with TestClient(app) as client:
        pid = client.post("/api/topics/profiles", json={
            "name": "财经号", "domain": "金融", "audience": "上班族", "positioning": "大白话", "taboos": ["不荐股"],
            "canvas": "9:16"}).json()["id"]
        client.post(f"/api/topics/profiles/{pid}/sources", json={"kind": "rss", "name": "要闻", "config": {"url": FEED}})
        run = client.post(f"/api/topics/profiles/{pid}/runs", json={}).json()
        for _ in range(600):
            if client.get(f"/api/topics/runs/{run['id']}").json()["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        cards = client.get(f"/api/topics/profiles/{pid}/cards").json()["items"]
        yield config, app, client, writer, pid, cards


def run_script(client, jid):
    data = client.get(f"/api/studio/jobs/{jid}/workspace").json()
    response = client.post(f"/api/studio/jobs/{jid}/plans/{data['active_plan']}/nodes",
                           json={"revision": data["revision"], "inputs": {}, "stage": "script", "force": True})
    assert response.status_code == 202, response.text
    for _ in range(2000):
        data = client.get(f"/api/studio/jobs/{jid}/workspace").json()
        if data["builds"] and data["builds"][-1]["status"] != "running":
            return data
        time.sleep(0.01)
    raise AssertionError("build timeout")


def test_create_job_from_card(env):
    config, _, client, writer, pid, cards = env
    card = cards[0]
    created = client.post(f"/api/topics/cards/{card['id']}/job", json={})
    assert created.status_code == 201, created.text
    result = created.json()
    jid = result["job_id"]
    assert result["card"]["status"] == "adopted" and result["card"]["job_id"] == jid
    job = studio.load_job(config, jid)
    assert job["outline"] == card["outline"] and job["title"] == card["title"][:60]
    assert job["topic"]["card_id"] == card["id"] and job["topic"]["profile_id"] == pid
    assert job["original_input"].startswith("来自选题")
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    assert p["options"]["template_id"] == card["template_id"] and p["options"]["canvas"] == "9:16"
    assert p["profile_hash"] == job["topic"]["profile_hash"]
    [reference] = ws.references.references(p)
    assert reference["name"] == "选题来源资料"
    text = ws.references.load_texts(config, jid, p)[0]
    assert card["title"] in text and "链接：https://news.example.com/" in text
    saved = json.loads((studio.job_dir(config, jid) / "profile.json").read_text(encoding="utf-8"))
    assert saved["snapshot"]["taboos"] == ["不荐股"] and saved["hash"] == p["profile_hash"]
    # 再建：409，force 时再建一份
    again = client.post(f"/api/topics/cards/{card['id']}/job", json={})
    assert again.status_code == 409 and jid in again.json()["detail"]
    other = client.post(f"/api/topics/cards/{card['id']}/job", json={"force": True}).json()
    assert other["job_id"] != jid
    # 选题列表：已采纳的从"新"移到"已建任务"
    counts = client.get(f"/api/topics/profiles/{pid}/cards").json()["counts"]
    assert counts["adopted"] == 1 and counts["new"] == len(cards) - 1
    # 文案阶段带上档案写作约束；签名含 profile
    data = run_script(client, jid)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    block = writer.profiles[-1]
    assert "目标受众：上班族" in block and "禁区（不得出现）：不荐股" in block
    p = data["plans"][data["active_plan"]]
    version = data["versions"][p["script"]]
    assert version["parameters"]["profile"] == p["profile_hash"] and version["data"]["profile"]["hash"]
    assert data["script_profile_changed"] is False


def test_create_job_rolls_back_on_failure(env, monkeypatch):
    config, _, client, _, _, cards = env
    def jobs():
        root = studio.studio_root(config)
        return {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()

    before = jobs()
    with locks.named(config.data_dir, "studio"):
        busy = client.post(f"/api/topics/cards/{cards[0]['id']}/job", json={})
    assert busy.status_code == 409 and "创作步骤" in busy.json()["detail"]
    assert jobs() == before  # 任务目录已删除
    assert client.get(f"/api/topics/cards/{cards[0]['id']}").json()["status"] == "new"

    def boom(*args, **kwargs):
        raise studio.StudioError("参考文稿总字数超过上限")

    monkeypatch.setattr(ws.references, "register", boom)
    failed = client.post(f"/api/topics/cards/{cards[0]['id']}/job", json={})
    assert failed.status_code == 422
    assert jobs() == before
    # 没有大纲时用标题、角度与关键画面拼成
    store = Store(config.data_dir)
    client.patch(f"/api/topics/cards/{cards[1]['id']}", json={"outline": ""})
    monkeypatch.undo()
    install(monkeypatch, {FEED: RSS})
    result = topic_jobs.create_from_card(config, store, cards[1]["id"])
    assert studio.load_job(config, result["job_id"])["outline"].startswith(cards[1]["title"])


def test_profile_sync_and_signature(env):
    _, _, client, writer, pid, cards = env
    jid = client.post(f"/api/topics/cards/{cards[0]['id']}/job", json={}).json()["job_id"]
    run_script(client, jid)
    view = client.get(f"/api/topics/jobs/{jid}/profile").json()
    assert view["outdated"] is False and view["profile_name"] == "财经号" and view["card"]["id"] == cards[0]["id"]
    # 改名称与关键词不影响写作哈希；改禁区才提示已更新
    client.patch(f"/api/topics/profiles/{pid}", json={"name": "财经号2", "keywords": ["CPI"]})
    assert client.get(f"/api/topics/jobs/{jid}/profile").json()["outdated"] is False
    client.patch(f"/api/topics/profiles/{pid}", json={"taboos": ["不荐股", "不谈加密货币"]})
    view = client.get(f"/api/topics/jobs/{jid}/profile").json()
    assert view["outdated"] is True and view["snapshot"]["taboos"] == ["不荐股"]
    synced = client.post(f"/api/topics/jobs/{jid}/profile/sync").json()
    assert synced["outdated"] is False and synced["snapshot"]["taboos"] == ["不荐股", "不谈加密货币"]
    data = client.get(f"/api/studio/jobs/{jid}/workspace").json()
    assert data["script_profile_changed"] is True  # 已有文案不自动重写，只提示
    assert data["job"]["topic"]["profile_hash"] == synced["hash"]
    calls = writer.calls
    data = run_script(client, jid)
    assert writer.calls == calls + 1 and "不谈加密货币" in writer.profiles[-1]
    assert data["script_profile_changed"] is False
    # 新建方案沿用档案快照
    data = client.get(f"/api/studio/jobs/{jid}/workspace").json()
    created = client.post(f"/api/studio/jobs/{jid}/plans", json={"name": "方案二"}).json()
    assert created["plans"][created["active_plan"]]["profile_hash"] == synced["hash"]
    # 删除档案后：任务继续用快照，不能同步
    assert client.delete(f"/api/topics/profiles/{pid}").status_code == 200
    view = client.get(f"/api/topics/jobs/{jid}/profile").json()
    assert view["profile_exists"] is False and view["outdated"] is False
    assert client.post(f"/api/topics/jobs/{jid}/profile/sync").status_code == 422
    # 普通任务：没有档案
    plain = client.post("/api/studio/jobs", json={"text": "普通大纲。"}).json()["id"]
    assert client.get(f"/api/topics/jobs/{plain}/profile").json()["topic"] is None
    assert client.post(f"/api/topics/jobs/{plain}/profile/sync").status_code == 422


def test_plain_job_script_signature_unchanged(config):
    """没有档案的任务：script 签名参数没有 profile 键，writer 调用不带 profile（旧签名逐字节不变）。"""
    jid = studio.create_job(config, "普通大纲。")["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    spec = ws.specification(config, jid, w, p, "script")
    assert "profile" not in spec["parameters"] and "profile_hash" not in p
    assert set(spec["parameters"]) == {"writer", "prompt_version", "template"}


def test_qwen_writer_profile_block(config, monkeypatch):
    seen = []

    def fake(profile, parts, instructions, feedback, **kwargs):
        seen.append(instructions)
        return "文案正文"

    monkeypatch.setattr("mindex.models.writer.chat_completion", fake)
    writer = QwenWriter(config)
    template = st.builtins()["base"]
    writer.write("大纲", template=template)
    writer.write("大纲", template=template, profile="禁区（不得出现）：不荐股")
    assert seen[0] == template.instructions.strip()
    assert seen[1] == template.instructions.strip() + PROFILE_HEADER + "禁区（不得出现）：不荐股"


def test_cli_and_agent_create_job(env, tmp_path):
    config, app, client, _, pid, cards = env
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"data_dir: {config.data_dir}\nembedding:\n  dimensions: 8\n", encoding="utf-8")
    runner = CliRunner()
    base = ["--config", str(yaml_path), "topics"]
    created = runner.invoke(cli.app, [*base, "create-job", cards[0]["id"]])
    assert created.exit_code == 0, created.output
    jid = json.loads(created.output)["job_id"]
    assert runner.invoke(cli.app, [*base, "create-job", cards[0]["id"]]).exit_code == 1
    shown = json.loads(runner.invoke(cli.app, [*base, "job-profile", "show", jid]).output)
    assert shown["topic"]["card_id"] == cards[0]["id"]
    client.patch(f"/api/topics/profiles/{pid}", json={"audience": "学生"})
    synced = json.loads(runner.invoke(cli.app, [*base, "job-profile", "sync", jid]).output)
    assert synced["snapshot"]["audience"] == "学生"

    registry = build_registry()

    async def call(name, arguments, page=None):
        mindex = MindexClient(app)
        try:
            spec = registry.get(name)
            args = spec.parse(arguments)
            ctx = ToolContext(client=mindex, page=page or {})
            preview = await spec.preview(args, ctx) if spec.preview else None
            return preview, await spec.execute(args, ctx)
        finally:
            await mindex.aclose()

    preview, out = asyncio.run(call("create_job_from_topic", {"card_id": cards[1]["id"]}))
    assert preview.changes[3]["after"] == "9:16" and out.focus == {"page": "studio", "job_id": out.value["job_id"]}
    assert out.mutation["domain"] == "studio"
    _, out = asyncio.run(call("get_job_profile", {}, page={"page": "studio", "job_id": jid}))
    assert "快照与当前档案一致" in out.text
    client.patch(f"/api/topics/profiles/{pid}", json={"taboos": ["新禁区"]})
    _, out = asyncio.run(call("get_job_profile", {"job_id": jid}))
    assert "原档案已更新" in out.text
    preview, out = asyncio.run(call("sync_job_profile", {"job_id": jid}))
    assert "不会自动重写" in preview.warnings[0] and "新禁区" in out.text
    plain = client.post("/api/studio/jobs", json={"text": "普通大纲。"}).json()["id"]
    with pytest.raises(ToolError, match="不是从选题创建"):
        asyncio.run(call("sync_job_profile", {"job_id": plain}))
