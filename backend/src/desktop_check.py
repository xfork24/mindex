"""冻结环境自检：``mindex -m mindex.desktop_check <临时目录>``，输出一行 JSON。

验证桌面版冻结后端里容易漏打包的部分能真正工作（可选：``字体文件`` 做一次烧录字幕，``--animation`` 录制一段 AI 动画）：chromadb 本地存储（写入与查询）、jieba 词典、opencv/scenedetect、
Pillow、playwright 包、前端资源、文案模板与选题日历样例，以及以源码收集的模块（哈希须与源码运行一致）。不访问网络与模型服务。
源码环境中同样可以运行。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def run(work: Path) -> dict:
    from mindex.models import llama_cpp, model_server
    from mindex.processing import pipeline
    from mindex.shared.media import content_hash
    from mindex.shared.web_assets import frontend_root
    from mindex.studio import script_templates

    report: dict = {"frozen": bool(getattr(sys, "frozen", False))}

    from mindex.processing.indexing import VectorIndex

    index = VectorIndex(work, "frozen-check", create=True)
    segment = {"id": "seg-1", "source_id": "src", "start": 0.0, "end": 1.0, "kind": "shot"}
    index.upsert_many([("rep-1", [1.0, 0.0, 0.0, 0.0], segment, "video", "doc"),
                       ("rep-2", [0.0, 1.0, 0.0, 0.0], {**segment, "id": "seg-2"}, "video", "doc")])
    found = index.collection.query(query_embeddings=[[0.9, 0.1, 0.0, 0.0]], n_results=1)
    report["chromadb"] = found["ids"][0] == ["rep-1"]

    import jieba

    report["jieba"] = "检索" in list(jieba.cut("视频素材检索"))

    import cv2
    import numpy as np
    import scenedetect

    report["opencv"] = cv2.cvtColor(np.zeros((2, 2, 3), np.uint8), cv2.COLOR_BGR2GRAY).shape == (2, 2)
    report["scenedetect"] = bool(scenedetect.__version__)

    from PIL import Image

    Image.new("RGB", (4, 4)).save(work / "pixel.png")
    report["pillow"] = (work / "pixel.png").is_file()

    try:
        import playwright.sync_api  # noqa: F401

        report["playwright"] = True
    except ImportError:
        report["playwright"] = False

    root = frontend_root()
    report["frontend"] = all((root / group / f"{group}.html").is_file() for group in ("admin", "persons", "studio", "settings"))
    report["frontend_root"] = str(root)
    report["templates"] = len(list(Path(script_templates.BUILTIN_DIR).glob("*.yaml")))
    from mindex.topics.sources import calendar

    report["topic_samples"] = sum(item["events"] > 0 for item in calendar.sample_list())
    from mindex.studio import brands
    from mindex.studio.workspace.handlers import overlay

    report["brands"] = len(brands.builtins())
    report["overlay_template"] = overlay.TEMPLATE.is_file()
    from mindex.studio import platforms

    report["platforms"] = len(platforms.presets())
    report["align_worker"] = Path(pipeline.__file__).with_name("align_worker.py").is_file()
    report["source_hashes"] = {
        "mindex/models/llama_cpp.py": content_hash(Path(llama_cpp.__file__)),
        "mindex/models/model_server.py": content_hash(Path(model_server.__file__)),
    }
    return report


def subtitles(work: Path, font_file: Path) -> dict:
    """用指定字体做一次带烧录字幕的渲染（与成片同一条 check_font 路径：libass + 字体文件）。"""
    from mindex.shared.config import SubtitleConfig
    from mindex.shared.media import ffmpeg_bin
    from mindex.studio.studio_render import check_font

    srt = work / "check.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:00,200\n中文字幕字体检查\n", encoding="utf-8")
    check_font(srt, SubtitleConfig(font_file=str(font_file)))
    return {"subtitles": True, "ffmpeg": ffmpeg_bin()}


def dsh(work: Path) -> dict:
    """启动随包原生 dsh 并完成 SDK 握手，不调用模型、下载资源或使用系统 Node。"""
    import os
    import subprocess

    import yaml
    from deepseek_harness import HarnessClient, HarnessConfig
    from deepseek_harness_runtime import resolve_bundled_launch_args

    from mindex.agent.dsh_runtime import patch, readiness
    from mindex.shared.config import AgentConfig

    settings = AgentConfig(llm={"model": "desktop-check", "base_url": "http://127.0.0.1:1/v1"})
    status = readiness(settings)
    if not status["ready"]:
        raise RuntimeError("；".join(status["errors"]))
    executable = Path(resolve_bundled_launch_args("exe")[0])
    rg = executable.with_name(executable.stem + "-rg.exe" if sys.platform == "win32" else executable.name + "-rg")
    result = subprocess.run([str(rg), "--version"], capture_output=True, text=True, timeout=30, check=True)
    home = work / "dsh-home"
    home.mkdir(parents=True, exist_ok=True)
    content = patch(settings, "http://127.0.0.1:1/mcp")
    # 只加载与实际运行相同的模型适配器；握手不需要 MCP 服务或模型请求。
    content[-1]["insert"] = content[-1]["insert"][:1]
    config = home / "check.patch.yml"
    config.write_text(yaml.safe_dump(content), encoding="utf-8")
    # 固定生产 exe；不受开发者终端 DSH_RUNTIME_MODE=node 的影响。
    with HarnessClient(HarnessConfig(dsh_bin=str(executable), dsh_home=str(home), cwd=str(home),
                                    profile="sdk-minimal", patches=(str(config),),
                                    env={"DSH_TELEMETRY_DISABLED": "1"})) as client:
        response = client.initialize(cwd=str(home), provider="mindex", model="desktop-check")
    return {"dsh": response.serverInfo is not None, "dsh_version": status["version"],
            "dsh_native": os.access(executable, os.X_OK), "dsh_ripgrep": "ripgrep" in result.stdout}


def post(work: Path) -> dict:
    """成片后期（M3）在冻结环境里的 ffmpeg 路径：镜内转场、重叠转场（xfade）与水印叠加，都经 shared/media。"""
    from types import SimpleNamespace

    from PIL import Image

    from mindex.shared.media import atomic_media, command, ffprobe_bin
    from mindex.studio.studio_render import apply_edges, watermark_graph

    render = SimpleNamespace(width=160, height=90, fps=25, preset="ultrafast", crf=28)
    clips = []
    for index, color in enumerate(("red", "blue")):
        target = work / f"post-{index}.mp4"
        atomic_media(["-f", "lavfi", "-i", f"color=c={color}:s=160x90:r=25:d=1", "-c:v", "libx264", "-preset",
                      "ultrafast", "-pix_fmt", "yuv420p"], target)
        clips.append(target)
    edged = work / "post-edged.mp4"
    apply_edges(clips[0], edged, render, frames=25, enter={"kind": "fade", "duration": 0.3},
                leave={"kind": "slide", "duration": 0.3})
    joined = work / "post-xfade.mp4"
    atomic_media(["-i", str(edged), "-i", str(clips[1]), "-filter_complex",
                  "[0:v][1:v]xfade=transition=dissolve:duration=0.2:offset=0.8[v]", "-map", "[v]", "-c:v", "libx264",
                  "-preset", "ultrafast", "-pix_fmt", "yuv420p"], joined)
    logo = work / "logo.png"
    Image.new("RGBA", (20, 20), (255, 255, 255, 255)).save(logo)
    marked = work / "post-watermark.mp4"
    atomic_media(["-i", str(joined), "-vf", watermark_graph(logo, {"position": "bottom-left"}, 160, 90), "-c:v",
                  "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"], marked)
    frames = command([ffprobe_bin(), "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                      "stream=nb_read_frames", "-of", "csv=p=0", str(marked)]).decode().strip()
    return {"post": frames == "45", "post_frames": frames}


def extensions(work: Path) -> dict:
    """扩展能力（M3 第三期）在冻结环境里的路径：曲目节奏分析（numpy + ffmpeg 解码）、HTTP 工具适配器
    （本机替身服务：提交 → 产物下载 → 图片校验），以及随包的 mcp 客户端组件可导入。"""
    import io
    import math
    import struct
    import threading
    import wave
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from PIL import Image

    from mindex.studio import beats
    from mindex.studio.tools import adapter, artifacts
    from mindex.studio.tools.mcp import unavailable

    rate, bpm = 22050, 120
    samples = [0.0] * (rate * 6)
    for beat in range(12):
        start = int(beat * 60 / bpm * rate)
        for i in range(400):
            samples[start + i] += math.sin(2 * math.pi * 1000 * i / rate) * math.exp(-i / 90)
    metronome = work / "metronome.wav"
    with wave.open(str(metronome), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 20000)) for value in samples))
    found = beats.analyze(metronome)

    buffer = io.BytesIO()
    Image.new("RGB", (64, 36), (30, 120, 200)).save(buffer, "PNG")
    picture = buffer.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body: bytes, kind: str):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.reply(json.dumps({"status": "succeeded", "output": {"url": "/still.png"}}).encode(), "application/json")

        def do_GET(self):
            self.reply(picture, "image/png")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        record = {"protocol": "http", "secrets": {}, "artifact_hosts": [],
                  "connection": {"base_url": base, "headers": {}, "submit_path": "/tools/{tool}/jobs", "poll_path": None,
                                 "poll_interval": 1, "id_field": "id", "status_field": "status", "done": ["succeeded"],
                                 "failed": ["failed"], "result_field": "output.url", "error_field": "error"}}
        produced, _ = adapter(record).call("still", {}, [], timeout=30)
        raw = artifacts.download(produced[0], work / "tool.bin", hosts={base.split("//", 1)[1]}, max_bytes=1 << 20,
                                 timeout=30)
        _, info = artifacts.normalize(raw, work, "image", 1_000_000)
    finally:
        server.shutdown()
        server.server_close()
    return {"beats": abs(found["bpm"] - bpm) / bpm <= 0.02, "beats_bpm": found["bpm"],
            "tool_http": info == {"width": 64, "height": 36}, "mcp_client": unavailable() is None}


def animation(work: Path) -> dict:
    """录制一段 0.4 秒的 AI 动画（需要已安装 Chromium headless shell，位置取 PLAYWRIGHT_BROWSERS_PATH）。"""
    from mindex.shared.media import probe
    from mindex.studio.animation_capture import capture

    html = ("<html><body style='margin:0;background:#123'><div id=b style='width:40px;height:40px;background:#fc0'>"
            "</div><script>let t=0;requestAnimationFrame(function f(){t+=4;b.style.marginLeft=t+'px';"
            "requestAnimationFrame(f)})</script></body></html>")
    target = work / "animation.mp4"
    stats = capture(html, seconds=0.4, width=160, height=90, fps=10, target=target, max_seconds=120)
    # HTML 文字层：透明录制为 argb MOV。
    from mindex.shared.media import command, ffprobe_bin

    layer = work / "overlay.mov"
    capture(html.replace("background:#123", "background:transparent"), seconds=0.4, width=160, height=90, fps=10,
            target=layer, max_seconds=120, transparent=True)
    pix = command([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt", "-of",
                   "csv=p=0", str(layer)]).decode().strip()
    return {"animation": probe(target)["duration"] > 0.3, "animation_frames": stats.get("frames"),
            "overlay_alpha": pix == "argb"}


BENCH_HTML = ("<html><body style='margin:0;background:#123'><div id=b style='width:80px;height:80px;background:#fc0'>"
              "</div><script>let t=0;requestAnimationFrame(function f(){t+=3;b.style.marginLeft=(t%1200)+'px';"
              "requestAnimationFrame(f)})</script></body></html>")


def animation_bench(work: Path) -> dict:
    """性能对比用：录制 10 秒、25 fps、1280×720 的 AI 动画，返回耗时（scripts/desktop-bench.py）。"""
    import time

    from mindex.studio.animation_capture import capture

    started = time.monotonic()
    capture(BENCH_HTML, seconds=10, width=1280, height=720, fps=25, target=work / "bench.mp4", max_seconds=600)
    return {"animation_bench_s": time.monotonic() - started}


def main() -> None:
    args = sys.argv[1:]
    if "--dsh-only" in args:
        args.remove("--dsh-only")
        if len(args) != 1:
            sys.exit("用法：mindex -m mindex.desktop_check <临时目录> --dsh-only")
        work = Path(args[0]).resolve()
        work.mkdir(parents=True, exist_ok=True)
        report = {"frozen": bool(getattr(sys, "frozen", False)), **dsh(work)}
        print(json.dumps(report, ensure_ascii=False))
        if not all(report[key] for key in ("dsh", "dsh_native", "dsh_ripgrep")):
            sys.exit(1)
        return
    record_animation = "--animation" in args
    bench = "--animation-bench" in args
    args = [arg for arg in args if arg not in ("--animation", "--animation-bench")]
    if bench:
        work = Path(args[0])
        work.mkdir(parents=True, exist_ok=True)
        print(json.dumps(animation_bench(work)))
        return
    if len(args) not in (1, 2):
        sys.exit("用法：mindex -m mindex.desktop_check <临时目录> [字体文件] [--animation]")
    work = Path(args[0])
    work.mkdir(parents=True, exist_ok=True)
    report = run(work)
    report.update(post(work))
    report.update(extensions(work))
    report.update(dsh(work.resolve()))
    if len(args) == 2:
        report.update(subtitles(work, Path(args[1])))
    if record_animation:
        report.update(animation(work))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
