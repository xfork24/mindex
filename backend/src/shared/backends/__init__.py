"""后端抽象：关系数据库、向量、媒体、锁与队列的协议与本地实现。

水平扩展方案（plans/horizontal-scaling-plan.md）阶段 0 引入。每个协议都有一个
``local`` 实现，保持现有 metadata.sqlite3、本地嵌入 chroma、单机文件系统、
data_dir 文件锁与进程内立即执行的行为零变化；分布式实现（postgres、
chroma_server、s3 等）在后续阶段加入，经 shared/config.py 的部署形态配置选择。
集群配置缺共享后端时 ``open_context`` 启动失败并列出中文修复提示，不静默落回
本地 SQLite。
"""

from mindex.shared.backends.context import BackendContext, open_context
from mindex.shared.backends.db import DatabaseConnection, RelationalDatabase
from mindex.shared.backends.locks import FileLockProvider, LockProvider
from mindex.shared.backends.media import LocalMediaStore, MediaObject, MediaStore
from mindex.shared.backends.queue import InlineTaskQueue, TaskQueue, TaskReceipt
from mindex.shared.backends.vectors import LocalVectorStore, LocalVectorStores, VectorStore, VectorStores

__all__ = [
    "BackendContext",
    "DatabaseConnection",
    "FileLockProvider",
    "InlineTaskQueue",
    "LocalMediaStore",
    "LocalVectorStore",
    "LocalVectorStores",
    "LockProvider",
    "MediaObject",
    "MediaStore",
    "RelationalDatabase",
    "TaskQueue",
    "TaskReceipt",
    "VectorStore",
    "VectorStores",
    "open_context",
]
