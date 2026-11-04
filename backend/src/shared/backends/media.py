"""媒体存储协议与本地实现（plans/horizontal-scaling-plan.md §8）。

key 沿用相对 POSIX 路径（与 SQLite 里的 ``path`` 列一致）；protocol 不返回裸
``Path``——远端实现的 ``materialize`` 是「下载到节点缓存并 pin」的上下文管理器。
``MediaObject`` 区分 etag 与内容 SHA256：ETag 不是通用内容摘要，multipart 场景
两者无对应关系。

本地实现（``LocalMediaStore``）：单机文件系统即事实存储，无版本概念
（``version`` 只接受 ``None``），写入走「临时文件 + 校验 + fsync + rename」，
与 shared/jsonio.py 的原子写约定一致。S3 实现（阶段 4B）在此协议上增加
multipart、版本与预签名。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager as ContextManager
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO, Protocol

__all__ = ["LocalMediaStore", "MediaObject", "MediaStore"]

MediaSource = str | os.PathLike | IO[bytes]
_WRITE_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class MediaObject:
    """一次媒体读写的后端元数据。``sha256`` 在本地实现按需计算（stat 默认不算）。"""

    key: str
    size: int
    backend_version: str | None
    etag: str | None
    sha256: str | None = None


class MediaStore(Protocol):
    def stat(self, key: str, *, version: str | None = None, with_sha256: bool = False) -> MediaObject: ...

    def open_read(self, key: str, *, version: str | None = None) -> BinaryIO: ...

    def materialize(self, key: str, *, version: str | None = None) -> ContextManager[Path]: ...

    def put_immutable(self, key: str, source: MediaSource, *, sha256: str) -> MediaObject: ...

    def delete_version(self, key: str, *, version: str | None = None) -> None: ...

    def iter_prefix(self, prefix: str) -> Iterator[str]: ...

    def presign_get(self, key: str, *, version: str | None = None, expires: int) -> str | None: ...


class LocalMediaStore:
    """单机文件系统实现：``root`` 即事实存储，key 为相对 POSIX 路径。

    阶段 0 的本地原型；阶段 4A 在此之上叠加 staging 预约、活动 pin 与 LRU，
    业务路径（ShotMedia、uploads、studio 工作文件）分批接入。
    """

    backend = "local"

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, *, version: str | None = None) -> Path:
        if version is not None:
            raise FileNotFoundError(f"本地媒体存储没有版本概念：{key}@{version}")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise FileNotFoundError(key)
        return path

    def stat(self, key: str, *, version: str | None = None, with_sha256: bool = False) -> MediaObject:
        path = self._path(key, version=version)
        if not path.is_file():
            raise FileNotFoundError(key)
        return MediaObject(
            key=key,
            size=path.stat().st_size,
            backend_version=None,
            etag=None,
            sha256=_file_sha256(path) if with_sha256 else None,
        )

    def open_read(self, key: str, *, version: str | None = None) -> BinaryIO:
        return self._path(key, version=version).open("rb")

    @contextmanager
    def materialize(self, key: str, *, version: str | None = None) -> Iterator[Path]:
        """本地文件已可用；上下文保留给远端实现（下载 pin / 缓存租约）。"""
        yield self._path(key, version=version)

    def put_immutable(self, key: str, source: MediaSource, *, sha256: str) -> MediaObject:
        """写入不可变对象：临时文件 → 流式复制并算摘要 → 校验 → fsync → rename。

        目标已存在且摘要一致时幂等；同 key 不同内容视为键冲突（不可变键语义）。
        """
        target = self._path(key)
        if target.is_file():
            existing = _file_sha256(target)
            if existing == sha256:  # 幂等：同 key 同内容重复写为 no-op
                return MediaObject(key=key, size=target.stat().st_size, backend_version=None,
                                   etag=None, sha256=sha256)
            raise ValueError(f"不可变键已存在且内容不同：{key}")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        temp = target.with_name(f".{target.name}.{os.urandom(6).hex()}.partial")
        try:
            with _open_source(source) as origin, temp.open("wb") as sink:
                while chunk := origin.read(_WRITE_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                    sink.write(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            if digest.hexdigest() != sha256:
                raise ValueError(f"内容摘要与声明的 sha256 不一致：{key}")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return MediaObject(key=key, size=size, backend_version=None, etag=None, sha256=sha256)

    def delete_version(self, key: str, *, version: str | None = None) -> None:
        self._path(key, version=version).unlink(missing_ok=True)

    def iter_prefix(self, prefix: str) -> Iterator[str]:
        base = (self.root / prefix).resolve()
        if not base.is_relative_to(self.root):
            return
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file() and not path.name.startswith("."):
                    yield path.relative_to(self.root).as_posix()
        elif base.is_file():
            yield base.relative_to(self.root).as_posix()

    def presign_get(self, key: str, *, version: str | None = None, expires: int) -> str | None:
        """本地没有可转交的读 URL；调用方直接读文件或走现有 HTTP 出口。"""
        return None


def _open_source(source: MediaSource) -> IO[bytes]:
    if isinstance(source, (str, os.PathLike)):
        return Path(source).open("rb")
    return source


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_WRITE_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
