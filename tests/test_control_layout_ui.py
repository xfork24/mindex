"""Responsive control layout across application pages and dynamic dialogs."""
import os
import subprocess
import wave

import pytest
from seed_layout_fixture import seed
from serve_agent_fixture import build_app
from test_agent_panel_ui import ROOT, playwright_available, serve

from mindex.shared.config import Config
from mindex.shared.storage import Store
from mindex.studio import music_library
from mindex.topics import profiles
from mindex.topics.models import ProfileBody


@pytest.mark.slow
@pytest.mark.skipif(not playwright_available(), reason="Requires Node and Playwright")
def test_all_page_control_layouts(tmp_path):
    root = tmp_path / "data"
    app, job_id = build_app(root, media=True)
    seed(root)
    config, store = Config(data_dir=root), Store(root)
    profiles.create_profile(store, ProfileBody(name="布局检查", domain="科技", subtopics=["技术"]))
    audio = tmp_path / "sample.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 32000)
    music_library.import_track(config, store, audio, ".wav", music_library.TrackMeta(
        title="布局检查音乐", source="Test fixture", license="Test only", attribution="Fixture"), now="2026-01-01T00:00:00")
    server, thread, url = serve(app)
    try:
        result = subprocess.run(["node", "tests/control_layout_ui.cjs"], cwd=ROOT,
                                env={**os.environ, "MINDEX_CONTROL_URL": url, "MINDEX_CONTROL_JOB": job_id,
                                     "MINDEX_CONTROL_FIXTURE": str(root / "layout-fixture.json"),
                                     "MINDEX_CONTROL_OUTPUT": str(tmp_path / "layout")},
                                capture_output=True, text=True, timeout=240, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
