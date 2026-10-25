#!/usr/bin/env python3
"""桌面版与同机 Web 版的性能对比（ROADMAP M4 验收：劣化不超过 5%）。

Web 版 = 未冻结的源码安装（缺省为 build-desktop-sidecar.sh 留下的 ``desktop/build/sidecar-venv``：与冻结后端依赖
版本完全相同，只差冻结；``--web-mindex`` 可改用其他环境的 ``mindex``），配 Homebrew ffmpeg；桌面版 = 冻结后端
``desktop/build/backend/mindex`` 配随包 ffmpeg。两者用同一配置与数据、依次（不并发）运行。指标：

- startup：``serve`` 启动到 ``/api/health`` 就绪（各 3 次取中位数）；计时类指标两种后端交替运行，抵消顺序影响；
- search：文字检索 P50/P95（需要 ``--library-config`` 指向已建索引的配置，且嵌入服务在线；只读）；
- prepare：同一段合成 1080p 视频的 prepare 耗时（各自全新的临时数据目录）；
- render：成片编码的主体——1080p libx264（preset fast、crf 20）+ 烧录中文字幕，Homebrew ffmpeg 对比随包 ffmpeg；
- animation：10 秒、25 fps、1280×720 的 AI 动画录制（同一个 Chromium headless shell）；
- memory：后端进程常驻内存（空闲、检索后）。

    uv run python scripts/desktop-bench.py [--library-config ../mindex/config.local.yaml] [--prepare-seconds 120]

结果以 Markdown 表格输出，并写入 ``--output``（缺省 desktop/build/bench.json）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "desktop-bench-token"
QUERIES = ["夜晚的城市街道", "海边日落", "两个人在餐厅吃饭", "雨中奔跑", "热闹的夜市", "办公室里开会",
           "孩子在公园玩耍", "汽车在高速公路上行驶", "灯笼高挂的老街", "雪山与湖泊"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(url: str, body: dict | None = None, timeout: float = 120):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}", **({"Content-Type": "application/json"} if data else {})}
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as response:
        return response.status, response.read()


def rss_mb(pid: int) -> float:
    """进程树常驻内存（MB）：后端进程及其子进程。"""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,rss="], capture_output=True, text=True, check=True).stdout
    rows = [tuple(map(int, line.split())) for line in out.splitlines() if line.strip()]
    tree, frontier = {pid}, [pid]
    while frontier:
        parent = frontier.pop()
        for child, ppid, _ in rows:
            if ppid == parent and child not in tree:
                tree.add(child)
                frontier.append(child)
    return round(sum(rss for child, _, rss in rows if child in tree) / 1024, 1)


class Backend:
    def __init__(self, name: str, argv: list[str], env: dict, cwd: Path):
        self.name, self.argv, self.env, self.cwd = name, argv, env, cwd

    def run(self, *args, timeout=1800):
        started = time.monotonic()
        result = subprocess.run([*self.argv, *args], env=self.env, cwd=self.cwd, capture_output=True, text=True,
                                timeout=timeout, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"{self.name} {' '.join(args[:3])} 失败：{result.stderr[-1500:]}")
        return time.monotonic() - started, result.stdout

    def serve(self, config: Path, data_dir: Path | None = None):
        port = free_port()
        args = ["--config", str(config), *(["--data-dir", str(data_dir)] if data_dir else []),
                "serve", "--host", "127.0.0.1", "--port", str(port)]
        log = tempfile.TemporaryFile()
        started = time.monotonic()
        process = subprocess.Popen([*self.argv, *args], env={**self.env, "MINDEX_WEB_TOKEN": TOKEN}, cwd=self.cwd,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        base = f"http://127.0.0.1:{port}"
        while time.monotonic() - started < 180:
            if process.poll() is not None:
                log.seek(0)
                raise RuntimeError(f"{self.name} serve 退出：{log.read().decode(errors='replace')[-1500:]}")
            try:
                pid = json.loads(request(f"{base}/api/health", timeout=2)[1])["pid"]
                return process, base, time.monotonic() - started, pid
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"{self.name} serve 180 秒内未就绪")


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(20)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        os.killpg(process.pid, signal.SIGKILL)


def percentile(values, q):
    values = sorted(values)
    index = min(len(values) - 1, max(0, round(q * (len(values) - 1))))
    return values[index]


def make_video(ffmpeg: str, path: Path, seconds: int):
    """合成 1080p 测试视频：每 4 秒换一种画面（产生分镜），带音轨。"""
    sources, filters = [], []
    for index in range(max(1, seconds // 4)):
        # 两种快速生成的画面交替（mandelbrot 等滤镜在 1080p 下太慢；加噪声会让画面无法压缩，不代表真实素材）。
        pattern = "testsrc2" if index % 2 == 0 else "smptehdbars"
        sources += ["-f", "lavfi", "-i", f"{pattern}=size=1920x1080:rate=25:duration=4"]
        filters.append(f"[{index}:v]")
    count = len(filters)
    subprocess.run([ffmpeg, "-v", "error", "-y", *sources, "-f", "lavfi", "-i", f"sine=frequency=440:duration={count * 4}",
                    "-filter_complex", "".join(filters) + f"concat=n={count}:v=1:a=0[v]", "-map", "[v]",
                    "-map", f"{count}:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def render_workload(ffmpeg: str, source: Path, srt: Path, fonts: Path, output: Path) -> float:
    started = time.monotonic()
    subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(source), "-vf",
                    f"subtitles={srt}:fontsdir={fonts}:force_style='FontName=Noto Sans CJK SC,FontSize=7'",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "192k", "-movflags", "+faststart", str(output)], check=True)
    return time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", type=Path, default=ROOT / "desktop" / "build" / "backend" / "mindex")
    parser.add_argument("--web-mindex", type=Path, default=ROOT / "desktop" / "build" / "sidecar-venv" / "bin" / "mindex")
    parser.add_argument("--ffmpeg-dir", type=Path, default=ROOT / "desktop" / "build" / "ffmpeg" / "bin")
    parser.add_argument("--font", type=Path, default=ROOT / "desktop" / "build" / "fonts" / "NotoSansCJKsc-Regular.otf")
    parser.add_argument("--library-config", type=Path, help="已建索引的配置（只读检索）；省略则跳过检索与检索内存")
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--prepare-seconds", type=int, default=120)
    parser.add_argument("--skip-animation", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "desktop" / "build" / "bench.json")
    args = parser.parse_args()

    clean = {k: v for k, v in os.environ.items() if not k.startswith("MINDEX_")}
    web_ffmpeg = shutil.which("ffmpeg")
    bundled = {"MINDEX_FFMPEG": str(args.ffmpeg_dir / "ffmpeg"), "MINDEX_FFPROBE": str(args.ffmpeg_dir / "ffprobe")}
    web = Backend("Web", [str(args.web_mindex)], clean, ROOT)
    desktop = Backend("桌面", [str(args.backend.resolve())], {**clean, **bundled}, ROOT)
    machine = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                             check=False).stdout.strip()
    report: dict = {"machine": machine, "results": {}}
    results = report["results"]

    def save():  # 每完成一项就写盘：后面的指标失败时前面的结果不丢
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="mindex-bench-") as directory:
        work = Path(directory)
        config = work / "config.yaml"
        shutil.copy(ROOT / "desktop" / "config.template.yaml", config)

        print("· 启动到就绪", flush=True)
        for backend in (web, desktop):
            times, idle = [], None
            for attempt in range(3):
                process, _base, elapsed, pid = backend.serve(config, work / f"start-{backend.name}-{attempt}")
                times.append(elapsed)
                time.sleep(2)
                idle = rss_mb(pid)
                stop(process)
            results.setdefault("startup_s", {})[backend.name] = round(statistics.median(times), 2)
            results.setdefault("idle_rss_mb", {})[backend.name] = idle
            save()

        if args.library_config:
            print("· 文字检索（只读）", flush=True)
            for backend in (web, desktop):
                process, base, _elapsed, pid = backend.serve(args.library_config.resolve())
                try:
                    for query in QUERIES[:3]:  # 预热（加载索引、jieba 词典、嵌入服务缓存）
                        request(f"{base}/api/search/text", {"text": query, "limit": 20})
                    latencies = []
                    for index in range(args.queries):
                        started = time.monotonic()
                        status, _ = request(f"{base}/api/search/text", {"text": QUERIES[index % len(QUERIES)], "limit": 20})
                        assert status == 200
                        latencies.append((time.monotonic() - started) * 1000)
                    results.setdefault("search_p50_ms", {})[backend.name] = round(percentile(latencies, 0.5), 1)
                    results.setdefault("search_p95_ms", {})[backend.name] = round(percentile(latencies, 0.95), 1)
                    results.setdefault("search_rss_mb", {})[backend.name] = rss_mb(pid)
                    save()
                finally:
                    stop(process)

        print(f"· prepare（{args.prepare_seconds} 秒 1080p 合成视频，两种后端交替各 2 次取最小值）", flush=True)
        video = work / "bench-1080p.mp4"
        make_video(web_ffmpeg, video, args.prepare_seconds)
        timings: dict[str, list[float]] = {}
        for attempt in range(2):
            for backend in (web, desktop):
                elapsed, _ = backend.run("--config", str(config), "--data-dir",
                                         str(work / f"prepare-{backend.name}-{attempt}"), "prepare", str(video))
                timings.setdefault(backend.name, []).append(elapsed)
        for name, values in timings.items():
            results.setdefault("prepare_s", {})[name] = round(min(values), 2)
        save()

        print("· 成片编码（1080p libx264 + 烧录中文字幕）", flush=True)
        srt = work / "bench.srt"
        srt.write_text("".join(f"{i + 1}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 2:02d},000\n第 {i + 1} 句中文字幕\n\n"
                               for i in range(29)), encoding="utf-8")
        render_times: dict[str, list[float]] = {}
        for attempt in range(3):  # 两种构建交替，取中位数，抵消顺序与温度影响
            for name, binary in (("Web", web_ffmpeg), ("桌面", str(args.ffmpeg_dir / "ffmpeg"))):
                render_times.setdefault(name, []).append(
                    render_workload(binary, video, srt, args.font.parent, work / f"render-{name}-{attempt}.mp4"))
        results["render_s"] = {name: round(statistics.median(values), 2) for name, values in render_times.items()}
        save()

        if not args.skip_animation:
            print("· AI 动画录制（10 秒 25 fps 1280×720）", flush=True)
            web_python = args.web_mindex.parent / "python"
            runs: dict[str, list[float]] = {}
            for attempt in range(5):  # 交替运行取中位数：录制耗时受浏览器启动与系统负载影响，单次波动大
                for name, argv, env in (("Web", [str(web_python), "-m", "mindex.desktop_check"], clean),
                                        ("桌面", [str(args.backend.resolve()), "-m", "mindex.desktop_check"],
                                         {**clean, **bundled})):
                    out = subprocess.run([*argv, str(work / f"anim-{name}-{attempt}"), "--animation-bench"], env=env,
                                         capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
                    runs.setdefault(name, []).append(json.loads(out)["animation_bench_s"])
            results["animation_s"] = {name: round(statistics.median(values), 2) for name, values in runs.items()}
            save()

    save()
    print(f"\n机器：{report['machine']}\n")
    print("| 指标 | Web 版 | 桌面版 | 差异 |\n|---|---|---|---|")
    for metric, values in results.items():
        web_value, desk_value = values.get("Web"), values.get("桌面")
        delta = f"{(desk_value - web_value) / web_value * 100:+.1f}%" if web_value else "—"
        print(f"| {metric} | {web_value} | {desk_value} | {delta} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
