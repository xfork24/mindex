"""把执行归属绑定到业务事务，旧 attempt/租约不能写回共享状态。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from threading import RLock

from mindex.shared.db.locks_pg import LeaseLost

_ATTEMPT = ContextVar("mindex_attempt", default=None)
_LEASES = ContextVar("mindex_leases", default=())


@dataclass
class Attempt:
    backend: object
    library_id: str
    task: object
    lost: object
    cancelled: object
    publications: dict = field(default_factory=dict)
    terminals: dict = field(default_factory=dict)
    _contexts: ExitStack = field(default_factory=ExitStack, repr=False)
    _held: set = field(default_factory=set, repr=False)
    _held_values: dict = field(default_factory=dict, repr=False)
    _publication_leases: dict = field(default_factory=dict, repr=False)
    _states: dict = field(default_factory=dict, repr=False)
    _closed: bool = False
    _guard: object = field(default_factory=RLock, repr=False)

    def check(self):
        if self._closed or self.lost.is_set():
            raise LeaseLost("任务租约已失联，停止提交")
        if self.cancelled.is_set():
            from mindex.shared.task_cancel import TaskCancelled
            raise TaskCancelled()

    def retain(self, key, context):
        """发布串行区持续到队列/账本/指针共同提交；同一 attempt 内不重复拿锁。"""
        with self._guard:
            self.check()
            if key not in self._held:
                previous = {id(lease) for lease in _LEASES.get()}
                self._held_values[key] = self._contexts.enter_context(context)
                self._held.add(key)
                self._publication_leases.update((id(lease), lease) for lease in _LEASES.get()
                                                if id(lease) not in previous)
            return self._held_values[key]

    def defer(self, key, publish):
        with self._guard:
            self.check()
            self.publications[key] = publish

    def state(self, key, factory):
        with self._guard:
            self.check()
            if key not in self._states:
                self._states[key] = factory()
            return self._states[key]

    def publication_fence(self):
        # 资源锁可能由子线程获取；终态事务显式复验，不依赖结束线程的 ContextVar。
        return ExecutionFence(leases=tuple(self._publication_leases.values()))

    def close(self):
        self._closed = True
        try:
            self._contexts.close()
        finally:
            self.publications.clear()
            self.terminals.clear()
            self._held.clear()
            self._held_values.clear()
            self._publication_leases.clear()
            self._states.clear()


@contextmanager
def publication_scope(store, *, config=None, check=None, only_attempt=False):
    """持久任务把索引锁交给当前 attempt；本地/inline 保持原临界区。"""
    from mindex.shared.processing_lock import index_write_lock

    attempt = current_attempt()
    if only_attempt and (not store.pg or attempt is None):
        yield
        return
    lock = index_write_lock(store.root, config=config, check=check) if config is not None else None
    if lock is None:
        from mindex.shared import lock_backend

        lock = lock_backend.named(store, "index-write", exclusive=True, timeout=None)
    if store.pg and attempt is not None:
        attempt.retain((store.library_id, "index-write"), lock)
        yield
    else:
        with lock:
            yield


def current_attempt():
    return _ATTEMPT.get()


@dataclass(frozen=True)
class ExecutionFence:
    """可显式传给事务/线程的执行凭证；捕获后不依赖调用线程的 ContextVar。"""

    attempt: Attempt | None = None
    leases: tuple = ()

    @classmethod
    def capture(cls):
        return cls(current_attempt(), _LEASES.get())

    def run(self, fn, *args, **kwargs):
        attempt_token = _ATTEMPT.set(self.attempt)
        lease_token = _LEASES.set(self.leases)
        try:
            if self.attempt is not None:
                self.attempt.check()
            return fn(*args, **kwargs)
        finally:
            _LEASES.reset(lease_token)
            _ATTEMPT.reset(attempt_token)


class FencedThreadPoolExecutor(ThreadPoolExecutor):
    """每个提交显式携带 attempt 和资源租约，防止子线程脱离发布约束。"""

    def submit(self, fn, /, *args, **kwargs):
        return super().submit(copy_context().run, ExecutionFence.capture().run, fn, *args, **kwargs)


@contextmanager
def executing(attempt):
    token = _ATTEMPT.set(attempt)
    try:
        yield
    finally:
        _ATTEMPT.reset(token)


def add_lease(lease):
    _LEASES.set((*_LEASES.get(), lease))


def remove_lease(lease):
    _LEASES.set(tuple(item for item in _LEASES.get() if item is not lease))


def verify_transaction(connection, backend, *, execution=None):
    """在事务开始与提交前复验，并持共享行锁直至提交，阻止并发回收/重派。"""
    fence = execution if execution is not None else ExecutionFence.capture()
    attempt = fence.attempt
    if attempt is not None and attempt.backend.dsn == backend.dsn:
        attempt.check()
        task = attempt.task
        row = connection.execute(
            "SELECT cancel_requested FROM task_queue WHERE library_id=%s AND id=%s AND owner=%s"
            " AND attempt_id=%s AND fencing_token=%s AND status='running'"
            " AND expires_at >= clock_timestamp() FOR SHARE",
            (attempt.library_id, task.id, task.owner, task.attempt_id, task.fencing_token)).fetchone()
        if row is None:
            attempt.lost.set()
            raise LeaseLost("任务 attempt 已过期或易主，拒绝旧 worker 提交")
        if row[0]:
            attempt.cancelled.set()
            attempt.check()
        from mindex.shared.db.scaling_schema import ALL_LEDGER_TABLES

        if task.ledger_table in ALL_LEDGER_TABLES and task.ledger_id:
            bound = connection.execute(
                f"SELECT 1 FROM {task.ledger_table} WHERE id=%s AND queue_attempt_id=%s"
                " AND queue_fencing_token=%s", (task.ledger_id, task.attempt_id, task.fencing_token)).fetchone()
            if bound is None:
                attempt.lost.set()
                raise LeaseLost("账本当前 attempt 与执行令牌不一致，拒绝提交")
    for lease in fence.leases:
        if lease.provider.backend_pool.dsn != backend.dsn:
            continue
        row = connection.execute(
            "SELECT 1 FROM lock_holders WHERE library_id=%s AND name=%s AND owner_id=%s"
            " AND fencing_token=%s AND expires_at >= clock_timestamp() FOR SHARE",
            (lease.provider.library_id, lease.name, lease.owner_id, lease.token)).fetchone()
        if row is None:
            raise LeaseLost(f"资源租约已失效：{lease.name}，拒绝提交")
