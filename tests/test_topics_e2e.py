"""选题挖掘端到端验收（slow）：在一个领域档案下，从数据源到首版成片全程只经 Mindex 的接口与工具。

验收标准（plans/roadmap.md M2）：
1. 一次挖掘产出至少 10 张带分数的选题，前 10 名带大纲；
2. 素材覆盖报告对应到素材库里真实存在的片段；
3. 从选题建出的任务带推荐模板、来源资料与档案快照，文案请求带上档案写作约束，并能构建出成片；
4. 同样的流程可以完全经 CLI 与智能助手工具完成。

模型均为确定性桩（脚本化选题模型、颜色 fixture 嵌入、确定性文案/拆分镜/配音），只验证工程链路，不代表真实模型质量。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from email.utils import format_datetime

import pytest
from conftest import FixtureSpeaker, FixtureStoryboarder, FixtureWriter
from fastapi.testclient import TestClient
from topics_fixtures import ScriptedTopicLlm, analyzed_library, install
from typer.testing import CliRunner

from mindex import cli
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio import workspace as ws

pytestmark = pytest.mark.slow

FEED = "https://news.example.com/feed.xml"
TOPICS = ["红色消费品牌发布财报", "央行宣布降息", "蓝色芯片发布会", "新能源车销量创新高", "绿色债券扩容", "房地产政策调整",
          "人民币汇率波动", "黄金价格走高", "居民存款利率下调", "跨境电商出口增长", "消费电子回暖", "国债发行提速"]


def feed() -> bytes:
    now = datetime.now().astimezone()
    items = "".join(
        f"<item><title>{title}</title><link>https://news.example.com/{index}</link>"
        f"<description>{title}的最新进展。</description><pubDate>{format_datetime(now - timedelta(hours=index))}</pubDate></item>"
        for index, title in enumerate(TOPICS))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>财经</title>{items}</channel></rss>'.encode()


@pytest.fixture
def world(config, video_file, monkeypatch):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 29.97
    embedder = analyzed_library(config, video_file)
    install(monkeypatch, {FEED: feed()})
    writer = FixtureWriter(config)
    app = create_app(config, embedder, writer=writer, storyboarder=FixtureStoryboarder(config),
                     speaker=FixtureSpeaker(config), topic_llm=ScriptedTopicLlm())
    return config, embedder, writer, app


def wait_run(client, run_id):
    for _ in range(1200):
        run = client.get(f"/api/topics/runs/{run_id}").json()
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("挖掘任务没有结束")


def build_final(config, embedder, writer, job_id):
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    checked = ws.preflight(config, job_id, w, p)
    bid = str(len(w["builds"]))
    w["builds"].append({"id": bid, "plan_id": p["id"], "status": "running", "force": False, "auto_split": True,
                        "nodes": [{**n, "status": "pending"} for n in checked["nodes"]]})
    ws.persist(config, job_id, w)
    ws.run_build(config, job_id, bid, speaker=FixtureSpeaker(config), storyboarder=FixtureStoryboarder(config),
                 embedder=embedder, writer=writer)
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


def test_page_path_from_sources_to_first_cut(world):
    config, embedder, writer, app = world
    store = Store(config.data_dir)
    with TestClient(app) as client:
        pid = client.post("/api/topics/profiles", json={
            "name": "财经号", "domain": "金融", "audience": "上班族", "positioning": "大白话讲财经",
            "taboos": ["不荐股"], "schedule": {"enabled": True, "time": "08:00", "count": 10}}).json()["id"]
        client.post(f"/api/topics/profiles/{pid}/sources", json={"kind": "rss", "name": "要闻", "config": {"url": FEED}})
        client.post(f"/api/topics/profiles/{pid}/sources", json={"kind": "local", "name": "素材库"})
        run = wait_run(client, client.post(f"/api/topics/profiles/{pid}/runs", json={}).json()["id"])
        assert run["status"] == "complete", run
        assert run["result"]["cards"] >= 10 and run["result"]["outlined"] == 10
        cards = client.get(f"/api/topics/profiles/{pid}/cards").json()["items"]
        assert len(cards) >= 10 and all(set(c["scores"]) == {"timeliness", "audience", "coverage", "novelty", "cost"}
                                        for c in cards)
        top = cards[:10]
        assert all(c["outline"] for c in top) and [c["total"] for c in cards] == sorted((c["total"] for c in cards),
                                                                                      reverse=True)
        segments = {row["id"] for row in store.segments()}
        detail = client.get(f"/api/topics/cards/{top[0]['id']}").json()
        hits = [clip for row in detail["coverage"]["visuals"] if row["hit"] for clip in row["clips"]]
        assert hits and all(clip["segment_id"] in segments for clip in hits)
        assert client.get(f"/api/topics/profiles/{pid}/gaps").json()["items"]  # 缺口进入采集清单
        job = client.post(f"/api/topics/cards/{top[0]['id']}/job", json={}).json()
        job_id = job["job_id"]
        w = ws.load(config, job_id)
        p = ws.plan(w, w["active_plan"])
        assert p["options"]["template_id"] == top[0]["template_id"] and p["profile_hash"]
        assert ws.references.references(p)[0]["name"] == "选题来源资料"
    w, p = build_final(config, embedder, writer, job_id)
    problems = [(n["stage"], n.get("shot_id"), n["status"], n.get("error")) for n in w["builds"][-1]["nodes"]
                if n["status"] not in ("complete", "reused", "skipped")]
    assert w["builds"][-1]["status"] == "complete", problems
    assert p["final"], "应构建出首版成片"
    assert "禁区（不得出现）：不荐股" in writer.profiles[-1]
    assert studio.load_job(config, job_id)["topic"]["card_id"] == top[0]["id"]


def test_cli_and_agent_path(world, tmp_path, monkeypatch):
    config, embedder, writer, app = world
    from mindex.models import adapters
    from mindex.topics import llm as topic_llm
    from mindex.topics import runner as topic_runner

    model = ScriptedTopicLlm()
    monkeypatch.setattr(adapters, "make_embedder", lambda cfg: embedder)
    for module in (topic_llm, topic_runner):
        monkeypatch.setattr(module, "make_llm", lambda cfg, injected=None: injected or model)
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"data_dir: {config.data_dir}\nembedding:\n  dimensions: 8\nstudio:\n  recall:\n"
                         "    granularity: shot\n", encoding="utf-8")
    runner = CliRunner()
    base = ["--config", str(yaml_path), "topics"]
    pid = json.loads(runner.invoke(cli.app, [*base, "profiles", "create", "--name", "财经号", "--domain", "金融",
                                             "--taboo", "不荐股"]).output)["id"]
    runner.invoke(cli.app, [*base, "sources", "add", pid, "--kind", "rss", "--name", "要闻", "--url", FEED])
    discovered = runner.invoke(cli.app, [*base, "discover", pid])
    assert discovered.exit_code == 0, discovered.output
    assert json.loads(discovered.stdout)["result"]["outlined"] == 10
    registry = build_registry()

    async def call(name, arguments):
        client = MindexClient(app)
        try:
            spec = registry.get(name)
            return await spec.execute(spec.parse(arguments), ToolContext(client=client))
        finally:
            await client.aclose()

    listed = asyncio.run(call("list_topics", {"topic_profile_id": pid, "limit": 10}))
    best = listed.value["items"][0]["id"]
    coverage = asyncio.run(call("topic_coverage", {"card_id": best}))
    assert "命中" in coverage.text
    created = asyncio.run(call("create_job_from_topic", {"card_id": best}))
    job_id = created.value["job_id"]
    profile = asyncio.run(call("get_job_profile", {"job_id": job_id}))
    assert "快照与当前档案一致" in profile.text
    w, p = build_final(config, embedder, writer, job_id)
    assert w["builds"][-1]["status"] == "complete" and p["final"]
