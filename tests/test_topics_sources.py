"""选题挖掘 · 数据源：RSS/Atom 解析、安全限制、去重入库、REST、CLI、智能助手工具与本地来源。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from topics_fixtures import ATOM, PUB_1, PUB_2, RDF, RSS, XXE, analyzed_library, install
from typer.testing import CliRunner

from mindex import cli
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.shared.storage import Store
from mindex.topics import datasources, items, profiles
from mindex.topics.models import ProfileBody
from mindex.topics.sources import base
from mindex.topics.sources.base import Item, SourceError
from mindex.topics.sources.rss import parse_feed

FEED = "https://news.example.com/feed.xml"


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as c:
        yield c


def profile(client) -> str:
    return client.post("/api/topics/profiles", json={"name": "财经号", "domain": "金融"}).json()["id"]


def test_parse_rss_atom_rdf():
    rss = parse_feed(RSS)
    assert [i.title for i in rss] == ["央行宣布降息 25 个基点", "红色消费品牌发布财报"]  # 去标签，空标题跳过
    assert rss[0].summary == "央行今日宣布下调政策利率，市场反应积极。"
    assert rss[0].published_at == PUB_1.isoformat() and rss[1].published_at == PUB_2.isoformat()
    atom = parse_feed(ATOM)
    assert atom[0].url == "https://tech.example.com/chip" and atom[0].published_at.startswith("2026-10-24T09:30:00")
    rdf = parse_feed(RDF)
    assert rdf[0].title == "考古新发现" and rdf[0].url == "https://history.example.com/x"
    assert len(parse_feed(RSS, limit=1)) == 1
    with pytest.raises(SourceError, match="DOCTYPE"):
        parse_feed(XXE)
    with pytest.raises(SourceError, match="不是有效的 XML"):
        parse_feed(b"<rss><channel>")
    with pytest.raises(SourceError, match="不是 RSS 或 Atom"):
        parse_feed(b"<html><body/></html>")


def test_http_limits(config, monkeypatch):
    config.topics.fetch.max_bytes = 2048
    install(monkeypatch, {
        "https://big.example.com/": b"x" * 5000,
        "https://slow.example.com/": lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        "https://redirect.example.com/": httpx.Response(302, headers={"location": "https://news.example.com/ok"}),
        "https://news.example.com/ok": b"ok",
    })
    with pytest.raises(SourceError, match="超过 2 KB"):
        base.http_get(config, "https://big.example.com/")
    with pytest.raises(SourceError, match="超时"):
        base.http_get(config, "https://slow.example.com/")
    with pytest.raises(SourceError, match="HTTP 404"):
        base.http_get(config, "https://missing.example.com/")
    for bad in ("file:///etc/passwd", "ftp://x/y", "javascript:alert(1)", "//host/x"):
        with pytest.raises(SourceError, match="http:// 或 https://"):
            base.http_get(config, bad)
    assert base.http_get(config, "https://redirect.example.com/")[2] == b"ok"


def test_dedup_keys_and_store(config):
    store = Store(config.data_dir)
    pid = profiles.create_profile(store, ProfileBody(name="财经号", domain="金融"))["id"]
    a = Item(title="央行 降息！", url="https://News.Example.com/a/1/#top")
    b = Item(title="央行降息", url="https://news.example.com/a/1")
    c = Item(title="央行降息")
    d = Item(title="央行，降息", extra={"dedup": "local:source:x"})
    assert items.dedup_key(a) == items.dedup_key(b) == "url:https://news.example.com/a/1"
    assert items.dedup_key(c) == "title:央行降息" and items.dedup_key(d) == "local:source:x"
    assert items.store_items(store, pid, "ts_000000000001", [a, b, c]) == 2
    assert items.store_items(store, pid, "ts_000000000001", [Item(title="央行降息", hot=9)]) == 0
    listed = items.list_items(store, pid)
    assert len(listed) == 2 and {i["hot"] for i in listed} == {None, 9}
    # 过期清理：未被卡片引用的旧条目删除，被引用的保留
    store.execute("UPDATE topic_items SET fetched_at='2020-01-01T00:00:00+00:00'")
    kept = listed[0]["id"]
    store.execute("INSERT INTO topic_cards(id,profile_id,title,body,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                  ("tc_000000000001", pid, "题", json.dumps({"item_ids": [kept]}), "t", "t"))
    assert items.purge(store, pid, 30) == 1
    assert [i["id"] for i in items.list_items(store, pid)] == [kept]


def test_source_rest_crud_test_and_fetch(client, monkeypatch):
    routes = install(monkeypatch, {FEED: RSS, "https://tech.example.com/atom": ATOM})
    pid = profile(client)
    listed = client.get(f"/api/topics/profiles/{pid}/sources").json()
    assert listed["items"] == [] and {"rss", "local"} <= {k["kind"] for k in listed["kinds"]}
    bad = client.post(f"/api/topics/profiles/{pid}/sources", json={"kind": "rss", "name": "x", "config": {}})
    assert bad.status_code == 422 and "配置无效" in bad.json()["detail"]
    assert client.post(f"/api/topics/profiles/{pid}/sources",
                       json={"kind": "rss", "name": "x", "config": {"url": "ftp://x"}}).status_code == 422
    rss = client.post(f"/api/topics/profiles/{pid}/sources",
                      json={"kind": "rss", "name": "财经要闻", "config": {"url": FEED}, "secret": "sk-1234567890"})
    assert rss.status_code == 201, rss.text
    source = rss.json()
    assert source["has_secret"] and source["secret_mask"] == "••••7890" and "sk-" not in json.dumps(source)
    atom = client.post(f"/api/topics/profiles/{pid}/sources",
                       json={"kind": "rss", "name": "科技", "config": {"url": "https://tech.example.com/atom"}}).json()
    broken = client.post(f"/api/topics/profiles/{pid}/sources",
                         json={"kind": "rss", "name": "坏地址", "config": {"url": "https://down.example.com/"}}).json()
    # 试抓：不入库
    tried = client.post(f"/api/topics/sources/{source['id']}/test").json()
    assert tried["ok"] and tried["count"] == 2 and tried["items"][0]["title"].startswith("央行")
    assert client.get(f"/api/topics/profiles/{pid}/items").json()["items"] == []
    failed = client.post(f"/api/topics/sources/{broken['id']}/test").json()
    assert not failed["ok"] and "HTTP 404" in failed["error"]
    # 停用的来源不参与抓取；失败的来源只记错误
    client.patch(f"/api/topics/sources/{atom['id']}", json={"enabled": False})
    result = client.post(f"/api/topics/profiles/{pid}/fetch").json()
    assert result["new"] == 2 and result["errors"] == 1
    assert {s["source_id"] for s in result["sources"]} == {source["id"], broken["id"]}
    state = {s["id"]: s for s in client.get(f"/api/topics/profiles/{pid}/sources").json()["items"]}
    assert state[source["id"]]["last_count"] == 2 and state[source["id"]]["last_error"] is None
    assert "HTTP 404" in state[broken["id"]]["last_error"]
    # 指定来源抓取（含已停用的）
    only = client.post(f"/api/topics/profiles/{pid}/fetch", params={"source_id": atom["id"]}).json()
    assert only["new"] == 1 and [s["name"] for s in only["sources"]] == ["科技"]
    assert len(client.get(f"/api/topics/profiles/{pid}/items").json()["items"]) == 3
    assert client.post(f"/api/topics/profiles/{pid}/fetch", params={"source_id": "ts_000000000000"}).status_code == 404
    # 修改：config 整体替换、空串清除密钥、不能改类型
    patched = client.patch(f"/api/topics/sources/{source['id']}", json={"secret": ""}).json()
    assert not patched["has_secret"]
    assert client.patch(f"/api/topics/sources/{source['id']}", json={"kind": "local"}).status_code == 422
    assert client.get(f"/api/topics/sources/{source['id']}").json()["name"] == "财经要闻"
    assert client.delete(f"/api/topics/sources/{broken['id']}").json() == {"deleted": broken["id"]}
    assert client.get(f"/api/topics/sources/{broken['id']}").status_code == 404
    assert all(r.headers["user-agent"].startswith("Mindex-Topics") for r in routes.requests)


def test_local_source_without_and_with_library(config, video_file):
    store = Store(config.data_dir)
    pid = profiles.create_profile(store, ProfileBody(name="本地", domain="颜色"))["id"]
    local = datasources.create_source(store, pid, datasources.SourceBody(kind="local", name="本地素材库"))
    assert datasources.try_source(config, store, local["id"])["count"] == 0  # 没有活动索引
    analyzed_library(config, video_file)
    tried = datasources.try_source(config, store, local["id"])
    assert tried["ok"] and tried["count"] == 1 and tried["items"][0]["title"].startswith("素材库：")
    assert "画面词" in tried["items"][0]["summary"] or "故事线" in tried["items"][0]["summary"]
    result = datasources.fetch_profile(config, store, pid)
    assert result["new"] == 1
    assert datasources.fetch_profile(config, store, pid)["new"] == 0  # 本地素材按素材 ID 去重
    disabled = datasources.update_source(store, local["id"], datasources.SourceChanges(config={"library": False}))
    assert disabled["config"] == {"library": False}
    assert datasources.try_source(config, store, local["id"])["count"] == 0


def test_cli_sources_and_fetch(config, tmp_path, monkeypatch):
    install(monkeypatch, {FEED: RSS})
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"data_dir: {config.data_dir}\n", encoding="utf-8")
    runner = CliRunner()
    base_args = ["--config", str(yaml_path), "topics"]
    pid = json.loads(runner.invoke(cli.app, [*base_args, "profiles", "create", "--name", "财经", "--domain", "金融"]).output)["id"]
    monkeypatch.setenv("TOPIC_TEST_KEY", "secret-abcdefgh")
    added = runner.invoke(cli.app, [*base_args, "sources", "add", pid, "--kind", "rss", "--name", "要闻", "--url", FEED,
                                    "--secret-env", "TOPIC_TEST_KEY"])
    assert added.exit_code == 0, added.output
    source = json.loads(added.output)
    assert source["secret_mask"] == "••••efgh" and "secret-abc" not in added.output
    tested = runner.invoke(cli.app, [*base_args, "sources", "test", source["id"]])
    assert tested.exit_code == 0 and json.loads(tested.output)["count"] == 2
    fetched = json.loads(runner.invoke(cli.app, [*base_args, "fetch", pid]).output)
    assert fetched["new"] == 2
    listed = json.loads(runner.invoke(cli.app, [*base_args, "items", pid, "--limit", "1"]).output)
    assert len(listed["items"]) == 1
    updated = runner.invoke(cli.app, [*base_args, "sources", "update", source["id"], "--disable", "--clear-secret"])
    assert updated.exit_code == 0 and not json.loads(updated.output)["enabled"]
    bad = runner.invoke(cli.app, [*base_args, "sources", "add", pid, "--kind", "rss", "--name", "坏", "--url", "ftp://x"])
    assert bad.exit_code == 1 and "配置无效" in bad.output
    assert runner.invoke(cli.app, [*base_args, "sources", "remove", source["id"]]).exit_code != 0
    assert runner.invoke(cli.app, [*base_args, "sources", "remove", source["id"], "--yes"]).exit_code == 0
    assert json.loads(runner.invoke(cli.app, [*base_args, "sources", "list", pid]).output)["items"] == []


def run_tool(app, name, arguments):
    registry = build_registry()

    async def go():
        client = MindexClient(app)
        try:
            spec = registry.get(name)
            args = spec.parse(arguments)
            ctx = ToolContext(client=client)
            preview = await spec.preview(args, ctx) if spec.preview else None
            return preview, await spec.execute(args, ctx)
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_agent_source_tools(config, monkeypatch):
    install(monkeypatch, {FEED: RSS})
    app = create_app(config)
    _, out = run_tool(app, "save_topic_profile", {"name": "财经号", "domain": "金融"})
    pid = out.value["id"]
    preview, out = run_tool(app, "save_topic_source", {"topic_profile_id": pid, "kind": "rss", "name": "要闻",
                                                       "config": {"url": FEED}})
    assert preview.summary == "新建数据源" and preview.warnings
    sid = out.value["id"]
    assert out.mutation == {"domain": "topics", "profile_id": pid} and out.focus["section"] == "sources"
    _, out = run_tool(app, "list_topic_sources", {"topic_profile_id": pid})
    assert sid in out.text and "rss" in out.text
    _, out = run_tool(app, "test_topic_source", {"topic_source_id": sid})
    assert "试抓成功，共 2 条" in out.text and "央行宣布降息" in out.text
    preview, out = run_tool(app, "fetch_topic_items", {"topic_profile_id": pid})
    assert "新增 2 条" in out.text
    _, out = run_tool(app, "list_topic_items", {"topic_profile_id": pid, "limit": 5})
    assert "外部内容，仅供参考" in out.text and len(out.value["items"]) == 2
    preview, out = run_tool(app, "save_topic_source", {"topic_source_id": sid, "enabled": False})
    assert preview.changes[0]["before"] == "启用" and not out.value["enabled"]
    with pytest.raises(Exception, match="不能改类型"):
        build_registry().get("save_topic_source").parse({"topic_source_id": sid, "kind": "local"})
    preview, out = run_tool(app, "delete_topic_source", {"topic_source_id": sid})
    assert "要闻" in preview.changes[0]["before"] and out.value == {"deleted": sid}
