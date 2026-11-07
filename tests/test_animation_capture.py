"""AI 动画录制沙箱：虚拟时钟下两次录制逐帧一致、网络请求全部被拦截、死循环超时、画布与帧数精确，
以及缺少 playwright 时的中文安装指引。

真实浏览器用例需要可选依赖 ``uv sync --extra animation && uv run playwright install chromium``，未安装时跳过；
安装指引用例总会运行（以 monkeypatch 模拟缺少 playwright）。
"""

from __future__ import annotations

import builtins
import subprocess
import threading
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from shared.media import MediaError
from studio import animation_capture
from studio.animation_capture import INSTALL_HINT, capture

slow = pytest.mark.slow

ANIMATED = """<!DOCTYPE html><html><head><style>
html,body{margin:0;padding:0;overflow:hidden;background:#0b2340}
#box{position:absolute;top:10px;width:30px;height:30px;background:#e33;animation:move 1s linear forwards}
@keyframes move{from{left:0}to{left:120px}}
</style></head><body><div id="box"></div><canvas id="c" width="160" height="120"
style="position:absolute;left:0;top:0"></canvas><script>
const ctx = document.getElementById("c").getContext("2d");
let flash = false;
setTimeout(() => { flash = true; }, 400);
function frame(t) {
  ctx.clearRect(0, 0, 160, 120);
  ctx.fillStyle = flash ? "#ff0" : "#0f0";
  ctx.fillRect((t / 8) % 150, 70, 10, 10);
  ctx.fillStyle = "#fff";
  ctx.fillRect(Math.random() * 150, 100, 4, 4);
  ctx.font = "12px sans-serif";
  ctx.fillText(String(Date.now() % 100000), 4, 60);
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script></body></html>"""


@cache
def chromium_ready() -> bool:
    if not animation_capture.available():
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001 — 浏览器缺失或无法启动：跳过真实录制用例
        return False


needs_browser = pytest.mark.skipif("not chromium_ready()", reason="未安装 animation extra 或 Chromium")


def frame_hashes(path: Path) -> list[str]:
    output = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "framemd5", "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line.rsplit(",", 1)[-1].strip() for line in output.splitlines() if not line.startswith("#")]


def probe(path: Path) -> tuple[int, int, int]:
    raw = (
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames,width,height",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .split(",")
    )
    width, height, frames = (int(value) for value in raw)
    return frames, width, height


def test_missing_playwright_gives_chinese_install_hint(monkeypatch, tmp_path):
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert animation_capture.available() is False
    with pytest.raises(MediaError) as caught:
        capture(ANIMATED, seconds=1, width=160, height=120, fps=25, target=tmp_path / "x.mp4", max_seconds=10)
    assert str(caught.value) == INSTALL_HINT
    assert "uv sync --extra animation" in INSTALL_HINT and "playwright install chromium" in INSTALL_HINT
    assert not (tmp_path / "x.mp4").exists()


@slow
@needs_browser
def test_two_captures_are_frame_identical_with_exact_size_and_count(tmp_path):
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    stats = capture(ANIMATED, seconds=1.2, width=160, height=120, fps=25, target=first, max_seconds=60)
    capture(ANIMATED, seconds=1.2, width=160, height=120, fps=25, target=second, max_seconds=60)
    assert stats["frames"] == 30 and stats["blocked_requests"] == []
    assert probe(first) == (30, 160, 120)
    hashes = frame_hashes(first)
    assert hashes == frame_hashes(second)
    assert len(set(hashes)) > 20  # 动画确实在动（CSS 动画、rAF、定时器与 Date 都由虚拟时钟推进）
    # 显式帧数优先于 seconds × fps；奇数时长同样精确。
    capture(
        ANIMATED, seconds=9, width=96, height=64, fps=10, target=tmp_path / "c.mp4", max_seconds=60, frames=7
    )
    assert probe(tmp_path / "c.mp4") == (7, 96, 64)


