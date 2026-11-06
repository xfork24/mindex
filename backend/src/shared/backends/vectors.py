"""向量后端协议与本地实现（plans/horizontal-scaling-plan.md §6.2）。

一个 ``VectorStore`` 对应一个索引版本的向量集合，embedding_function=None：向量一律
由调用方显式提供，嵌入空间身份由元数据库的索引 spec 保证（「元数据库是业务事实
来源」不变式）。协议只提供查询原语（无过滤/带 where 的最近邻）；过采样、
eligible 过滤与逐片段去重等检索策略由检索层组合（阶段 2 改造），因此本模块不依赖
processing，shared 也不得反向导入上层包。

本地实现与 ``processing/indexing.py`` 的 ``VectorIndex`` 读写同一集合
（``<root>/chroma``、集合名 ``idx-<index_id>``、余弦空间、相同 metadata 形状），
元数据列 segment_id/source_id/granularity/view 保持一致。chroma_server 实现
（HttpClient）在阶段 2 引入。
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# chroma HNSW 段的默认落盘阈值：与 processing/indexing.VectorIndex 一致。
CHROMA_HNSW_SYNC_THRESHOLD = 1000
_PAGE = 1000

__all__ = ["LocalVectorStore", "LocalVectorStores", "VectorStore", "VectorStores"]

# (向量 id, 向量, 片段行, 视图, 文档)，与 VectorIndex.upsert_many 的条目一致。
VectorEntry = tuple[str, list[float], dict, str, str]


class VectorStore(Protocol):
    """一个索引版本的向量集合：批量写入/读取、分页导出、查询原语与删除。

    ``query`` 的 ``where`` 语义与 chroma 元数据过滤一致（view/segment_id 等），
    是阶段 2 服务端过滤的下推边界。``flush`` 是本地实现的持久化细节，不在协议内。
    """

    def count(self) -> int: ...

    def upsert_many(self, entries: Sequence[VectorEntry]) -> None: ...

    def vectors(self, identifiers: Sequence[str]) -> dict[str, list[float]]: ...

    def ids(self) -> set[str]: ...

    def query(self, vector: Sequence[float], *, count: int,
              where: dict | None = None) -> list[dict]: ...

    def delete(self, identifiers: Sequence[str]) -> None: ...


class VectorStores(Protocol):
    """向量后端工厂：按索引版本打开集合。集合名/namespace 绑定 library_id
    （阶段 2 起），保持已发布索引的向量身份不可变。"""

    def open(self, index_id: str, *, create: bool = False) -> VectorStore: ...


class LocalVectorStore:
    """本地实现：chromadb PersistentClient，集合 ``idx-<index_id>``，余弦空间。"""

    backend = "chroma_local"

    def __init__(self, collection, *, owner: ChromaServerVectorStores | None = None):
        self.collection = collection
        self._owner = owner

    def count(self) -> int:
        return self.collection.count()

    def upsert_many(self, entries: Sequence[VectorEntry]) -> None:
        if not entries:
            return
        self.collection.upsert(
            ids=[entry[0] for entry in entries],
            embeddings=[entry[1] for entry in entries],
            documents=[entry[4] or entry[3] for entry in entries],
            metadatas=[{"segment_id": segment["id"], "source_id": segment["source_id"],
                        "granularity": segment["kind"], "view": view}
                       for _, _, segment, view, _ in entries],
        )

    def vector(self, identifier: str) -> list[float] | None:
        """读单条向量；「元数据在、HNSW 无」的库内不一致视为缺失（与 VectorIndex 一致）。"""
        try:
            raw = self.collection.get(ids=[identifier], include=["embeddings"])
        except Exception as exc:
            if "Error finding id" not in str(exc):
                raise
            log.warning("向量 %s 在 HNSW 段缺失（元数据仍在）：视为不可复用，将重新嵌入", identifier)
            return None
        return raw["embeddings"][0].tolist() if raw["ids"] else None

    def vectors(self, identifiers: Sequence[str]) -> dict[str, list[float]]:
        try:
            raw = self.collection.get(ids=list(dict.fromkeys(identifiers)), include=["embeddings"])
        except Exception as exc:
            if "Error finding id" not in str(exc):
                raise
            return {identifier: vector for identifier in identifiers
                    if (vector := self.vector(identifier)) is not None}
        return {identifier: vector.tolist()
                for identifier, vector in zip(raw["ids"], raw["embeddings"])}

    def ids(self) -> set[str]:
        result: set[str] = set()
        offset = 0
        while True:
            batch = self.collection.get(limit=_PAGE, offset=offset, include=[])["ids"]
            result.update(batch)
            if len(batch) < _PAGE:
                return result
            offset += _PAGE

    def query(self, vector: Sequence[float], *, count: int,
              where: dict | None = None) -> list[dict]:
        raw = self.collection.query(
            query_embeddings=[list(vector)], n_results=count, where=where,
            include=["metadatas", "distances"],
        )
        return [
            {"id": identifier, "distance": distance, "metadata": metadata}
            for identifier, distance, metadata in zip(raw["ids"][0], raw["distances"][0], raw["metadatas"][0])
        ]

    def delete(self, identifiers: Sequence[str]) -> None:
        if identifiers:
            self.collection.delete(ids=list(identifiers))

    def flush(self) -> None:
        """幂等重写既有向量冲过 chroma HNSW 落盘阈值（本地实现细节，见 VectorIndex.flush）。"""
        if self.collection.count() == 0:
            return
        try:
            batch = self.collection.get(
                limit=CHROMA_HNSW_SYNC_THRESHOLD, include=["embeddings", "documents", "metadatas"]
            )
        except Exception as exc:  # noqa: BLE001 — 落盘尽力而为，缺 id 由 vector() 自愈兜底
            log.warning("索引落盘预读失败，跳过强制持久化：%s", exc)
            return
        ids = list(batch["ids"])
        embeddings = list(batch["embeddings"]) if batch["embeddings"] is not None else []
        if not ids or len(embeddings) != len(ids):
            return
        documents = list(batch["documents"]) if batch["documents"] is not None else [None] * len(ids)
        metadatas = list(batch["metadatas"]) if batch["metadatas"] is not None else [None] * len(ids)
        rounds = -(-CHROMA_HNSW_SYNC_THRESHOLD // len(ids))
        for _ in range(rounds):
            self.collection.upsert(
                ids=ids,
                embeddings=[embedding.tolist() for embedding in embeddings],
                documents=documents,
                metadatas=metadatas,
            )


class LocalVectorStores:
    """本地实现工厂：``<root>/chroma`` 下的持久集合，与 VectorIndex 互操作。"""

    backend = "chroma_local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def open(self, index_id: str, *, create: bool = False) -> LocalVectorStore:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=str(self.root / "chroma"), settings=Settings(anonymized_telemetry=False)
        )
        name = "idx-" + index_id
        if create:
            collection = client.get_or_create_collection(
                name, metadata={"hnsw:space": "cosine"}, embedding_function=None
            )
        else:
            collection = client.get_collection(name, embedding_function=None)
        return LocalVectorStore(collection)


_HTTP_CLIENTS: dict[tuple[str, str], object] = {}


def chroma_http_client(section, *, shared=True):
    """进程内按 (url, token) 共享的 Chroma HttpClient（阶段 2 的最小改动路径：
    独立服务 + 显式配置；集合读写与本地完全一致，检索过滤策略随后续阶段下推）。"""
    import os
    from urllib.parse import urlparse

    import chromadb
    from chromadb.config import Settings

    token = os.environ.get(section.token_env, "").strip()
    key = (section.url, token)
    client = _HTTP_CLIENTS.get(key) if shared else None
    if client is None:
        parsed = urlparse(section.url if "//" in section.url else f"http://{section.url}")
        headers = {"Authorization": f"Bearer {token}"} if token else None
        if shared:
            client = chromadb.HttpClient(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or (443 if parsed.scheme == "https" else 8000),
                ssl=parsed.scheme == "https",
                headers=headers,
                settings=Settings(anonymized_telemetry=False),
            )
        else:
            from mindex.shared.backends.chroma_client import ChromaClient

            settings = Settings(anonymized_telemetry=False, chroma_api_impl="chromadb.api.fastapi.FastAPI",
                chroma_server_host=parsed.hostname or "127.0.0.1",
                chroma_server_http_port=parsed.port or (443 if parsed.scheme == "https" else 8000),
                chroma_server_ssl_enabled=parsed.scheme == "https", chroma_server_headers=headers)
            client = ChromaClient(settings, section.request_timeout_seconds)
        if shared:
            _HTTP_CLIENTS[key] = client
    return client


class ChromaServerVectorStores:
    """共享集合按库 UUID 隔离；未绑定的工厂只供旧集合迁移工具使用。"""

    backend = "chroma_server"

    def __init__(self, section, *, library_id: str | None = None):
        self.section = section
        self._client = None
        self._closed = False
        self._client_finalizer = None
        if library_id is not None:
            from uuid import UUID

            library_id = str(UUID(library_id))
        self.library_id = library_id

    @property
    def client(self):
        if self._closed:
            raise RuntimeError("Chroma 后端已关闭")
        if self._client is None:
            self._client = chroma_http_client(self.section, shared=False)
            self._client_finalizer = weakref.finalize(self, self._client.close)
        return self._client

    def close(self):
        self._closed = True
        if self._client_finalizer is not None:
            self._client_finalizer()

    def as_client(self):
        if self.library_id is None:
            return self.client
        return LibraryVectorClient(self.client, self.library_id, owner=self)

    def open(self, index_id: str, *, create: bool = False):
        client = self.as_client()
        name = "idx-" + index_id
        if create:
            collection = client.get_or_create_collection(
                name, metadata={"hnsw:space": "cosine"}, embedding_function=None
            )
        else:
            collection = client.get_collection(name, embedding_function=None)
        return LocalVectorStore(collection, owner=self)


class LibraryVectorClient:
    """向量业务连接只暴露本库集合；原始 HttpClient 仍按 endpoint/token 复用。"""

    def __init__(self, client, library_id: str, *, owner=None):
        from uuid import UUID

        self.client = client
        self._owner = owner
        self.library_id = str(UUID(library_id))
        self.prefix = "lib-" + UUID(library_id).hex + "-"

    def get_collection(self, name: str, **kwargs):
        collection = self.client.get_collection(self.prefix + name, **kwargs)
        object.__setattr__(collection, "_mindex_owner", self)
        return collection

    def get_or_create_collection(self, name: str, **kwargs):
        collection = self.client.get_or_create_collection(self.prefix + name, **kwargs)
        object.__setattr__(collection, "_mindex_owner", self)
        return collection
