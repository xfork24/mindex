"""Persistent work summaries; serve the last result while refreshing expensive coverage."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from mindex.admin.curation import source_public
from mindex.admin.source_work import attach_tasks

log = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="work-summary")


class WorkCache:
    # Periodically recheck artifacts changed outside SQLite as well.
    max_age = 60
    version = 4

    def __init__(self, library):
        self.library = library
        self.store = library.store
        self.directory = self.store.root / "cache" / "admin-work"
        self.lock = threading.Lock()
        self.compute_lock = threading.Lock()
        self.cold_lock = threading.Lock()
        self.pending = set()

    def token(self):
        result = []
        for path in (self.store.path, self.store.path.with_name(self.store.path.name + "-wal")):
            try:
                stat = path.stat()
                result.append([stat.st_mtime_ns, stat.st_size])
            except FileNotFoundError:
                result.append(None)
        return result

    def path(self, sid):
        return self.directory / (hashlib.sha256(sid.encode()).hexdigest() + ".json")

    def refresh(self, sid):
        with self.compute_lock:
            return self._refresh(sid)

    def _refresh(self, sid):
        try:
            token = self.token()
            value = self.library.source(sid)
            entry = {"version": self.version, "token": token, "created": time.time(), "value": value}
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.path(sid)
            temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                temporary.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            return value
        finally:
            with self.lock:
                self.pending.discard(sid)

    def background(self, sid):
        try:
            self.refresh(sid)
        except Exception:
            log.exception("Failed to refresh work summary for %s", sid)

    def get(self, sid):
        # Always validate existence and read current organization metadata.
        source = source_public(self.library.source_row(sid))
        try:
            entry = json.loads(self.path(sid).read_text(encoding="utf-8"))
            if entry["version"] != self.version:
                raise ValueError("Old summary schema")
        except (OSError, ValueError, KeyError):
            # The first read builds the persistent checkpoint. Subsequent reads,
            # including after restart, never wait for a coverage refresh.
            with self.cold_lock:
                try:
                    entry = json.loads(self.path(sid).read_text(encoding="utf-8"))
                    if entry["version"] != self.version:
                        raise ValueError("Old summary schema")
                except (OSError, ValueError, KeyError):
                    return {**self.refresh(sid), **source, "work_refreshing": False}
        stale = entry["token"] != self.token() or time.time() - entry["created"] >= self.max_age
        if stale:
            with self.lock:
                if sid not in self.pending:
                    self.pending.add(sid)
                    _executor.submit(self.background, sid)
        value = {**entry["value"], **source, "work_refreshing": stale}
        # Progress/log messages already live in the task tables. Keep these fresh
        # even while coverage and publication comparisons refresh in the background.
        attach_tasks(self.store, value, value["work"])
        return value
