from __future__ import annotations

import json

import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder
from fastapi.testclient import TestClient

from mindex.adapters import AdaptiveShotDetector, ModelError
from mindex.api import create_app
from mindex.evaluation import evaluate
from mindex.indexing import IndexBuilder, VectorIndex, check_index
from mindex.media import command, cut, frame_data, probe, thumbnail, windows
from mindex.pipeline import Pipeline
from mindex.search import SearchEngine, fuse
from mindex.types import Filters, TextSearch


@pytest.fixture
def processed(config, video_file):
    embedder = FixtureEmbedder(config)
    analyzer = FixtureAnalyzer(embedder)
    pipeline = Pipeline(config, analyzer=analyzer)
    with pipeline.lock():
        source_id = pipeline.prepare(video_file)
        pipeline.transcribe()
        pipeline.analyze()
        version = IndexBuilder(config, embedder).build(activate=True)
    return pipeline, embedder, analyzer, source_id, version


def test_detector_preserves_one_second_shots(config, video_file):
    ranges = AdaptiveShotDetector(config).detect(video_file)
    assert len(ranges) == 3
    assert ranges[0] == pytest.approx((0, 1), abs=0.08)
    assert ranges[1] == pytest.approx((1, 2), abs=0.08)
    assert ranges[-1][1] == pytest.approx(10, abs=0.05)


def test_windows_cover_tail_and_do_not_cross_bounds():
    pieces = windows(2, 10)
    assert pieces == [(2, 6), (5, 9), (6, 10)]
    assert windows(0, 0.1) == [(0, 0.1)]
    with pytest.raises(ValueError):
        windows(0, 5, size=2, stride=3)


def test_single_frame_odd_size_clip_can_be_processed(tmp_path):
    source = tmp_path / "one-frame.mkv"
    command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=161x121:rate=25:duration=0.04",
            "-frames:v",
            "1",
            "-c:v",
            "ffv1",
            str(source),
        ]
    )
    clip = tmp_path / "clip.mp4"
    cut(source, 0, 0.04, clip)
    info = probe(clip)
    assert info["width"] == 162 and info["height"] == 122
    image = tmp_path / "thumb.jpg"
    thumbnail(clip, image)
    assert image.stat().st_size > 0
    assert len(frame_data(clip)) == 1


def test_resume_search_api_and_range_playback(processed, config, video_file):
    pipeline, embedder, analyzer, source_id, version = processed
    store = pipeline.store
    assert len(store.segments(source_id, "shot")) == 3
    assert len(store.segments(source_id, "moment")) == 5
    assert len(store.segments(source_id, "scene")) == 1
    for snapshot in store.snapshots(version):
        assert (
            abs(
                probe(store.resolve_media(snapshot["path"]))["duration"]
                - (snapshot["end"] - snapshot["start"])
            )
            < 0.12
        )
    calls = analyzer.calls
    pipeline.prepare(video_file)
    pipeline.transcribe()
    pipeline.analyze()
    assert analyzer.calls == calls
    embedding_calls = embedder.calls
    assert IndexBuilder(config, embedder).build(activate=True) == version
    assert embedder.calls == embedding_calls
    assert not check_index(store, version)

    engine = SearchEngine(config, embedder)
    result = engine.text(TextSearch(text="红色画面"))
    assert result["results"][0]["start"] == pytest.approx(0, abs=0.1)
    assert result["results"][0]["end"] == pytest.approx(1, abs=0.1)
    assert result["results"][0]["score"] > 0
    filtered = engine.text(TextSearch(text="红色", filters=Filters(scene="不存在的场景")))
    assert filtered["results"] == []

    client = TestClient(create_app(config, embedder))
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    response = client.post("/api/search/text", json={"text": "蓝色画面"})
    assert response.status_code == 200
    top = response.json()["results"][0]
    assert top["start"] == pytest.approx(1, abs=0.1)
    assert "path" not in top
    media = client.get(top["media_url"], headers={"Range": "bytes=0-127"})
    assert media.status_code == 206
    assert len(media.content) == 128
    head = client.head(top["media_url"])
    assert head.status_code == 200
    assert head.content == b""
    assert int(head.headers["content-length"]) > 128
    assert client.get(top["thumbnail_url"]).status_code == 200
    assert client.get(top["context_url"], headers={"Range": "bytes=0-127"}).status_code == 206
    assert client.get(f"/api/segments/{top['id']}").status_code == 200
    assert client.get(f"/api/sources/{source_id}/media").status_code == 200
    assert client.post("/api/search/text", json={"text": "   "}).status_code == 422
    assert client.post("/api/search/text", json={"text": "红色", "limit": 51}).status_code == 422
    assert client.post("/api/search/video", files={"file": ("bad.mp4", b"broken")}).status_code == 422
    assert client.post("/api/search/video", files={"file": ("empty.mp4", b"")}).status_code == 422
    assert client.get("/api/media/unknown").status_code == 404
    with pytest.raises(FileNotFoundError):
        store.resolve_media("../metadata.sqlite3")

    short = config.data_dir / "reference.mp4"
    cut(video_file, 1.1, 1.9, short)
    with short.open("rb") as handle:
        response = client.post("/api/search/video", files={"file": ("reference.mp4", handle)})
    assert response.status_code == 200
    assert response.json()["results"][0]["start"] == pytest.approx(1, abs=0.1)
    assert not list(config.data_dir.glob("query-*"))


