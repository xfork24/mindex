"""后台任务取消标记：进程内 Event 与 data_dir/locks/cancel/<key> 哨兵文件双写，跨进程生效。"""

from __future__ import annotations

import re
import threading
from pathlib import Path

CANCEL_MESSAGE = "已取消"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


class TaskCancelled(Exception):
    """任务在检查点之间被取消；不继承 ValueError，避免被通用参数错误处理吞掉。"""

    def __init__(self, message: str = CANCEL_MESSAGE):
        super().__init__(message)


def _safe_key(key: str) -> str:
    text = _UNSAFE.sub("_", str(key))
    if not text or set(text) <= {"."}:
        raise ValueError("无效的取消标识")
    return text[:200]


class CancelRegistry:
    def __init__(self, root: Path):
        self.directory = Path(root) / "locks" / "cancel"
        self._events: dict[str, threading.Event] = {}
        self._guard = threading.Lock()

    def _event(self, key: str) -> threading.Event:
        with self._guard:
            return self._events.setdefault(key, threading.Event())

    def sentinel(self, key: str) -> Path:
        return self.directory / _safe_key(key)

    def request(self, key: str) -> None:
        path = self.sentinel(key)
        self._event(key).set()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def requested(self, key: str) -> bool:
        path = self.sentinel(key)
        with self._guard:
            event = self._events.get(key)
        return bool(event and event.is_set()) or path.exists()

    def check(self, key: str) -> None:
        if self.requested(key):
            raise TaskCancelled()

    def clear(self, key: str) -> None:
        path = self.sentinel(key)
        with self._guard:
            self._events.pop(key, None)
        path.unlink(missing_ok=True)


_registries: dict[Path, CancelRegistry] = {}
_registries_guard = threading.Lock()


def registry_for(root) -> CancelRegistry:
    """每个 data_dir 一个单例，使 API 线程与任务线程共享进程内 Event。"""
    key = Path(root).resolve()
    with _registries_guard:
        registry = _registries.get(key)
        if registry is None:
            registry = _registries[key] = CancelRegistry(key)
        return registry
