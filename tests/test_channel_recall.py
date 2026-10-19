import itertools
import json

import pytest
from conftest import FixtureEmbedder

from mindex.adapters import ModelError
from mindex.channel_index import snapshots_for
from mindex.config import fingerprint
from mindex.curation import Library
from mindex.dialogue import DialogueProcessor
from mindex.indexing import IndexBuilder, VectorIndex, check_index
from mindex.media import command
from mindex.pipeline import Pipeline
from mindex.search import SearchEngine
from mindex.types import Annotation, TextSearch


class CountingEmbedder(FixtureEmbedder):
    video_calls = 0
    text_calls = 0

    def video(self, path):
        self.video_calls += 1
        return super().video(path)

    def text(self, texts, role="document"):
        self.text_calls += len(texts)
        return super().text(texts, role)


def add_dialogue(config, sid, text="阿明，明天再见。"):
    return DialogueProcessor(config).save(
        sid,
        fingerprint(text),
        {
            "status": "complete" if text else "empty",
            "source": "subtitle",
            "model_identity": {"extractor": "test"},
            "utterances": [{"start": 0.2, "end": 0.8, "text": text}] if text else [],
            "words": [],
        },
    )


def add_analysis(store, segment):
    store.put_json(
        "annotations",
        "segment_id",
        segment["id"],
        "independent-model-profile",
        {
            "annotation": Annotation(
                description="红色房间", people=["穿红衣服的人"], emotions=["平静"]
            ).model_dump(),
            "dialogue": [{"start": 0, "end": 1, "text": "绝不能泄漏的对白"}],
        },
    )


COMBINATIONS = [
    combo for n in range(1, 4) for combo in itertools.combinations(("visual", "dialogue", "analysis"), n)
]


@pytest.mark.parametrize("channels", COMBINATIONS)
def test_any_channel_combination_and_partial_analysis(config, video_file, monkeypatch, channels):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    add_dialogue(config, sid)
    segment = pipeline.store.segments(sid, "moment")[0]
    add_analysis(pipeline.store, segment)
    monkeypatch.setattr("mindex.pipeline.make_analyzer", lambda *_: pytest.fail("must not initialize LLM"))
    monkeypatch.setattr("mindex.pipeline.make_transcriber", lambda *_: pytest.fail("must not initialize ASR"))
    embedder = CountingEmbedder(config)
    version = IndexBuilder(config, embedder).build(activate=True, channels=channels)
    snapshots = pipeline.store.snapshots(version)
    available = {v for s in snapshots for v in s["indexed_views"]}
    assert available == {"video" if c == "visual" else c for c in channels}
    assert check_index(pipeline.store, version) == []
    if channels == ("analysis",):
        assert len(snapshots) == 1  # no placeholders for the four unanalyzed moments
    assert all("绝不能泄漏" not in s["views"].get("analysis", "") for s in snapshots)
    result = SearchEngine(config, embedder).text(TextSearch(text="阿明", channels=list(channels)))
    assert {m["channel"] for hit in result["results"] for m in hit["matches"]} <= set(channels)
    if "dialogue" in channels:
        assert result["results"][0]["start"] == pytest.approx(0)
        match = next(m for m in result["results"][0]["matches"] if m["channel"] == "dialogue")
        assert match["matched_text"] == "阿明，明天再见。"
        assert match["evidence"][0]["start"] == 0.2
        assert {r["route"] for r in match["rank_details"]} == {"semantic", "bm25"}