def test_version_isolation_reindex_and_rollback(processed, config):
    pipeline, embedder, _, _, old = processed
    original_ids = VectorIndex(config.data_dir, old).ids()
    original_reps = pipeline.store.rows("SELECT * FROM representations WHERE index_id=?", (old,))
    changed = config.model_copy(deep=True)
    changed.embedding.quantization = "nf4"
    next_embedder = FixtureEmbedder(changed)
    with pytest.raises(ModelError, match="向量空间不同"):
        SearchEngine(changed, next_embedder).text(TextSearch(text="红色"))
    new = IndexBuilder(changed, next_embedder).build(activate=False)
    assert new != old
    assert pipeline.store.active_index()["id"] == old
    assert VectorIndex(config.data_dir, old).ids() == original_ids
    assert pipeline.store.rows("SELECT * FROM representations WHERE index_id=?", (old,)) == original_reps
    pipeline.store.activate(new)
    assert SearchEngine(changed, next_embedder).text(TextSearch(text="蓝色"))["results"]
    pipeline.store.activate(old)
    assert SearchEngine(config, embedder).text(TextSearch(text="红色"))["index_id"] == old
    assert not check_index(pipeline.store, old)
    assert not check_index(pipeline.store, new)


def test_interrupted_index_resumes_without_publishing_partial(processed, config):
    pipeline, _, _, _, old = processed
    changed = config.model_copy(deep=True)
    changed.embedding.quantization = "nf4"

    class FailingEmbedder(FixtureEmbedder):
        def text(self, texts, role="document"):
            raise ModelError("temporary outage")

    with pytest.raises(ModelError, match="temporary outage"):
        IndexBuilder(changed, FailingEmbedder(changed)).build(activate=True)
    assert pipeline.store.active_index()["id"] == old
    building = pipeline.store.one("SELECT id FROM index_versions WHERE status='building'")["id"]
    assert check_index(pipeline.store, building)
    recovered = IndexBuilder(changed, FixtureEmbedder(changed)).build(activate=True)
    assert recovered == building
    assert not check_index(pipeline.store, recovered)


def test_rrf_deduplicates_windows_before_ranking():
    result = fuse({"video": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}, {"video": 1}, 60)
    assert result[0]["score"] == pytest.approx(1 / 61)
    assert result[1]["score"] == pytest.approx(1 / 62)


def test_evaluation_isolates_qwen_from_asr_in_vectors_and_lexical(processed, config, monkeypatch, tmp_path):
    pipeline, embedder, _, source_id, published = processed
    store = pipeline.store
    moments = store.segments(source_id, "moment")
    for segment in moments:
        value = store.json_value("annotations", "segment_id", segment["id"], config.analysis_key)
        value["annotation"]["description"] = "建筑"
        value["annotation"]["emotions"] = ["悲伤"]
        value["dialogue"] = [{"start": segment["start"], "end": segment["end"], "text": "再见"}]
        store.put_json("annotations", "segment_id", segment["id"], config.analysis_key, value)
    version = IndexBuilder(config, embedder).build(kinds=("moment",), activate=False)
    monkeypatch.setattr("mindex.search.make_embedder", lambda config: embedder)
    queries = tmp_path / "labels.jsonl"
    queries.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": text,
                    "text": text,
                    "split": "test",
                    "label_status": "confirmed",
                    "relevant": [{"segment_id": moments[0]["id"], "grade": 3}],
                },
                ensure_ascii=False,
            )
            for text in ("建筑", "悲伤", "再见")
        )
    )
    report = evaluate(config, queries, "test", index_id=version)

    def routes(profile, text):
        row = next(r for r in report["profiles"][profile]["details"] if r["id"] == text)
        return {match["route"] for hit in row["results"] for match in hit["matches"]}

    for text in ("建筑", "悲伤"):
        assert routes("video_asr", text) == {"video", "dialogue"}
        assert "lexical" in routes("fusion", text)
    assert routes("video_asr", "再见") == {"video", "dialogue", "lexical"}
    assert routes("no_dialogue", "再见") == {"video", "description", "style"}
    assert "lexical" not in routes("no_style", "悲伤")
    assert {r["id"] for r in report["candidate_layout"]} == {r["id"] for r in moments}
    assert report["candidate_layout_hash"] and report["curation_hash"]
    assert store.active_index()["id"] == published


