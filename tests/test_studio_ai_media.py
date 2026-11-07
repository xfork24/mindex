"""AI 生成图片（aigc_image / draw 阶段）、背景音乐（Shot.music / music 阶段与成片混音）、
MiniMax H3 视频生成 V2、
image-01 / music-3.0 / ComfyUI 适配器与默认档案卡。

所有生成服务都用确定性的假实现或拦截 HTTP，绝不调用真实付费接口。
"""

from __future__ import annotations

import base64
import io
import json
import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import FixtureSpeaker
from models import adapters
from models.image_generators import ImageRequest, MiniMaxImageGenerator
from models.music_generators import MiniMaxMusicGenerator, MusicRequest
from models.video_generators import MiniMaxH3VideoGenerator, VideoRequest, h3_content
from shared.media import audio_info, command
from studio import studio
from studio import workspace as ws
from studio.workspace.post import MUSIC_MIX_VERSION
from test_studio_sources import frame_count, make_image, setup

slow = pytest.mark.slow


def png_bytes(color=(30, 160, 60)) -> bytes:

    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(buffer, format="PNG")
    return buffer.getvalue()


def tone_wav(seconds=1.0, rate=48000, frequency=330) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * frequency * i / rate)))
                for i in range(int(rate * seconds))
            )
        )
    return buffer.getvalue()


class FakeImageGenerator:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return png_bytes()

    def health(self):
        return {"ready": True}


class FakeMusicGenerator:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return tone_wav(0.6), {"music_duration": 600}

    def lyrics(self, prompt, *, mode="write_full_song", lyrics=None, title=None):
        from models.music_generators import Lyrics

        return Lyrics(title=title or "测试", style_tags="Pop", lyrics=f"[Verse]\n{prompt}")

    def health(self):
        return {"ready": True}


@pytest.fixture
def media(config):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 25
    config.studio.image_generator.enabled = True
    config.studio.music_generator.enabled = True
    return config


def shot(sid, text, **extra):
    return studio.Shot(id=sid, text=text, query="夜景城市", estimated_seconds=1, **extra)


def run(config, job_id, w, p, *, images=None, music=None):
    checked = ws.preflight(config, job_id, w, p)
    bid = str(len(w["builds"]))
    w["builds"].append(
        {
            "id": bid,
            "plan_id": p["id"],
            "status": "running",
            "force": False,
            "nodes": [{**n, "status": "pending"} for n in checked["nodes"]],
        }
    )
    ws.persist(config, job_id, w)
    ws.run_build(
        config, job_id, bid, speaker=FixtureSpeaker(config), image_generator=images, music_generator=music
    )
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


# ---------------------------------------------------------------- 模型（快速）


def test_sources_and_music_dump_and_stage_sequences():
    image = shot("a", "开场。", source={"kind": "aigc_image", "prompt": " 雪山 "}).model_dump()["source"]
    assert image == {
        "kind": "aigc_image",
        "prompt": "雪山",
        "reference_id": None,
        "seed": None,
        "prompt_optimizer": False,
        "motion": "zoom-in",
        "strength": 0.3,
    }
    assert ws.SHOT_SOURCES["aigc_image"] == ("tts", "draw", "render")
    # 老 aigc 内容逐字节不变：H3 选项为 None 时不落键。
    old = shot("a", "开场。", source={"kind": "aigc", "prompt": "雨夜"}).model_dump()["source"]
    assert old == {"kind": "aigc", "prompt": "雨夜", "negative": None, "image_id": None, "seed": None}
    h3 = shot(
        "a",
        "开场。",
        source={
            "kind": "aigc",
            "mode": "reference",
            "duration": 8,
            "references": [{"kind": "image", "media_id": "a" * 64}],
        },
    ).model_dump()["source"]
    assert h3["mode"] == "reference" and h3["duration"] == 8 and "ratio" not in h3
    with pytest.raises(ValueError, match="尾帧"):
        shot("a", "开场。", source={"kind": "aigc", "mode": "last_frame"})
    with pytest.raises(ValueError, match="只有参考音频"):
        shot(
            "a",
            "开场。",
            source={
                "kind": "aigc",
                "mode": "reference",
                "references": [{"kind": "audio", "media_id": "a" * 64}],
            },
        )
    assert "music" not in shot("a", "开场。").model_dump()
    cue = shot("a", "开场。", music={"kind": "generate", "prompt": "轻快"})
    assert ws.stages.shot_stages(cue.model_dump()) == ("recall", "tts", "music", "select", "render")
    stop = shot("a", "开场。", music={"kind": "none"})
    assert ws.stages.shot_stages(stop.model_dump()) == ws.SHOT_SOURCES["recall"]
    with pytest.raises(ValueError, match="10–300"):
        shot("a", "开场。", music={"kind": "cover", "prompt": "短", "audio_id": "a" * 64})