def test_incremental_legacy_reuse_other_sources_curation_failure_and_rollback(config, video_file, tmp_path):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    second = tmp_path / "second.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(video_file),
            "-map",
            "0",
            "-c",
            "copy",
            "-metadata",
            "title=second",
            str(second),
        ]
    )
    sid2 = pipeline.prepare(second)
    embedder = CountingEmbedder(config)
    builder = IndexBuilder(config, embedder)
    old = builder._legacy_build(mode="visual", activate=True)
    ids = VectorIndex(config.data_dir, old).ids()
    original = pipeline.store.snapshots(old)
    calls = embedder.video_calls
    pipeline.store.execute("INSERT INTO source_curation(id,hidden) VALUES(?,1)", (sid2,))
    add_dialogue(config, sid)
    source = Library(pipeline.store).source(sid)
    assert source["channels"]["dialogue"]["content_segments"] == 1
    assert source["published_channels"]["dialogue"]["indexed_segments"] == 0
    new = builder.build(source_id=sid, channels=["dialogue"], activate=True)
    source = Library(pipeline.store).source(sid)
    assert source["channels"]["dialogue"]["content_segments"] == 1
    assert source["published_channels"]["dialogue"]["indexed_segments"] == 2
    assert embedder.video_calls == calls
    assert {s["source_id"] for s in pipeline.store.snapshots(new)} == {sid, sid2}
    assert VectorIndex(config.data_dir, old).ids() == ids
    assert pipeline.store.snapshots(old) == original
    report = json.loads(
        pipeline.store.one("SELECT value FROM settings WHERE key=?", ("index_report." + new,))["value"]
    )
    assert report["reused_vectors"] == len(ids) + 1  # identical dialogue is shared across kinds
    assert report["rollback_index"] == old
    hidden = SearchEngine(config, embedder).text(TextSearch(text="红色"))
    assert all(s["source_id"] != sid2 for s in hidden["results"])
    before = embedder.calls
    assert builder.build(source_id=sid, channels=["dialogue"], activate=True) == new
    assert embedder.calls == before
    # Independent analysis survives a later dialogue update.
    segment = pipeline.store.segments(sid, "moment")[0]
    add_analysis(pipeline.store, segment)
    analyzed = builder.build(source_id=sid, channels=["analysis"], activate=True)
    add_dialogue(config, sid, "全新的台词")

    class Failed(CountingEmbedder):
        def text(self, *args, **kwargs):
            raise ModelError("offline")

    with pytest.raises(ModelError, match="offline"):
        IndexBuilder(config, Failed(config)).build(source_id=sid, channels=["dialogue"], activate=True)
    assert pipeline.store.active_index()["id"] == analyzed
    recovered = builder.build(source_id=sid, channels=["dialogue"], activate=True)
    snap = next(s for s in pipeline.store.snapshots(recovered) if s["id"] == segment["id"])
    assert "analysis" in snap["indexed_views"] and "dialogue" in snap["indexed_views"]
    for version in (old, new, analyzed, recovered):
        assert check_index(pipeline.store, version) == []
    pipeline.store.activate(old)
    assert SearchEngine(config, embedder).text(TextSearch(text="红色"))["index_id"] == old


