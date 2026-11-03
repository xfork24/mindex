"""运行后端上下文：一个素材库的 db/media/locks/vectors/queue 集合（plans §5）。

集群调用链（API/worker）必须传递同一个 ``BackendContext``，禁止按路径偷偷发现
远端配置或全局 monkey patch。``library_id`` 是建库生成并在元数据库中校验的
UUID（settings 键 ``library.id``），锁与缓存按它隔离；不用路径 hash 当租户键
（plans §2）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from mindex.shared.backends.db import RelationalDatabase
from mindex.shared.backends.locks import FileLockProvider, LockProvider
from mindex.shared.backends.media import LocalMediaStore, MediaStore
from mindex.shared.backends.queue import InlineTaskQueue, TaskQueue
from mindex.shared.backends.vectors import LocalVectorStores, VectorStores
from mindex.shared.config import Config

__all__ = ["BackendContext", "open_context"]

LIBRARY_ID_KEY = "library.id"


@dataclass(frozen=True)
class BackendContext:
    """阶段 0 只有 local 形态：db 即 Store（metadata.sqlite3）、媒体为单机文件、
    锁为 data_dir 文件锁、向量为本地嵌入 chroma、队列为进程内立即执行。"""

    form: str  # local / pg-single / cluster（plans §4；阶段 0 恒为 local）
    library_id: str
    data_dir: Path  # local 为事实存储；cluster 仅节点缓存/临时目录
    db: RelationalDatabase
    media: MediaStore
    locks: LockProvider
    vectors: VectorStores
    queue: TaskQueue

    @property
    def store(self):
        """业务 Store 门面。local 形态下 db 即 Store；集群形态（阶段 1 起）由
        PostgreSQL 元数据库的统一 Store 承担，此入口不变。"""
        from mindex.shared.storage import Store

        if isinstance(self.db, Store):
            return self.db
        raise RuntimeError("该形态的业务 Store 由阶段 1 的 PostgreSQL 适配器提供")


def open_context(config: Config) -> BackendContext:
    """按配置构造运行后端。

    阶段 1 支持 local（缺省，零行为变化）与 pg-single（database.backend=postgres，
    单主机过渡测试形态，媒体/向量/锁仍为本地实现，禁止多机写入）。后续阶段的
    cluster 形态缺任何共享后端时在此抛出中文修复提示并启动失败，绝不静默落回
    本地 SQLite。
    """
    from mindex.shared.storage import PgStore, Store

    if config.database.backend == "postgres":
        store = PgStore(config)
        library_id = store.library_id
        form = "pg-single"
    else:
        store = Store(config.data_dir)
        library_id = _library_id(store)
        form = "local"
    if config.vector.backend == "chroma_server":
        if config.database.backend != "postgres":
            raise RuntimeError(
                "vector.backend=chroma_server 属共享向量后端，与 sqlite 单机形态的组合未定义；"
                "请将 database.backend 设为 postgres（plans §4 运行形态表）")
        from mindex.shared.backends.vectors import ChromaServerVectorStores

        vectors: VectorStores = ChromaServerVectorStores(config.vector.chroma_server)
    else:
        vectors = LocalVectorStores(store.root)
    return BackendContext(
        form=form,
        library_id=library_id,
        data_dir=store.root,
        db=store,
        media=LocalMediaStore(store.root),
        locks=FileLockProvider(store.root),
        vectors=vectors,
        queue=InlineTaskQueue(),
    )


def _library_id(store) -> str:
    """取或建库生成 library UUID；并发首次创建以先写者为准。"""
    row = store.one("SELECT value FROM settings WHERE key=?", (LIBRARY_ID_KEY,))
    if row:
        return row["value"]
    candidate = str(uuid.uuid4())
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT value FROM settings WHERE key=?", (LIBRARY_ID_KEY,)).fetchone()
        if existing:
            return existing[0]
        db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (LIBRARY_ID_KEY, candidate))
    return candidate
