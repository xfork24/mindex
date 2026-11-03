"""具名锁后端协议与本地实现（plans/horizontal-scaling-plan.md §4、§7.1）。

锁名与规范顺序沿用 shared/locks.py：``pipeline → index-write → library →
register → studio → topics``。分布式实现（阶段 3 的 PgLockProvider）必须保持
同一顺序与超时语义（拿不到抛 ``filelock.Timeout`` 的等价错误），并把锁与缓存
按 library_id 隔离（plans §2：不能用路径 hash 当租户键）。
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager as ContextManager
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Protocol

from mindex.shared import locks

__all__ = ["FileLockProvider", "LockProvider"]


class LockProvider(Protocol):
    def acquire(self, name: str, *, exclusive: bool | None = None,
                timeout: float | None = 0) -> ContextManager[Any]: ...

    def acquire_ordered(self, names: Sequence[str], *, exclusive: bool | dict[str, bool] | None = None,
                        timeout: float | dict[str, float | None] | None = 0) -> ContextManager[ExitStack]: ...


class FileLockProvider:
    """本地实现：data_dir 下的 flock 文件锁（shared/locks.py），跨进程有效。

    ``acquire`` 返回的上下文在进入时加锁、退出时释放，与 ``SharedFileLock``
    一致；拿不到锁抛 ``filelock.Timeout``。
    """

    backend = "file"

    def __init__(self, root: Path):
        self.root = Path(root)

    def acquire(self, name: str, *, exclusive: bool | None = None,
                timeout: float | None = 0) -> ContextManager[Any]:
        return locks.named(self.root, name, exclusive=exclusive, timeout=timeout)

    @contextmanager
    def acquire_ordered(self, names: Sequence[str], *, exclusive: bool | dict[str, bool] | None = None,
                        timeout: float | dict[str, float | None] | None = 0) -> ContextManager[ExitStack]:
        stack = locks.acquire_ordered(self.root, names, exclusive=exclusive, timeout=timeout)
        try:
            yield stack
        finally:
            stack.close()
