"""选题挖掘 · 领域档案：迁移、REST、CLI、智能助手工具与写作约束。"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import cli
from agent.loopback import MindexClient
from agent.registry import ToolContext
from agent.tools import build_registry
from api import create_app
from shared import locks, writing_profile
from shared.migrations import user_version
from shared.storage import METADATA_STEPS, Store
from topics import profiles
from topics.models import ProfileBody

TABLES = {"topic_profiles", "topic_sources", "topic_items", "topic_runs", "topic_cards", "topic_gaps"}
BODY = {
    "name": "财经号",
    "domain": "金融",
    "subtopics": ["宏观", "宏观", " A股 "],
    "audience": "上班族",
    "positioning": "大白话讲财经",
    "taboos": ["不荐股"],
    "keywords": ["降息"],
}


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as c:
        yield c


def run_tool(app, name, arguments, page=None):
    registry = build_registry()

    async def go():
        client = MindexClient(app)
        try:
            spec = registry.get(name)
            args = spec.parse(arguments)
            preview = (
                await spec.preview(args, ToolContext(client=client, page=page or {}))
                if spec.preview
                else None
            )
            out = await spec.execute(args, ToolContext(client=client, page=page or {}))
            return preview, out
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_migration_v3_creates_topic_tables(tmp_path):
    store = Store(tmp_path)
    with store.connect() as db:
        assert user_version(db) == len(METADATA_STEPS) >= 3
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names
    Store(tmp_path)  # 重复打开不重跑
    assert "topics" in locks.ORDER and locks.ORDER[-1] == "topics"
    assert locks.path(tmp_path, "topics") == tmp_path / "locks" / "topics.lock"


def test_profile_rest_crud_and_validation(client):
    created = client.post("/api/topics/profiles", json=BODY)
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["id"].startswith("tp_") and item["subtopics"] == ["宏观", "A股"]
    assert item["weights"] == {
        "timeliness": 0.25,
        "audience": 0.25,
        "coverage": 0.25,
        "novelty": 0.15,
        "cost": 0.1,
    }
    assert item["schedule"] == {"enabled": False, "time": "08:00", "count": 10}
    pid = item["id"]
    assert client.get("/api/topics/profiles").json()["items"][0]["id"] == pid
    # 权重和不为 1、未知字段、非法时刻都拒绝
    bad = dict(BODY, weights={"timeliness": 0.5, "audience": 0.5, "coverage": 0.5, "novelty": 0, "cost": 0})
    assert client.post("/api/topics/profiles", json=bad).status_code == 422
    assert client.post("/api/topics/profiles", json={**BODY, "extra": 1}).status_code == 422
    assert (
        client.patch(f"/api/topics/profiles/{pid}", json={"schedule": {"time": "25:00"}}).status_code == 422
    )
    # 部分修改：改名称与定时，不影响写作哈希；改禁区影响
    before = item["hash"]
    patched = client.patch(
        f"/api/topics/profiles/{pid}",
        json={"name": "财经号2", "schedule": {"enabled": True, "time": "07:30", "count": 5}},
    ).json()
    assert patched["name"] == "财经号2" and patched["schedule"]["enabled"] and patched["hash"] == before
    patched = client.patch(f"/api/topics/profiles/{pid}", json={"taboos": ["不荐股", "不谈加密货币"]}).json()
    assert patched["hash"] != before and patched["domain"] == "金融"
    assert client.get("/api/topics/profiles/tp_000000000000").status_code == 404
    assert client.get("/api/topics/profiles/../x").status_code == 404
    assert client.delete(f"/api/topics/profiles/{pid}").json()["deleted"] == pid
    assert client.get(f"/api/topics/profiles/{pid}").status_code == 404


def test_coverage_profile_settings_rest_and_agent(config):
    app = create_app(config)
    with TestClient(app) as client:
        config.topics.coverage.min_similarity = 0.45
        payload = client.get("/api/topics/profiles").json()
        assert payload["coverage_defaults"]["min_similarity"] == 0.45
        response = client.post(
            "/api/topics/profiles",
            json={**BODY, "coverage": {"min_similarity": 0.6, "limit": 5, "min_duration": 2}},
        )
        assert response.status_code == 201, response.text
        profile = response.json()
        pid = profile["id"]
        for value in ({"min_similarity": 1.1}, {"min_similarity": -1}, {"limit": 11}, {"min_duration": 0}):
            assert client.patch(f"/api/topics/profiles/{pid}", json={"coverage": value}).status_code == 422
        assert client.get(f"/api/topics/profiles/{pid}").json()["coverage"] == {
            "min_similarity": 0.6,
            "limit": 5,
            "min_duration": 2,
        }
        # 工具调用经进程内回环访问应用：必须持有打开的应用上下文（lifespan 退出即关闭后端）。
        preview, output = run_tool(
            app, "save_topic_profile", {"topic_profile_id": pid, "coverage": {"min_similarity": 0.7}}
        )
        assert output.value["coverage"]["min_similarity"] == 0.7
        assert any(change["label"] == "素材覆盖核验" for change in preview.changes)
        reset = client.patch(f"/api/topics/profiles/{pid}", json={"coverage": {}}).json()
        assert all(v is None for v in reset["coverage"].values())
    assert profiles.get_profile(Store(config.data_dir), pid)["hash"] == profile["hash"]


def test_delete_cascades_and_refuses_running(config):
    store = Store(config.data_dir)
    pid = profiles.create_profile(store, ProfileBody(name="历史号", domain="历史"))["id"]
    other = profiles.create_profile(store, ProfileBody(name="科技号", domain="科技"))["id"]
    for owner in (pid, other):
        store.execute(
            "INSERT INTO topic_cards(id,profile_id,title,body,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (f"tc_{owner[3:]}", owner, "题", "{}", "t", "t"),
        )
    store.execute(
        "INSERT INTO topic_runs(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
        ("a" * 32, pid, "running", "{}", "{}"),
    )
    with pytest.raises(profiles.TopicConflict):
        profiles.delete_profile(store, pid)
    store.execute("UPDATE topic_runs SET status='complete'")
    result = profiles.delete_profile(store, pid)
    assert result["removed"]["topic_cards"] == 1 and result["removed"]["topic_runs"] == 1
    assert store.one("SELECT COUNT(*) AS n FROM topic_cards")["n"] == 1  # 其他档案不受影响


def test_delete_running_returns_409(client, config):
    pid = client.post("/api/topics/profiles", json=BODY).json()["id"]
    Store(config.data_dir).execute(
        "INSERT INTO topic_runs(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
        ("b" * 32, pid, "queued", "{}", "{}"),
    )
    response = client.delete(f"/api/topics/profiles/{pid}")
    assert response.status_code == 409 and "挖掘任务" in response.json()["detail"]


def test_writer_block_and_snapshot():
    body = ProfileBody(**BODY)
    snapshot = profiles.snapshot_body(body)
    assert set(snapshot) == set(profiles.WRITING_KEYS)
    block = writing_profile.writer_block(snapshot)
    assert (
        "领域：金融（子方向：宏观、A股）" in block
        and "目标受众：上班族" in block
        and "禁区（不得出现）：不荐股" in block
    )
    assert writing_profile.writer_block({}) == ""
    assert profiles.profile_hash(snapshot) == profiles.profile_hash(dict(snapshot))


def test_cli_profiles(config, tmp_path):
    runner = CliRunner()
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(f"data_dir: {config.data_dir}\n", encoding="utf-8")
    base = ["--config", str(yaml_path), "topics", "profiles"]
    result = runner.invoke(
        cli.app,
        [
            *base,
            "create",
            "--name",
            "科技号",
            "--domain",
            "科技",
            "--taboo",
            "不黑产品",
            "--schedule",
            "--schedule-time",
            "09:15",
        ],
    )
    assert result.exit_code == 0, result.output
    item = json.loads(result.output)
    assert item["taboos"] == ["不黑产品"] and item["schedule"] == {
        "enabled": True,
        "time": "09:15",
        "count": 10,
    }
    file = tmp_path / "p.json"
    file.write_text(json.dumps({"audience": "极客"}), encoding="utf-8")
    result = runner.invoke(
        cli.app, [*base, "update", item["id"], "--file", str(file), "--schedule-count", "3"]
    )
    assert result.exit_code == 0, result.output
    updated = json.loads(result.output)
    assert (
        updated["audience"] == "极客"
        and updated["schedule"]["count"] == 3
        and updated["schedule"]["time"] == "09:15"
    )
    listed = json.loads(runner.invoke(cli.app, [*base, "list"]).output)
    assert [p["id"] for p in listed["items"]] == [item["id"]]
    missing = runner.invoke(cli.app, [*base, "show", "tp_000000000000"])
    assert missing.exit_code == 1 and "找不到领域档案" in missing.output
    assert runner.invoke(cli.app, [*base, "delete", item["id"]]).exit_code != 0  # 需要 --yes
    assert runner.invoke(cli.app, [*base, "delete", item["id"], "--yes"]).exit_code == 0
    assert json.loads(runner.invoke(cli.app, [*base, "list"]).output)["items"] == []


def test_agent_profile_tools(config):
    app = create_app(config)
    _, out = run_tool(app, "list_topic_profiles", {})
    assert out.value["items"] == [] and "save_topic_profile" in out.text
    preview, out = run_tool(
        app, "save_topic_profile", {"name": "财经号", "domain": "金融", "taboos": ["不荐股"]}
    )
    assert preview.summary == "新建领域档案" and out.mutation == {
        "domain": "topics",
        "profile_id": out.value["id"],
    }
    assert out.focus["page"] == "topics" and out.focus["profile_id"] == out.value["id"]
    pid = out.value["id"]
    preview, out = run_tool(
        app, "save_topic_profile", {"topic_profile_id": pid, "taboos": ["不荐股", "不谈币"]}
    )
    assert any("档案已更新" in w for w in preview.warnings) and out.value["taboos"] == ["不荐股", "不谈币"]
    _, out = run_tool(app, "list_topic_profiles", {})
    assert pid in out.text and "未开启定时" in out.text
    registry = build_registry()
    assert registry.get("delete_topic_profile").risk == "danger"
    with pytest.raises(Exception, match="name 与 domain"):
        registry.get("save_topic_profile").parse({"name": "缺领域"})
    _, out = run_tool(app, "delete_topic_profile", {"topic_profile_id": pid})
    assert out.value["deleted"] == pid


def test_topics_page_served_with_tabs_in_order(client):
    for url in ("/studio/topics", "/studio/topics/"):
        page = client.get(url)
        assert page.status_code == 200 and 'id="profile-form"' in page.text
    for url in ("/", "/studio", "/studio/templates", "/studio/topics"):
        html = client.get(url).text
        tabs = [
            html.index(f'href="{href}"', html.index('class="studio-tabs"'))
            for href in ("/", "/studio/topics", "/studio/templates")
        ]
        assert tabs == sorted(tabs), url