def test_music_segments_continue_until_next_cue(media):
    shots = [
        shot("a", "一。", source={"kind": "aigc_image"}),
        shot("b", "二。", source={"kind": "aigc_image"}, music={"kind": "generate", "prompt": "轻快"}),
        shot("c", "三。", source={"kind": "aigc_image"}),
        shot("d", "四。", source={"kind": "aigc_image"}, music={"kind": "none"}),
        shot("e", "五。", source={"kind": "aigc_image"}, music={"kind": "generate", "prompt": "抒情"}),
        shot("f", "六。", source={"kind": "aigc_image"}),
    ]
    job_id, w, p = setup(media, shots)
    segments = ws.music.segments(w, p)
    assert [(s["cue"], s["shots"]) for s in segments] == [("b", ["b", "c"]), ("e", ["e", "f"])]
    belong = ws.music.shot_music(w, p)
    assert (
        "a" not in belong and belong["c"] == {"from": "b", "start": False} and belong["d"] == {"stop": True}
    )
    keys = [n["key"] for n in ws.preflight(media, job_id, w, p)["nodes"]]
    assert "music:b" in keys and "music:e" in keys and "music:c" not in keys
    assert ws.node_dependencies(w, p, "compose", None)[-2:] == ["music:b", "music:e"]


def test_compose_signature_unchanged_without_music(media):
    job_id, w, p = setup(media, [shot("a", "一。", source={"kind": "aigc_image"})])
    spec = ws.specification(media, job_id, w, p, "compose")
    assert not any(key.startswith("music:") for key in spec["inputs"])


def test_draw_preflight_and_signature(media, tmp_path):
    job_id, w, p = setup(media, [shot("a", "一。", visual="雪山日出", source={"kind": "aigc_image"})])
    spec = ws.specification(media, job_id, w, p, "draw", "a")
    assert spec["parameters"] == {"image_generator": {"backend": "minimax", "model": "image-01"}}
    assert ws.node_dependencies(w, p, "render", "a") == ["draw:a", "tts:a"]
    media.studio.image_generator.enabled = False
    nodes = {n["key"]: n for n in ws.preflight(media, job_id, w, p)["nodes"]}
    assert nodes["draw:a"]["action"] == "blocked" and "图片生成" in nodes["draw:a"]["reason"]
    assert nodes["render:a"]["action"] == "blocked"
    media.studio.image_generator.enabled = True
    ws.set_board(
        media, job_id, w, p, [shot("a", "一。", source={"kind": "aigc_image", "reference_id": "c" * 64})]
    )
    nodes = {n["key"]: n for n in ws.preflight(media, job_id, w, p)["nodes"]}
    assert nodes["draw:a"]["reason"] == ws.sources.REFERENCE_MISSING


def test_video_mode_blocked_on_non_h3_backend(media):
    media.studio.video_generator.enabled = True
    media.studio.video_generator.backend = "minimax"
    media.studio.video_generator.model = "hailuo"
    job_id, w, p = setup(
        media,
        [
            shot(
                "a",
                "一。",
                source={
                    "kind": "aigc",
                    "mode": "first_last",
                    "image_id": "a" * 64,
                    "last_image_id": "b" * 64,
                },
            )
        ],
    )
    nodes = {n["key"]: n for n in ws.preflight(media, job_id, w, p)["nodes"]}
    assert "不支持首尾帧生视频" in nodes["generate:a"]["reason"]
    media.studio.video_generator.backend = "minimax_h3"
    nodes = {n["key"]: n for n in ws.preflight(media, job_id, w, p)["nodes"]}
    assert nodes["generate:a"]["reason"] == ws.sources.REFERENCE_MISSING  # 参考图尚未上传


