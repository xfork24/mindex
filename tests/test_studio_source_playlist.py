"""选片原片区间播放列表（分镜卡片静音预览）：只读、不调用 ffmpeg、按选片区间返回镜头列表，
并拒绝不适用的版本。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import create_app
from conftest import FixtureSpeaker, FixtureStoryboarder
from studio import workspace as ws
from test_studio_workspace import build, prepared, setup_workspace  # noqa: F401 — prepared 是 fixture

pytestmark = pytest.mark.slow  # 需要真实切片与构建


def test_source_playlist_matches_select_range_and_rejects_others(prepared, monkeypatch):  # noqa: F811
    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    w, p = build(config, jid, w, p, embedder)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    client = TestClient(
        create_app(config, embedder, speaker=FixtureSpeaker(config), storyboarder=FixtureStoryboarder(config))
    )
    base = f"/api/studio/jobs/{jid}"
    sid = ws.ordered_shots(w, p)[0]
    select = ws.version(w, p["shots"][sid]["select"])
    video = select["data"]["event"]["video"]

    # 只读：不得调用 ffmpeg，也不改动任务数据。
    from shared import media

    def no_ffmpeg(*_args, **_kwargs):
        raise AssertionError("source-playlist 不应调用 ffmpeg")

    monkeypatch.setattr(media, "command", no_ffmpeg)
    before = ws.path(config, jid).read_bytes()
    response = client.get(f"{base}/versions/{select['id']}/source-playlist")
    assert response.status_code == 200, response.text
    playlist = response.json()
    assert playlist["duration"] == pytest.approx(video["end"] - video["start"], abs=1e-3)
    assert playlist["items"] and all(
        item["url"].startswith("/api/admin/shots/") for item in playlist["items"]
    )
    assert ws.path(config, jid).read_bytes() == before
    # 列表里的镜头地址可直接播放。
    assert client.get(playlist["items"][0]["url"]).status_code == 200

    # 非选片版本、未知版本、其他任务的版本：4xx 中文错误。
    tts = p["shots"][sid]["tts"]
    rejected = client.get(f"{base}/versions/{tts}/source-playlist")
    assert rejected.status_code == 422 and "选片" in rejected.text
    assert client.get(f"{base}/versions/unknown/source-playlist").status_code == 422
    other, _, _ = setup_workspace(config)
    assert client.get(f"/api/studio/jobs/{other}/versions/{select['id']}/source-playlist").status_code == 422
    assert (
        client.get(f"/api/studio/jobs/missing-job/versions/{select['id']}/source-playlist").status_code == 404
    )

    # 上传片段/生成画面的选片（kind=file）：提示直接播放文件。
    w = ws.load(config, jid)
    item = ws.version(w, select["id"])
    item["data"]["event"]["video"]["kind"] = "file"
    ws.persist(config, jid, w)
    rejected = client.get(f"{base}/versions/{select['id']}/source-playlist")
    assert rejected.status_code == 422 and "直接播放" in rejected.text
    item["data"]["event"]["video"]["kind"] = "library"
    ws.persist(config, jid, w)

    # 区间内的镜头被隐藏：拒绝。
    segment = video["segment_ids"][0]
    assert client.patch(f"/api/admin/segments/{segment}", json={"hidden": True}).status_code == 200
    rejected = client.get(f"{base}/versions/{select['id']}/source-playlist")
    assert rejected.status_code == 422 and "隐藏" in rejected.text
