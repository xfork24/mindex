"""Worker 运行时（plans §7.2 阶段 3）：领取持久队列任务并执行。

- 至少一次交付：claim 后开启心跳线程续期；执行体按 ``attempt_id`` 写产物、
  以 fencing 校验提交；失去租约（心跳失败/暂停超 TTL）立即停止提交并放弃任务
  （回收重派，不认定成功）。
- 取消：claim 前已取消的任务不会被领取；执行中轮询 ``cancel_requested`` 并通过
  ``task_cancel`` 传播（与本地形态共用同一终止语义）。
- 处理器注册表：``kind → handler(config, task) -> None``；payload 只携带账本引用
  （表/任务 ID/参数），不序列化函数、局部路径或凭据。
- GPU 准入：worker 声明并发槽位（``--concurrency``，缺省 1；GPU 任务组容量随
  阶段 5 集群装配细化为声明式容量表），槽内串行、槽间并行。
"""

from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from filelock import Timeout

from mindex.shared.backends import open_context

if TYPE_CHECKING:
    from mindex.shared.config import Config

log = logging.getLogger(__name__)

__all__ = ["TaskHandlers", "Worker", "register"]

POLL_SECONDS = 1.0
RENEW_SECONDS = 20.0

Handler = Callable[["Config", "object"], None]


class TaskHandlers:
    """kind → 处理器注册表（worker 进程内静态声明）。"""

    def __init__(self):
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[kind] = handler

    def get(self, kind: str) -> Handler:
        handler = self._handlers.get(kind)
        if handler is None:
            raise KeyError(f"worker 未注册任务类型：{kind}（已注册：{sorted(self._handlers)}）")
        return handler


def register(handlers: TaskHandlers, config) -> None:
    """登记业务任务处理器：账本行引用 → 对应执行链。"""
    from mindex.worker_handlers import register_ledger_handlers

    register_ledger_handlers(handlers, config)


