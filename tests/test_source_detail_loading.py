"""素材详情按需加载与响应开销回归。"""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from admin.curation import Library
from admin.source_work import same_json
from api import create_app
from conftest import FixtureAnalyzer, FixtureEmbedder
from processing.channel_index import snapshots_for
from processing.dialogue import indexed_dialogue, relative_dialogue
from processing.pipeline import Pipeline
from shared.shot_media import ShotMedia


def test_summary_skips_processing_status(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    monkeypatch.setattr(
        ShotMedia, "references", lambda *a: (_ for _ in ()).throw(AssertionError("media check"))
    )
    client = TestClient(create_app(config))
    response = client.get(f"/api/admin/sources/{sid}?detail=summary")
    assert response.status_code == 200
    value = response.json()
    assert value["shot_count"] and value["moment_count"]
    assert "work" not in value and "channels" not in value
    assert client.get(f"/api/admin/sources/{sid}?detail=invalid").status_code == 422


def test_status_projection_matches_snapshots_and_query_count_is_bounded(config, video_file, monkeypatch):
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(FixtureEmbedder(config)))
    sid = pipeline.prepare(video_file)
    store = pipeline.store
    segments = store.segments(sid)
    pipeline._annotate(segments[0])
    store.put_json(
        "annotations",
        "segment_id",
        segments[-1]["id"],
        "skipped-test",
        {"status": "skipped", "dialogue": [{"start": 0, "end": 1, "text": "legacy"}]},
    )
    builder = SimpleNamespace(store=store)
    full = snapshots_for(builder, sid, ("shot", "moment", "scene"))
    calls = []
    original = store.rows

    def rows(sql, args=()):
        calls.append(sql)
        return original(sql, args)

    monkeypatch.setattr(store, "rows", rows)
    light = snapshots_for(builder, sid, ("shot", "moment", "scene"), status_only=True)
    assert light == [{k: s[k] for k in ("id", "kind", "views", "channel_versions")} for s in full]
    before = len(calls)
    segments = store.segments(sid)
    calls.clear()
    snapshots_for(builder, sid, ("shot", "moment", "scene"), status_only=True, segments=segments * 100)
    assert len(calls) <= before
    calls.clear()
    Library(store).source(sid)
    assert not any("WHERE segment_id=? ORDER BY rowid" in sql for sql in calls)


def test_indexed_dialogue_preserves_overlap_order_and_word_clipping():
    data = {
        "utterances": [
            {"start": 8, "end": 12, "text": "late"},
            {"start": 0, "end": 20, "text": "overlap"},
            {
                "start": 3,
                "end": 7,
                "text": "words",
                "words": [{"start": 3, "end": 4, "text": "a"}, {"start": 6, "end": 7, "text": "b"}],
            },
        ]
    }
    indexed = indexed_dialogue(data)
    for start, end in [(0, 3), (4, 6), (6, 9), (12, 20), (20, 21)]:
        assert relative_dialogue(indexed, start, end) == relative_dialogue(data, start, end)


def test_canonical_comparison():
    assert same_json({"a": [1, "x"], "b": 2}, {"b": 2, "a": [1, "x"]})
    assert not same_json([1], [1.0])
    assert not same_json(-0.0, 0.0)
