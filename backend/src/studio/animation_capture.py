"""AI 动画录制沙箱：在无头 Chromium 里以虚拟时钟逐帧推进动画源码，截图经管道交给 ffmpeg 编码。

隔离与确定性：

- 每次录制使用全新的临时 user-data-dir（录制结束即删除），离线模式，``context.route`` 中止除 ``data:``/``blob:
``/
  ``about:`` 以外的全部请求，WebSocket 一律关闭，Service Worker 禁用，不接受下载；
- 初始化脚本 ``VIRTUAL_CLOCK`` 在页面任何脚本之前接管 ``Date``、``performance.now``、
  ``requestAnimationFrame``/``cancelAnimationFrame``、``setTimeout``/``setInterval``/``clear*``、
  ``requestIdleCallback`` 与 ``document.timeline.currentTime``，并把 ``Math.random`` 换成固定种子的伪随机；
  CSS 动画/过渡与 Web Animations 每帧暂停后把 ``currentTime`` 设为虚拟时刻；
- ``window.__mindexAdvance(ms)`` 推进虚拟时间：按时刻顺序执行到期定时器，再以新时刻调用一轮 rAF 回调；
  同一源码、画布与 fps 的两次录制逐帧一致。

超时：页面加载与每帧推进各有 ``frame_timeout`` 秒上限（推进经 CDP ``Runtime.evaluate`` 的 timeout 终止脚本，
死循环不会挂住录制），整体墙钟不超过 ``max_seconds``；超时即关闭浏览器、终止 ffmpeg 并抛中文 ``MediaError``。

依赖为可选 extra ``animation``（playwright）；未安装或缺少浏览器时抛出带安装指引的 ``MediaError``。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path

from shared.media import MediaError, ffmpeg_bin

INSTALL_HINT = "AI 动画录制需要安装可选依赖：uv sync --extra animation && uv run playwright install chromium"
# 桌面版（外壳设置 MINDEX_DESKTOP=1）已随包带 playwright，只缺浏览器：引导到设置页一键安装。
DESKTOP_INSTALL_HINT = "AI 动画录制需要先安装浏览器组件：在设置页“本机服务”的“AI 动画录制组件”中一键安装"


def install_hint() -> str:
    return DESKTOP_INSTALL_HINT if os.environ.get("MINDEX_DESKTOP") == "1" else INSTALL_HINT


ALLOWED_SCHEMES = ("data:", "blob:", "about:")
# 单次推进最多执行的定时器回调数：超过视为定时器风暴（例如 setTimeout(f, 0) 自我递归）。
TIMER_BUDGET = 100000

VIRTUAL_CLOCK = (
    "\n"
    "(() => {\n"
    "  if (window.__mindexAdvance) return;\n"
    "  const EPOCH = 1767225600000;  // 2026-01-01T00:00:00Z：Date 从固定时刻起走\n"
    "  let now = 0;\n"
    "  let seed = 0x9e3779b9 | 0;\n"
    "  Math.random = () => {\n"
    "    seed = (seed + 0x6d2b79f5) | 0;\n"
    "    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);\n"
    "    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;\n"
    "    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;\n"
    "  };\n"
    "  const RealDate = Date;\n"
    "  function VirtualDate(...args) {\n"
    "    if (!new.target) return new RealDate(EPOCH + now).toString();\n"
    "    return args.length ? new RealDate(...args) : new RealDate(EPOCH + now);\n"
    "  }\n"
    "  VirtualDate.prototype = RealDate.prototype;\n"
    "  VirtualDate.now = () => EPOCH + now;\n"
    "  VirtualDate.parse = RealDate.parse;\n"
    "  VirtualDate.UTC = RealDate.UTC;\n"
    "  window.Date = VirtualDate;\n"
    '  Object.defineProperty(performance, "now", { value: () => now, configurable: '
    "true });\n"
    "  try {\n"
    '    Object.defineProperty(AnimationTimeline.prototype, "currentTime", { get: '
    "() => now, configurable: true });\n"
    "  } catch (e) {}\n"
    "\n"
    "  const timers = new Map();\n"
    "  let nextTimer = 1;\n"
    "  const schedule = (cb, ms, args, repeat) => {\n"
    "    const id = nextTimer++;\n"
    "    const delay = Math.max(0, Number(ms) || 0);\n"
    "    timers.set(id, { at: now + delay, cb, args, interval: repeat ? "
    "Math.max(1, delay) : null, id });\n"
    "    return id;\n"
    "  };\n"
    '  const call = (cb, args) => (typeof cb === "function" ? cb(...args) : (0, '
    "eval)(String(cb)));\n"
    "  window.setTimeout = (cb, ms, ...args) => schedule(cb, ms, args, false);\n"
    "  window.setInterval = (cb, ms, ...args) => schedule(cb, ms, args, true);\n"
    "  window.clearTimeout = window.clearInterval = (id) => { timers.delete(id); };"
    "\n"
    "  window.requestIdleCallback = (cb) => schedule(cb, 0, [{ didTimeout: false, "
    "timeRemaining: () => 0 }], false);\n"
    "  window.cancelIdleCallback = (id) => { timers.delete(id); };\n"
    "\n"
    "  let frames = new Map();\n"
    "  let nextFrame = 1;\n"
    "  window.requestAnimationFrame = (cb) => { const id = nextFrame++; "
    "frames.set(id, cb); return id; };\n"
    "  window.cancelAnimationFrame = (id) => { frames.delete(id); };\n"
    "\n"
    "  const births = new WeakMap();\n"
    "  const syncAnimations = () => {\n"
    "    if (!document.getAnimations) return;\n"
    "    for (const animation of document.getAnimations()) {\n"
    "      if (!births.has(animation)) births.set(animation, now);\n"
    "      try {\n"
    "        animation.pause();\n"
    "        animation.currentTime = now - births.get(animation);\n"
    "      } catch (e) {}\n"
    "    }\n"
    "  };\n"
    "\n"
    "  window.__mindexAdvance = (ms) => {\n"
    "    const target = now + Math.max(0, Number(ms) || 0);\n"
    "    let budget = __BUDGET__;\n"
    "    for (;;) {\n"
    "      let due = null;\n"
    "      for (const timer of timers.values()) {\n"
    "        if (timer.at <= target && (!due || timer.at < due.at || (timer.at === "
    "due.at && timer.id < due.id))) due = timer;\n"
    "      }\n"
    "      if (!due) break;\n"
    "      if (--budget < 0) throw new "
    'Error("定时器回调过多（可能存在定时器死循环）");\n'
    "      now = Math.max(now, due.at);\n"
    "      if (due.interval) due.at += due.interval; else timers.delete(due.id);\n"
    "      try { call(due.cb, due.args); } catch (e) { console.error(e); }\n"
    "    }\n"
    "    now = target;\n"
    "    const pending = frames;\n"
    "    frames = new Map();\n"
    "    for (const cb of pending.values()) {\n"
    "      try { cb(now); } catch (e) { console.error(e); }\n"
    "    }\n"
    "    syncAnimations();\n"
    "    return now;\n"
    "  };\n"
    "})();\n"
).replace("__BUDGET__", str(TIMER_BUDGET))


def _playwright():
    try:
        # 延迟导入：保留可选依赖探测或异常捕获时的降级行为。
        from playwright.sync_api import Error, sync_playwright
    except ImportError as exc:
        raise MediaError(install_hint()) from exc
    return sync_playwright, Error


def available() -> bool:
    """playwright 已安装（不检查浏览器是否已下载）。"""
    try:
        _playwright()
    except MediaError:
        return False
    return True


def capture(
    html: str,
    *,
    seconds: float,
    width: int,
    height: int,
    fps: float,
    target: Path,
    max_seconds: float,
    frame_timeout: float = 5,
    frames: int | None = None,
    transparent: bool = False,
) -> dict:
    """录制 ``frames``（缺省 ``round(seconds × fps)``）帧到 ``target``（H.264/yuv420p MP4）。

        ``transparent=True`` 时截图省略页面背景（页面自身须为透明背景），
        编码为带透明通道的 QuickTime RLE（argb）
        MOV，供分镜渲染作为文字叠加层合成。第 i 帧对应虚拟时刻 ``i × 1000 / fps`` 毫秒（第 0
    帧为加载完成时的画面）。
        返回录制统计。"""
    sync_playwright, PlaywrightError = _playwright()
    rate = Fraction(str(fps))
    count = frames if frames is not None else max(1, round(seconds * float(rate)))
    step = 1000 / float(rate)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.stem + (".partial.mov" if transparent else ".partial.mp4"))
    output = (
        ["-c:v", "qtrle", "-pix_fmt", "argb", "-f", "mov"]
        if transparent
        else [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
        ]
    )
    started = time.monotonic()
    blocked: list[str] = []
    profile = Path(tempfile.mkdtemp(prefix="mindex-animation-"))
    encoder = None

    def check_wall():
        if time.monotonic() - started > max_seconds:
            raise MediaError(f"动画录制超过 {max_seconds:g} 秒上限，已终止")

    def route(request_route):
        url = request_route.request.url
        if url.startswith(ALLOWED_SCHEMES):
            request_route.continue_()
        else:
            blocked.append(url[:200])
            request_route.abort("blockedbyclient")

    try:
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(profile),
                    headless=True,
                    offline=True,
                    java_script_enabled=True,
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    service_workers="block",
                    accept_downloads=False,
                    color_scheme="light",
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    reduced_motion="no-preference",
                    args=[
                        "--disable-gpu",
                        "--font-render-hinting=none",
                        "--disable-lcd-text",
                        "--disable-background-networking",
                        "--no-first-run",
                    ],
                )
            except PlaywrightError as exc:
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    raise MediaError(install_hint()) from exc
                raise MediaError(f"无法启动录制浏览器：{str(exc).splitlines()[0][:300]}") from exc
            try:
                context.route("**/*", route)
                if hasattr(context, "route_web_socket"):
                    # 接管全部 WebSocket 且从不连接服务器（处理器里不能同步调用 close，
                    # 会与页面事件循环互等）。
                    context.route_web_socket("**/*", lambda socket: blocked.append(socket.url[:200]))
                context.add_init_script(VIRTUAL_CLOCK)
                # 新页面才会执行初始化脚本（启动时自带的空白页早于 add_init_script）。
                page = context.new_page()
                page.set_default_timeout(frame_timeout * 1000)
                try:
                    page.set_content(html, wait_until="load", timeout=frame_timeout * 1000)
                except PlaywrightError as exc:
                    raise MediaError(
                        f"动画页面在 {frame_timeout:g} 秒内未加载完成（可能存在死循环）"
                    ) from exc
                cdp = context.new_cdp_session(page)
                encoder = subprocess.Popen(
                    [
                        ffmpeg_bin(),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "image2pipe",
                        "-framerate",
                        f"{rate.numerator}/{rate.denominator}",
                        "-c:v",
                        "png",
                        "-i",
                        "-",
                        "-frames:v",
                        str(count),
                        *output,
                        str(partial),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                for index in range(count):
                    check_wall()
                    _advance(cdp, 0 if index == 0 else step, frame_timeout, index)
                    try:
                        shot = page.screenshot(
                            type="png", timeout=frame_timeout * 1000, omit_background=transparent
                        )
                    except PlaywrightError as exc:
                        raise MediaError(f"第 {index + 1} 帧截图超时或失败") from exc
                    try:
                        encoder.stdin.write(shot)
                    except BrokenPipeError as exc:
                        raise MediaError("ffmpeg 编码进程意外退出") from exc
                check_wall()
            finally:
                context.close()
        encoder.stdin.close()
        error = encoder.stderr.read().decode("utf-8", "replace")
        if encoder.wait(timeout=max(60, max_seconds)) != 0:
            raise MediaError(f"动画编码失败：{error.strip()[:300]}")
        partial.replace(target)
    except BaseException:
        if encoder is not None and encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        partial.unlink(missing_ok=True)
        raise
    finally:
        if encoder is not None:
            for stream in (encoder.stdin, encoder.stderr):
                if stream and not stream.closed:
                    try:
                        stream.close()
                    except OSError:  # 编码进程已被终止：缓冲区写不出去，丢弃即可
                        pass
        shutil.rmtree(profile, ignore_errors=True)
    return {
        "frames": count,
        "fps": str(rate),
        "blocked_requests": blocked,
        "wall_seconds": round(time.monotonic() - started, 3),
    }


def _advance(cdp, ms: float, frame_timeout: float, index: int) -> None:
    """经 CDP 推进虚拟时钟：Runtime.evaluate 的 timeout 会终止陷入死循环的回调。"""
    # 延迟导入：独立模型或扩展功能使用可选依赖，仅在实际调用时加载。
    from playwright.sync_api import Error as PlaywrightError

    timeout = f"第 {index + 1} 帧在 {frame_timeout:g} 秒内未完成（动画脚本可能存在死循环）"
    try:
        result = cdp.send(
            "Runtime.evaluate",
            {
                "expression": f"window.__mindexAdvance({json.dumps(ms)})",
                "timeout": int(frame_timeout * 1000),
                "returnByValue": True,
            },
        )
    except PlaywrightError as exc:
        if "terminated" in str(exc).lower():
            raise MediaError(timeout) from exc
        raise MediaError(f"第 {index + 1} 帧推进失败：{str(exc).splitlines()[0][:300]}") from exc
    details = result.get("exceptionDetails")
    if details:
        text = (details.get("exception") or {}).get("description") or details.get("text") or ""
        if "terminated" in text.lower():
            raise MediaError(timeout)
        raise MediaError(f"第 {index + 1} 帧推进失败：{text.splitlines()[0][:300] if text else '未知错误'}")
