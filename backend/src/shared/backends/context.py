"""运行后端上下文：一个素材库的 db/media/locks/vectors/queue 集合（plans §5）。

集群调用链（API/worker）必须传递同一个 ``BackendContext``，禁止按路径偷偷发现
远端配置或全局 monkey patch。``library_id`` 是建库生成并在元数据库中校验的
UUID（settings 键 ``library.id``），锁与缓存按它隔离；不用路径 hash 当租户键
（plans §2）。
"""

from __future__ import annotations

import threading
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from mindex.shared.backends.db import RelationalDatabase
from mindex.shared.backends.locks import FileLockProvider, LockProvider
from mindex.shared.backends.media import LocalMediaStore, MediaStore
from mindex.shared.backends.queue import InlineTaskQueue, TaskQueue
from mindex.shared.backends.vectors import LocalVectorStores, VectorStores
from mindex.shared.config import Config
from mindex.shared.db.pg_backend import PgBackend

__all__ = ["BackendContext", "open_context"]

LIBRARY_ID_KEY = "library.id"
_RESOURCE_SCOPE: ContextVar[ExitStack | None] = ContextVar("backend_resource_scope", default=None)


@contextmanager
def resource_scope(*, close_on_success=True):
    """CLI/诊断的调用级资源边界；异常退出也关闭该调用打开的上下文。"""
    with ExitStack() as stack:
        token = _RESOURCE_SCOPE.set(stack)
        try:
            yield
            if not close_on_success:
                stack.pop_all()
        finally:
            _RESOURCE_SCOPE.reset(token)


