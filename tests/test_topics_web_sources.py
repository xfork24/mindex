"""选题挖掘 · 热榜接口与 Brave 搜索：字段映射、DailyHotApi 预设、查询参数、密钥、错误码与查询上限。

全部请求经 ``httpx.MockTransport`` 拦截，不访问网络。
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from api import create_app
from shared.storage import Store
from topics import datasources, profiles
from topics.models import ProfileBody
from topics.sources import hotlist
from topics.sources.base import SourceError
from topics_fixtures import install

NOW_MS = int(time.time() * 1000)
DAILYHOT = {
    "code": 200,
    "name": "weibo",
    "title": "微博",
    "type": "热搜榜",
    "data": [
        {
            "id": "1",
            "title": "红色热搜",
            "desc": "#红色热搜#",
            "hot": 123456,
            "timestamp": NOW_MS,
            "url": "https://s.weibo.com/weibo?q=red",
            "mobileUrl": "https://m.weibo.cn/x",
        },
        {
            "id": "2",
            "title": "蓝色热搜",
            "desc": "",
            "hot": None,
            "timestamp": None,
            "url": "javascript:alert(1)",
        },
    ],
}
CUSTOM = {
    "result": {
        "list": [
            {
                "name": "绿色榜首",
                "link": "https://hot.example.com/1",
                "score": "12.5万",
                "time": "2026-10-26T08:00:00+08:00",
            },
            {"name": "第二", "link": "https://hot.example.com/2", "score": 3, "time": 1761436800},
        ]
    }
}
NEWS = {
    "type": "news",
    "results": [
        {
            "title": "央行宣布降息",
            "url": "https://news.example.com/1",
            "description": "<strong>降息</strong> 25 个基点",
            "page_age": "2026-10-25T08:00:00",
        }
    ],
}
WEB = {
    "web": {
        "results": [{"title": "芯片发布会", "url": "https://tech.example.com/chip", "description": "发布会"}]
    }
}


def test_hot_values():
    assert (
        hotlist.hot_value("12.5万") == 125000
        and hotlist.hot_value("3亿") == 3e8
        and hotlist.hot_value(7) == 7
    )
    assert (
        hotlist.hot_value("1,234") == 1234
        and hotlist.hot_value("热") is None
        and hotlist.hot_value(True) is None
    )
    assert (
        hotlist.time_value(1761436800)
        == hotlist.time_value(1761436800000)
        == hotlist.time_value("1761436800")
    )
    assert hotlist.time_value("2026-10-26T08:00:00+08:00").startswith("2026-10-26T08:00:00")
    assert hotlist.time_value("不是时间") is None and hotlist.time_value(0) is None


def test_hotlist_config_validation():
    with pytest.raises(ValueError, match="通用热榜需要 url"):
        hotlist.HotlistConfig()
    with pytest.raises(ValueError, match="base_url"):
        hotlist.HotlistConfig(preset="dailyhot", boards=["weibo"])
    with pytest.raises(ValueError, match="至少选择一个榜单"):
        hotlist.HotlistConfig(preset="dailyhot", base_url="http://127.0.0.1:6688")
    with pytest.raises(ValueError, match="榜单名不合法"):
        hotlist.HotlistConfig(preset="dailyhot", base_url="http://127.0.0.1:6688", boards=["../admin"])


@pytest.fixture
def store_profile(config):
    store = Store(config.data_dir)
    body = ProfileBody(
        name="财经号", domain="金融", subtopics=["宏观", "股市"], keywords=[], region="cn", language="zh-hans"
    )
    return store, profiles.create_profile(store, body)["id"]


def test_dailyhot_and_custom_hotlists(config, store_profile, monkeypatch):
    store, pid = store_profile
    routes = install(
        monkeypatch,
        {
            "http://hot.local:6688/weibo": DAILYHOT,
            "http://hot.local:6688/zhihu": httpx.Response(500),
            "https://api.example.com/hot": CUSTOM,
            "https://api.example.com/bad": {"result": {"list": "x"}},
            "https://api.example.com/fields": {"data": [{"foo": "bar"}]},
        },
    )
    daily = datasources.create_source(
        store,
        pid,
        datasources.SourceBody(
            kind="hotlist",
            name="今日热榜",
            config={
                "preset": "dailyhot",
                "base_url": "http://hot.local:6688/",
                "boards": ["weibo", "zhihu"],
                "limit": 5,
            },
        ),
    )
    result = datasources.try_source(config, store, daily["id"])
    assert result["ok"] and [i["title"] for i in result["items"]] == [
        "红色热搜",
        "蓝色热搜",
    ]  # 一个榜单失败不影响其他
    first, second = result["items"]
    assert first["hot"] == 123456 and first["url"].startswith("https://s.weibo.com") and first["published_at"]
    assert second["url"] is None and second["published_at"] is None  # 非 http 链接丢弃
    weibo = next(r for r in routes.requests if r.url.path == "/weibo")
    assert weibo.url.params["limit"] == "5"
    custom = datasources.create_source(
        store,
        pid,
        datasources.SourceBody(
            kind="hotlist",
            name="自定义",
            config={
                "url": "https://api.example.com/hot",
                "items_path": "result.list",
                "fields": {"title": "name", "desc": None, "url": "link", "hot": "score", "time": "time"},
            },
        ),
    )
    items = datasources.try_source(config, store, custom["id"])["items"]
    assert [(i["title"], i["hot"]) for i in items] == [("绿色榜首", 125000), ("第二", 3)]
    assert items[0]["published_at"].startswith("2026-10-26T08:00:00")
    bad = datasources.create_source(
        store,
        pid,
        datasources.SourceBody(
            kind="hotlist",
            name="坏路径",
            config={"url": "https://api.example.com/bad", "items_path": "result.list"},
        ),
    )
    assert "不是条目数组" in datasources.try_source(config, store, bad["id"])["error"]
    fields = datasources.create_source(
        store,
        pid,
        datasources.SourceBody(
            kind="hotlist", name="坏字段", config={"url": "https://api.example.com/fields"}
        ),
    )
    assert "没有找到标题字段 title" in datasources.try_source(config, store, fields["id"])["error"]
    down = datasources.create_source(
        store,
        pid,
        datasources.SourceBody(
            kind="hotlist",
            name="全挂",
            config={"preset": "dailyhot", "base_url": "http://hot.local:6688", "boards": ["zhihu"]},
        ),
    )
    assert "HTTP 500" in datasources.try_source(config, store, down["id"])["error"]


def test_brave_queries_params_errors_and_budget(config, store_profile, monkeypatch):
    store, pid = store_profile
    calls = []

    def news(request):
        calls.append(request)
        token = request.headers.get("x-subscription-token")
        if token == "bad-key":
            return httpx.Response(401, json={"error": "unauthorized"})
        if token == "slow-key":
            return httpx.Response(429, json={})
        return httpx.Response(200, json=NEWS)

    install(
        monkeypatch,
        {
            "https://api.search.brave.com/res/v1/news/search": news,
            "https://api.search.brave.com/res/v1/web/search": WEB,
        },
    )
    with pytest.raises(ValueError, match="需要Brave Search API Key"):
        datasources.create_source(store, pid, datasources.SourceBody(kind="brave", name="搜索"))
    brave = datasources.create_source(
        store, pid, datasources.SourceBody(kind="brave", name="搜索", secret="good-key-1234")
    )
    result = datasources.try_source(config, store, brave["id"])
    assert (
        result["ok"]
        and result["items"][0]["title"] == "央行宣布降息"
        and result["items"][0]["summary"] == "降息 25 个基点"
    )
    assert result["items"][0]["published_at"].startswith("2026-10-25T08:00:00")
    # 查询词：没有关键词时用"领域 + 子方向"；默认全球，不再继承旧档案的语言/地区
    assert [c.url.params["q"] for c in calls] == ["金融 宏观", "金融 股市"]
    params = calls[0].url.params
    assert params["freshness"] == "pd" and params["country"] == "ALL" and "search_lang" not in params
    assert params["count"] == "10" and calls[0].headers["x-subscription-token"] == "good-key-1234"
    web = datasources.create_source(
        store,
        pid,
        datasources.SourceBody(
            kind="brave",
            name="网页",
            secret="good-key-1234",
            config={
                "endpoint": "web",
                "queries": ["芯片", "芯片 "],
                "freshness": "pw",
                "country": "us",
                "search_lang": "en",
            },
        ),
    )
    assert (
        datasources.try_source(config, store, web["id"])["items"][0]["url"] == "https://tech.example.com/chip"
    )
    # 错误码给出中文原因
    datasources.update_source(store, brave["id"], datasources.SourceChanges(secret="bad-key"))
    assert "密钥无效或没有权限（HTTP 401）" in datasources.try_source(config, store, brave["id"])["error"]
    datasources.update_source(store, brave["id"], datasources.SourceChanges(secret="slow-key"))
    assert "额度已用完（HTTP 429）" in datasources.try_source(config, store, brave["id"])["error"]
    with pytest.raises(ValueError, match="需要Brave Search API Key"):
        datasources.update_source(store, brave["id"], datasources.SourceChanges(secret=""))
    # 查询上限：一次抓取里所有 Brave 来源合计不超过 max_queries
    datasources.update_source(store, brave["id"], datasources.SourceChanges(secret="good-key-1234"))
    config.topics.brave.max_queries = 2
    calls.clear()
    fetched = datasources.fetch_profile(config, store, pid)
    by_name = {s["name"]: s for s in fetched["sources"]}
    assert len(calls) == 2 and by_name["搜索"]["count"] == 2
    assert "Brave 查询上限" in by_name["网页"]["error"]


def test_web_sources_over_rest(config, monkeypatch):
    install(monkeypatch, {"http://hot.local:6688/weibo": DAILYHOT})
    with TestClient(create_app(config)) as client:
        pid = client.post("/api/topics/profiles", json={"name": "号", "domain": "综合"}).json()["id"]
        kinds = {k["kind"]: k for k in client.get(f"/api/topics/profiles/{pid}/sources").json()["kinds"]}
        assert kinds["brave"]["secret"] == "Brave Search API Key" and kinds["brave"]["third_party"]
        assert kinds["hotlist"]["schema"]["properties"]["preset"]["enum"] == ["custom", "dailyhot"]
        created = client.post(
            f"/api/topics/profiles/{pid}/sources",
            json={
                "kind": "hotlist",
                "name": "热榜",
                "config": {"preset": "dailyhot", "base_url": "http://hot.local:6688", "boards": ["weibo"]},
            },
        )
        assert created.status_code == 201, created.text
        assert client.post(f"/api/topics/profiles/{pid}/fetch").json()["new"] == 2
        missing = client.post(f"/api/topics/profiles/{pid}/sources", json={"kind": "brave", "name": "搜索"})
        assert missing.status_code == 422 and "API Key" in missing.json()["detail"]


def test_source_error_is_value_error():
    assert issubclass(SourceError, ValueError)