# ---------------------------------------------------------------- 适配器（拦截 HTTP）


def test_h3_request_body_for_each_mode(config, tmp_path):
    image = make_image(tmp_path / "a.png")
    config.studio.video_generator.backend = "minimax_h3"
    config.studio.video_generator.base_url = "https://api.minimax.cn/v1"
    generator = MiniMaxH3VideoGenerator(config)
    assert generator._url("/v2/video_generation") == "https://api.minimax.cn/v2/video_generation"
    text = generator.request_body(VideoRequest(prompt="雨夜", seconds=6.3, width=720, height=1280))
    assert text["content"] == [{"type": "text", "text": "雨夜"}]
    assert (text["model"], text["duration"], text["ratio"], text["resolution"]) == (
        "MiniMax-H3",
        6,
        "9:16",
        "768P",
    )
    first_last = generator.request_body(VideoRequest(prompt="p", image=image, last_image=image, seconds=4))
    assert [item.get("role") for item in first_last["content"]] == [None, "first_frame", "last_frame"]
    assert first_last["ratio"] == "adaptive"
    ref = generator.request_body(
        VideoRequest(
            prompt="p", references=(("image", image),), model="MiniMax-H3-Max", seconds=4, ratio="16:9"
        )
    )
    assert ref["content"][1]["role"] == "reference_image" and ref["duration"] == 5 and ref["ratio"] == "16:9"
    with pytest.raises(adapters.ModelError, match="尾帧"):
        h3_content(VideoRequest(prompt="p", mode="last_frame"))


def test_h3_submit_poll_fetch(config, monkeypatch):
    config.studio.video_generator.backend = "minimax_h3"
    config.studio.video_generator.base_url = "https://api.minimax.cn"
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if method == "POST":
            return {"task_id": "t1", "base_resp": {"status_code": 0}}
        return {
            "task": {
                "id": "t1",
                "status": "succeeded",
                "content": {"url": "https://cdn.example.invalid/v.mp4"},
            }
        }

    monkeypatch.setattr(adapters, "request", fake_request)
    generator = adapters.make_video_generator(config)
    assert generator.submit(VideoRequest(prompt="p")) == "t1"
    status = generator.poll("t1")
    assert status.state == "succeeded" and status.url.endswith("v.mp4")
    assert calls == [
        ("POST", "https://api.minimax.cn/v2/video_generation"),
        ("GET", "https://api.minimax.cn/v2/query/video_generation/t1"),
    ]


def test_minimax_image_and_music_bodies(config, monkeypatch, tmp_path):
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((url, kwargs["json"]))
        if url.endswith("/image_generation"):
            return {
                "data": {"image_base64": [base64.b64encode(png_bytes()).decode()]},
                "base_resp": {"status_code": 0},
            }
        if url.endswith("/lyrics_generation"):
            return {
                "song_title": "雨",
                "style_tags": "Pop",
                "lyrics": "[Verse]\nla",
                "base_resp": {"status_code": 0},
            }
        return {
            "data": {"audio": tone_wav(0.2).hex()},
            "extra_info": {"music_duration": 200},
            "base_resp": {"status_code": 0},
        }

    monkeypatch.setattr(adapters, "request", fake_request)
    reference = make_image(tmp_path / "ref.png")
    payload = MiniMaxImageGenerator(config).generate(
        ImageRequest(prompt="雪山", aspect_ratio="9:16", reference=reference)
    )
    assert payload.startswith(b"\x89PNG")
    url, body = seen[-1]
    assert url == "https://api.minimax.cn/v1/image_generation"
    assert body["model"] == "image-01" and body["aspect_ratio"] == "9:16" and body["n"] == 1
    assert body["subject_reference"][0]["type"] == "character"

    music = MiniMaxMusicGenerator(config)
    data, extra = music.generate(MusicRequest(prompt="轻快"))
    assert data.startswith(b"RIFF") and extra["music_duration"] == 200
    assert seen[-1][1]["model"] == "music-3.0" and seen[-1][1]["is_instrumental"] is True
    music.generate(MusicRequest(prompt="流行翻唱风格，温暖明亮", cover_audio=reference))
    assert seen[-1][1]["model"] == "music-cover" and "audio_base64" in seen[-1][1]
    music.generate(MusicRequest(prompt="民谣", instrumental=False))
    assert seen[-1][1]["lyrics_optimizer"] is True
    assert music.lyrics("下雨天").lyrics.startswith("[Verse]")
    assert seen[-1][0].endswith("/lyrics_generation")


