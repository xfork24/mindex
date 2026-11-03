"""任务分发后端协议与本地实现（plans/horizontal-scaling-plan.md §7.2）。

local（inline）：``enqueue`` 立即在守护线程执行 ``fn``——与现有
``BackgroundTaskRunner.launch`` 的进程内语义一致；任务表（source_tasks 等）自身
就是用户可见账本，队列不持久化任何状态。

postgres（阶段 3）：持久队列（claim 用 FOR UPDATE SKIP LOCKED、attempt 与
fencing token、至少一次交付），协议届时扩展 claim/续期/完成/取消；账本与入队
同事务、外部副作用带幂等键。payload 不序列化函数、局部路径、数据库连接或
凭据（plans §7.2），因此本协议的阶段 0 形态只承诺「入队即执行」，签名随阶段 3
演进。
"""

from __future__ import annotations

import itertools
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["InlineTaskQueue", "TaskQueue", "TaskReceipt"]

_THREAD_IDS = itertools.count(1)


@dataclass(frozen=True)
class TaskReceipt:
    task_id: str
    backend: str


class TaskQueue(Protocol):
    backend: str

    def enqueue(self, fn: Callable[[], Any], *, task_id: str | None = None,
                kind: str = "", dedupe_key: str | None = None) -> TaskReceipt: ...


class InlineTaskQueue:
    """本地实现：入队即启动守护线程执行；与现有后台任务一样不做持久化去重。"""

    backend = "inline"

    def __init__(self):
        self.threads: dict[str, threading.Thread] = {}

    def enqueue(self, fn: Callable[[], Any], *, task_id: str | None = None,
                kind: str = "", dedupe_key: str | None = None) -> TaskReceipt:
        task_id = task_id or f"inline-{uuid.uuid4().hex}"
        thread = threading.Thread(
            target=fn, daemon=True, name=f"{kind or 'queue'}-{next(_THREAD_IDS)}-{task_id[:8]}"
        )
        self.threads[task_id] = thread
        thread.start()
        return TaskReceipt(task_id=task_id, backend=self.backend)