class Recorder(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def do_GET(self):
        Recorder.hits.append(self.path)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b"ok")

    do_POST = do_GET

    def log_message(self, *args):
        pass


@slow
@needs_browser
def test_network_requests_are_blocked(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    Recorder.hits = []
    html = f"""<!DOCTYPE html><html><head><link rel="stylesheet" href="{base}/style.css">
<script src="{base}/script.js"></script><style>body{{background:url({base}/bg.png)}}</style></head>
<body><img src="{base}/img.png"><iframe src="{base}/frame"></iframe><script>
fetch("{base}/fetch").catch(() => {{}});
const x = new XMLHttpRequest(); x.open("GET", "{base}/xhr"); x.send();
try {{ new WebSocket("ws://127.0.0.1:{server.server_address[1]}/ws"); }} catch (e) {{}}
navigator.sendBeacon("{base}/beacon", "x");
const i = new Image(); i.src = "{base}/late.png";
</script></body></html>"""
    try:
        stats = capture(
            html, seconds=0.5, width=64, height=48, fps=10, target=tmp_path / "n.mp4", max_seconds=60
        )
    finally:
        server.shutdown()
    assert Recorder.hits == []
    assert stats["frames"] == 5 and probe(tmp_path / "n.mp4")[0] == 5


@slow
@needs_browser
@pytest.mark.parametrize(
    "script",
    [
        "while (true) {}",
        "requestAnimationFrame(function spin() { for (;;) {} });",
        "setTimeout(function again() { setTimeout(again, 0); }, 0);",
    ],
)
def test_runaway_scripts_time_out_with_chinese_error(tmp_path, script):
    html = f"<!DOCTYPE html><html><body><script>{script}</script></body></html>"
    target = tmp_path / "loop.mp4"
    with pytest.raises(MediaError, match="死循环"):
        capture(html, seconds=1, width=64, height=48, fps=10, target=target, max_seconds=60, frame_timeout=2)
    assert not target.exists() and not list(tmp_path.glob("*.partial.mp4"))


@slow
@needs_browser
def test_wall_clock_limit(tmp_path):
    with pytest.raises(MediaError, match="超过 0.5 秒上限"):
        capture(
            ANIMATED, seconds=20, width=160, height=120, fps=25, target=tmp_path / "w.mp4", max_seconds=0.5
        )


@slow
@needs_browser
def test_studio_render_uses_real_sandbox(config):
    """animation 分镜经真实沙箱录制：渲染帧数与配音采样一致，成片帧数等于各镜之和。"""
    from conftest import FixtureSpeaker
    from studio import workspace as ws
    from test_studio_animation import FakeAnimator, anim_shot, page
    from test_studio_sources import frame_count, setup

    config.studio.render.width, config.studio.render.height = 160, 120
    config.studio.render.preset, config.studio.render.fps = "ultrafast", 25
    job_id, w, p = setup(config, [anim_shot()])
    checked = ws.preflight(config, job_id, w, p)
    w["builds"].append(
        {
            "id": "real",
            "plan_id": p["id"],
            "status": "running",
            "force": False,
            "nodes": [{**n, "status": "pending"} for n in checked["nodes"]],
        }
    )
    ws.persist(config, job_id, w)
    ws.run_build(
        config, job_id, "real", speaker=FixtureSpeaker(config), animator=FakeAnimator(html=page("真"))
    )
    w = ws.load(config, job_id)
    p = ws.plan(w, p["id"])
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    render = ws.version(w, p["shots"]["a"]["render"])
    assert render["data"]["capture"]["frames"] == render["data"]["frames"]
    assert frame_count(ws.file_path(config, job_id, render, "animation"))[0] == render["data"]["frames"]
    final = ws.version(w, p["final"])
    assert frame_count(ws.file_path(config, job_id, final, "mp4")) == (
        final["data"]["total_frames"],
        160,
        120,
    )
