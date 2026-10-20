"""settings_api：配置档案 CRUD/激活/测试连接/业务调参 与热生效接线。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mindex import adapters
from mindex.api import create_app

TOKENHUB = {
    "role": "embedding",
    "provider": "tencent",
    "name": "腾讯TokenHub",
    "config": {
        "backend": "tokenhub",
        "base_url": "https://tokenhub.tencentcloudmaas.com/v1",
        "model": "kinfra-vl-embedding-8b",
        "dimensions": 4096,
        "quantization": "cloud",
    },
}

MINIMAX_TTS = {
    "role": "tts",
    "provider": "minimax",
    "name": "MiniMax 语音合成",
    "config": {
        "backend": "minimax",
        "base_url": "https://api.minimax.cn/v1",
        "model": "speech-2.8-hd",
        "voice": "male-qn-qingse",
    },
    "api_key": "sk-minimax-key",
}


def client_for(config) -> TestClient:
    return TestClient(create_app(config))


def test_get_settings_snapshot(config):
    data = client_for(config).get("/api/settings").json()
    roles = {role["role"]: role for role in data["roles"]}
    assert set(roles) == {"analyzer", "generator", "embedding", "asr", "tts", "person"}
    assert roles["embedding"]["effective"]["backend"] == "mindex"
    assert roles["embedding"]["active_profile_id"] is None
    assert roles["analyzer"]["label"] == "多模态理解"
    assert roles["analyzer"]["effective"]["thinking_mode"] == "auto"
    assert "presets" in data
    assert "self" in data["presets"] and "tencent" in data["presets"]
    assert "embedding" in data["presets"]["tencent"]
    assert data["business"]["search"]["rrf_k"] == config.search.rrf_k
    assert data["embedding_status"]["active_index_id"] is None


def test_profile_crud_activate_and_delete_guard(config):
    client = client_for(config)
    created = client.post("/api/settings/profiles", json={**TOKENHUB, "api_key": "sk-test-1234"})
    assert created.status_code == 201
    profile = created.json()
    assert profile["api_key"] == {"has_key": True, "hint": "…1234"}
    assert "sk-test" not in json.dumps(profile, ensure_ascii=False)

    duplicate = client.post("/api/settings/profiles", json=TOKENHUB)
    assert duplicate.status_code == 422

    # PUT 不带 api_key 表示保留
    updated = client.put(
        f"/api/settings/profiles/{profile['id']}",
        json={
            "role": "embedding",
            "provider": "tencent",
            "name": "腾讯TokenHub",
            "config": TOKENHUB["config"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["api_key"]["has_key"] is True

    activated = client.post(f"/api/settings/profiles/{profile['id']}/activate")
    assert activated.status_code == 200
    assert activated.json()["role"] == "embedding"

    # 激活中的档案不允许删除
    assert client.delete(f"/api/settings/profiles/{profile['id']}").status_code == 422

    snapshot = client.get("/api/settings").json()
    roles = {role["role"]: role for role in snapshot["roles"]}
    assert roles["embedding"]["active_profile_id"] == profile["id"]
    assert roles["embedding"]["effective"]["model"] == "kinfra-vl-embedding-8b"


def test_profile_test_connection(config, monkeypatch):
    client = client_for(config)
    profile = client.post("/api/settings/profiles", json=TOKENHUB).json()

    def reply(method, url, **kwargs):
        return {"data": [{"embedding": [1.0] * 4096}]}

    monkeypatch.setattr(adapters, "request", reply)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True
    assert result["dims"] == 4096

    def fail(method, url, **kwargs):
        raise adapters.ModelError("连接被拒绝")

    monkeypatch.setattr(adapters, "request", fail)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is False
    assert "连接被拒绝" in result["detail"]


def test_minimax_tts_profile_carries_voice(config, monkeypatch):
    """TTS 档案必须能携带 voice：云端档案继承本地默认音色 Cherry 会直接 2054 voice id not exist。"""
    client = client_for(config)
    created = client.post("/api/settings/profiles", json=MINIMAX_TTS)
    assert created.status_code == 201, created.text
    profile = created.json()
    assert profile["config"]["voice"] == "male-qn-qingse"
    client.post(f"/api/settings/profiles/{profile['id']}/activate")

    captured = {}

    def reply(method, url, **kwargs):
        if url.endswith('/get_voice'):
            return {"system_voice": [{"voice_id": "male-qn-qingse", "voice_name": "Test"}]}
        captured.update(url=url, json=kwargs["json"])
        return {"base_resp": {"status_code": 0}, "data": {"audio": b"RIFF-probe".hex()}}

    monkeypatch.setattr(adapters, "request", reply)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True
    assert captured["url"].endswith("/t2a_v2")
    assert captured["json"]["voice_setting"] == {"voice_id": "male-qn-qingse"}


def test_profile_test_connection_reasoning_model_without_content(config, monkeypatch):
    """推理模型思考耗尽 token 时 message 无 content 键，应给出可读回执而非裸 KeyError('content')。"""
    client = client_for(config)
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "analyzer",
            "provider": "custom",
            "name": "MiniMax-推理模型测试",
            "config": {
                "backend": "openai",
                "base_url": "https://api.minimax.cn/v1",
                "model": "MiniMax-M3.1-Flash-Preview",
            },
        },
    ).json()
    calls = {}

    def reasoning_only(method, url, **kwargs):
        calls["json"] = kwargs.get("json")
        return {
            "choices": [
                {
                    "finish_reason": "length",
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "reasoning_content": "We need answer exactly ok likely.",
                    },
                }
            ]
        }

    monkeypatch.setattr(adapters, "request", reasoning_only)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True, result
    # 探测预算须足够推理模型产出正文（思考先行消耗 token）
    assert calls["json"]["max_tokens"] >= 64
    # 缺 content 时呈现可读信息，而不是裸键名 'content'
    assert "推理" in result["reply"] or "正文" in result["reply"]

    def with_content(method, url, **kwargs):
        calls["json"] = kwargs.get("json")
        return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(adapters, "request", with_content)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True and result["reply"] == "ok"


def test_business_search_tuning_hot(config):
    client = client_for(config)
    weights = {"video": 2, "description": 1, "style": 1, "dialogue": 0.5, "lexical": 0.5}
    response = client.put("/api/settings/business", json={"search": {"weights": weights, "rrf_k": 30}})
    assert response.status_code == 200
    data = client.get("/api/settings").json()
    assert data["business"]["search"]["rrf_k"] == 30
    assert data["business"]["search"]["weights"]["video"] == 2

    assert client.put("/api/settings/business", json={"search": {"weights": {"video": 1}}}).status_code == 422
    assert client.put("/api/settings/business", json={"search": {"max_upload_mb": 1}}).status_code == 422

    client.delete("/api/settings/business")
    data = client.get("/api/settings").json()
    assert data["business"]["search"]["rrf_k"] == config.search.rrf_k


def test_profile_test_connection_llama_cpp_without_model_field(config, monkeypatch):
    """schema 2 空间契约没有 model 键，测试连接应回退到配置里的模型名，而不是裸 KeyError('model')。"""
    client = client_for(config)
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "embedding",
            "provider": "self",
            "name": "本地 WeMM",
            "config": {
                "backend": "llama_cpp",
                "base_url": "http://127.0.0.1:8888",
                "model": "tencent/WeMM-Embedding-9B",
                "dimensions": 4096,
                "quantization": "Q8_0",
            },
        },
    ).json()

    class Schema2Embedder:
        def __init__(self, cfg):
            self.cfg = cfg

        def identity(self):
            return {
                "schema": 2,
                "content": {"weights_sha256": "a" * 64, "mmproj_sha256": "b" * 64},
                "dimensions": 4096,
                "implementation": "llama-native-wemm-2",
            }

        def text(self, texts, role="document"):
            return [[0.0] * 4096 for _ in texts]

    monkeypatch.setattr("mindex.settings_api.make_embedder", Schema2Embedder)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True
    assert result["model"] == "tencent/WeMM-Embedding-9B"
    assert result["dims"] == 4096


def test_hot_switch_reaches_search_engine(config, monkeypatch):
    captured = []

    class Probe:
        def __init__(self, cfg):
            self.cfg = cfg
            captured.append(cfg)

        def identity(self):
            return {"model": self.cfg.embedding.model, "implementation": "probe"}

    monkeypatch.setattr("mindex.search.make_embedder", Probe)
    client = client_for(config)
    client.get("/api/status", params={"health": True})
    assert captured[-1].embedding.backend == "mindex"

    profile = client.post("/api/settings/profiles", json=TOKENHUB).json()
    client.post(f"/api/settings/profiles/{profile['id']}/activate")
    client.get("/api/status", params={"health": True})
    assert captured[-1].embedding.backend == "tokenhub"
    assert captured[-1].embedding.model == "kinfra-vl-embedding-8b"


def test_studio_job_snapshots_active_generator(config):
    client = client_for(config)
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "generator",
            "provider": "deepseek",
            "name": "deepseek-chat",
            "config": {
                "backend": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "llama_template": False,
            },
        },
    ).json()
    client.post(f"/api/settings/profiles/{profile['id']}/activate")
    created = client.post(
        "/api/studio/jobs", json={"text": "夜色渐深，城市灯火依次亮起，镜头缓缓推近一扇亮着灯的窗。"}
    )
    assert created.status_code == 201
    state = created.json()
    assert state["config_snapshot"]["generator_model"] == "deepseek-chat"
    job_file = config.data_dir / "studio" / state["id"] / "job.json"
    assert "sk-" not in job_file.read_text(encoding="utf-8")


def test_settings_page_served(config):
    response = client_for(config).get("/settings")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_profile_test_connection_uses_configured_max_tokens(config, monkeypatch):
    """测试连接必须用档案配置的 max_tokens；否则参数超限这类错误在测试里看不见。"""
    sent = []

    def capture(method, url, **kwargs):
        sent.append(kwargs.get("json", {}))
        return {"choices": [{"message": {"content": "ok"}}]}

    client = client_for(config)
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "generator",
            "provider": "minimax",
            "name": "MiniMax 分镜",
            "config": {
                "backend": "openai",
                "base_url": "https://api.example.com/v1",
                "model": "M1",
                "max_tokens": 1048576,
            },
        },
    ).json()

    monkeypatch.setattr(adapters, "request", capture)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True
    assert sent[0]["max_tokens"] == 1048576


def test_channel_weights_share_runtime_with_cli_and_keep_legacy_weights(config):
    from mindex.runtime_settings import apply_runtime_overrides

    client = client_for(config)
    weights = {"visual": 1, "dialogue": 2, "analysis": 0}
    assert (
        client.put("/api/settings/business", json={"search": {"channel_weights": weights}}).status_code == 200
    )
    current = apply_runtime_overrides(config)
    assert current.search.channel_weights == weights
    assert current.search.weights == config.search.weights
    snapshot = client.get("/api/settings").json()["business"]["search"]
    assert snapshot["channel_weights"] == weights
    assert (
        client.put(
            "/api/settings/business",
            json={"search": {"channel_weights": {"visual": -1, "dialogue": 1, "analysis": 1}}},
        ).status_code
        == 422
    )


def test_local_voiceclone_probe_reports_missing_voice_without_synthesis(config, monkeypatch):
    client = client_for(config)
    profile = client.post('/api/settings/profiles', json={
        'role': 'tts', 'provider': 'self', 'name': 'LocalMac-Qwen3-TTS-VoiceClone',
        'config': {'backend': 'openai', 'base_url': 'http://localhost:8890/v1',
                   'model': 'Qwen3-TTS-VoiceClone', 'voice': 'foreign-provider-voice'},
    }).json()
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        return {'voices': []}
    monkeypatch.setattr(adapters, 'request', request)
    monkeypatch.setattr(adapters, 'request_bytes', lambda *a, **kw: pytest.fail('missing clone must not synthesize'))
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result['ok'] is True and result['ready'] is False
    assert '服务连接正常' in result['reply'] and '未执行合成测试' in result['reply']
    assert calls == ['http://localhost:8890/v1/audio/voices']


@pytest.mark.parametrize(('model', 'configured_voice'), [
    ('Qwen3-TTS-VoiceClone', 'my-clone'),
    ('Qwen3-TTS-VoiceClone', 'Chinese (Mandarin)_News_Anchor'),
    ('Qwen3-TTS-CustomVoice', 'Chinese (Mandarin)_News_Anchor'),
])
def test_local_voiceclone_probe_synthesizes_registered_voice(config, monkeypatch, model, configured_voice):
    client = client_for(config)
    profile = client.post('/api/settings/profiles', json={
        'role': 'tts', 'provider': 'self', 'name': 'LocalMac-Qwen3-TTS-VoiceClone',
        'config': {'backend': 'openai', 'base_url': 'http://localhost:8890/v1',
                   'model': model, 'voice': configured_voice},
    }).json()
    monkeypatch.setattr(adapters, 'request', lambda *a, **kw: {'voices': ['my-clone']})
    captured = []
    def speech(method, url, **kwargs):
        captured.append(kwargs['json'])
        return b'RIFF-probe'
    monkeypatch.setattr(adapters, 'request_bytes', speech)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result['ok'] is True and result['bytes'] > 0
    assert captured[0]['voice'] == 'my-clone'


def test_local_voiceclone_probe_still_fails_if_service_unreachable(config, monkeypatch):
    client = client_for(config)
    profile = client.post('/api/settings/profiles', json={
        'role': 'tts', 'provider': 'self', 'name': 'LocalMac-Qwen3-TTS-VoiceClone',
        'config': {'backend': 'openai', 'model': 'Qwen3-TTS-VoiceClone'},
    }).json()
    def offline(*a, **kw):
        raise adapters.ModelError('connection refused')
    monkeypatch.setattr(adapters, 'request', offline)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result['ok'] is False and 'connection refused' in result['detail']
