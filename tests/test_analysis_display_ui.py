"""LLM language display in actual media Details and search View details pages."""
import os
import subprocess

import pytest
from conftest import FixtureEmbedder
from test_agent_panel_ui import ROOT, playwright_available, serve

from mindex.api import create_app
from mindex.processing.dialogue import DialogueProcessor
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.shared.types import Annotation


@pytest.mark.slow
@pytest.mark.skipif(not playwright_available(), reason="Requires Node and Playwright")
def test_analysis_language_display(config, video_file):
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    store = pipeline.store
    store.execute("UPDATE sources SET name=? WHERE id=?", ("用户素材 / User source", source))
    shots = store.segments(source, "shot")
    annotation = Annotation(
        description="城市夜景。 / City lights at night.", actions=["行走 / walking"],
        people=["穿红衣的人 / A person in red"], objects=["灯光 / lights"], scene=["滨水区 / waterfront"],
        emotions=["平静 / calm"], atmosphere=["安静 / quiet"], shot_size=["远景 / wide shot"],
        angle=["平视 / eye level"], camera_motion=["固定镜头 / static shot"],
        visual_evidence=["水面有倒影 / Reflections on water"], dialogue_inference=["提及夜晚 / Mentions the night"],
        uncertainty=["身份不明 / Identity unknown"],
    )
    for segment in store.segments(source):
        value = Annotation(description="旧版中文描述", actions=["行走"]) if segment["id"] == shots[-1]["id"] else annotation
        store.put_json("annotations", "segment_id", segment["id"], "fixture", {"annotation": value.model_dump()})
    DialogueProcessor(config).save(source, "fixture-dialogue", {
        "status": "complete", "source": "asr", "utterances": [
            {"start": .1, "end": 17.9, "text": "对白原文 / Original dialogue"},
        ], "words": [], "model_identity": {"fixture": True},
    })
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(channels=["analysis", "dialogue"], activate=True)
    server, thread, url = serve(create_app(config, embedder))
    try:
        result = subprocess.run(["node", "tests/analysis_display_ui.cjs"], cwd=ROOT, env={
            **os.environ, "MINDEX_ANALYSIS_URL": url, "MINDEX_ANALYSIS_SOURCE": source,
            "MINDEX_ANALYSIS_SHOT": shots[0]["id"], "MINDEX_ANALYSIS_LEGACY": shots[-1]["id"],
        }, capture_output=True, text=True, timeout=90, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
