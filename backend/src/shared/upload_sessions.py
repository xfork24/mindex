"""持久分片上传：pending 对象不可见，校验完成后才登记正式素材。"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import time
from contextlib import ExitStack
from pathlib import PurePosixPath
from uuid import uuid4

from shared import lock_backend
from shared.object_coordination import ObjectCoordinator
from shared.storage import dumps


class UploadError(ValueError):
    def __init__(self, message, status=409):
        self.status = status
        super().__init__(message)


def valid_digest(value):
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


class UploadSessions:
    def __init__(self, store, config):
        self.store, self.config = store, config

    def create(self, filename, size, sha256, transport="api"):
        if not valid_digest(sha256) or not 0 < size <= self.config.source_upload_max_bytes:
            raise UploadError("文件大小或 SHA256 声明无效", 422)
        name = PurePosixPath(filename.replace("\\", "/")).name
        if name in {"", ".", ".."} or len(name) > 255:
            raise UploadError("文件名无效", 422)
        if transport not in {"api", "s3"}:
            raise UploadError("上传传输方式必须为 api 或 s3", 422)
        if transport == "s3" and getattr(self.store.media, "backend", "local") != "s3":
            raise UploadError("当前部署未启用 S3 直传", 422)
        task_id, token = uuid4().hex, secrets.token_urlsafe(32)
        limits = self.config.uploads
        part_size = max(limits.part_bytes, 5 * 1024**2) if transport == "s3" else limits.part_bytes
        if (size + part_size - 1) // part_size > 10000:
            raise UploadError("分片数超过 10000，请增大 uploads.part_bytes", 422)
        with lock_backend.resource(self.store, "upload-quota", timeout=None), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT count(*) FROM upload_sessions WHERE state IN ('pending','completing')"
            ).fetchone()[0]
            if active >= limits.max_sessions:
                raise UploadError("未完成上传会话数已达到上限", 413)
            reserved = db.execute(
                "SELECT COALESCE(SUM(size),0) FROM upload_sessions WHERE state IN ('pending','completing')"
            ).fetchone()[0]
            if reserved + size > limits.pending_bytes:
                raise UploadError("未完成上传占用已超过配额，请完成、取消或回收过期会话", 413)
            db.execute(
                "INSERT INTO "
                "upload_sessions(id,owner,filename,size,sha256,state,expires_at,part_size,transport) "
                "VALUES(?,?,?,?,?,'pending',?,?,?)",
                (
                    task_id,
                    hashlib.sha256(token.encode()).hexdigest(),
                    name,
                    size,
                    sha256,
                    time.time() + limits.ttl_seconds,
                    part_size,
                    transport,
                ),
            )
        return {**self.get(task_id, token), "upload_token": token}

    def _row(self, db, task_id, token, *, active=False):
        row = db.execute("SELECT * FROM upload_sessions WHERE id=?", (task_id,)).fetchone()
        if row is None or not secrets.compare_digest(
            row["owner"], hashlib.sha256(token.encode()).hexdigest()
        ):
            raise UploadError("找不到上传会话或访问凭据无效", 404)
        if active and (row["state"] != "pending" or row["expires_at"] <= time.time()):
            raise UploadError("上传会话已结束或过期", 410)
        return row

    def get(self, task_id, token):
        with lock_backend.resource(self.store, "upload:" + task_id, timeout=None):
            with self.store.connect() as db:
                row = self._row(db, task_id, token)
            if row["transport"] == "s3" and row["state"] == "pending" and row["expires_at"] > time.time():
                # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
                from shared.s3_multipart import S3Multipart

                S3Multipart(self.store).parts(row)
            with self.store.connect() as db:
                parts = db.execute(
                    "SELECT number,size,sha256,etag FROM upload_parts WHERE session_id=? ORDER BY number",
                    (task_id,),
                ).fetchall()
        return {
            k: row[k]
            for k in (
                "id",
                "filename",
                "size",
                "sha256",
                "state",
                "expires_at",
                "part_size",
                "transport",
                "completion_phase",
                "completion_error",
            )
        } | {
            "parts": [dict(p) for p in parts],
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def sign_part(self, task_id, token, number):
        # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
        from shared.s3_multipart import S3Multipart

        with lock_backend.resource(self.store, "upload:" + task_id, timeout=None):
            with self.store.connect() as db:
                row = self._row(db, task_id, token, active=True)
            if row["transport"] != "s3":
                raise UploadError("该会话使用 API 分片中转", 422)
            expires = min(self.config.storage.s3.presign_expire_seconds, int(row["expires_at"] - time.time()))
            if expires < 1:
                raise UploadError("上传会话已过期", 410)
            return S3Multipart(self.store).sign(row, number, expires)

    def register_part(self, task_id, token, number, etag):
        # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
        from shared.s3_multipart import S3Multipart

        with lock_backend.resource(self.store, "upload:" + task_id, timeout=None):
            with self.store.connect() as db:
                row = self._row(db, task_id, token, active=True)
            if row["transport"] != "s3":
                raise UploadError("该会话使用 API 分片中转", 422)
            actual = next(
                (part for part in S3Multipart(self.store).parts(row) if part["number"] == number), None
            )
            if actual is None or actual["etag"] != etag:
                raise UploadError("分片回执与对象存储当前状态不符", 409)
            return actual

    def part(self, task_id, token, number, source, size, sha256):
        with lock_backend.resource(self.store, "upload:" + task_id, timeout=None):
            with self.store.connect() as db:
                row = self._row(db, task_id, token, active=True)
            if row["transport"] != "api":
                raise UploadError("该会话使用 S3 直传，请申请分片签名", 422)
            expected = min(row["part_size"], row["size"] - (number - 1) * row["part_size"])
            if number < 1 or expected <= 0 or size != expected or not valid_digest(sha256):
                raise UploadError("分片编号、大小或摘要无效", 422)
            key = f"pending/uploads/{task_id}/{number}/{sha256}"
            self.store.media.put_immutable(key, source, sha256=sha256)
            with self.store.connect() as db:
                self._row(db, task_id, token, active=True)
                old = db.execute(
                    "SELECT sha256 FROM upload_parts WHERE session_id=? AND number=?", (task_id, number)
                ).fetchone()
                if old and old[0] != sha256:
                    raise UploadError("同一分片已登记不同内容")
                db.execute(
                    "INSERT OR IGNORE INTO upload_parts(session_id,number,size,sha256,object_key) "
                    "VALUES(?,?,?,?,?)",
                    (task_id, number, size, sha256, key),
                )
        return {"number": number, "sha256": sha256, "size": size}

    def complete(self, task_id, token):
        with lock_backend.resource(self.store, "upload:" + task_id, timeout=None):
            with self.store.connect() as db:
                row = self._row(db, task_id, token)
            if row["state"] == "complete":
                return json.loads(row["result"])
            if row["state"] not in {"pending", "completing"} or row["expires_at"] <= time.time():
                raise UploadError("上传会话已结束或过期", 410)
            if row["transport"] == "s3" and row["state"] == "pending":
                # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
                from shared.s3_multipart import S3Multipart

                S3Multipart(self.store).parts(row)
            parts = self.store.rows(
                "SELECT * FROM upload_parts WHERE session_id=? ORDER BY number", (task_id,)
            )
            count = (row["size"] + row["part_size"] - 1) // row["part_size"]
            if [p["number"] for p in parts] != list(range(1, count + 1)):
                raise UploadError("上传分片尚未齐全")
            self.store.execute(
                "UPDATE upload_sessions SET state='completing',completion_phase='assembling',"
                "completion_error=NULL WHERE id=?",
                (task_id,),
            )
            key = f"uploads/{task_id}/{row['sha256']}"
            try:
                with ExitStack() as stack:
                    if row["transport"] == "s3":
                        # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
                        from shared.s3_multipart import S3Multipart

                        pending = S3Multipart(self.store).complete(row, parts)
                        self.store.execute(
                            "UPDATE upload_sessions SET completion_phase='verifying' WHERE id=?", (task_id,)
                        )
                        source = stack.enter_context(self.store.media.open_read(pending))
                    else:
                        source = _Parts(self.store.media, parts, stack)
                    # 两条路径都流式复算全文件 SHA256，ETag 只作分片回执。
                    obj = self.store.media.put_immutable(key, source, sha256=row["sha256"])
                if obj.size != row["size"]:
                    raise UploadError("合并后的文件大小不符", 422)
            except ValueError as exc:
                self.store.execute(
                    "UPDATE upload_sessions SET completion_error=? WHERE id=?", (str(exc), task_id)
                )
                raise UploadError(str(exc), 422) from exc
            result = {
                "asset_id": task_id,
                "filename": row["filename"],
                "sha256": row["sha256"],
                "size": row["size"],
                "object_key": key,
                "url": f"/api/uploads/{task_id}/content",
            }
            with self.store.connect() as db:
                current = self._row(db, task_id, token)
                if current["state"] != "completing":
                    raise UploadError("上传完成状态已变化")
                db.execute(
                    "INSERT INTO uploaded_assets(id,filename,size,sha256,object_key) VALUES(?,?,?,?,?)",
                    (task_id, row["filename"], row["size"], row["sha256"], key),
                )
                db.execute(
                    "UPDATE upload_sessions SET state='complete',object_key=?,result=?,"
                    "completion_phase='published',completion_error=NULL WHERE id=?",
                    (key, dumps(result), task_id),
                )
            return result

    def abort(self, task_id, token):
        with lock_backend.resource(self.store, "upload:" + task_id, timeout=None):
            with self.store.connect() as db:
                row = self._row(db, task_id, token)
                if row["state"] == "complete":
                    raise UploadError("已完成的上传不能取消")
                db.execute("UPDATE upload_sessions SET state='aborted' WHERE id=?", (task_id,))
            self._clean_parts(task_id)
        return {"state": "aborted"}

    def _clean_parts(self, task_id):
        row = self.store.one("SELECT * FROM upload_sessions WHERE id=?", (task_id,))
        if row and row["transport"] == "s3" and getattr(self.store.media, "backend", "local") == "s3":
            # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
            from shared.s3_multipart import S3Multipart

            S3Multipart(self.store).abort(row)
        # 仅处理本会话 pending 前缀；中断后再次执行仍能清理孤分片。
        for key in self.store.media.iter_prefix(f"pending/uploads/{task_id}/"):
            if hasattr(self.store.media, "delete_pending"):
                self.store.media.delete_pending(key, task_id)
            else:
                self.store.media.delete_version(key)
        self.store.execute("DELETE FROM upload_parts WHERE session_id=?", (task_id,))

    def reap(self):

        ObjectCoordinator(self.store).prune()
        ids = self.store.rows(
            "SELECT id FROM upload_sessions WHERE expires_at<=? "
            "AND state IN ('pending','completing','expired','aborted','complete')",
            (time.time(),),
        )
        for row in ids:
            task_id = row["id"]
            with lock_backend.resource(self.store, "upload:" + task_id, timeout=None):
                self.store.execute(
                    "UPDATE upload_sessions SET state='expired' WHERE id=? AND state IN "
                    "('pending','completing') "
                    "AND expires_at<=?",
                    (task_id, time.time()),
                )
                self._clean_parts(task_id)
        return len(ids)


class _Parts(io.RawIOBase):
    def __init__(self, media, parts, stack):
        self.media, self.parts, self.stack = media, iter(parts), stack
        self.source = None
        self.digest, self.size, self.part = None, 0, None

    def readable(self):
        return True

    def read(self, size=-1):
        if size < 0:
            raise ValueError("上传合并必须使用有界流式读取")
        output = bytearray()
        while len(output) < size:
            if self.source is None:
                self.part = next(self.parts, None)
                if self.part is None:
                    break
                self.source = self.stack.enter_context(self.media.open_read(self.part["object_key"]))
                self.digest, self.size = hashlib.sha256(), 0
            chunk = self.source.read(size - len(output))
            if chunk:
                self.digest.update(chunk)
                self.size += len(chunk)
                output.extend(chunk)
            else:
                self.source.close()
                if self.size != self.part["size"] or self.digest.hexdigest() != self.part["sha256"]:
                    raise UploadError("上传分片已损坏，请重新上传", 422)
                self.source = None
        return bytes(output)


def adopt_path(store, config, path, filename):
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    sessions = UploadSessions(store, config)
    made = sessions.create(filename, path.stat().st_size, digest)
    task_id, token = made["id"], made["upload_token"]
    try:
        with path.open("rb") as source:
            number = 0
            while chunk := source.read(made["part_size"]):
                number += 1
                sessions.part(
                    task_id, token, number, io.BytesIO(chunk), len(chunk), hashlib.sha256(chunk).hexdigest()
                )
        return sessions.complete(task_id, token)
    except BaseException:
        sessions.abort(task_id, token)
        raise
