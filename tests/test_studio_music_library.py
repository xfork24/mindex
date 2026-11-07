"""曲库（spec m3-post-production 需求 16）：授权必填、内容去重、分析与情绪标签、推荐排序、曲库提示点构建、
署名进入成片数据、引用时拒绝删除、授权过期阻塞预检、迁移幂等与旧提示点签名不变。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import create_app
from shared.migrations import migrate, user_version
from shared.storage import METADATA_STEPS, Store
from studio import music_library, studio
from studio import workspace as ws
from studio.workspace.handlers.publish import music_credits
from test_studio_ai_media import FakeImageGenerator, media, run, shot  # noqa: F401 — media 是夹具
from test_studio_beats import metronome
from test_studio_sources import setup

LICENSE = {"source": "自制", "license": "CC0", "attribution": "测试作者 - 节拍"}


class MoodWriter:
    """确定性文案模型：情绪标签固定返回 energetic（夹一个词表外的值），段落推断返回 calm。"""

    def __init__(self):
        self.calls = []

    def complete(self, instructions, text):
        self.calls.append(instructions)
        if "视频段落" in instructions:
            return '{"moods": ["calm"], "bpm_min": 70, "bpm_max": 100}'
        return '```json\n{"moods": ["energetic", "bogus"]}\n```'


@pytest.fixture
def client(media):  # noqa: F811
    writer = MoodWriter()
    with TestClient(create_app(media, writer=writer)) as client:
        yield media, client, writer


def upload(client, bpm, seconds=8.0, **fields):
    data = {**LICENSE, **fields}
    return client.post(
        "/api/studio/music-library",
        data=data,
        files={"file": (f"m{bpm}.wav", metronome(bpm, seconds), "audio/wav")},
    )


def test_import_requires_license_and_deduplicates(client):
    config, client, _ = client
    missing = client.post(
        "/api/studio/music-library",
        data={"source": "x"},
        files={"file": ("m.wav", metronome(120, 4), "audio/wav")},
    )
    assert missing.status_code == 422
    assert (
        client.post(
            "/api/studio/music-library", data=LICENSE, files={"file": ("m.txt", b"hello", "text/plain")}
        ).status_code
        == 422
    )
    first = upload(client, 120, title="节拍 120", tags="电子, 测试")
    assert first.status_code == 201, first.text
    item = first.json()
    assert item["duplicate"] is False and item["title"] == "节拍 120" and item["tags"] == ["电子", "测试"]
    assert item["analysis_status"] == "ready" and item["bpm"] == pytest.approx(120, rel=0.02)
    assert item["moods"] == ["energetic"] and item["mood_source"] == "model"  # 词表外的值被丢弃
    path = music_library.track_path(config, item["id"], ".wav")
    assert path.is_file()
    again = upload(client, 120, title="别的名字", source="别处").json()
    assert again["duplicate"] is True and again["id"] == item["id"] and again["source"] == "自制"
    assert len(client.get("/api/studio/music-library").json()["items"]) == 1
    assert client.get(f"/api/studio/music-library/{item['id']}/file").content == path.read_bytes()
    assert client.get("/api/studio/music-library/" + "0" * 64).status_code == 404


def test_update_analyze_and_mood_vocabulary(client):
    _, client, writer = client
    item = upload(client, 90).json()
    url = f"/api/studio/music-library/{item['id']}"
    assert client.put(url, json={"moods": ["happy", "nope"]}).status_code == 422
    assert client.put(url, json={"expires_at": "2026-13-01"}).status_code == 422
    assert client.put(url, json={"attribution": ""}).status_code == 422
    changed = client.put(url, json={"moods": ["calm", "warm"], "expires_at": "2000-01-01"}).json()
    assert changed["moods"] == ["calm", "warm"] and changed["expired"] is True
    # 只重新分析节奏：保留人工修改的情绪标签；不用模型时按节奏特征推断。
    kept = client.post(f"{url}/analyze", json={"retag": False}).json()
    assert kept["moods"] == ["calm", "warm"] and kept["mood_source"] is None
    calls = len(writer.calls)
    retagged = client.post(f"{url}/analyze", json={"ai": False}).json()
    assert retagged["mood_source"] == "features" and len(writer.calls) == calls
    assert set(retagged["moods"]) <= set(music_library.MOOD_KEYS)
    assert client.put(url, json={"expires_at": None}).json()["expired"] is False


def test_recommend_by_mood_bpm_and_license(client):
    _, client, _ = client
    slow = upload(client, 80, seconds=6).json()
    fast = upload(client, 150, seconds=12).json()
    client.put(f"/api/studio/music-library/{slow['id']}", json={"moods": ["calm"]})
    client.put(f"/api/studio/music-library/{fast['id']}", json={"moods": ["energetic"]})
    result = client.post(
        "/api/studio/music-library/recommend",
        json={"text": "健身运动，燃起来的比赛", "seconds": 10, "ai": False},
    ).json()
    assert result["source"] == "keywords" and result["moods"][0] == "energetic"
    assert [item["id"] for item in result["items"]] == [fast["id"], slow["id"]]
    assert result["items"][0]["reasons"]
    # 模型推断（calm）时慢歌在前。
    by_model = client.post("/api/studio/music-library/recommend", json={"text": "夜晚的城市"}).json()
    assert by_model["source"] == "model" and by_model["items"][0]["id"] == slow["id"]
    # 授权过期的曲目不参与推荐。
    client.put(f"/api/studio/music-library/{slow['id']}", json={"expires_at": "2000-01-01"})
    again = client.post("/api/studio/music-library/recommend", json={"text": "夜晚的城市"}).json()
    assert [item["id"] for item in again["items"]] == [fast["id"]]
    assert client.post("/api/studio/music-library/recommend", json={}).status_code == 422


def test_library_cue_model_and_identity_compat():
    cue = shot("a", "开场。", music={"kind": "library", "track_id": "a" * 64}).model_dump()
    assert cue["music"]["track_id"] == "a" * 64
    assert ws.stages.shot_stages(cue) == ("recall", "tts", "music", "select", "render")
    generated = shot("a", "开场。", music={"kind": "generate", "prompt": "轻快"}).model_dump()["music"]
    assert "track_id" not in generated
    assert ws.music.cue_identity(generated) == {key: generated.get(key) for key in ws.music.CUE_FIELDS}
    assert ws.music.cue_identity(cue["music"])["track_id"] == "a" * 64
    with pytest.raises(ValueError, match="track_id"):
        shot("a", "开场。", music={"kind": "library"})
    with pytest.raises(ValueError, match="只有曲库音乐"):
        shot("a", "开场。", music={"kind": "generate", "prompt": "x", "track_id": "a" * 64})


def test_library_music_build_credits_and_protection(client):
    config, client, _ = client
    track = upload(client, 120, seconds=4).json()
    shots = [
        shot(
            "a",
            "红色开场。",
            source={"kind": "aigc_image"},
            music={"kind": "library", "track_id": track["id"], "volume": 0.4},
        ),
        shot("b", "蓝色结尾。", source={"kind": "aigc_image"}),
    ]
    job_id, w, p = setup(config, shots)
    # 段落推荐：按提示点分镜取本段旁白与预估时长。
    text, seconds = music_library.segment_text(config, job_id, p["id"], "a")
    assert text == "红色开场。\n蓝色结尾。" and seconds == 2
    w, p = run(config, job_id, w, p, images=FakeImageGenerator())
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    music = ws.version(w, p["shots"]["a"]["music"])
    assert music["data"]["attribution"] == LICENSE["attribution"] and music["parameters"] == {}
    assert (
        ws.file_path(config, job_id, music, "music").read_bytes()
        == music_library.track_path(config, track["id"], ".wav").read_bytes()
    )
    final = ws.version(w, p["final"])
    report = final["data"]["music"]
    assert report[0]["kind"] == "library" and report[0]["shots"] == ["a", "b"]
    assert music_credits(final) == [LICENSE["attribution"]]
    # 被分镜选用时拒绝删除。
    response = client.delete(f"/api/studio/music-library/{track['id']}")
    assert response.status_code == 409 and "选用" in response.json()["detail"]
    # 授权过期：即使已有背景音乐版本也阻塞预检。
    client.put(f"/api/studio/music-library/{track['id']}", json={"expires_at": "2000-01-01"})
    checked = ws.preflight(config, job_id, w, p)
    node = next(n for n in checked["nodes"] if n["key"] == "music:a")
    assert node["action"] == "blocked" and "过期" in node["reason"]
    # 换成生成音乐后可以删除；曲目文件随之删除。
    ws.set_board(config, job_id, w, p, [shot("a", "红色开场。", source={"kind": "aigc_image"}), shots[1]])
    ws.persist(config, job_id, w)
    assert client.delete(f"/api/studio/music-library/{track['id']}").status_code == 200
    assert not music_library.track_path(config, track["id"], ".wav").exists()


def test_preflight_blocks_missing_track(media):  # noqa: F811
    job_id, w, p = setup(
        media,
        [shot("a", "开场。", source={"kind": "aigc_image"}, music={"kind": "library", "track_id": "b" * 64})],
    )
    node = next(n for n in ws.preflight(media, job_id, w, p)["nodes"] if n["key"] == "music:a")
    assert node["action"] == "blocked" and "找不到" in node["reason"]


def test_music_tracks_migration_is_idempotent(tmp_path):
    store = Store(tmp_path)
    with store.connect() as db:
        assert user_version(db) == len(METADATA_STEPS)
        assert migrate(db, METADATA_STEPS) == len(METADATA_STEPS)
        columns = {row[1] for row in db.execute("PRAGMA table_info(music_tracks)")}
    assert {"id", "attribution", "expires_at", "beats", "moods", "analysis_status"} <= columns
    Store(tmp_path)  # 再次打开不报错
    assert studio.MEDIA_ID == music_library.TRACK_ID