def test_visual_failure_resumes_completed_vectors_without_publishing_partial(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    moments = pipeline.store.segments(sid, "moment")

    class FailingVideo(CountingEmbedder):
        def video(self, path):
            if self.video_calls == 2:
                raise ModelError("Failed to load image or audio file")
            return super().video(path)

    with pytest.raises(ModelError, match="Failed to load"):
        IndexBuilder(config, FailingVideo(config)).build(
            source_id=sid, kinds=["moment"], channels=["visual"], activate=True
        )
    assert pipeline.store.active_index() is None
    building = pipeline.store.one("SELECT id FROM index_versions WHERE status='building'")["id"]
    complete = pipeline.store.one(
        "SELECT COUNT(*) AS n FROM index_members WHERE index_id=? AND complete=1", (building,)
    )["n"]
    assert complete == 2
    vectors = VectorIndex(config.data_dir, building)
    saved = {identifier: vectors.vector(identifier) for identifier in vectors.ids()}
    assert len(saved) == 2
    recovered_embedder = CountingEmbedder(config)
    recovered = IndexBuilder(config, recovered_embedder).build(
        source_id=sid, kinds=["moment"], channels=["visual"], activate=True
    )
    assert recovered == building
    assert recovered_embedder.video_calls == len(moments) - 2
    assert all(vectors.vector(identifier) == value for identifier, value in saved.items())
    assert not check_index(pipeline.store, recovered)
    assert pipeline.store.active_index()["id"] == recovered
    report = json.loads(
        pipeline.store.one("SELECT value FROM settings WHERE key=?", ("index_report." + recovered,))["value"]
    )
    assert report["resumed_vectors"] == 2


def test_embedding_progress_counts_segments_after_all_windows_and_ready_index(config, video_file):
    from mindex.media import probe

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    total = len(pipeline.store.segments(sid, "shot"))
    events = []

    class ObservedEmbedder(CountingEmbedder):
        def video(self, path):
            before = sum(probe(config.data_dir / s["path"])["duration"] > config.video.short_shot_seconds
                         for s in pipeline.store.segments(sid, "shot"))
            assert before  # fixture contains a long shot with multiple embedding windows
            latest = events[-1]
            assert latest["stage"] == "embedding"
            assert latest["completed"] < latest["total"]
            return super().video(path)

    embedder = ObservedEmbedder(config)
    builder = IndexBuilder(config, embedder)
    version = builder.build(source_id=sid, kinds=["shot"], channels=["visual"], progress=events.append)
    embedding_events = [e for e in events if e["stage"] == "embedding"]
    assert [e["completed"] for e in embedding_events] == list(range(total + 1))
    assert all(e["total"] == total for e in events if "total" in e)
    assert embedder.video_calls > total  # windows are not misreported as extra completed segments
    assert [e["stage"] for e in events[-2:]] == ["index_check", "indexed"]
    events.clear()
    calls = embedder.calls
    assert builder.build(source_id=sid, kinds=["shot"], channels=["visual"],
                         activate=True, progress=events.append) == version
    assert embedder.calls == calls
    assert [e["stage"] for e in events] == ["index_wait", "index_prepare", "index_check", "publish", "published"]
    assert all(e["completed"] == e["total"] == total for e in events if "total" in e)


def test_lexical_degradation_and_channel_exclusion(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    add_dialogue(config, sid)
    add_analysis(pipeline.store, pipeline.store.segments(sid, "moment")[0])
    version = IndexBuilder(config, CountingEmbedder(config)).build(activate=True)

    class Offline(CountingEmbedder):
        def identity(self):
            raise ModelError("connection refused")

    engine = SearchEngine(config, Offline(config))
    result = engine.text(TextSearch(text="阿明", channels=["dialogue"]))
    assert result["degraded"] and result["warnings"] and result["results"]
    assert {m["channel"] for s in result["results"] for m in s["matches"]} == {"dialogue"}
    assert result["results"][0]["matches"][0]["rank_details"][0]["route"] == "bm25"
    assert engine.text(TextSearch(text="阿明", channels=["analysis"]))["results"] == []
    with pytest.raises(ModelError, match="没有可用词法召回"):
        engine.text(TextSearch(text="阿明", channels=["visual"]))
    # Both stores losing a video window must still violate the immutable manifest.
    row = pipeline.store.one("SELECT id FROM representations WHERE index_id=? AND view='video'", (version,))
    VectorIndex(config.data_dir, version).delete([row["id"]])
    pipeline.store.execute("DELETE FROM representations WHERE id=?", (row["id"],))
    assert check_index(pipeline.store, version)


def test_empty_dialogue_creates_no_vector_and_llm_accepts_optional_dialogue(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    add_dialogue(config, sid, "")
    assert Library(pipeline.store).source(sid)["channels"]["dialogue"]["content_segments"] == 0
    embedder = CountingEmbedder(config)
    version = IndexBuilder(config, embedder).build(activate=True)
    assert embedder.text_calls == 0
    assert {
        r["view"]
        for r in pipeline.store.rows("SELECT view FROM representations WHERE index_id=?", (version,))
    } == {"video"}
    segment = pipeline.store.segments(sid, "moment")[0]

    class Analyzer:
        def analyze(self, path, dialogue):
            assert dialogue == []
            return Annotation(description="红色")

    pipeline._analyzer = Analyzer()
    pipeline._annotate(segment)
    assert (
        snapshots_for(IndexBuilder(config, embedder), sid, ("moment",))[0]["annotation_status"] == "complete"
    )


def test_native_evaluation_excludes_disabled_channel_lexical_text(config, video_file, monkeypatch, tmp_path):
    from mindex.evaluation import evaluate

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    add_dialogue(config, sid)
    segment = pipeline.store.segments(sid, "moment")[0]
    add_analysis(pipeline.store, segment)
    embedder = CountingEmbedder(config)
    version = IndexBuilder(config, embedder).build(activate=True)
    monkeypatch.setattr("mindex.search.make_embedder", lambda _: embedder)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        json.dumps(
            {
                "id": "mention",
                "text": "阿明",
                "split": "test",
                "label_status": "confirmed",
                "relevant": [{"segment_id": segment["id"], "grade": 3}],
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    profiles = evaluate(config, labels, "test", index_id=version)["profiles"]
    assert set(profiles) == {
        "video_only",
        "dialogue_only",
        "analysis_only",
        "video_asr",
        "fusion",
        "no_dialogue",
        "no_analysis",
    }
    for name, allowed in [
        ("video_only", {"visual"}),
        ("dialogue_only", {"dialogue"}),
        ("analysis_only", {"analysis"}),
        ("no_dialogue", {"visual", "analysis"}),
        ("no_analysis", {"visual", "dialogue"}),
    ]:
        matches = [m for row in profiles[name]["details"] for hit in row["results"] for m in hit["matches"]]
        assert {m["channel"] for m in matches} <= allowed
        if "dialogue" not in allowed:
            assert not any(r["route"] == "bm25" for m in matches for r in m["rank_details"])
    assert any(
        r["route"] == "bm25"
        for row in profiles["dialogue_only"]["details"]
        for hit in row["results"]
        for m in hit["matches"]
        for r in m["rank_details"]
    )


def test_default_upgrade_all_granularities_without_media_cache(config, video_file, monkeypatch):
    from mindex.media import probe
    from mindex.source_tasks import publish_work

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    pipeline._analyzer = __import__('conftest').FixtureAnalyzer(FixtureEmbedder(config))
    pipeline.analyze(sid)
    embedder = CountingEmbedder(config)
    builder = IndexBuilder(config, embedder)
    old = builder.build(kinds=['moment'], activate=True)
    add_dialogue(config, sid)
    before = {p.relative_to(config.data_dir) for p in config.data_dir.rglob('*')
              if p.is_file() and (p.suffix in {'.mp4', '.jpg', '.png'} or 'cache' in p.parts)}
    paths = []
    original = embedder.video

    def observe(path):
        paths.append(path)
        assert probe(path)['duration'] <= config.video.short_shot_seconds + .1
        return original(path)

    monkeypatch.setattr(embedder, 'video', observe)
    new = publish_work(config, sid, 'analysis', embedder, scope='extra')['index_id']
    snapshots = pipeline.store.snapshots(new)
    assert {s['kind'] for s in snapshots} == {'moment', 'shot', 'scene'}
    assert any('dialogue' in s['indexed_views'] for s in snapshots if s['kind'] == 'shot')
    assert pipeline.store.one('SELECT status FROM index_versions WHERE id=?', (old,))['status'] == 'ready'
    after = {p.relative_to(config.data_dir) for p in config.data_dir.rglob('*')
             if p.is_file() and (p.suffix in {'.mp4', '.jpg', '.png'} or 'cache' in p.parts)}
    assert before == after
    assert all(not p.exists() for p in paths if p.name == 'window.mp4')
    calls = embedder.calls
    assert publish_work(config, sid, 'analysis', embedder, scope='extra')['index_id'] == new
    assert embedder.calls == calls
