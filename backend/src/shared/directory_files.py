"""不可变目录清单；共享文件表仅保留兼容的当前文件投影。

对象先上传，清单与当前指针最后在同一元数据事务中发布。持久任务把发布交给终态事务。
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import PurePosixPath

_bound: ContextVar[dict | None] = ContextVar("directory_snapshots", default=None)


def context_key(store, prefix):
    return store.library_id, getattr(store.backend, "dsn", str(store.path)), prefix


@contextmanager
def bind(store, prefix, *, deferred=False):
    view = snapshot(store, prefix)
    previous = view.deferred
    view.deferred = previous or deferred
    token = _bound.set({**(_bound.get() or {}), context_key(store, prefix): view})
    try:
        yield view
    finally:
        view.deferred = previous
        _bound.reset(token)


def document_digest(document):
    return hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def directory_for(key):
    parts = PurePosixPath(key).parts
    if len(parts) >= 3 and parts[0] == "studio":
        return "/".join(parts[:2]) + "/"
    return None


def validate_entry(prefix, key, entry):
    path = PurePosixPath(key)
    digest = entry["sha256"]
    if (path.is_absolute() or ".." in path.parts or not key.startswith(prefix)
            or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            or entry["size"] < 0 or entry["object_key"] != f"objects/{digest}/{key}"):
        raise ValueError("目录清单中的文件引用无效")


class Snapshot:
    def __init__(self, store, prefix, *, db=None):
        from mindex.shared.db.fencing import ExecutionFence

        if directory_for(prefix + "__entry__") != prefix:
            raise ValueError("目录清单路径无效")
        self.store, self.prefix = store, prefix
        self.deferred = False
        self.execution = ExecutionFence.capture()
        query = "SELECT h.revision,m.document FROM directory_heads h JOIN directory_manifests m " \
                "ON m.revision=h.revision WHERE h.path=?"
        row = db.execute(query, (prefix,)).fetchone() if db is not None else store.one(query, (prefix,))
        self.revision = row["revision"] if row else None
        if row:
            document = json.loads(row["document"])
            if (document_digest(document) != self.revision or document["path"] != prefix
                    or document.get("format") != 1):
                raise ValueError("目录清单身份或摘要不符")
            self.entries = document["files"]
        else:
            # v19 单文件指针的首次发布升级：只读取数据库，不扫描节点旧目录。
            query = "SELECT * FROM shared_files WHERE size>=0 AND substr(path,1,?)=?"
            rows = db.execute(query, (len(prefix), prefix)).fetchall() if db is not None else store.rows(query, (len(prefix), prefix))
            self.entries = {r["path"]: {k: r[k] for k in ("object_key", "sha256", "size", "revision")} for r in rows}
        for key, entry in self.entries.items():
            validate_entry(prefix, key, entry)
        self.entries = copy.deepcopy(self.entries)
        self.dirty = False

    def put(self, key, payload):
        if not key.startswith(self.prefix):
            raise ValueError("目录文件路径越界")
        digest = hashlib.sha256(payload).hexdigest()
        object_key = f"objects/{digest}/{key}"
        validate_entry(self.prefix, key, {"sha256": digest, "object_key": object_key, "size": len(payload)})
        if self.execution.attempt is not None:
            self.execution.attempt.check()
        self.store.media.put_immutable(object_key, io.BytesIO(payload), sha256=digest)
        self.entries[key] = {"object_key": object_key, "sha256": digest, "size": len(payload), "revision": digest}
        self.dirty = True
        return digest

    def put_path(self, key, path, *, digest=None):
        # 大产物由媒体后端流式上传，不加载成 bytes。
        with path.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if digest is not None and actual != digest:
            raise ValueError("目录依赖的摘要与版本记录不符")
        existing = self.entries.get(key)
        if existing and existing["sha256"] == actual:
            return actual
        object_key = f"objects/{actual}/{key}"
        validate_entry(self.prefix, key, {"sha256": actual, "object_key": object_key, "size": path.stat().st_size})
        if self.execution.attempt is not None:
            self.execution.attempt.check()
        uploaded = self.store.media.put_immutable(object_key, path, sha256=actual)
        self.entries[key] = {"object_key": object_key, "sha256": actual, "size": uploaded.size, "revision": actual}
        self.dirty = True
        return actual

    def commit(self, db):
        if not self.dirty:
            return self.revision
        from mindex.shared.files import FileRevisionConflict

        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT revision FROM directory_heads WHERE path=?", (self.prefix,)).fetchone()
        if (current[0] if current else None) != self.revision:
            raise FileRevisionConflict("目录已被其他节点修改，请刷新后重试")
        document = {"format": 1, "path": self.prefix, "parent": self.revision, "files": self.entries}
        revision = document_digest(document)
        db.execute("INSERT OR IGNORE INTO directory_manifests(revision,path,document) VALUES(?,?,?)",
                   (revision, self.prefix, json.dumps(document, ensure_ascii=False, sort_keys=True)))
        db.execute("INSERT INTO directory_heads(path,revision) VALUES(?,?) ON CONFLICT(path) "
                   "DO UPDATE SET revision=excluded.revision", (self.prefix, revision))
        # 兼容导出、备份与媒体引用扫描；与目录指针同事务更新，不作为读路径权威。
        previous = db.execute("SELECT path FROM shared_files WHERE size>=0 AND substr(path,1,?)=?",
                              (len(self.prefix), self.prefix)).fetchall()
        for row in previous:
            key = row[0]
            if key.startswith(self.prefix) and key not in self.entries:
                db.execute("UPDATE shared_files SET size=-1,revision=? WHERE path=?", (revision, key))
        for key, entry in self.entries.items():
            db.execute("INSERT INTO shared_files(path,object_key,sha256,size,revision) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(path) DO UPDATE SET object_key=excluded.object_key,sha256=excluded.sha256,"
                       "size=excluded.size,revision=excluded.revision",
                       (key, entry["object_key"], entry["sha256"], entry["size"], entry["revision"]))
        return revision

    def publish(self):
        attempt = self.execution.attempt
        if self.store.pg and attempt is not None:
            attempt.defer((self.store.library_id, "directory", self.prefix), self.commit)
            return
        if self.deferred:
            return
        with self.store.connect() as db:
            revision = self.commit(db)
        self.revision, self.dirty = revision, False


def snapshot(store, prefix):
    from mindex.shared.db.fencing import current_attempt

    bound = (_bound.get() or {}).get(context_key(store, prefix))
    if bound is not None:
        return bound
    attempt = current_attempt()
    if store.pg and attempt is not None:
        from mindex.shared import lock_backend

        if attempt.library_id != store.library_id or attempt.backend.dsn != store.backend.dsn:
            raise ValueError("目录与任务所属素材库不一致")

        def create():
            attempt.retain((store.library_id, "directory", prefix),
                           lock_backend.resource(store, "directory:" + prefix, timeout=None))
            return Snapshot(store, prefix)

        return attempt.state((store.library_id, "directory", prefix), create)
    return Snapshot(store, prefix)


def referenced_objects(store):
    """备份和 GC 保留全部历史目录清单引用；按页读取，避免加载整个清单历史。"""
    cursor = 0
    while True:
        rows = store.rows("SELECT rowid AS sequence,revision,path,document FROM directory_manifests "
                          "WHERE rowid>? ORDER BY rowid LIMIT 32", (cursor,))
        if not rows:
            return
        for row in rows:
            document = json.loads(row["document"])
            if document_digest(document) != row["revision"] or document["path"] != row["path"] or document.get("format") != 1:
                raise ValueError("目录历史清单摘要或身份不符，拒绝备份或清理")
            for key, entry in document["files"].items():
                validate_entry(row["path"], key, entry)
                yield entry["object_key"]
        cursor = rows[-1]["sequence"]