def test_comfyui_backends_selected_and_lyrics_unsupported(config):
    config.studio.image_generator.backend = "comfyui"
    config.studio.music_generator.backend = "comfyui"
    assert type(adapters.make_image_generator(config)).__name__ == "ComfyUIImageGenerator"
    music = adapters.make_music_generator(config)
    assert type(music).__name__ == "ComfyUIMusicGenerator"
    with pytest.raises(adapters.ModelError, match="不支持歌词生成"):
        music.lyrics("x")
    with pytest.raises(ValueError, match="未知字段"):
        type(config.studio.music_generator).model_validate(
            {
                "backend": "comfyui",
                "comfyui_workflow": {"1": {"class_type": "X", "inputs": {"t": ""}}},
                "comfyui_mapping": {"prompt": "1.inputs.t", "frames": "1.inputs.t"},
            }
        )


def test_default_profile_cards_seeded_once(config):
    from settings.runtime_settings import RuntimeConfig, get_setting, list_profiles, seed_default_profiles
    from shared.storage import Store

    store = Store(config.data_dir)
    assert seed_default_profiles(store)
    runtime = RuntimeConfig(config, store)
    for role, active_model in (
        ("video_generator", "MiniMax-H3"),
        ("image_generator", "image-01"),
        ("music_generator", "music-3.0"),
        ("animator", "glm-5.3-flash"),
    ):
        profiles = list_profiles(store, role)
        active = int(get_setting(store, f"active.profile.{role}"))
        assert next(p for p in profiles if p["id"] == active)["config"]["model"] == active_model
        if role != "animator":
            assert any(p["config"]["backend"] == "comfyui" for p in profiles)
    current = runtime.current()
    assert current.studio.video_generator.backend == "minimax_h3" and current.studio.video_generator.enabled
    assert current.studio.image_generator.enabled and current.studio.music_generator.enabled
    assert current.studio.animator.model == "glm-5.3-flash"
    assert seed_default_profiles(store) == []


# ---------------------------------------------------------------- 构建（ffmpeg）


@slow
def test_aigc_image_build_renders_generated_image_like_still(media):
    job_id, w, p = setup(
        media, [shot("a", "红色开场。", visual="雪山日出", source={"kind": "aigc_image", "seed": 3})]
    )
    images = FakeImageGenerator()
    w, p = run(media, job_id, w, p, images=images)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    [request] = images.requests
    assert request.prompt == "雪山日出" and request.seed == 3 and request.aspect_ratio == "4:3"
    drawn = ws.version(w, p["shots"]["a"]["draw"])
    assert drawn["files"]["image"]["path"].endswith("image.png")
    render = ws.version(w, p["shots"]["a"]["render"])
    assert render["inputs"]["draw"] == drawn["id"]
    frames, width, height = frame_count(ws.file_path(media, job_id, render, "video"))
    assert (width, height) == (160, 120) and frames == render["data"]["frames"]
    # 再次构建：签名未变，复用生成图片，不再调用服务。
    w, p = run(media, job_id, w, p, images=images)
    assert len(images.requests) == 1


