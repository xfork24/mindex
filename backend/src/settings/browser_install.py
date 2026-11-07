"""AI 动画录制组件（Playwright 的 Chromium headless shell）的检测与按需安装。

动画录制（``studio/animation_capture.py``）以无头模式启动 Chromium，Playwright 使用 headless shell 构建，
所以只安装它（``playwright install --only-shell chromium``，约 100 MB）。浏览器目录取
``PLAYWRIGHT_BROWSERS_PATH``
（桌面版设为用户数据目录的 ``browsers/``），未设置时为 Playwright 的缺省缓存目录。

安装在后台线程中以子进程运行 ``<sys.executable> -m playwright install``；冻结的桌面后端经 ``server.
desktop_entry``
分发 ``-m``。进程内同时只有一个安装任务，状态（运行中/完成/失败与日志尾部）保存在内存里，页面轮询读取。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

INSTALL_COMMAND = ("-m", "playwright", "install", "--only-shell", "chromium")
INSTALL_TIMEOUT = 1800  # 秒


def browsers_path() -> Path:
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured and configured != "0":
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def expected_revision() -> str | None:
    """当前 playwright 包要求的 headless shell 版本号；未安装 playwright 时返回 None。"""
    try:
        import playwright
    except ImportError:
        return None
    manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
    try:
        browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
    except (OSError, ValueError, KeyError):
        return None
    entry = next((b for b in browsers if b.get("name") == "chromium-headless-shell"), None)
    return str(entry["revision"]) if entry else None


def installed() -> bool:
    revision = expected_revision()
    if revision is None:
        return False
    return (browsers_path() / f"chromium_headless_shell-{revision}" / "INSTALLATION_COMPLETE").is_file()


def progress_line(raw: bytes) -> str:
    """把子进程输出的一行解析成进度文本（空串表示无内容）。

    下载进度用 ``\\r`` 刷新同一行，只保留最后一段。Windows 的文本模式会把 ``\\n`` 转成
    ``\\r\\n``，因此要先去掉尾部的 ``\\r\\n`` 再按 ``\\r`` 切分——否则尾随的 ``\\r`` 会切出
    空段，整行被丢弃（macOS/Linux 的管道输出无此转换）。
    """
    return raw.decode("utf-8", errors="replace").rstrip("\r\n").split("\r")[-1].strip()


class BrowserInstaller:
    def __init__(self, command: list[str] | None = None):
        self.command = command  # 测试可替换为其他命令
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._log: deque[str] = deque(maxlen=200)
        self.status = "idle"
        self.error: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None

    def state(self) -> dict:
        revision = expected_revision()
        with self._lock:
            return {
                "available": revision is not None,  # playwright 包是否可用（源码安装需要 animation extra）
                "installed": installed(),
                "revision": revision,
                "path": str(browsers_path()),
                "task": {
                    "status": self.status,
                    "error": self.error,
                    "started_at": self.started_at,
                    "finished_at": self.finished_at,
                    "log": "\n".join(self._log),
                },
            }

    def start(self) -> dict:
        if expected_revision() is None and self.command is None:
            raise ValueError("未安装 playwright（源码运行需要 uv sync --extra animation）")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("浏览器组件正在安装中")
            self._log.clear()
            self.status, self.error = "running", None
            self.started_at, self.finished_at = datetime.now(UTC).isoformat(), None
            self._thread = threading.Thread(target=self._run, daemon=True, name="animation-browser-install")
            self._thread.start()
        return self.state()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        command = self.command or [sys.executable, *INSTALL_COMMAND]
        env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers_path())}
        try:
            browsers_path().mkdir(parents=True, exist_ok=True)
            # 以字节读取：文本模式会把进度刷新用的 \r 当成换行。
            process = subprocess.Popen(
                command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            for raw in process.stdout:
                # 下载进度用 \r 刷新同一行：只保留最后一段（Windows 的 \r\n 换行见 progress_line）。
                text = progress_line(raw)
                if text:
                    with self._lock:
                        self._log.append(text)
            code = process.wait(INSTALL_TIMEOUT)
            with self._lock:
                self.status = "complete" if code == 0 else "failed"
                self.error = None if code == 0 else f"安装命令退出码 {code}"
        except Exception as exc:  # 安装失败必须如实呈现给页面
            log.warning("浏览器组件安装失败", exc_info=True)
            with self._lock:
                self.status, self.error = "failed", f"无法安装浏览器组件：{exc}"
        finally:
            with self._lock:
                self.finished_at = datetime.now(UTC).isoformat()


_INSTALLER = BrowserInstaller()


def installer() -> BrowserInstaller:
    return _INSTALLER
