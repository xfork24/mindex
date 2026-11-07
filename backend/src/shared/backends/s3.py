"""S3 兼容对象存储的 MediaStore 实现（plans §8 阶段 4B）。

- key 沿用相对 POSIX 路径 + 可选 bucket prefix（建议 ``libraries/<library_id>/``）。
  ``version`` 命名为 ``<prefix>versions/<version>/<key>``（不可变内容版本；
  不依赖桶级 Object Versioning——S3 兼容产品该能力需逐项验证，plans §8）。
- ``put_immutable``：流式上传（boto3 传输管理器，超过阈值自动 multipart），内容
  sha256 写入对象元数据；已存在且摘要一致 → 幂等，不一致 → 键冲突。
- ``materialize``：下载到节点 staging 缓存（内容寻址文件名、先 ``.partial`` 后
  rename、按元数据 sha256 校验），上下文期内 pin（与远端删除 lease 是两种保护）。
- ``presign_get``：短期读 URL（可转交能力，过期前无法靠登出撤销；签发方负责
  下载保留记录——阶段 5 的 GC pin 统一登记）。
- 凭据只从环境变量读取；ETag 不是内容 SHA256（multipart ETag 尤其如此），
  ``MediaObject`` 严格区分两者。
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import tempfile
import weakref
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

from filelock import FileLock

from shared.backends.media import MediaObject, MediaSource
from shared.resource_registry import referenced

log = logging.getLogger(__name__)

__all__ = ["S3MediaStore"]

_READ_CHUNK = 1024 * 1024
# 本地缓存的文件名按 key+version 内容寻址，避免不同 key 互相覆盖。
_MULTIPART_THRESHOLD = 8 * 1024 * 1024


class S3MediaStore:
    backend = "s3"
    coordinator = None

    def __init__(
        self, section, *, library_id: str | None = None, staging=None, cache_dir: Path | None = None
    ):
        # 延迟导入：独立模型或扩展功能使用可选依赖，仅在实际调用时加载。
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config

        access = os.environ.get(section.access_key_id_env, "").strip()
        secret = os.environ.get(section.secret_access_key_env, "").strip()
        if not access or not secret:
            raise RuntimeError(
                f"storage.backend=s3 需要环境变量 {section.access_key_id_env} 与 "
                f"{section.secret_access_key_env}（凭据只从环境变量读取）"
            )
        self.section = section
        self.bucket = section.bucket
        self.prefix = (section.prefix or "").strip("/")
        self.library_id = library_id
        if library_id is not None:
            identity = UUID(library_id).hex
            self.prefix = "/".join(filter(None, (self.prefix, "libraries", identity)))
        self.staging = staging
        self.cache_dir = Path(cache_dir or (staging.root if staging else Path("data/cache/s3")))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = boto3.client(
            "s3",
            endpoint_url=section.endpoint_url,
            aws_access_key_id=access,
            aws_secret_access_key=secret,
            region_name=section.region or None,
            config=Config(
                signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}
            ),
        )
        self.browser_client = self.client
        if section.browser_endpoint_url and section.browser_endpoint_url != section.endpoint_url:
            self.browser_client = boto3.client(
                "s3",
                endpoint_url=section.browser_endpoint_url,
                aws_access_key_id=access,
                aws_secret_access_key=secret,
                region_name=section.region or None,
                config=Config(
                    signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}
                ),
            )
        clients = [self.client] if self.browser_client is self.client else [self.client, self.browser_client]
        self._finalizer = weakref.finalize(self, lambda: [client.close() for client in clients])
        self._transfer = TransferConfig(
            multipart_threshold=_MULTIPART_THRESHOLD, multipart_chunksize=_MULTIPART_THRESHOLD
        )

    def close(self):
        """释放本上下文的 HTTP 连接。"""
        self._finalizer()

    # ------------------------------------------------------------ key 约定
    def _object_key(self, key: str, version: str | None) -> str:
        body = f"{self.prefix}/" if self.prefix else ""
        if version is None:
            return body + key
        return f"{body}versions/{version}/{key}"

    def _cache_path(self, key: str, version: str | None) -> Path:
        digest = hashlib.sha256(
            repr((self.section.endpoint_url, self.bucket, self.prefix, key, version)).encode()
        ).hexdigest()
        return self.cache_dir / f"{digest}.obj"

    # ------------------------------------------------------------ 协议
    def _head(self, key: str, version: str | None):
        try:
            return self.client.head_object(Bucket=self.bucket, Key=self._object_key(key, version))
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise

    def stat(self, key: str, *, version: str | None = None, with_sha256: bool = False) -> MediaObject:
        head = self._head(key, version)
        sha256 = _meta(head.get("Metadata", {}), "sha256")
        return MediaObject(
            key=key,
            size=int(head["ContentLength"]),
            backend_version=version,
            etag=head.get("ETag", "").strip('"'),
            sha256=sha256 or None,
            version_id=head.get("VersionId"),
        )

    def open_read(self, key: str, *, version: str | None = None):
        pin = self.coordinator.retained(key, kind="read") if self.coordinator is not None else nullcontext()
        pin.__enter__()
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key, version))
        except self.client.exceptions.ClientError as exc:
            pin.__exit__(None, None, None)
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                raise FileNotFoundError(key) from exc
            raise
        except BaseException:
            pin.__exit__(None, None, None)
            raise
        return _Body(response["Body"], pin=pin)

    @contextmanager
    def materialize(self, key: str, *, version: str | None = None) -> Iterator[Path]:
        with self.coordinator.retained(key) if self.coordinator is not None else nullcontext():
            with self._materialize(key, version=version) as path:
                yield path

    @contextmanager
    def _materialize(self, key: str, *, version: str | None = None) -> Iterator[Path]:
        """下载到节点缓存并 pin；使用期内不被 LRU 淘汰，摘要不符立即失败。"""

        path = self._cache_path(key, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.with_name(f".{path.name}.download.lock"))):
            obj = self.stat(key, version=version)
            reserved = 0
            partial = path.with_name(f".{path.name}.partial")
            if self.staging is not None:
                self.staging.pin(path)
            try:
                if path.is_file() and (
                    path.stat().st_size != obj.size or (obj.sha256 and _file_digest(path) != obj.sha256)
                ):
                    path.unlink()
                if not path.is_file():
                    if self.staging is not None:
                        self.staging.reserve(obj.size, purpose=f"物化 {key}", path=partial)
                        reserved = obj.size
                    try:
                        args = {"Bucket": self.bucket, "Key": self._object_key(key, version)}
                        if obj.version_id:
                            args["VersionId"] = obj.version_id
                        elif obj.etag:
                            args["IfMatch"] = obj.etag
                        body = self.client.get_object(**args)["Body"]
                        try:
                            copied = 0
                            with partial.open("wb") as output:
                                while chunk := body.read(_READ_CHUNK):
                                    copied += len(chunk)
                                    if copied > obj.size:
                                        raise ValueError("物化对象超过已预约大小，停止下载")
                                    output.write(chunk)
                        finally:
                            body.close()
                        if partial.stat().st_size != obj.size or (
                            obj.sha256 and _file_digest(partial) != obj.sha256
                        ):
                            raise ValueError(f"对象 {key} 下载长度或摘要与元数据不一致")
                        with partial.open("rb") as handle:
                            os.fsync(handle.fileno())
                        if self.staging is not None:
                            self.staging.commit_path(partial, path, reserved)
                            reserved = 0
                        else:
                            os.replace(partial, path)
                    finally:
                        partial.unlink(missing_ok=True)
                if self.staging is not None:
                    self.staging.adopt(path)
            except BaseException:
                if self.staging is not None:
                    self.staging.unpin(path)
                raise
            finally:
                if reserved:
                    self.staging.release(reserved, path=partial)
        try:
            yield path
        finally:
            if self.staging is not None:
                self.staging.unpin(path)

    def put_immutable(
        self, key: str, source: MediaSource, *, sha256: str, version: str | None = None
    ) -> MediaObject:
        with self.coordinator.writing(key) if self.coordinator is not None else nullcontext():
            return self._put_immutable(key, source, sha256=sha256, version=version)

    def _put_immutable(
        self, key: str, source: MediaSource, *, sha256: str, version: str | None = None
    ) -> MediaObject:
        """服务端条件创建；摘要由实际输入复算，失败的 multipart 必须 abort。

        条件写入见 https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
        """
        object_key = self._object_key(key, version)
        # 流式暂存使非 seekable 输入也能校验与重试；不把大媒体装进内存。
        with self._staged_upload(source) as (staged, actual, size):
            if actual != sha256:
                raise ValueError(f"对象 {key} 的输入摘要与声明不一致")
            staged.seek(0)
            try:
                existing = self.stat(key, version=version)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing.sha256 != sha256 or existing.size != size:
                    raise ValueError(f"不可变键已存在且内容不同：{key}")
                return existing
            content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
            try:
                if size < _MULTIPART_THRESHOLD:
                    self.client.put_object(
                        Bucket=self.bucket,
                        Key=object_key,
                        Body=staged,
                        ContentLength=size,
                        Metadata={"sha256": sha256},
                        ContentType=content_type,
                        IfNoneMatch="*",
                    )
                else:
                    upload = self.client.create_multipart_upload(
                        Bucket=self.bucket,
                        Key=object_key,
                        Metadata={"sha256": sha256},
                        ContentType=content_type,
                    )["UploadId"]
                    try:
                        parts: list[dict] = []
                        # 不超过 S3 的 10000 part 上限，part 至少 8MiB。
                        part_size = max(_MULTIPART_THRESHOLD, (size + 9999) // 10000)
                        while chunk := staged.read(part_size):
                            number = len(parts) + 1
                            response = self.client.upload_part(
                                Bucket=self.bucket,
                                Key=object_key,
                                UploadId=upload,
                                PartNumber=number,
                                Body=chunk,
                            )
                            parts.append({"PartNumber": number, "ETag": response["ETag"]})
                        self.client.complete_multipart_upload(
                            Bucket=self.bucket,
                            Key=object_key,
                            UploadId=upload,
                            MultipartUpload={"Parts": parts},
                            IfNoneMatch="*",
                        )
                    except BaseException:
                        self.client.abort_multipart_upload(
                            Bucket=self.bucket, Key=object_key, UploadId=upload
                        )
                        raise
            except self.client.exceptions.ClientError as exc:
                if exc.response["Error"]["Code"] not in (
                    "PreconditionFailed",
                    "412",
                    "ConditionalRequestConflict",
                    "409",
                ):
                    raise
                existing = self.stat(key, version=version)
                if existing.sha256 != sha256 or existing.size != size:
                    raise ValueError(f"不可变键已存在且内容不同：{key}") from exc
            return self.stat(key, version=version)

    @contextmanager
    def _staged_upload(self, source):
        reserved = 0
        try:
            directory = self.staging.root if self.staging is not None else None
            with tempfile.TemporaryFile(dir=directory) as staged, _source_stream(source) as stream:
                digest, size = hashlib.sha256(), 0
                while chunk := stream.read(_READ_CHUNK):
                    if self.staging is not None:
                        self.staging.reserve(len(chunk), purpose="对象上传暂存")
                        reserved += len(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    staged.write(chunk)
                yield staged, digest.hexdigest(), size
        finally:
            if reserved:
                self.staging.release(reserved)

    def delete_version(self, key: str, *, version: str | None = None) -> None:
        """删除主对象（version=None）或某个不可变内容版本；精确到 key，不做前缀删除。"""
        if self.coordinator is not None and version is None:
            return self.coordinator.collect(self, key, grace=0)
        self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key, version))

    def delete_if_match(self, key, *, etag, size, version_id=None):
        if not self.verify_conditional_delete():
            raise RuntimeError("S3 后端未通过条件删除能力检查，已停止自动 GC；请查看 doctor")
        try:
            current = self.stat(key)
        except FileNotFoundError:
            return
        if current.etag != etag or current.size != size:
            raise RuntimeError("对象身份已经变化，拒绝删除")
        if getattr(self, "_conditional_delete_mode", None) == "version":
            if not version_id or current.version_id != version_id:
                raise RuntimeError("对象版本身份缺失或已经变化，拒绝删除")
            self.client.delete_object(
                Bucket=self.bucket, Key=self._object_key(key, None), VersionId=version_id
            )
        else:
            self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key, None), IfMatch=etag)

    def verify_conditional_delete(self):
        if hasattr(self, "_conditional_delete_verified"):
            return self._conditional_delete_verified

        key = self._object_key("pending/capability-probe/" + uuid4().hex, None)
        self._conditional_delete_verified = False
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=b"conditional-delete-probe")
            try:
                self.client.delete_object(
                    Bucket=self.bucket, Key=key, IfMatch="00000000000000000000000000000000"
                )
            except self.client.exceptions.ClientError as exc:
                if exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
                    head = self.client.head_object(Bucket=self.bucket, Key=key)
                    self.client.delete_object(Bucket=self.bucket, Key=key, IfMatch=head["ETag"])
                    self._conditional_delete_mode = "if-match"
                    self._conditional_delete_verified = True
                    return True
            # 只有既有 bucket 已开启版本化时，才核验精确版本删除；不自动修改 bucket。
            if self.client.get_bucket_versioning(Bucket=self.bucket).get("Status") != "Enabled":
                return False
            first = self.client.put_object(Bucket=self.bucket, Key=key, Body=b"version-one").get("VersionId")
            second = self.client.put_object(Bucket=self.bucket, Key=key, Body=b"version-two").get("VersionId")
            if not first or not second or first == second or first == "null":
                return False
            self.client.delete_object(Bucket=self.bucket, Key=key, VersionId=first)
            if self.client.head_object(Bucket=self.bucket, Key=key).get("VersionId") != second:
                return False
            self._conditional_delete_mode = "version"
            self._conditional_delete_verified = True
            return True
        except Exception:
            log.exception("S3 安全删除能力检查失败，停止自动 GC")
            return False
        finally:
            try:
                versions = self.client.list_object_versions(Bucket=self.bucket, Prefix=key)
                ids = [
                    {"Key": entry["Key"], "VersionId": entry["VersionId"]}
                    for entry in [*versions.get("Versions", []), *versions.get("DeleteMarkers", [])]
                    if entry["Key"] == key
                ]
                if ids:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": ids})
                else:
                    self.client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                log.exception("安全删除探针清理失败：%s", key)

    def delete_pending(self, key, session_id):
        if not key.startswith(f"pending/uploads/{session_id}/"):
            raise ValueError("只能清理该上传会话的 pending 对象")
        if self.coordinator is None:
            return self.delete_version(key)
        with self.coordinator.locked(key, deleting=True) as db:
            if referenced(self.coordinator.store, key, connection=db):
                raise RuntimeError("pending 对象仍被活动会话引用，拒绝清理")
            try:
                obj = self.stat(key)
            except FileNotFoundError:
                return
            # 私有 pending key 已确认不再被会话引用，清理不需要开放通用孤对象 GC。
            if self.verify_conditional_delete():
                self.delete_if_match(key, etag=obj.etag, size=obj.size, version_id=obj.version_id)
            else:
                self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key, None))
            db.execute("DELETE FROM resource_pins WHERE object_key=?", (key,))
            db.execute("UPDATE resource_objects SET state='deleted' WHERE object_key=?", (key,))

    def objects(self, prefix: str = "") -> dict[str, dict]:
        """列出对象元数据（key → size/last_modified），供 GC 宽限判断。"""
        body = f"{self.prefix}/" if self.prefix else ""
        found: dict[str, dict] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=body + prefix):
            for item in page.get("Contents", []):
                found[item["Key"][len(body) :]] = {
                    "size": int(item["Size"]),
                    "last_modified": item["LastModified"].timestamp(),
                }
        return found

    def iter_prefix(self, prefix: str) -> Iterator[str]:
        body = f"{self.prefix}/" if self.prefix else ""
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=body + prefix):
            for item in page.get("Contents", []):
                yield item["Key"][len(body) :]

    def presign_get(self, key: str, *, version: str | None = None, expires: int) -> str | None:
        if self.coordinator is not None:
            self.coordinator.download(key, ttl=expires + 300)
        return self.browser_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._object_key(key, version)},
            ExpiresIn=expires,
        )


class _Body:
    """get_object 响应体的最小 BinaryIO 适配（read/close/上下文）。"""

    def __init__(self, body, *, pin=None):
        self._body = body
        self._pin = pin

    def read(self, size: int = -1):
        return self._body.read(size if size and size > 0 else None)

    def close(self):
        try:
            self._body.close()
        finally:
            if self._pin is not None:
                pin, self._pin = self._pin, None
                pin.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@contextmanager
def _source_stream(source: MediaSource):
    if isinstance(source, (str, os.PathLike)):
        handle = Path(source).open("rb")
        try:
            yield handle
        finally:
            handle.close()
    else:
        yield source


def _meta(metadata: dict, name: str) -> str | None:
    """S3 元数据键大小写规范化（HTTP 头不区分大小写，服务端可能返回 Title-Case）。"""
    lowered = {key.lower(): value for key, value in metadata.items()}
    return lowered.get(name)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def endpoint_host(url: str) -> str:
    return urlparse(url).hostname or url
