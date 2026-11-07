"""引用发布、远端 pin 与 GC 的同一协调边界。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

from shared.db.dialect import _segments
from shared.db.fencing import current_attempt
from shared.db.pg_backend import BUSINESS_CONNECTION
from shared.migration_state import backend_scope, guard
from shared.resource_registry import PREFIXES, path_values, referenced


class ObjectDeleting(RuntimeError):
    pass


def coordination_keys(library_id):
    digest = hashlib.sha256(("objects:" + library_id).encode()).digest()
    return (int.from_bytes(digest[:4], "big", signed=True), int.from_bytes(digest[4:8], "big", signed=True))


def reference_arguments(sql, values):
    """只检查引用列，普通用户备注即使像对象 key 也不改写或拦截。"""

    fields = {
        "path",
        "thumbnail",
        "source_path",
        "image_path",
        "sample_path",
        "object_key",
        "snapshot",
        "document",
        "receipt",
        "data",
        "refs",
        "config",
    }

    def split(expression):
        result, current, depth = [], [], 0
        for code, text in _segments(expression):
            if not code:
                current.append(text)
                continue
            for char in text:
                depth += (char == "(") - (char == ")")
                if char == "," and depth == 0:
                    result.append("".join(current))
                    current = []
                else:
                    current.append(char)
        result.append("".join(current))
        return result

    def count(expression):
        return sum(text.count("?") + text.count("%s") for code, text in _segments(expression) if code)

    match = re.search(r"INSERT(?:\s+OR\s+\w+)?\s+INTO\s+[\w\"]+\s*\((.*?)\)\s*VALUES\s*\(", sql, re.I | re.S)
    pairs = []
    if match:
        body = sql[match.end() :]
        depth, end = 1, 0
        for i, char in enumerate(body):
            depth += (char == "(") - (char == ")")
            if depth == 0:
                end = i
                break
        pairs = list(zip(split(match[1]), split(body[:end])))
    else:
        match = re.search(r"\bUPDATE\s+[\w\"]+\s+SET\s+(.*?)(?:\bWHERE\b|$)", sql, re.I | re.S)
        if match:
            pairs = [tuple(part.split("=", 1)) for part in split(match[1]) if "=" in part]
    index = 0
    selected = []
    values = list(values)
    for name, expression in pairs:
        size = count(expression)
        if name.strip().strip('"').lower() in fields:
            selected.extend(values[index : index + size])
        index += size
    return selected


def publication_guard(connection, library_id, values=()):
    """PG 业务写入持共享引用锁；GC 删除意图持独占锁。"""
    connection.execute("SELECT pg_advisory_xact_lock_shared(%s,%s)", coordination_keys(library_id))

    keys = set()
    for value in values:
        if not isinstance(value, str):
            continue
        if value.startswith(PREFIXES):
            keys.add(value)
        elif value.startswith(("{", "[")):
            try:
                keys.update(path_values(json.loads(value)))
            except ValueError:
                continue
    if keys:
        rows = connection.execute(
            "SELECT object_key,state FROM resource_objects WHERE object_key=ANY(%s) FOR UPDATE",
            (sorted(keys),),
        ).fetchall()
        for key, state in rows:
            if state in {"deleting", "deleted"}:
                raise ObjectDeleting(f"对象正在删除或已删除，拒绝发布引用：{key}")
            if state == "candidate":
                connection.execute(
                    "UPDATE resource_objects SET state='live',marked_at=NULL WHERE object_key=%s", (key,)
                )


class ObjectCoordinator:
    def __init__(self, store):
        self.store = store

    @contextmanager
    def locked(self, key, *, deleting=False, writing=False):
        if self.store.pg and not deleting:
            active = BUSINESS_CONNECTION.get()
            if (
                active is not None
                and active._scope == self.store.backend.dsn
                and active._library_id == self.store.library_id
            ):
                active.execute(
                    "SELECT pg_advisory_xact_lock_shared(?,?)", coordination_keys(self.store.library_id)
                )
                yield active
                return
        with (
            self.store.backend.connect(control=not (deleting or writing))
            if self.store.pg
            else self.store.connect() as db
        ):
            if self.store.pg:
                fn = "pg_advisory_xact_lock" if deleting else "pg_advisory_xact_lock_shared"
                db.execute(f"SELECT {fn}(?,?)", coordination_keys(self.store.library_id))
                digest = hashlib.sha256((self.store.library_id + key).encode()).digest()
                db.execute(
                    "SELECT pg_advisory_xact_lock(?,?)",
                    (
                        int.from_bytes(digest[:4], "big", signed=True),
                        int.from_bytes(digest[4:8], "big", signed=True),
                    ),
                )
            else:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def ensure_live(self, db, key, *, writing=False):
        row = db.execute("SELECT state FROM resource_objects WHERE object_key=?", (key,)).fetchone()
        if row and (row[0] == "deleting" or row[0] == "deleted" and not writing):
            raise ObjectDeleting(f"对象正在删除或已删除，暂不能读取、签名或发布：{key}")
        if row and row[0] == "live":
            return
        db.execute(
            "INSERT INTO resource_objects(object_key,state) VALUES(?,'live') "
            "ON CONFLICT(object_key) DO UPDATE SET state='live',marked_at=NULL,delete_intent=NULL",
            (key,),
        )

    def pin(self, key, *, owner, kind, ttl=900, pin_id=None):
        pin_id = pin_id or uuid4().hex
        with self.locked(key) as db:
            self.ensure_live(db, key)
            deadline = self.now(db) + ttl
            db.execute(
                "INSERT INTO resource_pins(id,object_key,owner,kind,expires_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET expires_at=CASE WHEN "
                "resource_pins.expires_at>excluded.expires_at "
                "THEN resource_pins.expires_at ELSE excluded.expires_at END",
                (pin_id, key, owner, kind, deadline),
            )
        return pin_id

    def download(self, key, *, ttl):
        pin_id = hashlib.sha256((self.store.library_id + ":download:" + key).encode()).hexdigest()
        return self.pin(key, owner="download", kind="download", ttl=ttl, pin_id=pin_id)

    def prune(self):
        with self.store.backend.connect(control=True) if self.store.pg else self.store.connect() as db:
            return db.execute("DELETE FROM resource_pins WHERE expires_at<=?", (self.now(db),)).rowcount

    @contextmanager
    def retained(self, key, *, kind="materialize", ttl=900):
        pin_id = self.pin(key, owner=uuid4().hex, kind=kind, ttl=ttl)
        stop = threading.Event()

        def renew():
            while not stop.wait(ttl / 3):
                try:
                    self.pin(key, owner=pin_id, kind=kind, ttl=ttl, pin_id=pin_id)
                except Exception:
                    # 后端失联时不伪造成功；原 pin 的宽限仍保留，读取本地 pin 继续保护。

                    logging.getLogger(__name__).exception("远端 pin 续期失败：%s", key)

        thread = threading.Thread(target=renew, name="remote-pin-renew", daemon=True)

        transactional = self.store.pg and BUSINESS_CONNECTION.get() is not None
        if not transactional:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if not transactional:
                thread.join(timeout=5)
            try:
                with self.locked(key) as db:
                    db.execute("DELETE FROM resource_pins WHERE id=?", (pin_id,))
            except Exception:
                logging.getLogger(__name__).exception("远端 pin 释放失败，将按 TTL 回收：%s", key)

    def now(self, db):
        if self.store.pg:
            return float(db.execute("SELECT extract(epoch from clock_timestamp())").fetchone()[0])
        return time.time()

    def pinned(self, db, key):
        if (
            self.store.pg
            and db.execute(
                "SELECT 1 FROM download_pins WHERE library_id=? AND key=? AND expires_at>clock_timestamp()",
                (self.store.library_id, key),
            ).fetchone()
        ):
            return True
        return (
            db.execute(
                "SELECT 1 FROM resource_pins WHERE object_key=? AND expires_at>? LIMIT 1", (key, self.now(db))
            ).fetchone()
            is not None
        )

    @contextmanager
    def writing(self, key):
        with self.locked(key, writing=True) as db:
            guard(db, backend_scope(self.store))
            self.ensure_live(db, key, writing=True)
            yield

            attempt = current_attempt()
            owner = attempt.task.attempt_id if attempt else uuid4().hex
            db.execute(
                "INSERT INTO resource_pins(id,object_key,owner,kind,expires_at) VALUES(?,?,?,?,?)",
                (uuid4().hex, key, owner, "attempt" if attempt else "write", self.now(db) + 900),
            )

    def collect(self, media, key, *, grace, now=None):
        """标记、独立提交删除意图，再条件删除；崩溃后可重放 deleting 状态。"""

        with self.locked(key, deleting=True) as db:
            now = self.now(db) if now is None else now
            if referenced(self.store, key, connection=db) or self.pinned(db, key):
                return False
            try:
                obj = media.stat(key)
            except FileNotFoundError:
                db.execute("UPDATE resource_objects SET state='deleted' WHERE object_key=?", (key,))
                return False
            row = db.execute("SELECT * FROM resource_objects WHERE object_key=?", (key,)).fetchone()
            if row is None or row["state"] in {"live", "deleted"}:
                db.execute(
                    "INSERT INTO resource_objects(object_key,state,etag,size,version_id,marked_at) "
                    "VALUES(?,'candidate',?,?,?,?) "
                    "ON CONFLICT(object_key) DO UPDATE SET "
                    "state='candidate',etag=excluded.etag,size=excluded.size,"
                    "version_id=excluded.version_id,marked_at=excluded.marked_at",
                    (key, obj.etag, obj.size, getattr(obj, "version_id", None), now),
                )
                if grace > 0:
                    return False
                row = db.execute("SELECT * FROM resource_objects WHERE object_key=?", (key,)).fetchone()
            if (
                row["etag"] != obj.etag
                or row["size"] != obj.size
                or row["version_id"] != getattr(obj, "version_id", None)
            ):
                db.execute(
                    "UPDATE resource_objects SET state='live',marked_at=NULL WHERE object_key=?", (key,)
                )
                return False
            if row["state"] != "deleting" and now - row["marked_at"] < grace:
                return False
            db.execute(
                "UPDATE resource_objects SET state='deleting',delete_intent=? WHERE object_key=?",
                (row["delete_intent"] or uuid4().hex, key),
            )
        with self.locked(key, deleting=True) as db:
            # 必须在删除前重新读取当前引用及 pins，不能复用扫描开始时的集合。
            if referenced(self.store, key, connection=db) or self.pinned(db, key):
                db.execute(
                    "UPDATE resource_objects SET state='live',marked_at=NULL WHERE object_key=?", (key,)
                )
                return False
            media.delete_if_match(
                key, etag=obj.etag, size=obj.size, version_id=getattr(obj, "version_id", None)
            )
            db.execute("UPDATE resource_objects SET state='deleted' WHERE object_key=?", (key,))
        return True