def test_uninformative_tied_route_does_not_reorder_matching_routes():
    result = fuse(
        {
            "video": [{"id": "red", "distance": 0}, {"id": "green", "distance": 1}],
            "description": [{"id": "red", "distance": 0}, {"id": "green", "distance": 1}],
            "style": [{"id": "green", "distance": 1}, {"id": "red", "distance": 1}],
        },
        {"video": 1, "description": 1, "style": 1},
        60,
    )
    assert result[0]["id"] == "red"


def test_check_detects_missing_file(processed):
    pipeline, _, _, _, version = processed
    snapshot = pipeline.store.snapshots(version)[0]
    pipeline.store.resolve_media(snapshot["thumbnail"]).unlink()
    assert any("文件缺失" in issue for issue in check_index(pipeline.store, version))


def test_no_index_returns_actionable_message(config):
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    response = client.post("/api/search/text", json={"text": "雨夜"})
    assert response.status_code == 422
    assert "index --activate" in response.json()["detail"]
    assert client.get("/api/status").json()["active_index"] is None


def test_image_query_retrieves_video_and_cleans_uploads(processed, config, tmp_path, monkeypatch):
    import io

    from PIL import Image

    pipeline, embedder, _, _, version = processed
    client = TestClient(create_app(config, embedder, index_id=version))
    # Image matching is independent of the configured text-fusion route weights.
    config.search.weights["video"] = 0
    for image_format in ("JPEG", "PNG", "WEBP"):
        raw = io.BytesIO()
        Image.new("RGB", (160, 120), "blue").save(raw, format=image_format)
        response = client.post(
            "/api/search/image",
            files={"file": ("reference.bin", raw.getvalue(), "application/octet-stream")},
            data={"limit": "10", "granularity": "moment"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["index_id"] == version
        assert result["results"][0]["start"] == pytest.approx(1)
        assert {m["route"] for hit in result["results"] for m in hit["matches"]} == {"video"}
        assert (
            client.get(result["results"][0]["media_url"], headers={"Range": "bytes=0-127"}).status_code == 206
        )
    before = embedder.calls
    picture = tmp_path / "query.png"
    picture.write_bytes(raw.getvalue())
    labels = tmp_path / "images.jsonl"
    labels.write_text(
        json.dumps(
            {
                "id": "fixture-image",
                "split": "image",
                "image": picture.name,
                "label_status": "confirmed",
                "relevant": [{"segment_id": result["results"][0]["id"], "grade": 3}],
            }
        )
    )
    monkeypatch.setattr("mindex.search.make_embedder", lambda config: embedder)
    report = evaluate(config, labels, "image", index_id=version)
    assert report["profiles"]["video_only"]["hit_at_1"] == 1
    assert report["profiles"]["video_only"]["details"][0]["modality"] == "image"
    assert all(p["queries"] == 0 for name, p in report["profiles"].items() if name != "video_only")
    assert embedder.calls == before + 1
    before = embedder.calls
    for body in (
        b"",
        b"broken",
        pipeline.store.resolve_media(pipeline.store.snapshots(version)[0]["path"]).read_bytes(),
    ):
        response = client.post("/api/search/image", files={"file": ("fake.jpg", body, "image/jpeg")})
        assert response.status_code == 422
    assert embedder.calls == before
    assert (
        client.post(
            "/api/search/image", files={"file": ("x.png", raw.getvalue())}, data={"filters": "bad"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/search/image", files={"file": ("x.png", raw.getvalue())}, data={"limit": 51}
        ).status_code
        == 422
    )
    config.search.max_image_upload_mb = 1
    assert (
        client.post("/api/search/image", files={"file": ("big.png", b"x" * (1024 * 1024 + 1))}).status_code
        == 413
    )
    assert not list(config.data_dir.glob("query-image-*"))
    assert pipeline.store.active_index()["id"] == version


def test_pinned_preview_index_keeps_published_library(processed, config):
    pipeline, embedder, _, _, original = processed
    changed = config.model_copy(deep=True)
    changed.embedding.quantization = "nf4"
    published = IndexBuilder(changed, FixtureEmbedder(changed)).build(mode="visual", activate=True)
    client = TestClient(create_app(config, embedder, index_id=original))
    result = client.post("/api/search/text", json={"text": "蓝色", "granularity": "shot"})
    assert result.status_code == 200 and result.json()["index_id"] == original
    first = result.json()["results"][0]
    assert client.get(first["media_url"], headers={"Range": "bytes=0-127"}).status_code == 206
    assert client.get("/api/status").json()["active_index"]["id"] == original
    assert pipeline.store.active_index()["id"] == published
