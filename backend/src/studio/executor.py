"""创作执行器：进程内单执行线程、跨进程 studio 锁、取消标记与后台 worker 包装。

- 互斥：``guard``（进程内）+ ``data_dir/studio/studio.lock``（跨进程，``shared.locks.named``）。
  旧任务步骤执行与版本工作台构建共用同一个执行线程槽，两者在进程内互斥。
- 取消：进程内 ``threading.Event`` 加哨兵文件（跨进程可见）。两种执行各有一种哨兵::

      steps  任务目录/steps.cancel          （旧任务步骤执行）
      build  任务目录/builds/<build_id>.cancel（版本工作台构建）

  哨兵的路径、请求、检查与清理只在本模块实现，构建执行器（``workspace.builder``）与 API 共用。
- worker：``run_guarded`` 先记录原始异常（含 traceback），再单独保护地写失败终态，最后释放锁；
  写终态失败不会掩盖原始异常。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from filelock import Timeout

from mindex.shared import locks
from mindex.studio import studio
from mindex.studio.studio_artifacts import safe_path

log = logging.getLogger(__name__)

BUSY = "已有创作步骤在执行，请稍候再试"

Kind = Literal["steps", "build"]


# ---------------------------------------------------------------- 取消哨兵（跨进程）


def cancel_sentinel(config, kind: Kind, job_id: str, key: str | None = None) -> Path:
    """取消哨兵文件路径：``steps`` 为任务目录下的 steps.cancel，``build`` 为 builds/<key>.cancel。"""
    directory = studio.job_dir(config, job_id)
    if kind == "steps":
        return directory / "steps.cancel"
    if kind == "build" and key:
        return safe_path(directory, f"builds/{key}.cancel")
    raise ValueError(f"未知的取消类型：{kind}")


def request_cancel(config, kind: Kind, job_id: str, key: str | None = None) -> None:
    path = cancel_sentinel(config, kind, job_id, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def cancel_requested(config, kind: Kind, job_id: str, key: str | None = None,
                     event: threading.Event | None = None) -> bool:
    """进程内事件已置位，或哨兵文件存在，即视为已请求取消。"""
    return bool(event is not None and event.is_set()) or cancel_sentinel(config, kind, job_id, key).exists()


def clear_cancel(config, kind: Kind, job_id: str, key: str | None = None) -> None:
    cancel_sentinel(config, kind, job_id, key).unlink(missing_ok=True)


# ---------------------------------------------------------------- worker 包装


def run_guarded(fn: Callable[[], object], *, message: str,
                on_error: Callable[[Exception], object] | None = None,
                finally_: Callable[[], object] | None = None,
                lock: locks.SharedFileLock | None = None) -> None:
    """后台 worker 包装：异常先 ``log.exception``，再调用 ``on_error`` 写失败终态。

    ``on_error`` 与 ``finally_`` 各自单独保护，失败只记录日志、不掩盖原始异常；锁总在最后释放。
    """
    try:
        fn()
    except Exception as exc:  # 后台失败必须记录，不能静默丢失
        log.exception("%s", message)
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                log.exception("%s；写入失败终态时再次出错", message)
    finally:
        try:
            if finally_ is not None:
                finally_()
        except Exception:
            log.exception("%s；执行收尾时出错", message)
        finally:
            if lock is not None:
                lock.release()


# ---------------------------------------------------------------- 执行器


class StudioExecutor:
    """一个创作路由实例的执行器：单执行线程槽、studio 锁与运行中的取消事件。"""

    def __init__(self, base_config):
        self.base = base_config
        # 可重入：reserve() 持锁期间调用 start()；标题生成等 job.json 写入也用它串行。
        self.guard = threading.RLock()
        self._thread: threading.Thread | None = None
        self._events: dict[tuple[str, str, str | None], threading.Event] = {}

    # ------------------------------------------------------------ 锁

    def file_lock(self) -> locks.SharedFileLock:
        """非阻塞获取跨进程 studio 锁；已被占用时返回 409。"""
        lock = locks.named(self.base.data_dir, "studio")
        try:
            return lock.acquire()
        except Timeout:
            raise HTTPException(409, BUSY) from None

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """同步修改：进程内 guard + 跨进程 studio 锁。"""
        with self.guard:
            lock = self.file_lock()
            try:
                yield
            finally:
                lock.release()

    def busy(self) -> bool:
        with self.guard:
            return self._thread is not None and self._thread.is_alive()

    @contextmanager
    def reserve(self) -> Iterator[locks.SharedFileLock]:
        """为一次后台执行做准备：持 guard、确认没有执行线程、获取 studio 锁。

        块内应以 ``start(lock=...)`` 启动 worker，锁随之交给线程，线程结束时释放；
        块内抛出异常（含启动失败）时这里释放锁。
        """
        with self.guard:
            if self.busy():
                raise HTTPException(409, BUSY)
            lock = self.file_lock()
            try:
                yield lock
            except BaseException:
                lock.release()
                raise

    def start(self, name: str, target: Callable[[], object], *,
              lock: locks.SharedFileLock | None = None,
              on_failure: Callable[[Exception], object] | None = None) -> threading.Thread:
        """启动唯一的后台执行线程（守护线程）。

        已有存活线程时返回 409。``lock`` 交给线程：线程结束时先腾出执行线程槽再释放锁，
        因此锁一释放，新的执行即可启动。启动失败时调用 ``on_failure`` 写终态（单独保护）后
        重新抛出原异常，锁由 ``reserve`` 释放。
        """
        with self.guard:
            if self.busy():
                raise HTTPException(409, BUSY)

            def body():
                try:
                    target()
                finally:
                    try:
                        with self.guard:
                            if self._thread is threading.current_thread():
                                self._thread = None
                    finally:
                        if lock is not None:
                            lock.release()

            thread = threading.Thread(target=body, daemon=True, name=name)
            self._thread = thread
            try:
                thread.start()
            except Exception as exc:
                self._thread = None
                log.exception("创作执行线程 %s 启动失败", name)
                if on_failure is not None:
                    try:
                        on_failure(exc)
                    except Exception:
                        log.exception("创作执行线程 %s 启动失败后写入终态出错", name)
                raise
            return thread

    # ------------------------------------------------------------ 取消

    def cancel_event(self, kind: Kind, job_id: str, key: str | None = None) -> threading.Event:
        """登记一次执行的进程内取消事件（同一键重复登记返回同一事件）。"""
        with self.guard:
            return self._events.setdefault((kind, job_id, key), threading.Event())

    def request_cancel(self, kind: Kind, job_id: str, key: str | None = None) -> None:
        """写哨兵文件（其他进程可见），并置位本进程内的事件。"""
        request_cancel(self.base, kind, job_id, key)
        with self.guard:
            event = self._events.get((kind, job_id, key))
        if event is not None:
            event.set()

    def cancelled(self, kind: Kind, job_id: str, key: str | None = None) -> bool:
        with self.guard:
            event = self._events.get((kind, job_id, key))
        return cancel_requested(self.base, kind, job_id, key, event)

    def clear(self, kind: Kind, job_id: str, key: str | None = None) -> None:
        """注销事件并删除哨兵；幂等。"""
        with self.guard:
            self._events.pop((kind, job_id, key), None)
        clear_cancel(self.base, kind, job_id, key)

    run_guarded = staticmethod(run_guarded)