@slow
def test_background_music_mixed_under_voice_and_continues(media, tmp_path):
    shots = [
        shot("a", "红色开场。", source={"kind": "aigc_image"}),
        shot(
            "b",
            "蓝色中段。",
            source={"kind": "aigc_image"},
            music={"kind": "generate", "prompt": "轻快电子", "volume": 0.5},
        ),
        shot("c", "绿色结尾。", source={"kind": "aigc_image"}),
    ]
    job_id, w, p = setup(media, shots)
    images, music = FakeImageGenerator(), FakeMusicGenerator()
    w, p = run(media, job_id, w, p, images=images, music=music)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    assert len(music.requests) == 1 and music.requests[0].prompt == "轻快电子"
    final = ws.version(w, p["final"])
    assert final["inputs"]["music:b"] == p["shots"]["b"]["music"]
    mp4 = ws.file_path(media, job_id, final, "mp4")
    raw = Path(tmp_path / "final.wav")
    command(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "48000", str(raw)])
    assert audio_info(raw)["duration"] == pytest.approx(final["data"]["total_seconds"], abs=0.06)
    # 改变音量只重新合成成片，不重新生成音乐。
    ws.set_board(
        media,
        job_id,
        w,
        p,
        [
            shots[0],
            shot(
                "b",
                "蓝色中段。",
                source={"kind": "aigc_image"},
                music={"kind": "generate", "prompt": "轻快电子", "volume": 0.2},
            ),
            shots[2],
        ],
    )
    ws.persist(media, job_id, w)
    previous = p["final"]
    w, p = run(media, job_id, w, p, images=images, music=music)
    assert len(music.requests) == 1 and p["final"] != previous


class ToneMusicGenerator(FakeMusicGenerator):
    """每次请求返回不同频率的音乐（1000 / 2500 Hz），用来区分成片里哪一段背景音乐在响。配音是 440 Hz。"""

    def generate(self, request):
        self.requests.append(request)
        return tone_wav(2.0, frequency=(1000, 2500)[(len(self.requests) - 1) % 2]), {}


def band_level(path, start, seconds, frequency) -> float:
    """成片某一时间窗内、以 ``frequency`` 为中心的窄带最大音量（dB）。"""

    log = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-ss",
            str(start),
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-vn",
            "-af",
            f"bandpass=f={frequency}:width_type=h:w=40,volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    return float(
        next(line for line in log.splitlines() if "max_volume" in line).split("max_volume:")[1].split()[0]
    )


@slow
def test_background_music_is_audible_and_switches_at_the_cue_shot(media):
    """分镜 b 切入音乐 A（1000 Hz），顺延到 c；分镜 d 切入音乐 B（2500 Hz）；a 之前没有背景音乐。
    成片里：a 听不到任一段；b、c 只有 A；d 只有 B（B 的音量设得比 A 低，也必须明显可闻）。"""
    text = "一二三四五六七八九十一二三四五六七八"  # 18 字 → 配音约 2.5 秒，足够跨过淡入淡出
    shots = [
        shot("a", text, source={"kind": "aigc_image"}),
        shot(
            "b",
            text,
            source={"kind": "aigc_image"},
            music={"kind": "generate", "prompt": "甲", "volume": 0.6},
        ),
        shot("c", text, source={"kind": "aigc_image"}),
        shot(
            "d",
            text,
            source={"kind": "aigc_image"},
            music={"kind": "generate", "prompt": "乙", "volume": 0.4},
        ),
    ]
    job_id, w, p = setup(media, shots)
    music = ToneMusicGenerator()
    w, p = run(media, job_id, w, p, images=FakeImageGenerator(), music=music)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    assert [request.prompt for request in music.requests] == ["甲", "乙"]
    final = ws.version(w, p["final"])
    mp4 = ws.file_path(media, job_id, final, "mp4")
    spans = {item["shot_id"]: (item["start"], item["end"]) for item in final["data"]["boundaries"]}

    def level(sid, frequency, *, head=0.6, tail=0.6):
        start, end = spans[sid]
        return band_level(mp4, start + head, end - start - head - tail, frequency)

    silent = level("a", 1000)
    assert level("a", 2500) < silent + 3  # 开头没有背景音乐：两个频段都只是配音的泄漏
    assert level("b", 1000) > silent + 10 and level("c", 1000, head=0.1, tail=1.0) > silent + 10
    assert level("b", 2500) < silent + 3 and level("c", 2500, head=0.1, tail=1.0) < silent + 3
    assert level("d", 2500) > silent + 10  # 在 d 切换到另一段音乐
    assert level("d", 1000) < silent + 3
    # 成片版本记录实际混入的背景音乐素材（页面据此列出）。
    report = final["data"]["music"]
    assert [(item["cue"], item["shots"], item["volume"]) for item in report] == [
        ("b", ["b", "c"], 0.6),
        ("d", ["d"], 0.4),
    ]
    assert report[0]["version"] == p["shots"]["b"]["music"] and report[0]["start"] == pytest.approx(
        spans["b"][0]
    )
    assert report[1]["end"] == pytest.approx(spans["d"][1])


