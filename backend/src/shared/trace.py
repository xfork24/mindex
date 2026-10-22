"""调用诊断记录槽。

底层模块（模型适配、ffmpeg 调用）通过 `record` 写入诊断；Studio 执行某一步时把
`ArtifactSession` 放进 `current`，诊断即落入该步骤的 attempt 目录。没有活动会话时 `record` 什么也不做。
这样 shared、models 不必依赖 studio 包。
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Protocol


class TraceSession(Protocol):
    def record(self, kind: str, value: Any) -> None: ...


current: ContextVar[TraceSession | None] = ContextVar("studio_artifacts", default=None)


def record(kind: str, value: Any) -> None:
    session = current.get()
    if session:
        session.record(kind, value)
