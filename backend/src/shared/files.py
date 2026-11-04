"""共享权威文件：不可变对象先落盘，再事务发布 PG 修订指针。

本地模式仍使用原文件；远端读取每次核对指针，不把节点旧缓存当作权威状态。
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path, PurePosixPath
from uuid import uuid4

from mindex.shared import jsonio

UNCONDITIONAL = object()


class FileRevisionConflict(ValueError):
    pass


def key_for(store, path):
    path = Path(path).resolve()
    return path.relative_to(store.root.resolve()).as_posix()


def remote(store):
    return getattr(getattr(store, "media", None), "backend", "local") == "s3"


def pointer(store, key):
    return store.one("SELECT * FROM shared_files WHERE path=?", (key,))


def publish(store, path, *, expected=UNCONDITIONAL):
    if not remote(store):
        return None
    path = Path(path)
    key = key_for(store, path)
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    object_key = f"objects/{digest}/{key}"
    store.media.put_immutable(object_key, path, sha256=digest)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT revision FROM shared_files WHERE path=?", (key,)).fetchone()
        if expected is not UNCONDITIONAL and (current[0] if current else None) != expected:
            raise FileRevisionConflict("文件已被其他操作修改，请刷新后重试")
        db.execute(
            "INSERT INTO shared_files(path,object_key,sha256,size,revision) VALUES(?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET object_key=excluded.object_key,sha256=excluded.sha256,"
            " size=excluded.size,revision=excluded.revision",
            (key, object_key, digest, path.stat().st_size, digest))
    return digest


def save_bytes(store, path, payload, *, expected=UNCONDITIONAL):
    if not remote(store):
        jsonio.save_bytes(path, payload)
        return
    key = key_for(store, path)
    digest = hashlib.sha256(payload).hexdigest()
    object_key = f"objects/{digest}/{key}"
    store.media.put_immutable(object_key, io.BytesIO(payload), sha256=digest)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT revision FROM shared_files WHERE path=?", (key,)).fetchone()
        if expected is not UNCONDITIONAL and (current[0] if current else None) != expected:
            raise FileRevisionConflict("文件已被其他操作修改，请刷新后重试")
        db.execute(
            "INSERT INTO shared_files(path,object_key,sha256,size,revision) VALUES(?,?,?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET object_key=excluded.object_key,sha256=excluded.sha256,"
            " size=excluded.size,revision=excluded.revision", (key, object_key, digest, len(payload), digest))
    jsonio.save_bytes(path, payload)


def read_bytes(store, path):
    path = Path(path)
    if not remote(store):
        return path.read_bytes() if path.is_file() else None
    entry = pointer(store, key_for(store, path))
    if entry is None:
        return None
    if entry["size"] < 0:
        return None
    # 即使命中缓存，也核对摘要；节点目录可以在任何时刻丢弃或换库。
    if path.is_file():
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() == entry["sha256"]:
                return path.read_bytes()
    with store.media.materialize(entry["object_key"]) as cached:
        payload = cached.read_bytes()
    if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
        raise ValueError(f"共享文件摘要不符：{entry['path']}")
    jsonio.save_bytes(path, payload)
    return payload


def hydrate(store, path):
    if not remote(store):
        return Path(path)
    if read_bytes(store, path) is None:
        raise FileNotFoundError(key_for(store, path))
    return Path(path)


def entries(store, prefix):
    # 前缀通过比较而非 LIKE，避免文件名中的 %/_ 改变匹配范围。
    return [row for row in store.rows("SELECT * FROM shared_files ORDER BY path")
            if row["path"].startswith(prefix) and row["size"] >= 0]


def hydrate_tree(store, prefix):
    for row in entries(store, prefix):
        path = PurePosixPath(row["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("共享文件路径越界")
        hydrate(store, store.root / row["path"])


def delete_tree(store, prefix):
    with store.connect() as db:
        for row in entries(store, prefix):
            db.execute("UPDATE shared_files SET object_key='',sha256='',size=-1,revision=? WHERE path=?",
                       (uuid4().hex, row["path"]))
