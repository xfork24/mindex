"""音色库：注册 / 克隆 / 默认音色 与厂商匹配校验。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mindex import adapters
from mindex.api import create_app


def minimax_profile(client, name="Minimax TTS"):
    return client.post(
        "/api/settings/profiles",
        json={
            "role": "tts",
            "provider": "minimax",
            "name": name,
            "config": {"backend": "minimax", "base_url": "https://api.minimaxi.com/v1", "model": "speech-2.8-hd"},
            "api_key": "sk-minimax-key-1234",
        },
    ).json()


def sample_wav() -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 16000)
    return buffer.getvalue()


def test_register_and_default_voice_flow(config):
    client = TestClient(create_app(config))
    created = client.post(
        "/api/settings/voices",
        json={"name": "本地男声", "provider": "self", "voice_id": "Cherry", "kind": "preset"},
    )
    assert created.status_code == 201
    voice = created.json()
    assert voice["voice_id"] == "Cherry" and voice["status"] == "ready"

    # 本地 openai 后端允许 self 音色设为默认
    response = client.put("/api/settings/business", json={"default_voice_id": voice["id"]})
    assert response.status_code == 200
    assert response.json()["default_voice"] == "Cherry"

    # 默认音色随热生效配置进入新建任务
    job = client.post("/api/studio/jobs", json={"text": "镜头缓缓推近，一盏灯在风里明明灭灭。"})
    assert job.status_code == 201
    assert job.json()["options"]["voice"] == "Cherry"

    # 重复名称 422；删除后清默认
    assert client.post("/api/settings/voices", json={"name": "本地男声", "provider": "self", "voice_id": "x"}).status_code == 422
    assert client.delete(f"/api/settings/voices/{voice['id']}").status_code == 204
    data = client.get("/api/settings").json()
    assert data["business"]["default_voice"] is None


def test_default_voice_provider_must_match_active_backend(config):
    client = TestClient(create_app(config))
    voice = client.post(
        "/api/settings/voices",
        json={"name": "克隆旁白", "provider": "minimax", "voice_id": "mindex-clone-1", "kind": "clone"},
    ).json()
    # 当前 tts 后端是 openai（本地），不能选 minimax 音色
    response = client.put("/api/settings/business", json={"default_voice_id": voice["id"]})
    assert response.status_code == 422

    profile = minimax_profile(client)
    client.post(f"/api/settings/profiles/{profile['id']}/activate")
    assert client.put("/api/settings/business", json={"default_voice_id": voice["id"]}).status_code == 200
    # 清除默认
    assert client.put("/api/settings/business", json={"clear_default_voice": True}).status_code == 200


def test_clone_requires_consent_and_supported_backend(config):
    client = TestClient(create_app(config))
    response = client.post(
        "/api/settings/voices/clone",
        data={"name": "我的声音", "consent": "false"},
        files={"file": ("sample.wav", sample_wav(), "audio/wav")},
    )
    assert response.status_code == 422
    assert "授权" in response.json()["detail"]

    # 未激活支持克隆的线上 TTS 时拒绝
    response = client.post(
        "/api/settings/voices/clone",
        data={"name": "我的声音", "consent": "true"},
        files={"file": ("sample.wav", sample_wav(), "audio/wav")},
    )
    assert response.status_code == 422


def test_minimax_clone_flow(config, monkeypatch):
    client = TestClient(create_app(config))
    profile = minimax_profile(client)
    client.post(f"/api/settings/profiles/{profile['id']}/activate")

    calls = []

    def reply(method, url, **kwargs):
        calls.append((url, kwargs.get("json"), kwargs.get("data"), "files" in kwargs))
        if url.endswith("/files/upload"):
            assert kwargs["data"].get("purpose") == "voice_clone"
            return {"file": {"file_id": "file-123"}}
        assert url.endswith("/voice_clone")
        body = kwargs["json"]
        assert body["file_id"] == "file-123"
        assert body["voice_id"].startswith("mindex-")
        return {"base_resp": {"status_code": 0}, "data": {"audio": b"RIFF-preview".hex()}}

    monkeypatch.setattr(adapters, "request", reply)
    response = client.post(
        "/api/settings/voices/clone",
        data={"name": "我的声音", "consent": "true"},
        files={"file": ("sample.wav", sample_wav(), "audio/wav")},
    )
    assert response.status_code == 201
    voice = response.json()
    assert voice["kind"] == "clone" and voice["provider"] == "minimax"
    assert voice["status"] == "ready"
    assert voice["voice_id"].startswith("mindex-")
    assert voice["sample_present"] is True
    assert len(calls) == 2
    sample = config.data_dir / "voices" / f"{voice['id']}.wav"
    assert sample.is_file()


def test_aliyun_clone_url_mode(config, monkeypatch):
    client = TestClient(create_app(config))
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "tts",
            "provider": "aliyun",
            "name": "阿里 TTS",
            "config": {"backend": "dashscope", "base_url": "https://dashscope.aliyuncs.com", "model": "qwen3-tts-flash"},
        },
    ).json()
    client.post(f"/api/settings/profiles/{profile['id']}/activate")

    def reply(method, url, **kwargs):
        assert url.endswith("/api/v1/services/audio/tts/customization")
        assert kwargs["json"]["input"]["action"] == "create_voice"
        return {"output": {"voice_id": "cosyvoice-v2-user-abc", "status": "OK"}}

    monkeypatch.setattr(adapters, "request", reply)
    response = client.post(
        "/api/settings/voices/clone",
        data={"name": "阿里克隆", "consent": "true", "audio_url": "https://oss.example.com/sample.wav"},
    )
    assert response.status_code == 201
    voice = response.json()
    assert voice["provider"] == "dashscope"
    assert voice["voice_id"] == "cosyvoice-v2-user-abc"
    assert voice["sample_present"] is False


def test_clone_failure_records_status(config, monkeypatch):
    client = TestClient(create_app(config))
    profile = minimax_profile(client)
    client.post(f"/api/settings/profiles/{profile['id']}/activate")

    def reply(method, url, **kwargs):
        if url.endswith("/files/upload"):
            return {"file": {"file_id": "file-123"}}
        return {"base_resp": {"status_code": 1004, "status_msg": "配额不足"}}

    monkeypatch.setattr(adapters, "request", reply)
    response = client.post(
        "/api/settings/voices/clone",
        data={"name": "失败克隆", "consent": "true"},
        files={"file": ("sample.wav", sample_wav(), "audio/wav")},
    )
    assert response.status_code == 502
    assert "配额不足" in response.json()["detail"]
    # 失败留档：样本保留、状态 failed
    data = client.get("/api/settings").json()
    failed = [v for v in data["voices"] if v["name"] == "失败克隆"]
    assert failed and failed[0]["status"] == "failed"


def test_get_settings_includes_voices(config):
    client = TestClient(create_app(config))
    client.post("/api/settings/voices", json={"name": "Cherry", "provider": "self", "voice_id": "Cherry"})
    data = client.get("/api/settings").json()
    assert [v["name"] for v in data["voices"]] == ["Cherry"]
    assert data["business"]["default_voice"] is None
    serialized = json.dumps(data, ensure_ascii=False)
    assert "sk-minimax" not in serialized
