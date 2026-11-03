"""后台任务 runner：素材、对白、人物任务共用的生命周期。

子类声明任务表与中文提示，并实现 ``lock``、``restore``、``execute`` 等钩子；基类负责
查询、重试、取消、启动线程，以及 ``run`` 中的终态写入、取消处理、异常日志、锁释放与线程表清理。
"""

from __future__ import annotations

import json
import logging
import threading
import weakref
from pathlib import Path

from fastapi import HTTPException
from filelock import Timeout

from mindex.shared.processing_lock import recover_tasks
from mindex.shared.storage import dumps
from mindex.shared.task_cancel import TaskCancelled, registry_for
from mindex.shared.task_progress import (
    CANCELLABLE,
    RETRYABLE,
    begin_task,
    mark_cancelled,
    request_task_cancel,
    reset_task_attempt,
    save_task_progress,
    task_logs,
)

JSON_COLUMNS = frozenset({"options", "progress", "result", "publication"})
# 进程内全部 runner（弱引用）：健康接口据此报告哪些类别的后台任务在运行。
_RUNNERS: weakref.WeakSet = weakref.WeakSet()


def busy_categories(data_dir) -> list[str]:
    """属于 ``data_dir`` 的 runner 中有存活线程的类别（``thread_prefix``：source/dialogue/person）。"""
    target = Path(data_dir).resolve()
    found = set()
    for runner in list(_RUNNERS):
        if Path(runner.store.root).resolve() == target and any(t.is_alive() for t in list(runner.threads.values())):
            found.add(runner.thread_prefix)
    return sorted(found)


def public_task(row):
    """任务行 → 接口形状：去掉冻结配置，解码 JSON 列。"""
    return {k: json.loads(v) if v and k in JSON_COLUMNS else v for k, v in row.items() if k != "config"}


class BackgroundTaskRunner:
    """子类必须设置 ``table``、``label``、``busy``、``not_retryable``，并实现钩子。"""

    table: str
    label: str  # 例如“素材任务”，用于 404 与失败日志
    busy: str  # 拿不到锁时的 409 提示前缀，后接“，请稍后启动/重试”
    not_retryable: str
    thread_prefix = "task"

    def __init__(self, runtime, store, embedder=None):
        self.runtime, self.store, self.embedder = runtime, store, embedder
        self.threads = {}
        self.recover()
        _RUNNERS.add(self)

    # ------------------------------------------------------------ 子类钩子
    def recover(self):
        """启动时把无人持有工作锁的 queued/running 任务标记为中断；默认按素材工作锁判断。"""
        recover_tasks(self.store, self.table)

    def row_lock(self, row):
        """重试时按任务行重建锁；默认按素材加锁。"""
        return self.lock(row["source_id"])

    def lock(self, *args, **kwargs):
        raise NotImplementedError

    def restore(self, row):
        """冻结配置 → 可运行配置。"""
        raise NotImplementedError

    def prepare_retry(self, row, config):
        """重试前的额外校验（持锁中调用）；默认无。"""

    def retryable(self, row):
        """失败、中断、已取消，或链式检索更新失败的任务可以重试。"""
        publication = json.loads(row.get("publication") or "{}")
        return row["status"] in RETRYABLE or publication.get("status") == "failed"

    def admit(self, row, lock, cancels):
        """begin_task 之前的准入等待；默认无。"""

    def execute(self, task_id, row, config, save):
        """执行任务；正常返回即置为 complete。"""
        raise NotImplementedError

    # ------------------------------------------------------------ 基类
    @property
    def log(self):
        # 日志记在子类模块名下，保持原有的日志来源。
        return logging.getLogger(type(self).__module__)

    def row(self, task_id):
        row = self.store.one(f"SELECT * FROM {self.table} WHERE id=?", (task_id,))
        if not row:
            raise HTTPException(404, "找不到" + self.label)
        return row

    def get(self, task_id):
        return public_task(self.row(task_id))

    def acquire(self, lock, action):
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, f"{self.busy}，请稍后{action}") from exc

    def submit(self, lock, create):
        """持锁执行 ``create() -> (task_id, config)`` 后启动线程；任何失败都释放锁。"""
        self.acquire(lock, "启动")
        try:
            task_id, config = create()
            self.launch(task_id, config, lock)
            return self.get(task_id)
        except BaseException:
            lock.release()
            raise

    def retry(self, task_id):
        lock = self.row_lock(self.row(task_id))
        self.acquire(lock, "重试")
        try:
            row = self.row(task_id)
            if not self.retryable(row):
                raise HTTPException(409, self.not_retryable)
            config = self.restore(row)
            self.prepare_retry(row, config)
            reset_task_attempt(self.store, self.table, task_id)
            self.launch(task_id, config, lock)
            return self.get(task_id)
        except BaseException:
            lock.release()
            raise

    def cancel(self, task_id):
        status = request_task_cancel(self.store, self.table, task_id)
        if status is None:
            raise HTTPException(404, "找不到" + self.label)
        if status not in CANCELLABLE:
            raise HTTPException(409, "只有排队中或运行中的任务可以取消")
        return self.get(task_id)

    def launch(self, task_id, config, lock):
        from mindex.shared.queue_dispatch import queue_backend

        if queue_backend(self.runtime) == "postgres":
            self._enqueue(task_id, lock)
            return
        thread = threading.Thread(
            target=self.run, args=(task_id, config, lock), daemon=True,
            name=f"{self.thread_prefix}-{task_id[:8]}",
        )
        self.threads[task_id] = thread
        thread.start()

    def _enqueue(self, task_id, lock):
        """postgres 队列形态：入队（dedupe 幂等）后释放工作锁，由 worker 领取执行。

        账本先落、队列后入：崩溃窗口由 ``enqueue_backlog`` 补偿扫描兜底
        （plans §7.2 durable outbox 的简化变体；阶段 5 收紧为同事务）。
        """
        from mindex.shared.queue_dispatch import enqueue_task

        try:
            enqueue_task(self.runtime, self.table, task_id)
        finally:
            lock.release()

    def save(self, task_id, field, value):
        if field == "progress":
            return save_task_progress(self.store, self.table, task_id, value)
        if field not in {"result", "publication"}:
            raise ValueError("无效的任务字段")
        self.store.execute(
            f"UPDATE {self.table} SET {field}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (dumps(value), task_id)
        )

    def run(self, task_id, config, lock):
        """在当前线程执行任务（调用方已持锁）；只保留本线程最新一条日志到进度。"""
        return task_logs(self.table)(BackgroundTaskRunner._run)(self, task_id, config, lock)

    def _run(self, task_id, config, lock):
        cancels = registry_for(self.store.root)
        try:
            row = self.store.one(f"SELECT * FROM {self.table} WHERE id=?", (task_id,))
            self.admit(row, lock, cancels)
            begin_task(self.store, self.table, task_id)
            self.execute(task_id, row, config, lambda field, value: self.save(task_id, field, value))
            self.store.execute(
                f"UPDATE {self.table} SET status='complete',updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,)
            )
        except TaskCancelled:
            mark_cancelled(self.store, self.table, task_id)
        except BaseException as exc:  # 后台任务必须记录终态并释放锁
            self.log.exception("%s %s 失败：%s", self.label, task_id, exc)
            self.store.execute(
                f"UPDATE {self.table} SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc)[:2000], task_id),
            )
        finally:
            cancels.clear(task_id)
            lock.release()
            self.threads.pop(task_id, None)
