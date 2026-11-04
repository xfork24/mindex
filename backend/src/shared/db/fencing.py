"""把执行归属绑定到业务事务，旧 attempt/租约不能写回共享状态。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

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

    def check(self):
        if self.lost.is_set():
            raise LeaseLost("任务租约已失联，停止提交")
        if self.cancelled.is_set():
            from mindex.shared.task_cancel import TaskCancelled
            raise TaskCancelled()


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
        return super().submit(ExecutionFence.capture().run, fn, *args, **kwargs)


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
        from mindex.shared.db.scaling_schema import LEDGER_TABLES

        if task.ledger_table in LEDGER_TABLES and task.ledger_id:
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
