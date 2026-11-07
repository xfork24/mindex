"""Normalized relevance and bounded duration weighting across search and job recall."""

import math

import pytest
from fastapi.testclient import TestClient

from api import create_app
from conftest import FixtureEmbedder
from search.search import SearchEngine, duration_rank, fuse
from shared.config import SearchConfig
from shared.types import TextSearch
from test_search_similar import _visual_index


@pytest.mark.parametrize("length,expected", [(8, 1), (4, 1), (3.6, 0.81), (2, 0.25), (0, 0)])
def test_duration_score_has_smooth_shortfall_and_no_long_clip_bonus(length, expected):
    settings = SearchConfig()
    hits = [{"id": "a", "score": 0.8, "matches": []}]
    segments = {"a": {"start": 10, "end": 10 + length}}
    result = duration_rank(hits, segments, 4, settings)[0]
    assert result["duration_score"] == pytest.approx(expected)
    assert result["score"] == pytest.approx(0.85 * 0.8 + 0.15 * expected)
    assert hits[0]["score"] == 0.8  # No mutation of evidence or index snapshots.


def test_duration_is_optional_and_never_overcomes_large_relevance_difference():
    settings = SearchConfig()
    hits = [{"id": "a", "score": 0.95, "matches": []}, {"id": "b", "score": 0.1, "matches": []}]
    segments = {"a": {"start": 0, "end": 1}, "b": {"start": 0, "end": 100}}
    assert duration_rank(hits, segments, None, settings) is hits
    assert duration_rank(hits, segments, 4, settings)[0]["id"] == "a"
    settings.duration_weight = 0
    assert duration_rank(hits, segments, 4, settings) is hits


def test_rrf_is_scale_invariant_and_bounded_with_missing_routes_and_extreme_weights():
    routes = {"a": [{"id": "x"}, {"id": "y"}], "b": [{"id": "y"}, {"id": "x"}], "empty": []}
    baseline = fuse(routes, {"a": 0.6, "b": 0.4, "empty": 100}, 30)
    extreme = fuse(routes, {"a": 1.5e308, "b": 1e308, "empty": 1.7e308}, 30)
    assert [h["id"] for h in baseline] == [h["id"] for h in extreme]
    assert [h["score"] for h in baseline] == pytest.approx([h["score"] for h in extreme])
    assert all(0 <= h["score"] <= 1 and math.isfinite(h["score"]) for h in extreme)
    assert fuse(routes, {"a": 0, "b": 0}, 30) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("duration_weight", -0.1),
        ("duration_weight", 0.6),
        ("duration_exponent", 0),
        ("duration_exponent", float("nan")),
    ],
)
def test_invalid_duration_settings_are_rejected(field, value):
    with pytest.raises(ValueError):
        SearchConfig(**{field: value})


def test_duration_settings_persist_and_hot_reload(config):
    app = create_app(config)
    with TestClient(app) as client:
        assert (
            client.put(
                "/api/settings/business",
                json={
                    "search": {
                        "duration_weight": 0.2,
                        "duration_exponent": 3,
                    }
                },
            ).status_code
            == 200
        )
        search = client.get("/api/settings").json()["business"]["search"]
        assert search["duration_weight"] == 0.2 and search["duration_exponent"] == 3
        assert app.state.runtime_config.current().search.duration_weight == 0.2
        assert (
            client.put("/api/settings/business", json={"search": {"duration_weight": 0.9}}).status_code == 422
        )


def test_reranks_full_pool_before_limit_and_keeps_normal_search_unchanged(config, video_file):
    _store, _active, (red, _blue, green) = _visual_index(config, video_file)
    engine = SearchEngine(config, FixtureEmbedder(config))
    query = TextSearch(text="红", granularity="shot", channels=["visual"], limit=1)
    before = engine.text(query)
    assert before["results"][0]["id"] == red["id"]
    result = engine.text(query.model_copy(update={"target_duration": 4}))
    assert result["results"][0]["id"] == green["id"]
    assert result["results"][0]["duration_score"] == 1
    assert result["score_type"] == "duration_blend"
    assert engine.text(query)["results"] == before["results"]
    config.search.duration_weight = 0
    assert engine.text(query.model_copy(update={"target_duration": 4}))["results"] == before["results"]
