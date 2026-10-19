"""Portable review exports must include the shared UI without application URLs."""

import json

from conftest import FixtureAnalyzer, FixtureEmbedder

from mindex.film import export_source
from mindex.label_review import render_review
from mindex.pipeline import Pipeline


def test_both_review_exports_are_portable(config, video_file, tmp_path):
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(FixtureEmbedder(config)))
    source = pipeline.prepare(video_file)
    run = tmp_path / "review-run"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"source_id": source}))
    queries = run / "queries.jsonl"
    queries.write_text(json.dumps({"id": "layout-query", "text": "绿色 <script>画面", "split": "test"}))
    label_path = render_review(config, run, queries)
    film = export_source(config, source, run / "film")
    for page in (label_path, run / "film" / "review.html"):
        html = page.read_text()
        assert 'name="viewport"' in html
        assert "--control-size:32px" in html
        assert "resize:both" in html
        assert "100dvh" in html
        assert "__STYLE__" not in html
        assert '<link rel="stylesheet"' not in html
        assert "<script src=" not in html
    assert "<script>画面" not in label_path.read_text()
    assert film["segments"] > 0
