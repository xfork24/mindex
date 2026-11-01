#!/usr/bin/env python3
"""桌面版冻结后端冒烟：用临时配置与数据目录运行 desktop/build/backend/mindex[.exe]（三平台）。

依次验证：冻结自检（chromadb、jieba、opencv、Pillow、playwright、前端、模板、品牌模板与叠加层模板、成片后期的
转场与水印、align_worker、源码收集模块的哈希与仓库一致、原生 dsh SDK 握手及 ripgrep）、pyproject 声明的 package-data 全部打包、``-m`` 分发、
注册并 prepare 测试视频、``serve`` 健康与页面（含品牌模板页与 ``mindex studio --url`` 连接）、SIGTERM 后按时退出
（Windows 上 SIGTERM 即 TerminateProcess 强停，该步验证进程可被终止与退出码回收）。

    python scripts/check-desktop-sidecar.py [--backend 路径] [--ffmpeg-dir 目录] [--font 字体] [--isolated-path]

``--ffmpeg-dir``：以该目录的 ffmpeg/ffprobe 作为 MINDEX_FFMPEG/MINDEX_FFPROBE（随包构建）；
``--isolated-path``：PATH 只保留系统目录（POSIX 为 /usr/bin:/bin，Windows 为 System32），确认不依赖 Homebrew/MSYS2；
``--font``：额外做一次带烧录字幕的渲染；
``--animation``：经冻结后端的 ``-m playwright`` 把 Chromium headless shell 装到临时目录（需联网，约 100 MB），
再录制一段 AI 动画。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "sidecar-check-token"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(url: str, timeout=5):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def step(name: str):
    print(f"· {name}", flush=True)


def _force_utf8_streams():
    """控制台代码页可能是 cp1252/cp936，中文输出会 UnicodeEncodeError；强制 UTF-8 且不因个别
    不可编码字符失败（Windows 上 Python 缺省按代码页编码 stdout）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # 非标准流（被测试捕获等）
            pass


_force_utf8_streams()


