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

from shared.db.schema_pg import BASELINE_TABLES
from shared.storage import Store
from shared.work_budget import copy_file

if TYPE_CHECKING:
    from shared.storage import PgStore

log = logging.getLogger(__name__)

__all__ = ["export_media_bundle", "export_pg_to_sqlite"]

_BATCH = 500
# rowid 即 id 的表（PG 侧无独立 rowid 列；按 id 排序即 SQLite 语义序）。
_ROWID_IS_ID = {"model_profiles", "voices", "runs", "studio_events"}


def export_pg_to_sqlite(pg_store: PgStore, target: Path, *, tables=BASELINE_TABLES) -> dict:
    """单一可重复读只读快照内导出全部业务表；返回逐表清单。

    目标必须是刚完成基线迁移的暂存 SQLite（本函数先清空再灌入，保证与快照一致）。
    """
    staging = Store(target)
    manifest: dict[str, dict] = {}
    total = 0
    from shared.db.dialect import _baseline_columns

    columns_by_table = _baseline_columns()
    with pg_store.backend.pool().connection() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        with staging.connect() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            for table in tables:
                db.execute(f'DELETE FROM "{table}"')
            for table in tables:
                columns = [
                    column
                    for column in columns_by_table[table]
                    if column != "rowid" or table not in _ROWID_IS_ID
                ]
                names = ",".join(f'"{column}"' for column in columns)
                order = "id" if table in _ROWID_IS_ID else "rowid"
                placeholders = ",".join("?" * len(columns))
                insert = f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})'
                table_digest, count = hashlib.sha256(), 0
                # server-side cursor：fetchmany 不会先把整表装进客户端内存。
                with connection.cursor(name=f"export_{table}") as cursor:
                    cursor.execute(f'SELECT {names} FROM "{table}" ORDER BY {order}')
                    while chunk := cursor.fetchmany(_BATCH):
                        rows = [tuple(row) for row in chunk]
                        db.executemany(insert, rows)
                        for row in rows:
                            table_digest.update(repr(row).encode())
                        count += len(rows)
                manifest[table] = {"rows": count, "sha256": table_digest.hexdigest()}
                total += count
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("PG 导出存在悬空外键引用")
    log.info("PG 一致性导出完成：%d 行 / %d 表 → %s", total, len(tables), target)
    return {
        "tables": manifest,
        "total_rows": total,
        "metadata": {"isolation": "repeatable-read-read-only", "dialect": "pg-export-v1"},
    }


def export_media_bundle(store: Store, media, target: Path) -> dict:
    """把暂存 SQLite 引用到的全部媒体复制进 bundle（plans §10）。

    可达性由既有 ``referenced_files`` 计算（与本地备份同一口径）：给定 S3 的 key
    清单或本地文件清单，闭包出正文/缩略图/分镜/人物帧/Studio 输入等全部依赖。
    S3 形态逐 key 物化后复制（保持相对 key 布局）；本地形态直接复制文件。
    返回清单（key → size/sha256），供装载后逐项校验。
    """
    import hashlib
    from contextlib import nullcontext

    from maintenance.backup_files import referenced_files, safe_path

    target = Path(target)
    payload = target / "data"
    payload.mkdir(parents=True, exist_ok=True)
    if getattr(media, "backend", "local") == "s3":
        keys = referenced_files(
            target, target / "metadata.sqlite3", available_files=set(media.iter_prefix(""))
        )
        from shared.resource_registry import referenced_objects, required_objects

        available = set(media.iter_prefix(""))
        keys.update(key for key in referenced_objects(store) if key in available)
        keys.update(required_objects(store))
        for key in sorted(keys):
            destination = safe_path(payload, key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            coordinator = getattr(media, "coordinator", None)
            with coordinator.retained(key, kind="backup") if coordinator else nullcontext():
                with media.materialize(key) as path:
                    copy_file(path, destination)
    else:
        keys = referenced_files(media.root, target / "metadata.sqlite3")
        from shared.resource_registry import required_objects

        keys.update(required_objects(store))
        for key in sorted(keys):
            destination = safe_path(payload, key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_file(media.root / key, destination)
    manifest = {}
    for key in sorted(keys):
        path = payload / key
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        manifest[key] = {"size": path.stat().st_size, "sha256": digest.hexdigest()}
    (target / "media_manifest.json").write_text(
        __import__("json").dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("媒体 bundle 复制完成：%d 个对象 → %s", len(manifest), payload)
    return {"objects": len(manifest), "bytes": sum(item["size"] for item in manifest.values())}
