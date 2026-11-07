"""模型协议与领域数据契约回归。"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import FixtureEmbedder
from diagnostics.evaluation import metrics
from models import adapters
from models.adapters import ModelError, parse_json
from models.embedders import HTTPEmbedder
from models.model_server import create_model_app
from models.qwen import QwenAnalyzer
from processing.indexing import IndexBuilder, check_index
from processing.pipeline import Pipeline
from search.search import SearchEngine
from shared.config import fingerprint
from shared.types import TextSearch


def test_embedding_identity_and_output_validation(config, monkeypatch):
    space = FixtureEmbedder(config).identity()

    def reply(method, url, **kwargs):
        if url.endswith("/health"):
            return {"ready": True, "space": space}
        return {"space_id": fingerprint(space), "vectors": [[2.0] + [0.0] * 7]}

    monkeypatch.setattr(adapters, "request", reply)
    client = HTTPEmbedder(config)
    assert client.text(["中文"])[0][0] == 1
    space["dimensions"] = 16
    with pytest.raises(ModelError, match="dimensions"):
        client.identity()
    space["dimensions"] = 8
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"space_id": "changed", "vectors": [[1] * 8]})
    with pytest.raises(ModelError, match="空间发生变化"):
        client.text(["中文"])


def test_json_fences_and_invalid_scene_grouping(config, monkeypatch):
    assert parse_json('```json\n{"description":"中文"}\n```')["description"] == "中文"
    with pytest.raises(ModelError):
        parse_json("not json")
    analyzer = QwenAnalyzer(config)
    config.analyzer.response_retries = 0
    monkeypatch.setattr(
        adapters,
        "request",
        lambda *a, **kw: {"choices": [{"message": {"content": '{"groups":[[0,0]]}'}}]},
    )
    with pytest.raises(ModelError, match="重试已耗尽.*连续、完整且无重复"):
        analyzer.group_scenes([{"start": 0, "end": 1}])


def test_gpu_service_wire_contract_without_loading_models(config, monkeypatch, tmp_path):
    monkeypatch.setenv("MINDEX_GPU_LOCK", str(tmp_path / "gpu.lock"))
    monkeypatch.setenv("MINDEX_MODEL_API_KEY", "fixture-key")

    class Engine:
        space = FixtureEmbedder(config).identity()

        def text(self, texts, role):
            return [[1.0] + [0.0] * 7 for _ in texts]

    app = create_model_app(config, "embedding", engine_factory=Engine)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 401
        headers = {"Authorization": "Bearer fixture-key"}
        assert client.get("/health", headers=headers).json()["space"] == Engine.space
        response = client.post("/embed/text", headers=headers, json={"texts": ["雨夜"], "role": "query"})
        assert response.status_code == 200
        assert response.json()["space_id"] == fingerprint(Engine.space)
        assert len(response.json()["vectors"][0]) == 8


def test_evaluation_groups_do_not_reward_duplicate_windows():
    targets = [{"source_id": "s", "start": 0, "end": 10, "grade": 3}]
    results = [
        {"id": "a", "source_id": "s", "start": 0, "end": 4},
        {"id": "b", "source_id": "s", "start": 3, "end": 7},
    ]
    result = metrics(results, targets)
    assert result == {"hit_at_1": 1, "hit_at_5": 1, "mrr_at_10": 1, "ndcg_at_10": 1}
    assert math.isfinite(result["ndcg_at_10"])


def test_visual_pipeline_with_model_service_upload_contract(config, video_file, monkeypatch, tmp_path):
    """Run the real HTTP client adapter through server serialization and video uploads."""
    monkeypatch.setenv("MINDEX_GPU_LOCK", str(tmp_path / "wire-gpu.lock"))
    monkeypatch.delenv("MINDEX_MODEL_API_KEY", raising=False)
    fixture = FixtureEmbedder(config)
    seen = []

    class Engine:
        space = fixture.identity()

        def text(self, texts, role):
            return fixture.text(texts, role)

        def video(self, path):
            seen.append(path.stat().st_size)
            return [fixture.video(path)]

        def image(self, path):
            return [fixture.image(path)]

    model_app = create_model_app(config, "embedding", engine_factory=Engine)
    with TestClient(model_app) as client:

        def wire(method, url, **kwargs):
            kwargs.pop("timeout", None)
            kwargs.pop("attempts", None)
            path = url.removeprefix(config.embedding.base_url)
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(adapters, "request", wire)
        pipeline = Pipeline(config)
        pipeline.prepare(video_file)
        version = IndexBuilder(config).build(mode="visual", activate=True)
        assert len(seen) == 5 and all(size > 0 for size in seen)
        assert not check_index(pipeline.store, version)
        result = SearchEngine(config).text(TextSearch(text="蓝色画面"))
        assert result["results"][0]["start"] == pytest.approx(1)

        picture = tmp_path / "blue.png"
        Image.new("RGB", (160, 120), "blue").save(picture)
        result = SearchEngine(config).image(picture)
        assert result["results"][0]["start"] == pytest.approx(1)
