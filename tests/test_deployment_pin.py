"""设置页 / 智能助手的 e2e probe 与 e2e pin：自动定位本机文件、生成或复用清单并写入档案。"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.models import adapters
from mindex.settings import deployment_pin

WEMM = {
    "role": "embedding",
    "provider": "self",
    "name": "LocalMac-WeMM",
    "config": {"backend": "llama_cpp", "base_url": "http://127.0.0.1:8888/v1", "model": "tencent/WeMM-Embedding-9B",
               "quantization": "Q8_0", "dimensions": 8},
}


@pytest.fixture
def server(config, monkeypatch, tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    weights, mmproj = models / "wemm.gguf", models / "mmproj.gguf"
    weights.write_bytes(b"weights")
    mmproj.write_bytes(b"mmproj")
    props = {"build_info": "b-test", "model_path": str(weights), "chat_template": "t",
             "modalities": {"vision": True, "video": True, "audio": False}}

    def wire(method, url, **kwargs):
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/props"):
            return props
        if url.endswith("/v1/models"):
            return {"data": [{"id": "wemm", "meta": {"vocab_type": 2, "n_vocab": 100, "n_ctx_train": 32768,
                                                     "n_embd": 8, "n_params": 1000, "size": 2000, "ftype": "Q8_0"}}]}
        raise AssertionError(f"不应发起推理请求：{url}")

    monkeypatch.setattr(adapters, "request", wire)
    monkeypatch.setattr(deployment_pin, "listening_process", lambda port: {
        "pid": 1, "cwd": str(tmp_path),
        "argv": ["llama-server", "-m", str(weights), "--mmproj", "models/mmproj.gguf", "--port", str(port)]})
    return config, weights, mmproj


def test_probe_pin_reuse_and_profile_link(server):
    config, weights, mmproj = server
    client = TestClient(create_app(config))
    profile = client.post("/api/settings/profiles", json=WEMM).json()

    probe = client.post(f"/api/settings/profiles/{profile['id']}/deployment/probe").json()
    assert probe["ready"] is True and probe["metadata"]["ftype"] == "Q8_0"
    assert probe["detected"]["weights_file"] == str(weights.resolve())
    assert probe["detected"]["mmproj_file"] == str(mmproj.resolve())  # 相对 argv 按进程工作目录解析
    assert probe["reusable_manifests"] == [] and probe["current_manifest_valid"] is False

    pinned = client.post(f"/api/settings/profiles/{profile['id']}/deployment/pin", json={})
    assert pinned.status_code == 200, pinned.text
    result = pinned.json()
    assert result["created"] is True and result["trust"] == "local-sha256"
    assert result["manifest"].startswith("deployments/embedding-LocalMac-WeMM-")
    assert result["weights_sha256"] == hashlib.sha256(b"weights").hexdigest()
    assert result["mmproj_sha256"] == hashlib.sha256(b"mmproj").hexdigest()
    assert result["gate"]["passed"] is False  # 新部署须先通过 smoke 门槛
    stored = json.loads((config.data_dir / result["manifest"]).read_text(encoding="utf-8"))
    assert stored["schema"] == 2 and stored["server_cwd"]

    roles = {r["role"]: r for r in client.get("/api/settings", params={"probe_embedding": "false"}).json()["roles"]}
    linked = next(p for p in roles["embedding"]["profiles"] if p["id"] == profile["id"])
    assert linked["config"]["deployment_manifest"] == result["manifest"]

    # 再次探测：档案已关联有效清单；再次 pin 复用而不是生成新文件。
    again = client.post(f"/api/settings/profiles/{profile['id']}/deployment/probe").json()
    assert again["current_manifest_valid"] is True and again["reusable_manifests"] == [result["manifest"]]
    reused = client.post(f"/api/settings/profiles/{profile['id']}/deployment/pin", json={}).json()
    assert reused["created"] is False and reused["manifest"] == result["manifest"]
    forced = client.post(f"/api/settings/profiles/{profile['id']}/deployment/pin", json={"force_new": True}).json()
    assert forced["created"] is True and forced["manifest"] != result["manifest"]
    assert (config.data_dir / result["manifest"]).is_file()  # 旧清单保留

    # 测试连接不再报「缺少部署清单」，而是走到能力门槛。
    test = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert test["ok"] is False and "缺少" not in test["detail"] and "门槛" in test["detail"]


def test_pin_rejects_non_llama_and_mixed_modes(server):
    config, weights, _ = server
    client = TestClient(create_app(config))
    tokenhub = client.post("/api/settings/profiles", json={
        **WEMM, "name": "云端", "config": {**WEMM["config"], "backend": "tokenhub"}}).json()
    response = client.post(f"/api/settings/profiles/{tokenhub['id']}/deployment/probe")
    assert response.status_code == 422 and "llama_cpp" in response.json()["detail"]

    profile = client.post("/api/settings/profiles", json=WEMM).json()
    mixed = client.post(f"/api/settings/profiles/{profile['id']}/deployment/pin",
                        json={"weights_file": str(weights), "weights_sha256": "a" * 64, "mmproj_sha256": "b" * 64})
    assert mixed.status_code == 422 and "互斥" in mixed.json()["detail"]


def test_probe_and_pin_reuse_local_manifest_after_model_move(server, tmp_path):
    config, weights, mmproj = server
    client = TestClient(create_app(config))
    profile = client.post("/api/settings/profiles", json=WEMM).json()
    url = f"/api/settings/profiles/{profile['id']}/deployment"
    first = client.post(f"{url}/pin", json={}).json()
    manifest_path = config.data_dir / first["manifest"]
    manifest_bytes = manifest_path.read_bytes()

    moved = tmp_path / "relocated"
    moved.mkdir()
    weights.rename(moved / weights.name)
    mmproj.rename(moved / mmproj.name)
    props = adapters.request("GET", "http://127.0.0.1:8888/props")
    props["model_path"] = str(moved / weights.name)
    probe = client.post(f"{url}/probe").json()
    assert probe["current_manifest_valid"] is True
    assert probe["reusable_manifests"] == [first["manifest"]]
    reused = client.post(f"{url}/pin", json={}).json()
    assert reused["created"] is False and reused["manifest"] == first["manifest"]
    assert manifest_path.read_bytes() == manifest_bytes


def test_agent_tools_probe_and_pin(server):
    config, _, _ = server
    app = create_app(config)
    profile = TestClient(app).post("/api/settings/profiles", json=WEMM).json()
    registry = build_registry()

    async def go():
        client = MindexClient(app)
        try:
            ctx = ToolContext(client=client)
            probe_spec, pin_spec = registry.get("probe_model_deployment"), registry.get("pin_model_deployment")
            assert probe_spec.risk == "read" and pin_spec.risk == "write"
            probe = await probe_spec.execute(probe_spec.parse({"profile_id": profile["id"]}), ctx)
            args = pin_spec.parse({"profile_id": profile["id"]})
            preview = await pin_spec.preview(args, ctx)
            pinned = await pin_spec.execute(args, ctx)
            return probe, preview, pinned
        finally:
            await client.aclose()

    probe, preview, pinned = asyncio.run(go())
    assert "自动定位" in probe.text and "wemm.gguf" in probe.text
    assert "e2e pin" in preview.summary
    assert pinned.value["created"] is True and "已生成新清单" in pinned.text and pinned.mutation == {"domain": "settings"}
