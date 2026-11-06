"""Responsive control layout across application pages and dynamic dialogs."""
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import pytest
from seed_layout_fixture import seed
from serve_agent_fixture import build_app
from test_agent_panel_ui import ROOT, playwright_available, serve

from mindex.shared.config import Config
from mindex.shared.storage import Store
from mindex.studio import music_library
from mindex.topics import profiles
from mindex.topics.models import ProfileBody


@pytest.fixture
def ui_tmpdir():
    # seed_layout_fixture 拒绝 /tmp 之外的路径（防误种真实素材库）；
    # macOS 上 pytest 的 tmp_path 在 /private/var 下，须显式落在 /tmp。
    root = Path(tempfile.mkdtemp(prefix="mindex-control-ui-", dir="/tmp"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.mark.slow
@pytest.mark.skipif(not playwright_available(), reason="Requires Node and Playwright")
def test_all_page_control_layouts(ui_tmpdir):
    root = ui_tmpdir / "data"
    app, job_id = build_app(root, media=True)
    seed(root)
    config, store = Config(data_dir=root), Store(root)
    profiles.create_profile(store, ProfileBody(name="布局检查", domain="科技", subtopics=["技术"]))
    audio = ui_tmpdir / "sample.wav"
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
                                     "MINDEX_CONTROL_OUTPUT": str(ui_tmpdir / "layout")},
                                capture_output=True, text=True, timeout=240, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
