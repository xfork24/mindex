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
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from mindex.shared.backends.media import MediaObject, MediaSource

log = logging.getLogger(__name__)

__all__ = ["S3MediaStore"]

_READ_CHUNK = 1024 * 1024
# 本地缓存的文件名按 key+version 内容寻址，避免不同 key 互相覆盖。
_MULTIPART_THRESHOLD = 8 * 1024 * 1024


class S3MediaStore:
    backend = "s3"

    def __init__(self, section, *, staging=None, cache_dir: Path | None = None):
        import boto3
        from boto3.s3.transfer import TransferConfig
        from botocore.config import Config

        access = os.environ.get(section.access_key_id_env, "").strip()
        secret = os.environ.get(section.secret_access_key_env, "").strip()
        if not access or not secret:
            raise RuntimeError(
                f"storage.backend=s3 需要环境变量 {section.access_key_id_env} 与 "
                f"{section.secret_access_key_env}（凭据只从环境变量读取）")
        self.section = section
        self.bucket = section.bucket
        self.prefix = (section.prefix or "").strip("/")
        self.staging = staging
        self.cache_dir = Path(cache_dir or (staging.root if staging else Path("data/cache/s3")))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = boto3.client(
            "s3", endpoint_url=section.endpoint_url,
            aws_access_key_id=access, aws_secret_access_key=secret,
            region_name=section.region or None,
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        self._transfer = TransferConfig(multipart_threshold=_MULTIPART_THRESHOLD,
                                        multipart_chunksize=_MULTIPART_THRESHOLD)

    # ------------------------------------------------------------ key 约定
    def _object_key(self, key: str, version: str | None) -> str:
        body = f"{self.prefix}/" if self.prefix else ""
        if version is None:
            return body + key
        return f"{body}versions/{version}/{key}"

    def _cache_path(self, key: str, version: str | None) -> Path:
        digest = hashlib.sha256(f"{key}@{version or '-'}".encode()).hexdigest()
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
        return MediaObject(key=key, size=int(head["ContentLength"]), backend_version=version,
                           etag=head.get("ETag", "").strip('"'), sha256=sha256 or None)

    def open_read(self, key: str, *, version: str | None = None):
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key, version))
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(key) from exc
            raise
        return _Body(response["Body"])

    @contextmanager
    def materialize(self, key: str, *, version: str | None = None) -> Iterator[Path]:
        """下载到节点缓存并 pin；使用期内不被 LRU 淘汰，摘要不符立即失败。"""
        path = self._cache_path(key, version)
        if not path.is_file():
            expected = self.stat(key, version=version).sha256
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f".{path.name}.partial")
            try:
                self.client.download_file(self.bucket, self._object_key(key, version), str(partial))
                if expected and _file_digest(partial) != expected:
                    raise ValueError(f"对象 {key} 下载摘要与元数据不一致（传输损坏）")
                os.replace(partial, path)
            finally:
                partial.unlink(missing_ok=True)
            if self.staging is not None:
                self.staging.adopt(path)
        if self.staging is not None:
            self.staging.pin(path)
        try:
            yield path
        finally:
            if self.staging is not None:
                self.staging.unpin(path)

    def put_immutable(self, key: str, source: MediaSource, *, sha256: str,
                       version: str | None = None) -> MediaObject:
        object_key = self._object_key(key, version)
        try:
            existing = self.client.head_object(Bucket=self.bucket, Key=object_key)
            if _meta(existing.get("Metadata", {}), "sha256") == sha256:
                return MediaObject(key=key, size=int(existing["ContentLength"]),
                                   backend_version=None, etag=existing.get("ETag", "").strip('"'),
                                   sha256=sha256)
            raise ValueError(f"不可变键已存在且内容不同：{key}")
        except self.client.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "404 Not Found"):
                raise
        with _source_stream(source) as stream:
            self.client.upload_fileobj(
                stream, self.bucket, object_key, Config=self._transfer,
                ExtraArgs={"Metadata": {"sha256": sha256}})
        return MediaObject(key=key, size=None, backend_version=version, etag=None, sha256=sha256)

    def delete_version(self, key: str, *, version: str | None = None) -> None:
        """删除主对象（version=None）或某个不可变内容版本；精确到 key，不做前缀删除。"""
        self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key, version))

    def objects(self, prefix: str = "") -> dict[str, dict]:
        """列出对象元数据（key → size/last_modified），供 GC 宽限判断。"""
        body = f"{self.prefix}/" if self.prefix else ""
        found: dict[str, dict] = {}
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=body + prefix):
            for item in page.get("Contents", []):
                found[item["Key"][len(body):]] = {
                    "size": int(item["Size"]),
                    "last_modified": item["LastModified"].timestamp(),
                }
        return found

    def iter_prefix(self, prefix: str) -> Iterator[str]:
        body = f"{self.prefix}/" if self.prefix else ""
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=body + prefix):
            for item in page.get("Contents", []):
                yield item["Key"][len(body):]

    def presign_get(self, key: str, *, version: str | None = None, expires: int) -> str | None:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self._object_key(key, version)},
            ExpiresIn=expires)


class _Body:
    """get_object 响应体的最小 BinaryIO 适配（read/close/上下文）。"""

    def __init__(self, body):
        self._body = body

    def read(self, size: int = -1):
        return self._body.read(size if size and size > 0 else None)

    def close(self):
        self._body.close()

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
