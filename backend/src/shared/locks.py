"""数据目录的具名文件锁与规范加锁顺序。

规范顺序（``ORDER``）::

    pipeline → index-write → library → register → studio

- ``pipeline``（``data_dir/pipeline.lock``）：管线/GPU 互斥。处理任务持共享锁（开启 ``enforce_gpu_lock``
  时持独占锁）；clean、帧迁移、prune、备份迁移持独占锁以排除全部处理任务。
- ``index-write``（``data_dir/index-write.lock``）：索引构建、发布、激活与改写 index_members 的维护操作，
  只有独占模式。
- ``library``（``data_dir/locks/library.lock``）：素材库整体。普通处理持共享锁，删除素材与物理清理持独占锁。
- ``register``（``data_dir/sources/.register.lock``）：导入登记的短租约，只有独占模式。
- ``studio``（``data_dir/studio/studio.lock``）：创作步骤/构建全局单执行，只有独占模式。

一次需要多把锁时用 ``acquire_ordered``，名称必须按 ``ORDER`` 不递减排列，逆序直接拒绝（``ValueError``），
从而避免两个持锁方互相等待。``ProcessingLock`` 先取 library 再取 index-write 的历史顺序与此不同，
但 library 只做非阻塞尝试（拿不到立即失败），不会参与等待环，因此不会死锁。

实现：全部基于 ``fcntl.flock``。``filelock.FileLock`` 在 Unix 上同样使用 ``flock(LOCK_EX)``，
因此仍用 ``FileLock`` 打开同一文件的代码（如测试或外部工具）与这里的独占锁互斥。
拿不到锁时抛出 ``filelock.Timeout``，与既有调用方的异常处理保持一致。
任务粒度的工作锁（``locks/<sha>.<work>.lock``）仍由 ``processing_lock`` 用 ``filelock`` 管理。
"""

from __future__ import annotations

import fcntl
import itertools
import time
from contextlib import ExitStack
from pathlib import Path

from filelock import Timeout

ORDER = ("pipeline", "index-write", "library", "register", "studio")

PATHS = {
    "pipeline": "pipeline.lock",
    "index-write": "index-write.lock",
    "library": "locks/library.lock",
    "register": "sources/.register.lock",
    "studio": "studio/studio.lock",
}

# 可以共享持有的锁；其余锁历史上一直由 filelock 独占持有，不提供共享模式以免破坏互斥。
SHAREABLE = {"pipeline", "library"}

_POLL_SECONDS = 0.05


class SharedFileLock:
    """``flock`` 文件锁：共享或独占。

    ``timeout``：0 表示只尝试一次（默认）；正数表示最多等待的秒数；``None`` 或负数表示一直等待。
    同一对象重复 ``acquire`` 不会重复加锁（``release`` 一次即释放）。
    """

    def __init__(self, path, *, exclusive=False, timeout: float | None = 0):
        self.path, self.exclusive, self.timeout, self.handle = Path(path), exclusive, timeout, None

    @property
    def is_locked(self):
        return self.handle is not None

    def acquire(self, timeout: float | object | None = ..., **_):
        if self.handle is not None:
            return self
        wait = self.timeout if timeout is ... else timeout
        deadline = None if wait is None or wait < 0 else time.monotonic() + wait
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        handle = self.path.open("a+b")
        try:
            while True:
                try:
                    fcntl.flock(handle, mode)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise Timeout(str(self.path)) from None
                    time.sleep(_POLL_SECONDS)
        except BaseException:
            handle.close()
            raise
        self.handle = handle
        return self

    def release(self):
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def path(root, name: str) -> Path:
    if name not in PATHS:
        raise ValueError(f"未知的锁名称：{name}（可用：{'、'.join(ORDER)}）")
    return Path(root) / PATHS[name]


def named(root, name: str, *, exclusive: bool | None = None, timeout: float | None = 0) -> SharedFileLock:
    """``data_dir`` 下的具名锁（尚未获取）。``exclusive`` 缺省时 pipeline/library 为共享，其余为独占。"""
    location = path(root, name)
    if exclusive is None:
        exclusive = name not in SHAREABLE
    if not exclusive and name not in SHAREABLE:
        raise ValueError(f"锁 {name} 只能独占持有")
    return SharedFileLock(location, exclusive=exclusive, timeout=timeout)


def check_order(names) -> None:
    """名称必须按 ``ORDER`` 不递减排列，否则抛出 ``ValueError``。"""
    positions = []
    for name in names:
        if name not in ORDER:
            raise ValueError(f"未知的锁名称：{name}（可用：{'、'.join(ORDER)}）")
        positions.append(ORDER.index(name))
    if any(later < earlier for earlier, later in itertools.pairwise(positions)):
        raise ValueError(f"加锁顺序错误：{' → '.join(names)}；必须遵循 {' → '.join(ORDER)}")


def acquire_ordered(root, names, *, exclusive: bool | dict[str, bool] | None = None,
                    timeout: float | dict[str, float | None] | None = 0) -> ExitStack:
    """按规范顺序获取多把锁，返回持有全部锁的 ``ExitStack``（``close()`` 或 ``with`` 结束时逆序释放）。

    ``exclusive``、``timeout`` 可为统一取值或按名称的字典（字典未列出的名称：``exclusive`` 用 ``named``
    的缺省模式，``timeout`` 为 0）。任何一把拿不到时，已获取的锁全部释放后再抛出 ``Timeout``，
    其 ``lock_file`` 指明是哪一把。
    """
    names = list(names)
    check_order(names)
    stack = ExitStack()
    try:
        for name in names:
            mode = exclusive.get(name) if isinstance(exclusive, dict) else exclusive
            wait = timeout.get(name, 0) if isinstance(timeout, dict) else timeout
            stack.enter_context(named(root, name, exclusive=mode, timeout=wait))
    except BaseException:
        stack.close()
        raise
    return stack
