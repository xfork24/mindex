"""Optional GPU exclusivity with independent task and source lifetime guards."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from mindex.shared import locks


def scope_path(root, source_id, work):
    token = hashlib.sha256(str(source_id).encode()).hexdigest()
    return Path(root) / "locks" / f"{token}.{work}.lock"


class ProcessingLock:
    def __init__(self, config, *, source_id=None, work=None, deleting=False):
        self.locks = [locks.named(config.data_dir, "pipeline", exclusive=config.enforce_gpu_lock)]
        if source_id is None or deleting:
            self.locks.append(locks.named(config.data_dir, "library", exclusive=deleting))
        if source_id is not None:
            self.locks.append(
                locks.SharedFileLock(scope_path(config.data_dir, source_id, "source"), exclusive=deleting))
        if work is not None:
            path = scope_path(config.data_dir, source_id, work)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.locks.append(FileLock(str(path), timeout=0, thread_local=False))

    @property
    def is_locked(self):
        return all(lock.is_locked for lock in self.locks)

    def acquire(self, **_):
        try:
            for lock in self.locks:
                lock.acquire()
        except BaseException:
            self.release()
            raise
        return self

    def release(self):
        for lock in reversed(self.locks):
            lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


class PublicationLock(ProcessingLock):
    """Own a queued subtask independently while waiting for GPU admission."""

    def __init__(self, config, *, source_id, work):
        path = scope_path(config.data_dir, source_id, work)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.locks = [locks.named(config.data_dir, 'library'),
                      locks.SharedFileLock(scope_path(config.data_dir, source_id, 'source')),
                      FileLock(str(path), timeout=0, thread_local=False)]
        self.pipeline = locks.named(config.data_dir, 'pipeline', exclusive=config.enforce_gpu_lock)

    def admit(self, check=None):
        """等待 GPU 准入；check 每轮调用一次，可抛出 TaskCancelled 结束等待。"""
        while True:
            if check is not None:
                check()
            try:
                self.pipeline.acquire()
                return
            except Timeout:
                time.sleep(.1)

    def release(self):
        self.pipeline.release()
        super().release()


def recover_tasks(store, table):
    """A live task owns its work lock even when GPU exclusivity is disabled."""
    _recover_tasks(store, table)


def task_lock_work(work, options=None):
    if work == 'publish' and options:
        return f"publish.{options['work']}.{options.get('scope', 'moment')}"
    return work


def _recover_tasks(store, table):
    for row in store.rows(f"SELECT * FROM {table} WHERE status IN ('queued','running')"):
        work = row["work"] if table == "source_tasks" else {"dialogue_tasks": "dialogue", "person_tasks": "persons"}[table]
        scoped = task_lock_work(work, json.loads(row['options']) if work == 'publish' else None)
        path = scope_path(store.root, row["source_id"], scoped)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Also respect the old publication lock while a pre-upgrade worker
            # is still alive. New workers own only their individual subtask.
            from contextlib import ExitStack

            with ExitStack() as locks:
                locks.enter_context(FileLock(str(path), timeout=0))
                if scoped != work:
                    locks.enter_context(FileLock(str(scope_path(store.root, row['source_id'], work)), timeout=0))
                store.execute(
                    f"UPDATE {table} SET status='interrupted',error='进程已中断；请重试继续检查点',"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','running')",
                    (row["id"],),
                )
        except Timeout:
            pass


def index_write_lock(root, *, check=None):
    """串行发布；有检查回调时等待可取消，拿锁后再读取活动版本。"""
    if check is None:
        return locks.named(root, "index-write", timeout=None)
    return _checked_index_write_lock(root, check)


@contextmanager
def _checked_index_write_lock(root, check):
    lock = locks.named(root, 'index-write', timeout=0)
    last_check = time.monotonic()
    while True:
        try:
            lock.acquire()
            break
        except Timeout:
            if time.monotonic() - last_check >= .5:
                check()
                last_check = time.monotonic()
            time.sleep(.05)
    try:
        yield lock
    finally:
        lock.release()
