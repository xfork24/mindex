"""显式工作集作用域：未知输出按写入前增长预约，不允许先写后核算。"""

from __future__ import annotations

import threading
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

_CURRENT: ContextVar[object] = ContextVar("work_budget", default=None)


class WorkBudget:
    def __init__(self, store):
        self.store = store
        self.staging = store._backend_context.staging
        self.resources = ExitStack()
        self.materializations = {}
        self.paths = set()
        self.pending = {}
        self.guard = threading.RLock()

    def owns(self, path):
        return Path(path).resolve().is_relative_to(self.staging.root)

    def retain(self, path):
        path = Path(path).resolve()
        if not self.owns(path):
            return
        with self.guard:
            if path not in self.paths:
                self.staging.pin(path)
                self.paths.add(path)

    def materialize(self, key, context):
        with self.guard:
            if key not in self.materializations:
                self.materializations[key] = self.resources.enter_context(context)
            return self.materializations[key]

    def committed(self, path):
        reservation = self.pending.pop(Path(path).resolve(), None)
        if reservation is not None:
            reservation.deferred = False
            reservation.close()

    def close(self):
        self.resources.close()
        self.materializations.clear()
        for reservation in list(self.pending.values()):
            reservation.deferred = False
            reservation.close()
        self.pending.clear()
        for path in self.paths:
            self.staging.unpin(path)
        self.paths.clear()


def current():
    return _CURRENT.get()


@contextmanager
def scope(store):
    existing = current()
    staging = getattr(getattr(store, "_backend_context", None), "staging", None)
    if staging is None or existing is not None and existing.store.cache_identity == store.cache_identity:
        yield existing
        return
    budget = WorkBudget(store)
    token = _CURRENT.set(budget)
    try:
        yield budget
    finally:
        _CURRENT.reset(token)
        budget.close()


class GrowingReservation:
    def __init__(self, path):
        self.budget = current()
        if self.budget is not None and not self.budget.owns(path):
            self.budget = None
        self.reserved = 0
        self.deferred = False
        self.path = Path(path).resolve()
        if self.budget is not None:
            self.budget.retain(path)

    def grow(self, size):
        if self.budget is not None:
            self.budget.staging.reserve(size, purpose="输出与临时工作集增长", path=self.path)
            self.reserved += size

    def defer(self):
        if self.budget is not None:
            self.deferred = True
            self.budget.pending[self.path] = self

    def commit(self, target):
        import os

        if self.budget is None:
            os.replace(self.path, target)
            return
        self.budget.retain(target)
        self.budget.staging.commit_path(self.path, target, self.reserved)
        self.reserved = 0
        self.deferred = False
        self.budget.pending.pop(self.path, None)

    def close(self):
        if self.deferred:
            return
        if self.budget is not None and self.reserved:
            self.budget.staging.release(self.reserved, path=self.path)
            self.reserved = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def budgeted(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with scope(self.store):
            return method(self, *args, **kwargs)

    return wrapped


def resource_write(method):
    @wraps(method)
    def wrapped(store, *args, **kwargs):
        with scope(store):
            return method(store, *args, **kwargs)

    return wrapped


def stream_command(args, output, *, timeout):
    """外部编码器通过有界管道输出；写入前预约，失败立即终止子进程。"""
    import subprocess
    from collections import deque

    from shared.media import MediaError

    formats = {
        ".mp4": "mp4",
        ".m4a": "mp4",
        ".mov": "mov",
        ".mkv": "matroska",
        ".webm": "webm",
        ".wav": "wav",
        ".mp3": "mp3",
        ".flac": "flac",
        ".ogg": "ogg",
        ".png": "image2pipe",
        ".jpg": "image2pipe",
        ".jpeg": "image2pipe",
    }
    suffix = output.suffix.lower()
    if suffix not in formats or "%" in output.name:
        raise MediaError("输出类型不能可靠预约工作集；请使用受支持的单文件输出")
    options = [*args[:-1]]
    if suffix in {".mp4", ".m4a", ".mov"}:
        options.extend(["-movflags", "frag_keyframe+empty_moov"])
    options.extend(["-f", formats[suffix], "pipe:1"])
    error = deque(maxlen=32)
    stop = threading.Event()
    timed_out = threading.Event()
    process = subprocess.Popen(options, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def stderr():
        while chunk := process.stderr.read(2048):
            error.append(chunk)

    def watchdog():
        if not stop.wait(timeout):
            timed_out.set()
            process.kill()

    reader = threading.Thread(target=stderr, name="media-stderr", daemon=True)
    timer = threading.Thread(target=watchdog, name="media-deadline", daemon=True)
    reader.start()
    timer.start()
    try:
        with GrowingReservation(output) as reservation, output.open("wb") as target:
            while chunk := process.stdout.read(65536):
                from shared.db.fencing import current_attempt

                attempt = current_attempt()
                if attempt is not None:
                    attempt.check()
                reservation.grow(len(chunk))
                target.write(chunk)
            target.flush()
            process.wait()
            reader.join()
            if timed_out.is_set():
                from shared.query_budget import check

                check()
                raise MediaError("媒体处理超过期限，已终止")
            if process.returncode:
                raise MediaError(b"".join(error).decode(errors="replace")[-2000:])
            if output.name.startswith("."):
                reservation.defer()
    except BaseException:
        process.kill()
        process.wait()
        output.unlink(missing_ok=True)
        raise
    finally:
        stop.set()
        process.stdout.close()
        process.stderr.close()
        reader.join(timeout=5)
        timer.join(timeout=5)
    return b""


def configured(method):
    @wraps(method)
    def wrapped(config, *args, **kwargs):
        from shared.storage import Store

        if config.storage.backend != "s3":
            return method(config, *args, **kwargs)
        with Store.open(config) as store, scope(store):
            return method(config, *args, **kwargs)

    return wrapped


def copy_file(source, target):
    """有界复制；临时文件隐藏并按块预约，原子提交后计入可见工作集。"""
    import shutil
    import tempfile

    budget = current()
    if budget is None or not budget.owns(target):
        return shutil.copy2(source, target)
    source, target = Path(source), Path(target)
    if target.is_dir():
        target = target / source.name
    budget.retain(source)
    budget.retain(target)
    temporary = None
    reservation = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".copy-", delete=False) as sink:
            temporary = Path(sink.name)
            reservation = GrowingReservation(temporary)
            with source.open("rb") as origin:
                while chunk := origin.read(1024 * 1024):
                    reservation.grow(len(chunk))
                    sink.write(chunk)
        # 先关闭 NamedTemporaryFile，Windows 才允许修改属性和原子替换。
        shutil.copystat(source, temporary)
        reservation.commit(target)
        return target
    finally:
        if reservation is not None:
            reservation.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def commit_file(partial, target):
    import os

    budget = current()
    reservation = budget.pending.get(Path(partial).resolve()) if budget is not None else None
    if reservation is not None:
        reservation.commit(target)
    else:
        os.replace(partial, target)
