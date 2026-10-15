"""音色库：注册 / 克隆 / 默认音色 与厂商匹配校验。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mindex import adapters
from mindex.api import create_app
from mindex.storage import Store


def minimax_profile(client, name="Minimax TTS"):
    return client.post(
        "/api/settings/profiles",
        json={
            "role": "tts",
            "provider": "minimax",
            "name": name,
            "config": {"backend": "minimax", "base_url": "https://api.minimax.cn/v1", "model": "speech-2.8-hd"},
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


def test_clone_requires_consent(config):
    """三种内置后端（本地 qwentts / MiniMax / DashScope）均支持克隆，只剩授权门。"""
    client = TestClient(create_app(config))
    response = client.post(
        "/api/settings/voices/clone",
        data={"name": "我的声音", "consent": "false"},
        files={"file": ("sample.wav", sample_wav(), "audio/wav")},
    )
    assert response.status_code == 422
    assert "授权" in response.json()["detail"]


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


def test_qwentts_register_contract(monkeypatch, tmp_path):
    from mindex.voice_clone import qwentts_register

    captured = {}

    def reply(method, url, **kwargs):
        captured.update(method=method, url=url, json=kwargs["json"])
        return {}

    monkeypatch.setattr(adapters, "request", reply)
    sample = tmp_path / "s.wav"
    sample.write_bytes(b"RIFF-x")
    qwentts_register("http://127.0.0.1:8890/v1", sample, "mindex-clone-9", "参考文本")
    import base64

    assert captured["method"] == "POST" and captured["url"].endswith("/audio/voices")
    body = captured["json"]
    assert body["name"] == "mindex-clone-9"
    assert body["ref_text"] == "参考文本"
    assert base64.b64decode(body["wav"]) == b"RIFF-x"


def test_clone_local_qwentts_flow(config, monkeypatch):
    """本地 openai 后端：上传样本注册到 qwentts.cpp + 合成试听 + 落库留样本。"""
    client = TestClient(create_app(config))
    registers = []

    def reply(method, url, **kwargs):
        if url.endswith("/audio/voices") and method == "POST":
            registers.append(kwargs["json"])
            return {}
        raise AssertionError(f"意外请求 {method} {url}")

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters, "request_bytes", lambda *a, **kw: b"RIFF-preview")
    response = client.post(
        "/api/settings/voices/clone",
        data={"name": "我的旁白", "consent": "true", "ref_text": "这是参考文本"},
        files={"file": ("sample.wav", sample_wav(), "audio/wav")},
    )
    assert response.status_code == 201, response.text
    voice = response.json()
    assert voice["provider"] == "self" and voice["kind"] == "clone" and voice["status"] == "ready"
    assert voice["notes"] == "这是参考文本"
    assert registers and registers[0]["name"] == voice["voice_id"]
    store = Store(config.data_dir)
    assert (store.root / voice["sample_path"]).is_file()
    preview = store.root / "voices" / f"{voice['id']}-preview.wav"
    assert preview.is_file()

    # 重启自愈：目录接口对缺失克隆重注册（服务端列表为空时）
    monkeypatch.setattr(
        adapters, "request", lambda method, url, **kw: {"voices": []} if url.endswith("/audio/voices") else (_ for _ in ()).throw(AssertionError(url))
    )
    catalog = client.get("/api/settings/voices/catalog").json()
    ids = {v["voice_id"] for g in catalog["groups"] for v in g["voices"]}
    assert voice["voice_id"] in ids


def test_business_default_voice_literal(config, monkeypatch):
    """默认音色支持 voice_id 字面量：目录内合法，未知/跨厂商拒绝。"""
    client = TestClient(create_app(config))
    monkeypatch.setattr(
        adapters, "request", lambda method, url, **kw: {"voices": ["vivian", "serena"]}
    )
    ok = client.put("/api/settings/business", json={"default_voice": "vivian"})
    assert ok.status_code == 200
    assert ok.json()["default_voice"] == "vivian"

    unknown = client.put("/api/settings/business", json={"default_voice": "no-such-voice"})
    assert unknown.status_code == 422

    minimax_voice = client.post(
        "/api/settings/voices",
        json={"name": "克隆旁白", "provider": "minimax", "voice_id": "mindex-clone-1", "kind": "clone"},
    ).json()
    cross = client.put("/api/settings/business", json={"default_voice": "mindex-clone-1"})
    assert cross.status_code == 422
    assert client.delete(f"/api/settings/voices/{minimax_voice['id']}").status_code == 204


def test_catalog_route_reports_backend_and_groups(config, monkeypatch):
    client = TestClient(create_app(config))
    monkeypatch.setattr(
        adapters, "request", lambda method, url, **kw: {"voices": ["vivian"]}
    )
    catalog = client.get("/api/settings/voices/catalog").json()
    assert catalog["backend"] == "openai"
    assert catalog["default_voice"] is None
    ids = {v["voice_id"] for g in catalog["groups"] for v in g["voices"]}
    assert {"vivian", "serena"} <= ids  # live + 静态兜底并存


def test_catalog_preview_uses_profile_target(config, monkeypatch):
    """档案表单的音色候选：按被编辑档案（而非当前激活后端）的目标服务查询。"""
    client = TestClient(create_app(config))
    profile = minimax_profile(client)  # 未激活：当前后端仍是本地 openai
    captured = {}

    def reply(method, url, **kwargs):
        captured.update(url=url, headers=kwargs.get("headers"))
        return {
            "base_resp": {"status_code": 0},
            "system_voice": [{"voice_id": "female-yujie", "voice_name": "御姐", "description": ["成熟女性"]}],
            "voice_cloning": [],
            "voice_generation": [],
        }

    monkeypatch.setattr(adapters, "request", reply)
    catalog = client.post(
        "/api/settings/voices/catalog/preview",
        json={"profile_id": profile["id"]},
    ).json()
    assert catalog["backend"] == "minimax"
    assert captured["url"].endswith("/get_voice")
    assert "Bearer sk-minimax-key-1234" in captured["headers"]["Authorization"]
    ids = {v["voice_id"] for g in catalog["groups"] for v in g["voices"]}
    assert "female-yujie" in ids


def test_catalog_preview_inline_local_target(config, monkeypatch):
    """新建档案（尚未保存）：用表单里的 backend/base_url 查询，供本地服务免密钥场景。"""
    client = TestClient(create_app(config))
    captured = {}

    def reply(method, url, **kwargs):
        captured.update(url=url)
        return {"voices": ["vivian"]}

    monkeypatch.setattr(adapters, "request", reply)
    catalog = client.post(
        "/api/settings/voices/catalog/preview",
        json={"backend": "openai", "base_url": "http://127.0.0.1:8900/v1", "model": "qwen3-tts-customvoice"},
    ).json()
    assert catalog["backend"] == "openai"
    assert captured["url"] == "http://127.0.0.1:8900/v1/audio/voices"
    ids = {v["voice_id"] for g in catalog["groups"] for v in g["voices"]}
    assert "vivian" in ids


def test_catalog_preview_voiceclone_profile_drops_system_groups(config, monkeypatch):
    """VoiceClone 型档案（model/名称含标识）：候选只给音色库克隆音色，不给系统列表。"""

    client = TestClient(create_app(config))
    profile = client.post(
        "/api/settings/profiles",
        json={"role": "tts", "provider": "self", "name": "LocalMac-Qwen3-TTS-VoiceClone",
              "config": {"backend": "openai", "base_url": "http://127.0.0.1:8890/v1", "model": "Qwen3-TTS-VoiceClone"}},
    ).json()
    client.post(
        "/api/settings/voices",
        json={"name": "我的旁白", "provider": "self", "voice_id": "mindex-clone-1", "kind": "clone"},
    )
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"voices": []})
    catalog = client.post("/api/settings/voices/catalog/preview", json={"profile_id": profile["id"]}).json()
    assert catalog["voice_kind"] == "clone"
    assert catalog["groups"] == []
    library = {v["voice_id"] for v in catalog["library"]}
    assert "mindex-clone-1" in library


def test_catalog_preview_customvoice_local_lists_system(config, monkeypatch):
    client = TestClient(create_app(config))
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"voices": ["vivian", "uncle_fu"]})
    catalog = client.post(
        "/api/settings/voices/catalog/preview",
        json={"name": "LocalMac-Qwen3-TTS-CustomVoice", "backend": "openai",
              "base_url": "http://127.0.0.1:8890/v1", "model": "Qwen3-TTS-CustomVoice"},
    ).json()
    assert catalog["voice_kind"] == "system"
    ids = {v["voice_id"] for g in catalog["groups"] for v in g["voices"]}
    assert {"vivian", "uncle_fu", "serena"} <= ids


def test_catalog_preview_minimax_clone_kind_by_name(config, monkeypatch):
    """MiniMax 两档案 model 相同，靠档案名后缀区分 VoiceClone。"""
    client = TestClient(create_app(config))
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "tts", "provider": "minimax", "name": "MiniMax-Speech-2.8-HD-VoiceClone",
            "config": {"backend": "minimax", "base_url": "https://api.minimax.cn/v1", "model": "speech-2.8-hd"},
            "api_key": "sk-minimax-key-1234",
        },
    ).json()
    monkeypatch.setattr(
        adapters, "request",
        lambda *a, **kw: {"base_resp": {"status_code": 0}, "system_voice": [
            {"voice_id": "female-yujie", "voice_name": "御姐", "description": []}],
            "voice_cloning": [], "voice_generation": []},
    )
    catalog = client.post("/api/settings/voices/catalog/preview", json={"profile_id": profile["id"]}).json()
    assert catalog["voice_kind"] == "clone"
    assert catalog["groups"] == []
