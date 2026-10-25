"""创作页“取消执行”“删除方案”：控制逻辑（真实创作页脚本代码 + 轻量 DOM 桩）与真实浏览器回归。"""

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the UI controller checks")
def test_cancel_and_delete_plan_controller():
    result = subprocess.run(
        ["node", "tests/studio_cancel_ui.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_studio_page_has_accessible_cancel_and_delete_plan_buttons():
    html = (ROOT / "frontend/src/studio/studio.html").read_text()
    for button_id, key in (("delete-plan", "ui.delete_plan"), ("cancel-run", "ui.studio_cancel_run"),
                           ("cancel-legacy", "ui.studio_cancel_run")):
        tag = re.search(rf'<button id="{button_id}"[^>]*>([^<]+)</button>', html)
        assert tag and tag.group(1).strip(), button_id  # 有可见文字
        assert key in tag.group(0), button_id


def _playwright_available() -> bool:
    from test_agent_panel_ui import playwright_available

    return playwright_available()


@pytest.fixture
def blocked_studio(tmp_path):
    """隔离数据 + Fixture 适配器的进程内服务；已提交一次配音整列重做，配音桩在放行文件出现前一直等待。"""
    import httpx
    from conftest import FixtureSpeaker, FixtureStoryboarder
    from serve_agent_fixture import seed
    from test_agent_panel_ui import serve
    from test_studio_columns import request_body

    from mindex.api import create_app
    from mindex.shared.config import Config
    from mindex.studio import workspace as ws

    config = Config(data_dir=(tmp_path / "data").resolve())
    config.embedding.dimensions = 8
    config.data_dir.mkdir(parents=True)
    job_id = seed(config)
    release = tmp_path / "release-speaker"
    entered = threading.Event()

    class Gated(FixtureSpeaker):
        def speak(self, text, voice=None):
            entered.set()
            deadline = time.monotonic() + 60
            while not release.exists():
                assert time.monotonic() < deadline, "浏览器脚本未放行配音"
                time.sleep(0.05)
            return super().speak(text, voice)

    app = create_app(config, storyboarder=FixtureStoryboarder(config), speaker=Gated(config))
    server, thread, url = serve(app)
    try:
        w = ws.load(config, job_id)
        p = ws.plan(w, w["active_plan"])
        response = httpx.post(f"{url}/api/studio/jobs/{job_id}/plans/{p['id']}/regenerate-column",
                              json=request_body(config, job_id, w, p, "tts"), timeout=30)
        assert response.status_code == 202, response.text
        assert entered.wait(10)
        yield url, job_id, release
    finally:
        release.touch()  # 失败时也放行，避免后台线程悬挂
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.skipif(not _playwright_available(), reason="需要 node 与 Playwright")
def test_cancel_and_delete_plan_in_browser(blocked_studio):
    url, job_id, release = blocked_studio
    result = subprocess.run(
        ["node", "tests/studio_cancel_browser_ui.cjs"],
        cwd=ROOT,
        env={**os.environ, "MINDEX_STUDIO_URL": url, "MINDEX_STUDIO_JOB": job_id, "MINDEX_STUDIO_RELEASE": str(release)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