def test_compose_signature_tracks_mix_version_only_with_music(media, monkeypatch):
    from studio.workspace import handlers

    with_music = [
        shot("a", "一。", source={"kind": "aigc_image"}),
        shot("b", "二。", source={"kind": "aigc_image"}, music={"kind": "generate", "prompt": "甲"}),
    ]
    without = [with_music[0]]

    def signatures():
        job_id, w, p = setup(media, with_music)
        first = ws.specification(media, job_id, w, p, "compose")["signature"]
        job_id, w, p = setup(media, without)
        return first, ws.specification(media, job_id, w, p, "compose")["signature"]

    before = signatures()
    monkeypatch.setattr(handlers.compose, "MUSIC_MIX_VERSION", MUSIC_MIX_VERSION + 1)
    after = signatures()
    assert after[0] != before[0]  # 混音参数变化：有背景音乐的旧成片需要重新合成
    assert after[1] == before[1]  # 没有背景音乐的成片签名与混音版本无关


def test_lyrics_endpoint_and_audio_upload(media, tmp_path):

    from api import create_app

    job_id, _w, p = setup(media, [shot("a", "一。", source={"kind": "aigc_image"})])
    client = TestClient(create_app(media, music_generator=FakeMusicGenerator()))
    response = client.post(f"/api/studio/jobs/{job_id}/music/lyrics", json={"prompt": "下雨天"})
    assert response.status_code == 200 and response.json()["lyrics"] == "[Verse]\n下雨天"
    audio = tmp_path / "ref.wav"
    audio.write_bytes(tone_wav(1.0))
    response = client.post(
        f"/api/studio/jobs/{job_id}/plans/{p['id']}/query-media",
        files={"file": ("ref.wav", audio.read_bytes(), "audio/wav")},
        data={"kind": "audio", "purpose": "source"},
    )
    assert response.status_code == 201, response.text
    media_id = response.json()["media_id"]
    assert ws.sources.present(media, job_id, media_id, "audio")
    body = ws.load(media, job_id)
    detail = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    shots = [dict(ws.version(body, ws.plan(body, p["id"])["shots"]["a"]["content"])["data"])]
    shots[0]["music"] = {"kind": "cover", "prompt": "温暖流行风格翻唱版本", "audio_id": media_id}
    response = client.patch(
        f"/api/studio/jobs/{job_id}/plans/{p['id']}", json={"revision": detail["revision"], "shots": shots}
    )
    assert response.status_code == 200, response.text
    assert response.json()["shot_stages"]["a"] == ["tts", "music", "draw", "render"]
    assert json.dumps(response.json()["shot_music"]) == json.dumps({"a": {"from": "a", "start": True}})


def test_animator_reuses_analyzer_key_for_same_model(config, monkeypatch):
    from settings.runtime_settings import (
        activate_profile,
        apply_overrides,
        save_profile,
        seed_default_profiles,
    )
    from shared.storage import Store

    monkeypatch.delenv("MINDEX_AGENT_API_KEY", raising=False)
    store = Store(config.data_dir)
    seed_default_profiles(store)
    assert apply_overrides(config, store).studio.animator.api_key == ""
    other = save_profile(
        store,
        role="analyzer",
        name="其他",
        provider="zhipu",
        api_key="sk-other-0000",
        config={"backend": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.5v"},
    )
    save_profile(
        store,
        role="analyzer",
        name="GLM",
        provider="zhipu",
        api_key="sk-glm-flash-1234",
        config={
            "backend": "openai",
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "model": "GLM-5.3-Flash",
        },
    )
    activate_profile(store, other["id"])
    current = apply_overrides(config, store)
    assert current.studio.animator.model == "glm-5.3-flash"
    assert current.studio.animator.api_key == "sk-glm-flash-1234"  # 同名模型，不论多模态理解是否激活该档案
    # 密钥与端点成对借用（GLM Coding Plan 的密钥只能用于 coding 端点）。
    assert current.studio.animator.base_url == "https://open.bigmodel.cn/api/coding/paas/v4"
    assert current.analyzer.api_key == "sk-other-0000"
