"""具名锁的运行时后端选择（plans §7.1 阶段 3/5）。

- ``locks.backend=file``（缺省）：沿用 ``shared/locks.py`` 的 data_dir 文件锁，
  行为与锁文件名完全不变（零回归）。
- ``locks.backend=postgres``：经 ``PgLockProvider`` 的租约锁（epoch/fencing token、
  TTL+心跳），跨节点互斥——同一 PG 库的不同 data_dir/主机也互斥。

资源锁（按素材/工作的细粒度互斥）在文件形态下仍是 ``locks/<sha>.<work>.lock``；
PG 形态下用 ``resource:<key>`` 的具名租约，key 与文件形态同源（sha + work），
因此两种形态不会把不同资源误并。

``locks_backend`` 从 Store 读取（由 ``open_context`` 按配置写入），使只拿到 Store
的调用点（人物审核、任务恢复）也能选对后端。
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager

from filelock import Timeout

from mindex.shared import locks

__all__ = ["LeaseHandle", "acquire_ordered", "named", "provider_for", "resource", "uses_pg"]


@contextmanager
def acquire_ordered(source, names, *, exclusive=True, timeout=0):
    names = list(names)
    order = [locks.ORDER.index(name) for name in names]
    if order != sorted(order) or len(set(names)) != len(names):
        raise ValueError("必须按规范顺序获取锁，且不得重复")
    if getattr(source, "pg", False):
        from mindex.shared.db.maintenance_gate import _ACTIVE
        if _ACTIVE.get() is not None and _ACTIVE.get() == getattr(getattr(source, "backend", None), "dsn", None):
            yield
            return
    with ExitStack() as stack:
        for name in names:
            wait = timeout.get(name, 0) if isinstance(timeout, dict) else timeout
            stack.enter_context(named(source, name, exclusive=exclusive, timeout=wait))
        yield


def uses_pg(source) -> bool:
    if getattr(source, "locks_backend", None) is not None:
        return source.locks_backend == "postgres"
    section = getattr(source, "locks", None)
    return bool(section is not None and section.backend == "postgres")


def _root(source):
    return getattr(source, "data_dir", None) or source.root


def provider_for(source):
    """取得该库的 PG 锁提供者：Config 走 Store.open；Store 直接用其 backend。"""
    from mindex.shared.db.locks_pg import PgLockProvider

    if hasattr(source, "backend") and hasattr(source, "library_id"):
        return PgLockProvider(source.backend, library_id=source.library_id)
    from mindex.shared.storage import Store

    store = Store.open(source)
    return PgLockProvider(store.backend, library_id=store.library_id)


class LeaseHandle:
    """把 PG 租约适配成与 filelock 相同的 acquire(timeout=…)/release()/is_locked 接口。"""

    def __init__(self, provider, name: str, mode: str, default_timeout):
        self.provider, self.name, self.mode = provider, name, mode
        self.default_timeout = default_timeout
        self.lease = None

    @property
    def is_locked(self) -> bool:
        return self.lease is not None and self.lease.verify()

    def acquire(self, timeout=...):
        if self.lease is not None:
            return self
        wait = self.default_timeout if timeout is ... else timeout
        lease = self.provider.lease(self.name, mode=self.mode, wait=wait)
        if lease is None:
            raise Timeout(f"postgres-lease:{self.name}")
        lease.start_heartbeat()
        self.lease = lease
        from mindex.shared.db.fencing import add_lease
        add_lease(lease)
        return self

    def release(self):
        if self.lease is not None:
            from mindex.shared.db.fencing import remove_lease
            remove_lease(self.lease)
            self.lease.stop_heartbeat()
            self.lease.release()
            self.lease = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def named(source, name: str, *, exclusive: bool | None = None, timeout: float | None = 0):
    """ORDER 内的具名锁；file 形态与 ``locks.named`` 完全一致。"""
    if uses_pg(source):
        mode = "exclusive" if (
            exclusive if exclusive is not None else name not in locks.SHAREABLE) else "shared"
        return LeaseHandle(provider_for(source), name, mode, timeout)
    return locks.named(_root(source), name, exclusive=exclusive, timeout=timeout)


def resource(source, key: str, *, exclusive: bool = True, timeout: float | None = 0):
    """细粒度资源锁（素材/工作/发布子任务/工作区 CAS）。

    PG 形态用 ``resource:<key>`` 租约；文件形态用 ``locks/<sha(key)>.resource.lock``
    的 flock（与既有具名锁文件互不干扰）。
    """
    mode = "exclusive" if exclusive else "shared"
    if uses_pg(source):
        return LeaseHandle(provider_for(source), f"resource:{key}", mode, timeout)
    import hashlib
    from pathlib import Path

    from filelock import FileLock

    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    path = Path(_root(source)) / "locks" / f"{digest}.resource.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path), timeout=-1 if timeout is None else timeout, thread_local=False)
