"""模型请求超时：每个模型档案都可在设置页配置，所有调用该模型的地方都使用配置的值。

缺省：图片、视频、音乐与动画生成 10 分钟，其他模型 3 分钟。全部用桩，不访问任何真实服务。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.models import adapters
from mindex.settings import voice_clone, voices_catalog
from mindex.shared.config import Config


def roles(config: Config) -> dict:
    s = config.studio
    return {
        "person": config.person, "embedding": config.embedding, "analyzer": config.analyzer, "asr": config.asr,
        "generator": s.generator, "writer": s.writer, "tts": s.tts, "agent": config.agent.llm,
        "video_generator": s.video_generator, "image_generator": s.image_generator,
        "music_generator": s.music_generator, "animator": s.animator,
    }


def test_default_timeouts(config):
    generation = {"video_generator", "image_generator", "music_generator", "animator"}
    for role, section in roles(config).items():
        assert section.timeout == (600 if role in generation else 180), role
    # 智能助手继承给动画生成的只有连接字段，动画生成仍是 10 分钟。
    assert Config(agent={"llm": {"timeout": 30}}).studio.animator.timeout == 600


def test_settings_profile_timeout_is_applied_and_shown(config):
    client = TestClient(create_app(config))
    created = client.post("/api/settings/profiles", json={
        "role": "writer", "provider": "custom", "name": "慢模型",
        "config": {"backend": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 42},
    })
    assert created.status_code == 201, created.text
    assert client.post(f"/api/settings/profiles/{created.json()['id']}/activate").status_code == 200
    writer = {r["role"]: r for r in client.get("/api/settings").json()["roles"]}["writer"]
    assert writer["effective"]["timeout"] == 42
    bad = client.post("/api/settings/profiles", json={
        "role": "tts", "provider": "custom", "name": "x",
        "config": {"backend": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 0},
    })
    assert bad.status_code == 422


@pytest.fixture
def seen(monkeypatch):
    calls = []

    def fake(method, url, **kwargs):
        calls.append(kwargs.get("timeout"))
        return {"base_resp": {"status_code": 0}, "file": {"file_id": "f"}, "data": {}, "voices": [],
                "system_voice": [], "output": {"voice_id": "v"}}

    monkeypatch.setattr(adapters, "request", fake)
    return calls


def test_interactive_calls_use_profile_timeout(config, seen, tmp_path):
    """以前写死或封顶的调用：音色目录、声音克隆、连接测试、图片鉴权探测。"""
    tts = config.studio.tts.model_copy(update={"backend": "minimax", "timeout": 77})
    voices_catalog._minimax_groups(tts)
    sample = tmp_path / "s.wav"
    sample.write_bytes(b"RIFF")
    voice_clone.minimax_clone("http://x", "", sample, "v", "m", timeout=tts.timeout)
    voice_clone.aliyun_enroll("http://x", "", "http://a", "p", "m", timeout=tts.timeout)
    from mindex.models.image_generators import minimax_health

    image = config.studio.image_generator.model_copy(update={"timeout": 33})
    minimax_health("http://x", image, {}, "ok", "图片生成")
    assert seen == [77, 77, 77, 77, 33]


def test_connection_test_uses_profile_timeout(config, seen):
    client = TestClient(create_app(config))
    created = client.post("/api/settings/profiles", json={
        "role": "generator", "provider": "custom", "name": "g",
        "config": {"backend": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m", "timeout": 250},
    }).json()
    client.post(f"/api/settings/profiles/{created['id']}/test")
    assert seen == [250]


def test_video_download_and_detector_use_configured_timeout(config, monkeypatch, tmp_path):
    from mindex.models import detectors, video_generators

    captured = {}

    def fake_bytes(method, url, **kwargs):
        captured["download"] = kwargs["timeout"]
        return b"x"

    monkeypatch.setattr(adapters, "request_bytes", fake_bytes)
    config.studio.video_generator.timeout = 45
    backend = video_generators.MiniMaxVideoGenerator(config)
    backend._download("http://x/v.mp4", tmp_path / "v.mp4")
    assert captured["download"] == 45

    def fake_request(method, url, **kwargs):
        captured["detector"] = kwargs["timeout"]
        raise adapters.ModelError("stop")

    monkeypatch.setattr(adapters, "request", fake_request)
    config.detector.timeout = 99
    video = tmp_path / "in.mp4"
    video.write_bytes(b"0")
    with pytest.raises(adapters.ModelError):
        detectors.HTTPShotDetector(config).detect(video)
    assert captured["detector"] == 99
