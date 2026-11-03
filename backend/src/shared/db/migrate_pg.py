"""PostgreSQL 基线迁移器与版本检查（plans §4、§5）。

- PG 侧只有一个**基线版本**（对应当前 SQLite 逻辑 schema 的版本数）；以后逻辑版本
  追加时两侧同步。``schema_migrations`` 记录版本与校验值，受迁移 advisory lock 保护。
- API/worker 启动**只检查**版本（``check_schema``），集群 DDL 由独立的
  ``mindex migrate`` 执行，避免多个版本同时改表。
- 基线只应用于空库；装载已有数据走 transfer 的显式导入路径，随后修复序列。
"""

from __future__ import annotations

import hashlib
import uuid

from mindex.shared.db.errors import SchemaMismatch
from mindex.shared.db.schema_pg import BASELINE_DDL, DELIVERY_PLATFORMS_SEED

__all__ = ["check_schema", "migrate_pg", "pg_schema_version"]

# 迁移锁键（固定值）：与 library 无关，防止两个迁移进程同时改表。
_MIGRATION_LOCK_KEY = (0x4D49_4E44, 0x4D49_4752)  # "MIND"/"MIGR"


def _logical_version() -> int:
    from mindex.shared.storage import METADATA_STEPS

    return len(METADATA_STEPS)


def pg_schema_version(conn) -> int:
    row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0) if row is not None else 0


def check_schema(conn) -> None:
    """启动时校验版本；缺迁移给出中文修复提示。"""
    from mindex.shared.db.errors import BackendError

    try:
        current, expected = pg_schema_version(conn), _logical_version()
    except BackendError as exc:  # schema_migrations 尚不存在：目标库未初始化
        raise SchemaMismatch(
            "PostgreSQL 元数据库尚未初始化（无 schema_migrations）；请先执行 mindex migrate"
        ) from exc
    if current < expected:
        raise SchemaMismatch(
            f"PostgreSQL 元数据库 schema 版本 {current} 低于程序要求 {expected}；"
            "请先执行 mindex migrate（API/worker 不会自动执行集群 DDL）")
    if current > expected:
        raise SchemaMismatch(
            f"PostgreSQL 元数据库 schema 版本 {current} 高于程序要求 {expected}；"
            "请升级程序后再连接")


def migrate_pg(conn) -> int:
    """把基线应用到空库并登记版本；已有同版本则幂等跳过。返回最终版本。"""
    expected = _logical_version()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", _MIGRATION_LOCK_KEY)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,"
            "applied_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'utc','YYYY-MM-DD HH24:MI:SS'))"
        )
        current = pg_schema_version(conn)
        if current >= expected:
            return current
        if current == 0 and not _database_is_empty(conn):
            raise SchemaMismatch("目标库非空且没有迁移记录；装载已有数据请使用备份/迁移工具，不在启动路径改表")
        conn.execute(BASELINE_DDL)
        from mindex.shared.storage import dumps

        conn.cursor().executemany(
            "INSERT INTO delivery_platforms(id,body,created_at,updated_at) VALUES(%s,%s,%s,%s)"
            " ON CONFLICT (id) DO NOTHING",
            DELIVERY_PLATFORMS_SEED(dumps),
        )
        checksum = hashlib.sha256(BASELINE_DDL.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)", (expected, checksum)
        )
        # 建库生成 library UUID（锁与缓存按它隔离，plans §2；不用路径 hash 当租户键）。
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('library.id',%s) ON CONFLICT (key) DO NOTHING",
            (str(uuid.uuid4()),),
        )
        return expected


def _database_is_empty(conn) -> bool:
    """除 schema_migrations 外没有任何业务表。"""
    row = conn.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'"
    ).fetchone()
    return int(row[0]) == 0


def fix_sequences(conn, tables: list[str]) -> None:
    """显式导入 rowid/id 后，把 identity 序列推进到当前最大值（防主键回卷）。

    ``conn`` 接受 PgConnection 包装或原生 psycopg 连接（仅位置参数取值）。
    """
    for table in tables:
        conn.execute(
            "SELECT setval(pg_get_serial_sequence(%s, 'rowid'),"
            " COALESCE((SELECT max(rowid) FROM " + table + "), 0) + 1, false)", (table,)
        )
