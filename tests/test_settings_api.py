"""settings_api：配置档案 CRUD/激活/测试连接/业务调参 与热生效接线。"""

from __future__ import annotations

import json

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


def client_for(config) -> TestClient:
    return TestClient(create_app(config))


def test_get_settings_snapshot(config):
    data = client_for(config).get("/api/settings").json()
    roles = {role["role"]: role for role in data["roles"]}
    assert set(roles) == {"analyzer", "generator", "embedding", "asr", "tts"}
    assert roles["embedding"]["effective"]["backend"] == "mindex"
    assert roles["embedding"]["active_profile_id"] is None
    assert roles["analyzer"]["label"] == "多模态理解"
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
        json={"role": "embedding", "provider": "tencent", "name": "腾讯TokenHub", "config": TOKENHUB["config"]},
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
    created = client.post("/api/studio/jobs", json={"text": "夜色渐深，城市灯火依次亮起，镜头缓缓推近一扇亮着灯的窗。"})
    assert created.status_code == 201
    state = created.json()
    assert state["config_snapshot"]["generator_model"] == "deepseek-chat"
    job_file = config.data_dir / "studio" / state["id"] / "job.json"
    assert "sk-" not in job_file.read_text(encoding="utf-8")


def test_settings_page_served(config):
    response = client_for(config).get("/settings")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