@dataclass(frozen=True)
class BackendContext:
    """统一库身份与节点后端；local、pg-single、cluster 共用业务 Store 门面。"""

    form: str  # local / pg-single / cluster
    library_id: str
    data_dir: Path  # local 为事实存储；cluster 仅节点缓存/临时目录
    db: RelationalDatabase
    media: MediaStore
    locks: LockProvider
    vectors: VectorStores
    queue: TaskQueue | PgQueueAdapter
    staging: object | None = None  # StagingArea；未配置预算时为 None

    _resources: ExitStack = field(default_factory=ExitStack, repr=False, compare=False)
    _closed: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def close(self):
        """幂等释放本上下文资源；不关闭其他上下文持有的共享连接池。"""
        if not self.closed:
            self._closed.set()
            self._resources.close()

    def __enter__(self):
        if self.closed:
            raise RuntimeError("后端上下文已关闭")
        return self

    def __exit__(self, *_):
        self.close()

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
    单主机过渡测试形态）。阶段 5 起组合完整共享后端（postgres + chroma_server +
    s3 + postgres 队列 + postgres 锁）时形态为 **cluster**；任一后端组合缺共享
    能力则保持过渡形态并禁止多副本。形态缺共享后端时抛中文修复提示，绝不静默
    落回本地 SQLite。
    """
    resources = ExitStack()
    try:
        context = _open_context(config, resources)
        # 借出任一组件都保留上下文；自动回收由各资源自身的 finalizer 负责，
        # 不能用捕获组件的 context finalizer（会形成全局强引用环）。
        for component in (context.store, context.store.backend, context.media, context.vectors,
                          context.locks, context.queue, context.staging):
            if component is not None:
                cast(Any, component)._backend_context = context
        scope = _RESOURCE_SCOPE.get()
        if scope is not None:
            scope.callback(context.close)
        return context
    except BaseException:
        resources.close()
        raise


def _open_context(config: Config, resources: ExitStack) -> BackendContext:
    from mindex.shared.storage import PgStore, Store

    form = deployment_form(config)
    store: Store
    if config.database.backend == "postgres":
        pg_store = PgStore(config)
        store = pg_store
        resources.callback(cast(PgBackend, pg_store.backend).close)
        library_id = pg_store.library_id
    else:
        store = Store(config.data_dir)
        library_id = _library_id(store)
    if config.server.library_id is not None and config.server.library_id != library_id:
        raise RuntimeError("素材库 library_id 与 server.library_id 不一致；拒绝连接其他素材库")
    store.library_id = library_id
    if form != "local":
        cast(PgBackend, store.backend).enforce_identity = True
        cast(PgBackend, store.backend).object_coordination = True
        from mindex.shared.admission import validate_protocol

        validate_protocol(store)
    if config.vector.backend == "chroma_server":
        if config.database.backend != "postgres":
            raise RuntimeError(
                "vector.backend=chroma_server 属共享向量后端，与 sqlite 单机形态的组合未定义；"
                "请将 database.backend 设为 postgres（plans §4 运行形态表）")
        from mindex.shared.backends.vectors import ChromaServerVectorStores

        vectors: VectorStores = ChromaServerVectorStores(config.vector.chroma_server, library_id=library_id)
        resources.callback(cast(ChromaServerVectorStores, vectors).close)
    else:
        vectors = LocalVectorStores(store.root)
    store.locks_backend = config.locks.backend  # 供只拿 Store 的调用点选择锁后端
    if config.queue.backend == "postgres":
        if config.database.backend != "postgres":
            raise RuntimeError(
                "queue.backend=postgres 属共享队列，与 sqlite 单机形态的组合未定义；"
                "请将 database.backend 设为 postgres，并用 mindex worker 消费（plans §4）")
        # 队列实现按需绑定 PG 连接（shared/db/queue_pg.PgTaskQueue），上下文只登记形态。

        queue: TaskQueue | PgQueueAdapter = PgQueueAdapter(library_id)
    else:
        queue = InlineTaskQueue()
    if config.locks.backend == "postgres":
        if config.database.backend != "postgres":
            raise RuntimeError(
                "locks.backend=postgres 属共享租约锁，与 sqlite 单机形态的组合未定义；"
                "请将 database.backend 设为 postgres（plans §4 运行形态表）")
        from mindex.shared.db.locks_pg import PgLockProvider

        locks: LockProvider = PgLockProvider(cast(PgBackend, store.backend), library_id=library_id)
    else:
        locks = FileLockProvider(store.root)
    media: MediaStore
    if config.storage.backend == "s3":
        if config.database.backend != "postgres":
            raise RuntimeError(
                "storage.backend=s3 属共享媒体后端，与 sqlite 单机形态的组合未定义；"
                "请将 database.backend 设为 postgres（plans §4；媒体出口与检查点迁移"
                "随阶段 5 集群装配交付，当前仅单节点验证）")
        from mindex.shared.backends.s3 import S3MediaStore

        staging = _staging(config, store.root)
        if staging is not None:
            resources.callback(staging.close)
        media = S3MediaStore(
            config.storage.s3,
            library_id=library_id,
            staging=staging,
            cache_dir=staging.root / "libraries" / library_id / "s3")
        resources.callback(media.close)
        if (config.vector.backend == "chroma_server" and config.queue.backend == "postgres"
                and config.locks.backend == "postgres"):
            form = "cluster"  # 共享后端齐备（plans §4；准入与 runbook 见 docs/scaling.md）
    else:
        staging = _staging(config, store.root)
        if staging is not None:
            resources.callback(staging.close)
        media = LocalMediaStore(store.root)
    from mindex.shared.config import fingerprint

    store.deployment_form = form
    store.backend_identity = fingerprint({
        "database": config.database.backend,
        "vector": config.vector.model_dump(), "storage": config.storage.model_dump(),
    }) if form != "local" else ""
    store.deployment_config = config
    store.media = media  # Store.resolve_media/media_available 按存储后端解析
    store.vectors = vectors
    if config.storage.backend == "s3":
        from mindex.shared.object_coordination import ObjectCoordinator

        cast(Any, media).coordinator = ObjectCoordinator(store)
    return BackendContext(
        form=form,
        library_id=library_id,
        data_dir=store.root,
        db=store,
        media=media,
        locks=locks,
        vectors=vectors,
        queue=queue,
        staging=staging,
        _resources=resources,
    )


def deployment_form(config: Config) -> str:
    """打开连接或创建目录前检查部署组合；显式 cluster 不允许降级。"""
    shared = {
        "database": config.database.backend == "postgres",
        "vector": config.vector.backend == "chroma_server",
        "storage": config.storage.backend == "s3",
        "queue": config.queue.backend == "postgres",
        "locks": config.locks.backend == "postgres",
    }
    if not shared["database"] and any(shared.values()):
        names = "、".join(name for name, enabled in shared.items() if enabled)
        raise RuntimeError(f"共享后端 {names} 需要 database.backend=postgres；与 sqlite 单机形态的组合未定义")
    actual = "cluster" if all(shared.values()) else "pg-single" if shared["database"] else "local"
    requested = config.server.form
    if requested != "auto" and requested != actual:
        missing = "、".join(name for name, enabled in shared.items() if not enabled)
        raise RuntimeError(f"server.form={requested} 与部署后端组合 {actual} 不一致；缺少共享后端：{missing}")
    return actual


def _staging(config: Config, root: Path):
    """按配置构建节点 staging 工作集（plans §8）：local 用 storage.staging_quota_gb
    （0 = 不限，保持零行为变化）；s3 形态固定启用（s3.staging_quota_gb/min_free_gb）。"""
    from mindex.shared.backends.staging import StagingArea

    if config.storage.backend == "s3":
        section = config.storage.s3
        quota, minimum = int(section.staging_quota_gb * 1024**3), int(section.min_free_gb * 1024**3)
    elif config.storage.staging_quota_gb > 0:
        quota, minimum = int(config.storage.staging_quota_gb * 1024**3), 0
    else:
        return None
    # 账本在文件系统中跨实例协调；每个上下文独占 owner，关闭互不影响。
    staging_root = root if config.storage.backend == "s3" else root / "cache" / "staging"
    return StagingArea(staging_root, quota_bytes=quota, min_free_bytes=minimum)


class PgQueueAdapter:
    """持久队列描述符。账本必须通过 ledger_transaction 原子入队，不能传入进程内 callable。

    claim/续期/完成由 worker 使用 PgTaskQueue 和显式连接执行。
    """

    backend = "postgres"

    def __init__(self, library_id: str):
        self.library_id = library_id


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
