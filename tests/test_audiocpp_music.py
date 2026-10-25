"""本地 audio.cpp 音乐生成后端（audiocpp）：请求体、响应解码、只读健康检查与配置/签名。"""

import base64

import pytest

from mindex.models import adapters
from mindex.models.adapters import ModelError, make_music_generator
from mindex.models.music_generators import AudioCppMusicGenerator, MusicRequest
from mindex.settings.runtime_settings import merge_profile
from mindex.shared.config import Config


def config(**fields):
    cfg = Config()
    section = cfg.studio.music_generator.model_copy(update={
        "backend": "audiocpp", "base_url": "http://127.0.0.1:8891", "model": "minimax-music3", **fields})
    return cfg.model_copy(update={"studio": cfg.studio.model_copy(update={"music_generator": section})})


def test_factory_and_request_body():
    gen = make_music_generator(config(num_inference_steps=12))
    assert isinstance(gen, AudioCppMusicGenerator)
    body = gen.request_body(MusicRequest(prompt="轻快流行", lyrics="[verse] 你好", instrumental=False, seconds=15, seed=7))
    assert body == {"model": "minimax-music3", "request": {"text": "轻快流行", "options": {
        "lyrics": "[verse] 你好", "duration_sec": "15", "num_inference_steps": "12", "seed": "7"}}}


def test_instrumental_duration_clamp_and_missing_lyrics():
    gen = AudioCppMusicGenerator(config())
    options = gen.request_body(MusicRequest(prompt="x", seconds=9999))["request"]["options"]
    assert options["lyrics"] == "[Instrumental]" and options["duration_sec"] == "20"  # 默认 max_seconds
    capped = AudioCppMusicGenerator(config(max_seconds=60)).request_body(MusicRequest(prompt="x", seconds=9999))
    assert capped["request"]["options"]["duration_sec"] == "60"
    assert gen.request_body(MusicRequest(prompt="x", seconds=8))["request"]["options"]["duration_sec"] == "8"
    with pytest.raises(ModelError, match="歌词"):
        gen.request_body(MusicRequest(prompt="x", instrumental=False))


def test_generate_decodes_wav_and_rejects_cover_and_lyrics(monkeypatch, tmp_path):
    calls = []

    def fake(method, url, **kwargs):
        calls.append((method, url, kwargs.get("attempts")))
        return {"audio": base64.b64encode(b"RIFFwav").decode(), "sample_rate": 44100, "channels": 2}

    monkeypatch.setattr(adapters, "request", fake)
    gen = AudioCppMusicGenerator(config())
    data, extra = gen.generate(MusicRequest(prompt="x"))
    assert data == b"RIFFwav" and extra["sample_rate"] == 44100
    assert extra["request"]["text"] == "x" and extra["request"]["options"]["lyrics"] == "[Instrumental]"
    assert calls == [("POST", "http://127.0.0.1:8891/v1/tasks/run", 1)]  # 长请求不重试
    cover = tmp_path / "a.wav"
    cover.write_bytes(b"x")
    with pytest.raises(ModelError, match="翻唱"):
        gen.generate(MusicRequest(prompt="x", cover_audio=cover))
    with pytest.raises(ModelError, match="歌词生成"):
        gen.lyrics("x")
    monkeypatch.setattr(adapters, "request", lambda *a, **k: {"audio": ""})
    with pytest.raises(ModelError, match="没有返回音频"):
        gen.generate(MusicRequest(prompt="x"))


def test_health_is_read_only(monkeypatch):
    seen = []

    def fake(method, url, **kwargs):
        seen.append((method, url))
        if url.endswith("/v1/models"):
            return {"data": [{"id": "minimax-music3", "family": "minimax_music3"}]}
        return {"status": "ok"}

    monkeypatch.setattr(adapters, "request", fake)
    assert AudioCppMusicGenerator(config()).health()["ready"] is True
    assert {method for method, _ in seen} == {"GET"}
    with pytest.raises(ModelError, match="没有登记"):
        AudioCppMusicGenerator(config(model="other")).health()


def test_profile_merge_signature_and_validation():
    base = Config()
    profile = {"config": {"backend": "audiocpp", "base_url": "http://127.0.0.1:8891", "model": "minimax-music3",
                          "num_inference_steps": 15, "timeout": 3600}}
    merged = merge_profile(base, "music_generator", profile).studio.music_generator
    assert merged.enabled and merged.backend == "audiocpp" and merged.timeout == 3600
    assert merged.signature()["num_inference_steps"] == 15 and merged.signature()["max_seconds"] == 20
    assert "num_inference_steps" not in base.studio.music_generator.signature()  # MiniMax 签名不变