class Worker:
    """单进程 worker；``concurrency`` 为并行执行槽位（阶段 3 缺省 1）。"""

    def __init__(self, config: Config, *, queues: list[str], concurrency: int = 1,
                 once: bool = False, idle_exit: float | None = None,
                 extra_handlers: dict[str, Handler] | None = None,
                 task_ttl: float | None = None, renew_interval: float | None = None):
        self.config = config
        self.queues = queues or ["source", "dialogue", "person", "topic", "studio", "index"]
        self.concurrency = max(1, concurrency)
        self.once = once
        self.idle_exit = idle_exit
        self.task_ttl = task_ttl
        self.renew_interval = renew_interval
        self.identity = str(uuid.uuid4())
        if config.queue.backend != "postgres":
            # 本地形态没有共享队列：worker 显式拒绝而不是静默空转（plans §4）。
            raise RuntimeError(
                "mindex worker 需要 queue.backend=postgres；"
                "本地形态（inline）的后台任务仍由 API 进程内执行")
        self.context = open_context(config)
        self.handlers = TaskHandlers()
        register(self.handlers, config)
        for kind, handler in (extra_handlers or {}).items():
            self.handlers.register(kind, handler)
        from mindex.shared.db.queue_pg import PgTaskQueue

        self.queue = PgTaskQueue(self.context.library_id)
        self._stop = threading.Event()
        self._slots = threading.Semaphore(self.concurrency)

    # ------------------------------------------------------------ 生命周期
    def stop(self, *_):
        self._stop.set()

    def run(self) -> int:
        # 仅主线程可安装信号处理器；测试/嵌入场景在工作线程运行 worker 时跳过。
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.stop)
            signal.signal(signal.SIGINT, self.stop)
        log.info("worker 启动 · %s · queues=%s · concurrency=%d",
                 self.identity[:8], ",".join(self.queues), self.concurrency)
        executed = 0
        idle_since: float | None = None
        while not self._stop.is_set():
            task = self._claim_one()
            if task is None:
                if self.once:
                    break
                if self.idle_exit is not None:
                    idle_since = idle_since or time.monotonic()
                    if time.monotonic() - idle_since >= self.idle_exit:
                        log.info("空闲 %.0fs，按 --idle-exit 退出", self.idle_exit)
                        break
                time.sleep(POLL_SECONDS)
                continue
            idle_since = None
            executed += 1
            self._execute(task)
            if self.once:
                break
        log.info("worker 退出 · 执行 %d 个任务", executed)
        return executed

    # ------------------------------------------------------------ GPU 准入
    def _acquire_gpu_slot(self):
        """按 ``gpu.capacity`` 声明申请一个槽位；容量 0 或非 GPU 类别返回 None。

        槽位是资源租约（locks=postgres 时跨节点互斥、TTL 到期才释放），失联 worker
        不会立即释放槽位造成超卖；等待期间响应停止信号。
        """
        capacity = self.config.gpu.capacity
        if capacity <= 0:
            return None
        from mindex.shared import lock_backend

        source = self.context.store
        while not self._stop.is_set():
            for index in range(capacity):
                try:
                    handle = lock_backend.resource(
                        source, f"gpu-slot:{index}", exclusive=True, timeout=0)
                    handle.acquire(timeout=0)
                    return handle
                except Timeout:
                    continue
            self._stop.wait(0.2)
        return None

    # ------------------------------------------------------------ 执行
    def _claim_one(self):
        claim_kwargs = {"ttl": self.task_ttl} if self.task_ttl else {}
        with self.context.db.connect() as db:
            return self.queue.claim(db, self.identity, self.queues, **claim_kwargs)

    def _execute(self, task) -> None:
        """单任务执行：心跳续期 + 取消轮询 + 终态提交（仅原 owner+attempt 可提交）。"""
        stop = threading.Event()
        lost = threading.Event()

        def heartbeat():
            from mindex.shared.db.queue_pg import TASK_TTL_SECONDS

            interval = self.renew_interval if self.renew_interval is not None else RENEW_SECONDS
            ttl = self.task_ttl if self.task_ttl is not None else TASK_TTL_SECONDS
            while not stop.wait(interval):
                try:
                    with self.context.db.connect() as db:
                        if not self.queue.renew(db, task, ttl=ttl):
                            lost.set()
                            log.warning("任务 %d 心跳续期失败：租约已失效，停止执行", task.id)
                            return
                except Exception:
                    lost.set()
                    log.exception("任务 %d 心跳异常，按失联处理", task.id)
                    return

        beat = threading.Thread(target=heartbeat, daemon=True, name=f"beat-{task.id}")
        beat.start()
        self._slots.acquire()
        gpu_slot = None
        try:
            if task.kind in self.config.gpu.queues:
                gpu_slot = self._acquire_gpu_slot()
            if lost.is_set() or self._cancel_requested(task):
                self._finish_cancelled(task, lost)
                return
            handler = self.handlers.get(task.kind)
            handler(self.config, task)
            if lost.is_set():
                log.warning("任务 %d 执行完成但租约已失联：不提交（回收重派）", task.id)
                return
            with self.context.db.connect() as db:
                if not self.queue.complete(db, task):
                    log.warning("任务 %d 完成提交被拒（attempt 已易主）", task.id)
        except Exception as exc:
            log.exception("任务 %d（%s）执行失败", task.id, task.kind)
            if not lost.is_set():
                with self.context.db.connect() as db:
                    self.queue.fail(db, task, f"{type(exc).__name__}: {exc}")
        finally:
            if gpu_slot is not None:
                gpu_slot.release()
            self._slots.release()
            stop.set()

    def _cancel_requested(self, task) -> bool:
        try:
            with self.context.db.connect() as db:
                return self.queue.cancel_requested(db, task)
        except Exception:  # noqa: BLE001
            return False

    def _finish_cancelled(self, task, lost) -> None:
        if lost.is_set():
            log.warning("任务 %d 租约失联且收到取消：留给回收重派", task.id)
            return
        with self.context.db.connect() as db:
            db.execute(
                "UPDATE task_queue SET status='cancelled', expires_at=NULL,"
                " updated_at=clock_timestamp() WHERE library_id=%s AND id=%s"
                " AND status='running' AND owner=%s AND attempt_id=%s",
                (self.queue.library_id, task.id, task.owner, task.attempt_id))
        log.info("任务 %d 已按取消意图终止", task.id)
