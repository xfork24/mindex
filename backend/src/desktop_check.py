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


def animation(work: Path) -> dict:
    """录制一段 0.4 秒的 AI 动画（需要已安装 Chromium headless shell，位置取 PLAYWRIGHT_BROWSERS_PATH）。"""
    from mindex.shared.media import probe
    from mindex.studio.animation_capture import capture

    html = ("<html><body style='margin:0;background:#123'><div id=b style='width:40px;height:40px;background:#fc0'>"
            "</div><script>let t=0;requestAnimationFrame(function f(){t+=4;b.style.marginLeft=t+'px';"
            "requestAnimationFrame(f)})</script></body></html>")
    target = work / "animation.mp4"
    stats = capture(html, seconds=0.4, width=160, height=90, fps=10, target=target, max_seconds=120)
    return {"animation": probe(target)["duration"] > 0.3, "animation_frames": stats.get("frames")}


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
    if len(args) == 2:
        report.update(subtitles(work, Path(args[1])))
    if record_animation:
        report.update(animation(work))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
