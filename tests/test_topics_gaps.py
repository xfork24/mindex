"""选题挖掘 · 素材采集清单：合并规则、状态、CSV 导出（BOM、公式防护）、REST、CLI 与智能助手工具。"""

from __future__ import annotations

import asyncio
import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from topics_fixtures import topic_embedder
from typer.testing import CliRunner

from mindex import cli
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.shared.storage import Store
from mindex.topics import cards, gaps, profiles
from mindex.topics.models import ProfileBody


@pytest.fixture
def seeded(config):
    store = Store(config.data_dir)
    pid = profiles.create_profile(store, ProfileBody(name="财经号", domain="金融"))["id"]
    ids = []
    for title in ("选题一", "选题二"):
        card_id = f"tc_{len(ids):012d}"
        store.execute("INSERT INTO topic_cards(id,profile_id,title,body,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                      (card_id, pid, title, json.dumps({"item_ids": []}), "t", "t"))
        ids.append(card_id)
    return config, store, pid, ids


def test_record_merges_and_counts_once_per_card(seeded):
    config, store, pid, (a, b) = seeded
    embedder = topic_embedder(config)
    assert gaps.record(store, pid, a, ["紫色天空", "央行发布会现场"], embedder=embedder) == 2
    assert gaps.record(store, pid, a, ["紫色天空"], embedder=embedder) == 0  # 同一卡片不重复计数
    assert gaps.record(store, pid, b, ["紫色 天空！", "紫色夜空"], embedder=embedder) == 0  # 规范化相同 / 向量相同
    listed = gaps.list_gaps(store, pid)
    assert [(g["visual"], g["count"]) for g in listed] == [("紫色天空", 2), ("央行发布会现场", 1)]
    assert [c["title"] for c in listed[0]["cards"]] == ["选题一", "选题二"]
    # 没有嵌入时按字符重合度合并
    assert gaps.record(store, pid, b, ["央行发布会的现场"], embedder=None) == 0
    assert gaps.list_gaps(store, pid)[1]["count"] == 2
    assert gaps.record(store, pid, b, ["完全不同的画面"], embedder=None) == 1
    # 已采集的不再合并新缺口
    done = gaps.update_gap(store, listed[0]["id"], gaps.GapChanges(status="done"))
    assert done["status"] == "done"
    assert gaps.record(store, pid, a, ["紫色天空"], embedder=embedder) == 1
    assert len(gaps.list_gaps(store, pid, status="done")) == 1 and len(gaps.list_gaps(store, pid, status="all")) == 4
    with pytest.raises(ValueError):
        gaps.list_gaps(store, pid, status="bogus")
    # 撤回卡片：计数减少，只属于它的缺口删除
    cards.discard(store, [b])
    remaining = {g["visual"]: g["count"] for g in gaps.list_gaps(store, pid, status="all")}
    assert remaining == {"紫色天空": 1, "央行发布会现场": 1}


def test_matcher_shares_open_gap_vectors_across_cards(seeded):
    """Matcher 跨卡片共享缺口向量：嵌入文本数 = 开放缺口 + 各卡新画面，不再随卡片数 × 历史缺口数相乘。"""
    config, store, pid, (a, b) = seeded
    embedder = topic_embedder(config)
    counted = {"texts": 0}
    inner = embedder.text

    def counting_text(texts, role="document"):
        counted["texts"] += len(texts)
        return inner(texts, role=role)

    embedder.text = counting_text
    gaps.record(store, pid, a, ["紫色天空"], embedder=embedder)  # 预置 1 条开放缺口（1 次嵌入，已持久化）
    matcher = gaps.Matcher(store, pid, embedder)
    assert matcher.record(b, ["紫色夜空"]) == 0  # 余弦合并进预置缺口（快照命中缓存，只嵌本画面 1 次）
    assert matcher.record(b, ["全新的画面"]) == 1  # 新建缺口即时进入比对集（1 次）
    assert matcher.record(a, ["全新的画面"]) == 0  # 同运行内后续卡片可合并新缺口（1 次，不重嵌快照）
    assert matcher.record(a, ["紫色 天空！"]) == 0  # 规范化标题精确匹配（1 次）
    listed = {g["visual"]: g["count"] for g in gaps.list_gaps(store, pid)}
    assert listed == {"紫色天空": 2, "全新的画面": 2}
    assert counted["texts"] == 5  # 旧行为同场景嵌入 8 次文本，且每次调用随开放缺口数线性膨胀


def test_matcher_persists_and_reuses_gap_vectors(seeded):
    """缺口向量持久化：新 Matcher 实例直接复用库内向量，开放缺口不再逐次重嵌。"""
    config, store, pid, (a, b) = seeded
    embedder = topic_embedder(config)
    counted = {"texts": 0}
    inner = embedder.text

    def counting_text(texts, role="document"):
        counted["texts"] += len(texts)
        return inner(texts, role=role)

    embedder.text = counting_text
    gaps.record(store, pid, a, ["紫色天空", "全新的画面"], embedder=embedder)
    after_seed = counted["texts"]  # 空快照：两条新画面各嵌一次
    fresh = gaps.Matcher(store, pid, embedder)  # 新实例：向量来自持久化缓存
    assert fresh.record(b, ["紫色夜空"]) == 0  # 只嵌入本画面，紫色天空不重嵌
    assert counted["texts"] == after_seed + 1
    listed = {g["visual"]: g["count"] for g in gaps.list_gaps(store, pid)}
    assert listed == {"紫色天空": 2, "全新的画面": 1}
    row = store.one("SELECT embedding IS NOT NULL AS cached FROM topic_gaps WHERE visual='紫色天空'")
    assert row["cached"] == 1


def test_matcher_reembeds_when_embedding_space_changes(seeded):
    """嵌入空间身份变化后持久化向量失效重嵌，绝不跨空间比较。"""
    config, store, pid, (a, b) = seeded
    first = topic_embedder(config)
    gaps.record(store, pid, a, ["紫色天空"], embedder=first)
    config.embedding.model = "another-embedder"
    second = topic_embedder(config)
    counted = {"texts": 0}
    inner = second.text

    def counting_text(texts, role="document"):
        counted["texts"] += len(texts)
        return inner(texts, role=role)

    second.text = counting_text
    matcher = gaps.Matcher(store, pid, second)
    assert matcher.record(b, ["紫色夜空"]) == 0  # 指纹不符触发重嵌，合并结果仍正确
    assert counted["texts"] == 2  # 快照 1 + 本画面 1
    row = store.one("SELECT embedding_space FROM topic_gaps WHERE visual='紫色天空'")
    assert row["embedding_space"]  # 已用新身份覆写


def test_export_csv(seeded):
    _, store, pid, (a, _) = seeded
    gaps.record(store, pid, a, ["=HYPERLINK(\"http://x\")", "红色画面"], embedder=None)
    data = gaps.export_csv(store, pid)
    assert data.startswith("\ufeff".encode())
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == ["画面", "出现次数", "状态", "相关选题", "更新时间"]
    assert {r[0] for r in rows[1:]} == {"'=HYPERLINK(\"http://x\")", "红色画面"}
    assert all(r[2] == "待采集" and r[3] == "选题一" for r in rows[1:])


def test_gaps_rest_cli_and_tools(seeded, tmp_path):
    config, store, pid, (a, b) = seeded
    gaps.record(store, pid, a, ["紫色天空"], embedder=None)
    gaps.record(store, pid, b, ["紫色天空", "蓝色海面"], embedder=None)
    app = create_app(config)
    with TestClient(app) as client:
        listed = client.get(f"/api/topics/profiles/{pid}/gaps").json()["items"]
        assert [(g["visual"], g["count"]) for g in listed] == [("紫色天空", 2), ("蓝色海面", 1)]
        gid = listed[0]["id"]
        assert client.patch(f"/api/topics/gaps/{gid}", json={"status": "done"}).json()["status"] == "done"
        assert client.patch(f"/api/topics/gaps/{gid}", json={"status": "bad"}).status_code == 422
        assert client.patch("/api/topics/gaps/tg_000000000000", json={"status": "done"}).status_code == 404
        exported = client.get(f"/api/topics/profiles/{pid}/gaps.csv")
        assert exported.status_code == 200 and exported.headers["content-type"].startswith("text/csv")
        assert "attachment" in exported.headers["content-disposition"] and "UTF-8''" in exported.headers["content-disposition"]
        assert exported.content.startswith("\ufeff".encode()) and "已采集" in exported.content.decode("utf-8-sig")
        assert client.get("/api/topics/profiles/tp_000000000000/gaps").status_code == 404
    runner = CliRunner()
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"data_dir: {config.data_dir}\n", encoding="utf-8")
    base = ["--config", str(yaml_path), "topics", "gaps"]
    assert [g["visual"] for g in json.loads(runner.invoke(cli.app, [*base, "list", pid]).output)["items"]] == ["蓝色海面"]
    assert json.loads(runner.invoke(cli.app, [*base, "reopen", gid]).output)["status"] == "open"
    assert json.loads(runner.invoke(cli.app, [*base, "done", gid]).output)["status"] == "done"
    target = tmp_path / "gaps.csv"
    out = json.loads(runner.invoke(cli.app, [*base, "export", pid, "--output", str(target)]).output)
    assert out["bytes"] == target.stat().st_size and target.read_bytes().startswith("\ufeff".encode())
    assert "蓝色海面" in runner.invoke(cli.app, [*base, "export", pid]).output

    registry = build_registry()

    async def call(name, arguments):
        mindex = MindexClient(app)
        try:
            spec = registry.get(name)
            args = spec.parse(arguments)
            ctx = ToolContext(client=mindex)
            if spec.preview:
                await spec.preview(args, ctx)
            return await spec.execute(args, ctx)
        finally:
            await mindex.aclose()

    out = asyncio.run(call("topic_gaps", {"topic_profile_id": pid, "status": "all"}))
    assert "紫色天空 | 出现 2 次 | 已采集" in out.text and "gaps.csv" in out.text and out.focus["section"] == "gaps"
    out = asyncio.run(call("update_topic_gap", {"gap_id": gid, "status": "open"}))
    assert out.value["status"] == "open" and out.mutation == {"domain": "topics", "profile_id": pid}
