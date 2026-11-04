"""PG 一致性导出内核（plans §10 阶段 5）。

在 ``REPEATABLE READ READ ONLY`` 事务里按单一导出快照流式复制全部业务表到暂存
SQLite，并生成逐表清单（行数 + 行内容摘要）。媒体/向量对象由调用方按同一清单在
写 gate 解除前复制（完整 bundle 封装与迁移 CLI 的 preflight/verify/activate 随
后续提交）。运行表（lock_*/task_queue）不导出：集群运行态不属于业务备份。

用法::

    from mindex.maintenance.pg_export import export_pg_to_sqlite
    manifest = export_pg_to_sqlite(pg_store, target_path)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from mindex.shared.db.schema_pg import BASELINE_TABLES
from mindex.shared.storage import Store

if TYPE_CHECKING:
    from mindex.shared.storage import PgStore

log = logging.getLogger(__name__)

__all__ = ["export_pg_to_sqlite"]

_BATCH = 500
# rowid 即 id 的表（PG 侧无独立 rowid 列；按 id 排序即 SQLite 语义序）。
_ROWID_IS_ID = {"model_profiles", "voices", "runs"}


def export_pg_to_sqlite(pg_store: PgStore, target: Path, *, tables=BASELINE_TABLES) -> dict:
    """单一可重复读只读快照内导出全部业务表；返回逐表清单。

    目标必须是刚完成基线迁移的暂存 SQLite（本函数先清空再灌入，保证与快照一致）。
    """
    staging = Store(target)
    manifest: dict[str, dict] = {}
    total = 0
    pool = pg_store.backend.pool()
    connection = pool.getconn()
    try:
        connection.autocommit = False
        with connection.cursor() as cursor, staging.connect() as db:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            db.execute("PRAGMA foreign_keys=OFF")  # 按清单序装载；装载完由校验步骤整体核验
            for table in tables:  # 先清空目标，保证内容与快照一一对应
                db.execute(f"DELETE FROM {table}")
            for table in tables:
                if table in _ROWID_IS_ID:
                    cursor.execute(f'SELECT * FROM "{table}" ORDER BY id')
                else:  # rowid 列显式随行导出（SQLite 侧按同值回灌保序）
                    cursor.execute(f'SELECT rowid, * FROM "{table}" ORDER BY rowid')
                columns = tuple(column.name for column in cursor.description)
                names = ",".join(f'"{c}"' if c in ("end", "trigger", "start") else c for c in columns)
                placeholders = ",".join("?" * len(columns))
                insert = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
                table_digest = hashlib.sha256()
                count = 0
                while chunk := cursor.fetchmany(_BATCH):
                    rows = [tuple(row) for row in chunk]
                    db.executemany(insert, rows)
                    for row in rows:
                        table_digest.update(repr(row).encode())
                    count += len(rows)
                manifest[table] = {"rows": count, "sha256": table_digest.hexdigest()}
                total += count
            connection.rollback()  # 只读事务结束，快照释放
    finally:
        pool.putconn(connection)
    log.info("PG 一致性导出完成：%d 行 / %d 表 → %s", total, len(tables), target)
    return {"tables": manifest, "total_rows": total,
            "metadata": {"isolation": "repeatable-read-read-only", "dialect": "pg-export-v1"}}
