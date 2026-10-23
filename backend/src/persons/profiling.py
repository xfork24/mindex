"""人物库出镜审核读取链路的分段计时。

默认关闭，关闭时 ``profiled`` / ``stage`` 都是空操作，不改变任何响应。设置环境变量
``MINDEX_PROFILE_PERSONS=1`` 后，被 ``profiled`` 包住的请求会：

* 把各阶段耗时写进 ``Server-Timing`` 响应头（浏览器开发者工具的 Timing 页可直接查看）；
* 以 ``mindex.persons.profile`` 日志输出同样的内容。

阶段按名称累加，同名阶段会记录调用次数，因此 N+1 查询会表现为 ``n=50``。
"""

from __future__ import annotations

import contextvars
import logging
import os
import time
from contextlib import contextmanager

LOGGER = logging.getLogger("mindex.persons.profile")
ENV_FLAG = "MINDEX_PROFILE_PERSONS"
_current: contextvars.ContextVar[Profile | None] = contextvars.ContextVar("person_profile", default=None)


def enabled() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


class Profile:
    def __init__(self, name: str):
        self.name = name
        self.started = time.perf_counter()
        self.stages: dict[str, list[float]] = {}  # name -> [总毫秒, 次数]
        self.notes: dict[str, str] = {}

    def add(self, stage_name: str, milliseconds: float) -> None:
        entry = self.stages.setdefault(stage_name, [0.0, 0])
        entry[0] += milliseconds
        entry[1] += 1

    def note(self, key: str, value) -> None:
        self.notes[key] = str(value)

    def total_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def header(self) -> str:
        parts = []
        for stage_name, (milliseconds, count) in self.stages.items():
            description = f';desc="n={count}"' if count > 1 else ""
            parts.append(f"{stage_name};dur={milliseconds:.2f}{description}")
        parts.append(f"total;dur={self.total_ms():.2f}")
        return ", ".join(parts)

    def apply(self, response) -> None:
        """把当前累计耗时写进响应头；已有的 Server-Timing 会被保留并追加。"""
        existing = response.headers.get("server-timing")
        value = self.header()
        response.headers["Server-Timing"] = f"{existing}, {value}" if existing else value
        for key, note in self.notes.items():
            response.headers["X-Profile-" + key] = note


@contextmanager
def profiled(name: str, response=None):
    """为一次请求开启计时。关闭时产出 None。

    ``response`` 是 FastAPI 注入的 Response 对象时，退出时自动写头；返回自建响应（如
    FileResponse）的接口应改为拿到产出的 Profile 后调用 ``profile.apply(result)``。
    """
    if not enabled():
        yield None
        return
    profile = Profile(name)
    token = _current.set(profile)
    try:
        yield profile
    finally:
        _current.reset(token)
        if response is not None:
            profile.apply(response)
        LOGGER.info("%s %s notes=%s", name, profile.header(), profile.notes)


@contextmanager
def stage(stage_name: str):
    profile = _current.get()
    if profile is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        profile.add(stage_name, (time.perf_counter() - started) * 1000)


def note(key: str, value) -> None:
    profile = _current.get()
    if profile is not None:
        profile.note(key, value)