def package_data(internal: Path) -> list[str]:
    """pyproject 的 package-data 在冻结目录中缺失的文件。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]
    missing = []
    for package, patterns in project["package-data"].items():
        source = ROOT / project["package-dir"].get(package, project["package-dir"]["mindex"] + "/" +
                                                    "/".join(package.split(".")[1:]))
        target = internal.joinpath(*package.split("."))
        for pattern in patterns:
            for path in source.glob(pattern):
                relative = path.relative_to(source)
                if not (target / relative).is_file():
                    missing.append(f"{package}/{relative.as_posix()}")
    return missing


def make_video(ffmpeg: str, path: Path):
    """两段不同画面的 4 秒测试视频（带音轨），足以产生分镜。"""
    subprocess.run([ffmpeg, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
                    "-f", "lavfi", "-i", "color=c=red:size=320x180:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    exe = ".exe" if sys.platform == "win32" else ""
    parser.add_argument("--backend", type=Path, default=ROOT / "desktop" / "build" / "backend" / f"mindex{exe}")
    parser.add_argument("--ffmpeg-dir", type=Path)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--isolated-path", action="store_true")
    parser.add_argument("--animation", action="store_true")
    args = parser.parse_args()
    backend = args.backend.resolve()
    internal = backend.parent / "_internal"
    if not backend.is_file():
        builder = "scripts/build-desktop-sidecar.ps1" if sys.platform == "win32" else "scripts/build-desktop-sidecar.sh"
        print(f"找不到冻结后端：{backend}（先运行 {builder}）", file=sys.stderr)
        return 1

    env = {key: value for key, value in os.environ.items() if not key.startswith("MINDEX_")}
    if args.isolated_path:
        # 验证不依赖 Homebrew/MSYS2/Chocolatey：只保留系统目录。
        env["PATH"] = r"C:\Windows\System32;C:\Windows" if sys.platform == "win32" else "/usr/bin:/bin"
    if args.ffmpeg_dir:
        env["MINDEX_FFMPEG"] = str((args.ffmpeg_dir / f"ffmpeg{exe}").resolve())
        env["MINDEX_FFPROBE"] = str((args.ffmpeg_dir / f"ffprobe{exe}").resolve())
    ffmpeg = env.get("MINDEX_FFMPEG") or shutil.which("ffmpeg", path=env.get("PATH"))
    if not ffmpeg:
        print("PATH 中没有 ffmpeg，请用 --ffmpeg-dir 指定随包构建", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="mindex-sidecar-check-") as directory:
        work = Path(directory)
        config = work / "config.yaml"
        shutil.copy(ROOT / "desktop" / "config.template.yaml", config)
        run = lambda *argv, **kw: subprocess.run([str(backend), *argv], env=env, cwd=work, capture_output=True,
                                                 text=True, encoding="utf-8", errors="replace",
                                                 timeout=kw.pop("timeout", 300), check=False, **kw)

        if args.animation:
            step("安装 Chromium headless shell（-m playwright）")
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(work / "browsers")
            result = run("-m", "playwright", "install", "--only-shell", "chromium", timeout=1800)
            if result.returncode != 0:
                print(result.stdout[-3000:], result.stderr[-3000:], file=sys.stderr)
                return 1
        step("冻结自检（-m 分发）")
        extra = ([str(args.font.resolve())] if args.font else []) + (["--animation"] if args.animation else [])
        result = run("-m", "mindex.desktop_check", str(work / "selfcheck"), *extra)
        if result.returncode != 0:
            print(result.stdout, result.stderr, file=sys.stderr)
            return 1
        report = json.loads(result.stdout.strip().splitlines()[-1])
        print("  ", json.dumps(report, ensure_ascii=False))
        problems = [key for key in ("frozen", "chromadb", "jieba", "opencv", "scenedetect", "pillow", "playwright",
                                    "frontend", "align_worker") if report.get(key) is not True]
        if report.get("templates", 0) < 1:
            problems.append("templates")
        if report.get("topic_samples", 0) < 3:
            problems.append("topic_samples")
        # M3 成片后期：品牌模板、叠加层模板与转场/水印的 ffmpeg 路径。
        if report.get("brands", 0) < 3 or report.get("overlay_template") is not True or report.get("platforms") != 3:
            problems.append("brands/overlay_template")
        if report.get("post") is not True:
            problems.append("post")
        # M3 第三期：曲目节奏分析、HTTP 工具适配器与随包 mcp 客户端。
        for key in ("beats", "tool_http", "mcp_client", "dsh", "dsh_native", "dsh_ripgrep"):
            if report.get(key) is not True:
                problems.append(key)
        for relative, digest in report["source_hashes"].items():
            source = ROOT / "backend" / "src" / Path(relative).relative_to("mindex")
            if digest != sha256(source):
                problems.append(f"源码哈希不一致：{relative}")
        if args.font and report.get("subtitles") is not True:
            problems.append("subtitles")
        if args.animation and (report.get("animation") is not True or report.get("overlay_alpha") is not True):
            problems.append("animation")
        if args.ffmpeg_dir and report.get("ffmpeg") != env["MINDEX_FFMPEG"]:
            problems.append("ffmpeg 未使用 MINDEX_FFMPEG")
        if problems:
            print(f"冻结自检失败：{problems}", file=sys.stderr)
            return 1

        step("package-data")
        missing = package_data(internal)
        if missing:
            print(f"冻结目录缺少 package-data：{missing[:20]}", file=sys.stderr)
            return 1

        step("注册并 prepare 测试视频（adaptive 本地分镜）")
        video = work / "测试 视频.mp4"
        make_video(ffmpeg, video)
        result = run("--config", str(config), "prepare", str(video), timeout=600)
        if result.returncode != 0:
            print(result.stdout[-3000:], result.stderr[-3000:], file=sys.stderr)
            return 1

        step("serve：健康、页面、模板、状态")
        port = free_port()
        log = (work / "serve.log").open("wb")
        process = subprocess.Popen([str(backend), "--config", str(config), "serve", "--host", "127.0.0.1",
                                    "--port", str(port)], env={**env, "MINDEX_WEB_TOKEN": TOKEN}, cwd=work,
                                   stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        try:
            started = time.monotonic()
            health = None
            while time.monotonic() - started < 90:
                if process.poll() is not None:
                    break
                try:
                    health = json.loads(get(f"{base}/api/health")[1])
                    break
                except OSError:
                    time.sleep(0.25)
            if health is None:
                print((work / "serve.log").read_text(errors="replace")[-4000:], file=sys.stderr)
                return 1
            print(f"   就绪 {time.monotonic() - started:.1f}s，版本 {health['version']}，pid {health['pid']}")
            assert health["pid"] == process.pid and Path(health["data_dir"]) == (work / "data").resolve(), health
            for page in ("/", "/admin", "/persons", "/studio", "/settings", "/studio/templates", "/studio/topics", "/docs"):
                status, _ = get(base + page)
                assert status == 200, (page, status)
            templates = json.loads(get(f"{base}/api/studio/templates")[1])
            assert templates.get("items"), templates
            # M3：品牌模板页与接口、studio CLI（连接运行中的服务，与桌面外壳同一后端）。
            status, _ = get(base + "/studio/brands")
            assert status == 200, ("/studio/brands", status)
            brands = json.loads(get(f"{base}/api/studio/brands")[1])
            assert [item["id"] for item in brands["items"]][:3] == ["plain", "news", "tech"], brands
            for page in ("/admin/music",):
                status, _ = get(base + page)
                assert status == 200, (page, status)
            assert json.loads(get(f"{base}/api/studio/music-library")[1])["items"] == []
            assert json.loads(get(f"{base}/api/studio/tool-sources")[1])["items"] == []
            presets = json.loads(get(f"{base}/api/studio/platforms")[1])
            assert [item["id"] for item in presets["items"]] == ["bilibili", "douyin", "youtube"], presets
            cli = subprocess.run([str(backend), "--config", str(config), "studio", "--url", base, "jobs", "--json"],
                                 env={**env, "MINDEX_WEB_TOKEN": TOKEN}, cwd=work, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=120, check=False)
            assert cli.returncode == 0 and json.loads(cli.stdout) == [], (cli.stdout, cli.stderr)
            status = json.loads(get(f"{base}/api/status?summary=true", timeout=30)[1])
            assert len(status["sources"]) == 1, status.get("sources")
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                code = process.wait(15)
            except subprocess.TimeoutExpired:
                process.kill()
                print("SIGTERM 后 15 秒仍未退出", file=sys.stderr)
                return 1
            log.close()
        step(f"SIGTERM 后退出（code={code}）")
    print("冻结后端检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
