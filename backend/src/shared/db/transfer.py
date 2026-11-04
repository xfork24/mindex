"""SQLite ↔ PostgreSQL 的表级往返装载（plans §10 迁移 CLI 的内核，阶段 1 供验收）。

- 只处理**空目标**：按 ``BASELINE_TABLES`` 全表复制，不做行级合并/冲突跳过；
  正式切换 runbook（阶段 5）在此之上加 manifest、断点续跑与校验和。
- rowid 显式随行复制，装载后把 identity 序列推进到当前最大值（防主键回卷）。
- ``INTEGER PRIMARY KEY``（model_profiles/voices/runs）的 rowid 即 id，不另带列。
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from mindex.shared.db.schema_pg import BASELINE_TABLES

__all__ = ["copy_pg_to_sqlite", "copy_sqlite_to_pg"]

# rowid 即 id 的表（SQLite AUTOINCREMENT；PG 侧只有 id identity 列）。
_ROWID_IS_ID = {"model_profiles", "voices", "runs"}
_BATCH = 500


def _sqlite_rows(sqlite_store, table: str):
    rowid_expr = "id AS _rowid_" if table in _ROWID_IS_ID else "rowid AS _rowid_"
    return sqlite_store.rows(f"SELECT {rowid_expr},* FROM {table} ORDER BY _rowid_")


def _pg_rows(pg_store, table: str):
    """PG 侧显式列清单：``rowid AS _rowid_`` + 真实列（PG 的 ``*`` 会把 rowid 重复带出）。"""
    from mindex.shared.db.dialect import _baseline_columns

    columns = _baseline_columns().get(table, ())
    listed = [column for column in columns if column != "rowid"]
    names = ",".join(f'"{c}"' if c in ("end", "trigger", "start") else c for c in listed)
    rowid_expr = "id AS _rowid_" if table in _ROWID_IS_ID else "rowid AS _rowid_"
    return pg_store.rows(f"SELECT {rowid_expr},{names} FROM {table} ORDER BY _rowid_")


def copy_sqlite_to_pg(sqlite_store, pg_store, *, tables=BASELINE_TABLES, connection=None) -> dict[str, int]:
    """把 SQLite 全表复制到 PG 库；返回每表行数。

    目标应为刚完成基线迁移的库（delivery_platforms 已有种子里，先按子表→父表
    逆序清空再装载，保证以源库内容——含删除墓碑——为准）。
    """
    counts: dict[str, int] = {}
    with pg_store.connect() if connection is None else nullcontext(connection) as db:
        # 数据恢复不是合并；仅允许初始化种子，禁止清空已有业务数据。
        for table in tables:
            if table in {"settings", "delivery_platforms"}:
                continue
            if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError("迁移目标非空，拒绝覆盖已有数据")
        for table in reversed(tables):
            db.execute(f"DELETE FROM {table}")
        for table in tables:
            rows = _sqlite_rows(sqlite_store, table)
            if not rows:
                counts[table] = 0
                continue
            columns = [name for name in rows[0] if name != "_rowid_"]
            if table not in _ROWID_IS_ID:
                columns = ["rowid", *columns]
            placeholders = ",".join(["%s"] * len(columns))
            batch = [
                tuple(row["_rowid_"] if name == "rowid" else row[name] for name in columns)
                for row in rows
            ]
            names = ",".join(f'"{column}"' if column in ("end", "trigger", "start") else column
                             for column in columns)
            for offset in range(0, len(batch), _BATCH):
                db.executemany(
                    f'INSERT INTO {table} ({names}) VALUES ({placeholders})', batch[offset:offset + _BATCH])
            counts[table] = len(batch)
        from mindex.shared.db.migrate_pg import fix_sequences

        # 先把 identity 序列推进到导入最大值，再补库身份（否则新行会与显式
        # 导入的 rowid 撞车）。
        fix_sequences(db, list(tables))
        # 库身份：清空重灌会移除基线种子行；源库没有 library.id 时补建
        # （锁与缓存按它隔离，plans §2；正式切换应保持源库同一身份）。
        if db.execute("SELECT value FROM settings WHERE key='library.id'").fetchone() is None:
            import uuid

            db.execute("INSERT INTO settings(key,value) VALUES(?,?)",
                       ("library.id", str(uuid.uuid4())))
    return counts


def copy_pg_to_sqlite(pg_store, target: Path, *, tables=BASELINE_TABLES) -> dict[str, int]:
    """把 PG 全表复制到**新建** SQLite metadata 库（Store 构造完成基线迁移后清空再灌入）。"""
    from mindex.shared.storage import Store

    target_store = Store(target)
    counts: dict[str, int] = {}
    with target_store.connect() as db:
        db.execute("PRAGMA foreign_keys=OFF")  # 全量重灌不受外键顺序约束；装载完恢复
        for table in tables:
            db.execute(f"DELETE FROM {table}")
    for table in tables:
        rows = _pg_rows(pg_store, table)
        if not rows:
            counts[table] = 0
            continue
        columns = [name for name in rows[0] if name != "_rowid_"]
        if table not in _ROWID_IS_ID:
            columns = ["rowid", *columns]
        names = ",".join(columns)
        placeholders = ",".join(["?"] * len(columns))
        batch = [tuple(row["_rowid_"] if name == "rowid" else row[name] for name in columns)
                 for row in rows]
        with target_store.connect() as db:
            for offset in range(0, len(batch), _BATCH):
                db.executemany(f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                               batch[offset:offset + _BATCH])
        counts[table] = len(batch)
    return counts


def copy_sqlite_to_sqlite(origin, target, *, connection):
    """在目标事务中复制业务表，调用方可把恢复日志一起提交。

    ATTACH 后以 INSERT SELECT 复制，避免全库行对象驻留内存；保留 SQLite rowid。
    只接受同版本已校验 bundle，迁移的空目标与维护门闩由调用方负责。
    """
    from mindex.shared.db.dialect import _baseline_columns

    db = connection
    db.execute("BEGIN IMMEDIATE")
    db.execute("PRAGMA defer_foreign_keys=ON")
    db.execute("ATTACH DATABASE ? AS migration_source", (str(origin.path),))
    columns = _baseline_columns()
    for table in reversed(BASELINE_TABLES):
        db.execute(f'DELETE FROM "{table}"')
    for table in BASELINE_TABLES:
        names = [column for column in columns[table] if column != "rowid"]
        if table not in _ROWID_IS_ID:
            names.insert(0, "rowid")
        listed = ",".join(f'"{name}"' for name in names)
        db.execute(f'INSERT INTO "{table}" ({listed}) SELECT {listed} FROM migration_source."{table}"')


def table_digests(store, tables=BASELINE_TABLES) -> dict[str, list[tuple]]:
    """按 rowid 顺序取全表行做往返比对；两后端物理列序可能不同（ALTER 追加列），
    按「rowid + 排序后的(列名,值)对」规范化。"""
    from mindex.shared.storage import PgStore

    digests: dict[str, list[tuple]] = {}
    for table in tables:
        rows = _pg_rows(store, table) if isinstance(store, PgStore) else _sqlite_rows(store, table)
        digests[table] = [
            (row["_rowid_"], *sorted(
                (k, bytes(v) if isinstance(v, memoryview) else v)
                for k, v in row.items() if k != "_rowid_"))
            for row in rows
        ]
    return digests
