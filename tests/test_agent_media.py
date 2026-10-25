"""工具结果附带的媒体（Task 16）：各工具的媒体列表、只含本站地址且真实可加载、折叠默认值。"""

from __future__ import annotations

import asyncio
import time

import pytest
from agent_helpers import new_session, send, wait_idle
from agent_scenarios import build_world
from fastapi.testclient import TestClient

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.agent.tools.media import clean, item, safe

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


@pytest.fixture
def built(config, video_file):
    world = build_world(config, video_file)
    llm = ScriptedLlm()
    app = world.app(llm)
    with TestClient(app) as client:
        data = client.get(f"/api/studio/jobs/{world.job}/workspace").json()
        pid = data["active_plan"]
        token = client.get(f"/api/studio/jobs/{world.job}/plans/{pid}/preflight").json()["token"]
        build = client.post(f"/api/studio/jobs/{world.job}/plans/{pid}/build", json={"token": token}).json()
        deadline = time.monotonic() + 120
        while client.get(f"/api/studio/jobs/{world.job}/builds/{build['id']}/logs").json()["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        yield world, app, client, llm, build["id"]


def run(app, name, args):
    spec = build_registry().get(name)

    async def go():
        client = MindexClient(app)
        try:
            return await spec.execute(spec.parse(args), ToolContext(client=client))
        finally:
            await client.aclose()

    return asyncio.run(go())


def assert_loadable(client, media):
    for entry in media:
        assert safe(entry["url"]), entry
        response = client.get(entry["url"])
        assert response.status_code == 200, entry
        if entry.get("playlist"):
            assert response.json()["items"], entry
        if entry.get("poster"):
            assert client.get(entry["poster"]).status_code == 200, entry


def test_safe_urls():
    for bad in ("https://evil.example/x.png", "//evil/x", "/api/../etc/passwd", "javascript:alert(1)",
                "/assets/shared/x.png", "/api//evil", "", None):
        assert not safe(bad), bad
    assert safe("/api/studio/jobs/x/artifacts/versions/abc/audio.wav")
    assert item("image", "https://x/y.png", "外链") is None
    assert item("video", "/api/a", "x", poster="https://x/p.jpg") is None
    duplicated = [item("image", "/api/a", "a"), item("image", "/api/a", "a2"), None,
                  {"kind": "image", "url": "http://x", "title": "x", "collapsed": False}]
    assert clean(duplicated) == [item("image", "/api/a", "a")]


def test_studio_tool_media(built):
    world, app, client, _, build_id = built
    shot = run(app, "resolve_shots", {"job_id": world.job, "refs": ["#1"]}).media
    kinds = [(m["kind"], m["collapsed"], bool(m.get("playlist"))) for m in shot]
    assert ("audio", True, False) in kinds  # 配音默认折叠
    assert ("video", True, False) in kinds  # 单镜渲染默认折叠
    assert ("video", False, True) in kinds  # 召回候选：缩略图网格，点击播放
    assert all(m.get("poster") for m in shot if m.get("playlist"))
    assert_loadable(client, shot)

    final = run(app, "get_workspace_summary", {"job_id": world.job}).media
    assert len(final) == 1 and final[0]["kind"] == "video" and final[0]["collapsed"] is False
    assert final[0]["title"].startswith("成片")
    assert_loadable(client, final)
    assert run(app, "get_build_status", {"job_id": world.job, "build_id": build_id}).media == final

    versions = run(app, "list_versions", {"job_id": world.job, "stage": "tts"}).media
    assert versions and {m["kind"] for m in versions} == {"audio"}
    composed = run(app, "list_versions", {"job_id": world.job, "stage": "compose"}).media
    assert composed and composed[0]["collapsed"] is False

    summary = asyncio.run(app.state.agent_runtime.watcher._summary(MindexClient(app), world.job, build_id))
    assert summary["media"] == final


def test_library_tool_media(built):
    _, app, client, _, _ = built
    search = run(app, "search_media", {"text": "绿色 画面", "limit": 3}).media
    assert search and all(m["kind"] == "video" and m["playlist"] and m["poster"] and not m["collapsed"]
                          for m in search)
    assert all(m["url"].startswith("/api/playlists/") for m in search)
    assert_loadable(client, search)
    sources = run(app, "list_sources", {}).media
    assert sources and sources[0]["url"].endswith("/playlist") and sources[0]["poster"]
    assert_loadable(client, sources)
    assert run(app, "list_persons", {}).media == []  # 没有参考图


def test_media_in_tool_result_meta(built):
    world, _, client, llm, _ = built
    llm.extend([call("resolve_shots", job_id=world.job, refs=["#2"])], [say("好")])
    sid = new_session(client)
    send(client, sid, "看第二镜", context={"page": "studio", "job_id": world.job})
    events = wait_idle(client, sid)
    result = next(e for e in events if e["type"] == "tool/result")
    media = result["data"]["meta"]["media"]
    assert media and all(safe(m["url"]) for m in media)
    # 媒体只进界面元数据，不进模型可见的工具文本。
    assert "/artifacts/" not in result["data"]["message"]["content"][0]["text"]
