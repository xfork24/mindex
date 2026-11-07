"""选题挖掘页浏览器回归：进程内启动隔离服务（临时数据目录、脚本化模型），再用 Playwright 驱动真实页面。

需要 node 与 Playwright（可用 MINDEX_PLAYWRIGHT_MODULE 指定模块路径，例如
desktop/node_modules/playwright-core），
缺失时跳过。桌面端同一页面的冒烟检查在 tests/desktop_smoke.cjs。
"""

from __future__ import annotations

import os
import subprocess

import pytest

from api import create_app
from test_agent_panel_ui import ROOT, playwright_available, serve

needs_browser = pytest.mark.skipif(not playwright_available(), reason="需要 node 与 Playwright")


def start(app):
    server, thread, url = serve(app)
    return server, thread, url


@pytest.fixture
def topics_server(config, monkeypatch):
    from topics_fixtures import RSS, install, topic_embedder

    install(
        monkeypatch,
        {
            "https://news.example.com/feed.xml": RSS,
            "http://hot.local:6688/weibo": {"code": 200, "data": [{"title": "红色热搜", "hot": 9}]},
        },
    )
    server, thread, url = start(create_app(config, topic_embedder(config)))
    yield url
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def discovery_server(config, video_file, monkeypatch):
    """已分析的颜色素材库 + 脚本化选题模型：挖掘、打分、覆盖与大纲都可在页面里走通。"""
    from topics_fixtures import RSS, ScriptedTopicLlm, analyzed_library, install

    embedder = analyzed_library(config, video_file)
    install(monkeypatch, {"https://news.example.com/feed.xml": RSS})
    server, thread, url = start(create_app(config, embedder, topic_llm=ScriptedTopicLlm()))
    yield url
    server.should_exit = True
    thread.join(timeout=10)


def run_scenario(url: str, scenario: str):
    env = {**os.environ, "MINDEX_TOPICS_URL": url, "MINDEX_TOPICS_SCENARIO": scenario}
    result = subprocess.run(
        ["node", "tests/topics_ui.cjs"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"topics ui {scenario} ok" in result.stdout


@needs_browser
@pytest.mark.parametrize("scenario", ["profiles", "sources"])
def test_topics_page(topics_server, scenario):
    run_scenario(topics_server, scenario)


@needs_browser
@pytest.mark.parametrize("scenario", ["cards", "job"])
def test_topics_discovery_page(discovery_server, scenario):
    run_scenario(discovery_server, scenario)


@needs_browser
def test_topics_material_scope_page(config):
    from shared.media_groups import Groups
    from shared.storage import Store, dumps

    store = Store(config.data_dir)
    for sid in ("material-a", "material-b"):
        store.execute(
            "INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)",
            (sid, sid, f"sources/{sid}.mp4", dumps({"duration": 5}), "layout"),
        )
    groups = Groups(store)
    first = groups.save("source", "选题素材组")["id"]
    groups.change_members(first, ["material-a"])
    groups.save("source", "空素材组")
    server, thread, url = start(create_app(config))
    try:
        run_scenario(url, "material_scope")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
