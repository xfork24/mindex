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
from mindex.shared.db.scaling_schema import (
    PG_ADMISSION,
    PG_DELIVERY,
    PG_DIRECTORIES,
    PG_DURABLE_INDEX,
    PG_EXECUTION,
    PG_FILES,
    PG_QUEUE,
)
from mindex.shared.db.schema_pg import BASELINE_DDL, DELIVERY_PLATFORMS_SEED

__all__ = ["check_schema", "migrate_pg", "pg_schema_version"]

# 迁移锁键（固定值）：与 library 无关，防止两个迁移进程同时改表。
_MIGRATION_LOCK_KEY = (0x4D49_4E44, 0x4D49_4752)  # "MIND"/"MIGR"

# v15 为已发布基线；之后只追加，禁止升级时重跑基线覆盖已有表定义。
BASELINE_VERSION = 15
PG_STEPS = ((16, PG_FILES + PG_QUEUE), (17, PG_EXECUTION), (18, "SELECT 1;"), (19, PG_DURABLE_INDEX), (20, PG_DIRECTORIES), (21, PG_DELIVERY), (22, PG_ADMISSION))


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
    for version, ddl in PG_STEPS:
        row = conn.execute("SELECT checksum FROM schema_migrations WHERE version=%s", (version,)).fetchone()
        if row is None or row[0] != hashlib.sha256(ddl.encode()).hexdigest():
            raise SchemaMismatch(f"PostgreSQL 追加迁移 v{version} 校验值不一致；请核对迁移历史")


def migrate_pg(conn) -> int:
    """空库建立 v15 基线，旧库按顺序执行追加迁移；版本与 DDL 同事务提交。"""
    expected = _logical_version()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", _MIGRATION_LOCK_KEY)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,"
            "applied_at TEXT NOT NULL DEFAULT to_char(now() AT TIME ZONE 'utc','YYYY-MM-DD HH24:MI:SS'))"
        )
        current = pg_schema_version(conn)
        if current > expected:
            raise SchemaMismatch("PostgreSQL schema 高于程序要求；请升级程序，不执行降级迁移")
        if 0 < current < BASELINE_VERSION:
            raise SchemaMismatch("不支持的 PostgreSQL 基线版本；请先用对应版本导出再迁移")
        if current == 0 and not _database_is_empty(conn):
            raise SchemaMismatch("目标库非空且没有迁移记录；装载已有数据请使用备份/迁移工具，不在启动路径改表")
        if current == 0:
            conn.execute(BASELINE_DDL)
            from mindex.shared.storage import dumps

            conn.cursor().executemany(
                "INSERT INTO delivery_platforms(id,body,created_at,updated_at) VALUES(%s,%s,%s,%s)"
                " ON CONFLICT (id) DO NOTHING",
                DELIVERY_PLATFORMS_SEED(dumps),
            )
            conn.execute(
                "INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)",
                (BASELINE_VERSION, hashlib.sha256(BASELINE_DDL.encode()).hexdigest()))
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('library.id',%s) ON CONFLICT (key) DO NOTHING",
                (str(uuid.uuid4()),),
            )
            current = BASELINE_VERSION
        for version, ddl in PG_STEPS:
            checksum = hashlib.sha256(ddl.encode()).hexdigest()
            if version <= current:
                row = conn.execute("SELECT checksum FROM schema_migrations WHERE version=%s", (version,)).fetchone()
                if row is None or row[0] != checksum:
                    raise SchemaMismatch(f"PostgreSQL 追加迁移 v{version} 校验值不一致；拒绝改写迁移历史")
                continue
            if version != current + 1:
                raise SchemaMismatch("PostgreSQL 追加迁移不连续，拒绝启动迁移")
            conn.execute(ddl)
            conn.execute("INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)", (version, checksum))
            current = version
        if current != expected:
            raise SchemaMismatch("PostgreSQL 与 SQLite 的迁移版本不一致")
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
        column = "id" if table in {"model_profiles", "voices", "runs"} else "rowid"
        conn.execute(
            "SELECT setval(pg_get_serial_sequence(%s, %s),"
            f" COALESCE((SELECT max({column}) FROM {table}), 0) + 1, false)", (table, column)
        )
