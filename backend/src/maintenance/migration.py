"""同版本 local↔PG 业务迁移：清单校验、空目标保护与同 migration_id 续跑。"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from mindex.maintenance.backup_db import rewrite_database, schema, snapshot, store_at
from mindex.maintenance.backup_files import digest, safe_path
from mindex.shared import jsonio, lock_backend
from mindex.shared.backends import open_context
from mindex.shared.db.maintenance_gate import maintenance_gate
from mindex.shared.db.schema_pg import BASELINE_TABLES
from mindex.shared.storage import METADATA_STEPS


@contextmanager
def offline(context, name):
    if context.store.pg:
        with maintenance_gate(context.store, name=name):
            yield
    else:
        from mindex.shared.locks import ORDER
        with lock_backend.acquire_ordered(context.store, ORDER, exclusive=True):
            yield


def _value(value):
    if isinstance(value, bytes | memoryview):
        return {"bytes": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def database_manifest(store):
    from mindex.shared.db.dialect import _baseline_columns

    result = {}
    columns = _baseline_columns()
    with store.connect() as db:
        for table in BASELINE_TABLES:
            names = sorted(c for c in columns[table] if c != "rowid")
            query = ",".join(f'"{c}"' for c in names)
            order = "id" if table in {"runs", "voices", "model_profiles"} else "rowid"
            hashed, count = hashlib.sha256(), 0
            for row in db.execute(f'SELECT {order} AS _seq_,{query} FROM "{table}" ORDER BY {order}'):
                hashed.update(json.dumps([_value(x) for x in row], ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8") + b"\n")
                count += 1
            result[table] = {"rows": count, "sha256": hashed.hexdigest()}
    return result


def inspect_bundle(source):
    source = Path(source).resolve()
    path = source / "manifest.json"
    if not path.is_file():
        raise ValueError("缺少 manifest.json；请先执行 migration export")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("schema") != len(METADATA_STEPS):
        raise ValueError("迁移清单格式或 schema 不匹配；只支持同版本程序迁移")
    if not isinstance(manifest.get("migration_id"), str) or not manifest["migration_id"]:
        raise ValueError("迁移清单缺少 migration_id")
    database = safe_path(source, "metadata.sqlite3")
    if not database.is_file() or digest(database) != manifest.get("database_sha256"):
        raise ValueError("迁移元数据库缺失或摘要不一致")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as db:
        schema(db)
        if db.execute("PRAGMA user_version").fetchone()[0] != manifest["schema"]:
            raise ValueError("迁移数据库 schema 与清单不一致")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("迁移数据库完整性检查失败")
    origin = store_at(source)
    if database_manifest(origin) != manifest.get("tables"):
        raise ValueError("迁移逐表清单与数据库内容不一致")
    library = origin.one("SELECT value FROM settings WHERE key='library.id'")
    if library is None or library["value"] != manifest.get("library_id"):
        raise ValueError("迁移 library_id 不一致")
    if manifest.get("media"):
        media_path = safe_path(source, "media_manifest.json")
        if digest(media_path) != manifest.get("media_manifest_sha256"):
            raise ValueError("媒体清单摘要不一致")
        for key, entry in json.loads(media_path.read_text()).items():
            file = safe_path(source / "data", key)
            if not file.is_file() or file.stat().st_size != entry["size"] or digest(file) != entry["sha256"]:
                raise ValueError(f"媒体缺失或摘要不一致：{key}")
    if manifest.get("vectors"):
        from mindex.maintenance.vector_bundle import validate_vectors
        if digest(source / "vectors_manifest.json") != manifest.get("vectors_manifest_sha256"):
            raise ValueError("向量清单摘要不一致")
        validate_vectors(source)
    return manifest


def export_bundle(config, target, *, with_media=False, with_vectors=False, legacy_namespace=False):
    from mindex.maintenance.pg_export import export_media_bundle, export_pg_to_sqlite
    from mindex.maintenance.vector_bundle import export_vectors

    context = open_context(config)
    media = context.media
    if legacy_namespace:
        if config.database.backend != "postgres" or (
                config.storage.backend != "s3" and config.vector.backend != "chroma_server"):
            raise ValueError("--legacy-namespace 仅用于显式导出旧 PG 共享后端；本地库不需要此参数")
        if config.storage.backend == "s3":
            from mindex.shared.backends.s3 import S3MediaStore

            media = S3MediaStore(config.storage.s3, staging=context.staging,
                                 cache_dir=context.staging.root / "legacy-export")
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("导出目标必须为空目录，禁止覆盖已有 bundle")
    if target == context.data_dir or target.is_relative_to(context.data_dir):
        raise ValueError("导出目标不能位于正在使用的数据目录内")
    target.mkdir(parents=True, exist_ok=True)
    # 首版保持停写到媒体和向量复制完毕；不声称在线一致性。
    with offline(context, "migration-export"):
        if context.store.pg:
            export_pg_to_sqlite(context.store, target)
        else:
            snapshot(context.store.path, target / "metadata.sqlite3")
        rewrite_database(target / "metadata.sqlite3", lambda text: text)
        frozen = store_at(target)
        if with_media:
            export_media_bundle(frozen, media, target)
        if with_vectors:
            export_vectors(frozen if context.store.pg else context.store, target, config=config,
                           legacy_namespace=legacy_namespace)
        manifest = {"format": 1, "migration_id": uuid4().hex, "schema": len(METADATA_STEPS),
                    "library_id": context.library_id, "database_sha256": digest(target / "metadata.sqlite3"),
                    "tables": database_manifest(frozen), "media": with_media, "vectors": with_vectors,
                    "consistency": "offline", "secrets": "redacted"}
        manifest["resource_namespace"] = "legacy" if legacy_namespace else "library-uuid"
        for kind, present in (("media", with_media), ("vectors", with_vectors)):
            if present:
                manifest[f"{kind}_manifest_sha256"] = digest(target / f"{kind}_manifest.json")
        jsonio.save(target / "manifest.json", manifest)
    return manifest


def _journal(store):
    row = store.one("SELECT * FROM migration_runs ORDER BY updated_at DESC LIMIT 1")
    if row or store.pg:
        return row
    # 兼容旧版本地迁移记录；新记录统一与元数据存于同一事务。
    path = store.root / ".migration-state.json"
    return json.loads(path.read_text()) if path.is_file() else None


def _record(store, manifest, status):
    timestamp = "clock_timestamp()" if store.pg else "CURRENT_TIMESTAMP"
    store.execute(
        "INSERT INTO migration_runs(migration_id,source_sha256,status) VALUES(?,?,?)"
        f" ON CONFLICT(migration_id) DO UPDATE SET status=excluded.status,updated_at={timestamp}",
        (manifest["migration_id"], manifest["database_sha256"], status))


def _empty(store):
    for table in BASELINE_TABLES:
        if table == "settings":
            seeds = {"migration.person-roles-v2": "complete", "migration.person-role-only-v3": "complete"}
            if any(row["key"] != "library.id" and seeds.get(row["key"]) != row["value"]
                   for row in store.rows("SELECT key,value FROM settings")):
                return False
        elif table == "delivery_platforms":
            # 与新建本地库种子逐项核对，而非容许任意平台配置被覆盖。
            from mindex.shared.db.schema_pg import DELIVERY_PLATFORMS_SEED
            from mindex.shared.storage import dumps
            seeds = {row[0]: json.loads(row[1]) for row in DELIVERY_PLATFORMS_SEED(dumps)}
            rows = store.rows("SELECT id,body FROM delivery_platforms")
            if any(row["id"] not in seeds or json.loads(row["body"]) != seeds[row["id"]] for row in rows):
                return False
        elif store.one(f'SELECT 1 FROM "{table}" LIMIT 1'):
            return False
    if store.pg and store.one("SELECT 1 FROM task_queue LIMIT 1"):
        return False
    return True


def preflight(config, source):
    manifest = inspect_bundle(source)
    context = open_context(config)
    journal = _journal(context.store)
    same = bool(journal and journal["migration_id"] == manifest["migration_id"]
                and journal["source_sha256"] == manifest["database_sha256"])
    if journal and not same:
        raise ValueError("目标已有其他 migration_id 的记录；请使用新的空目标")
    if not same and not _empty(context.store):
        raise ValueError("迁移目标非空，拒绝覆盖已有数据；请新建空数据库/目录")
    return {"ok": True, "migration_id": manifest["migration_id"], "resume": same,
            "library_id": manifest["library_id"], "media": manifest["media"], "vectors": manifest["vectors"]}


def load_bundle(config, source, *, with_vectors=False):
    from mindex.maintenance.vector_bundle import load_vectors
    from mindex.shared.db.transfer import copy_sqlite_to_pg, copy_sqlite_to_sqlite

    manifest = inspect_bundle(source)
    if manifest["vectors"] and not with_vectors:
        raise ValueError("bundle 包含向量，请传 --with-vectors，避免恢复后缺向量")
    context = open_context(config)
    store, source = context.store, Path(source)
    with offline(context, "migration-load"):
        preflight(config, source)
        journal = _journal(store)
        if journal and journal["status"] == "activated":
            raise ValueError("目标已激活并可能有新写入，禁止重放旧迁移")
        if journal and database_manifest(store) != manifest["tables"]:
            raise ValueError("同 migration_id 的目标内容已变化，拒绝覆盖异值")
        # 空目标的临时 UUID 只用于维护锁；资源必须从第一份对象开始使用源库身份。
        # 元数据提交后重新 open_context 将读到同一身份，不能把对象留在临时库名下。
        store.library_id = manifest["library_id"]
        media = context.media
        if config.storage.backend == "s3":
            from mindex.shared.backends.s3 import S3MediaStore

            media = S3MediaStore(config.storage.s3, library_id=manifest["library_id"],
                                 staging=context.staging,
                                 cache_dir=context.staging.root / "libraries" / manifest["library_id"] / "s3")
        origin = store_at(source)
        if manifest["media"]:
            for key, entry in json.loads((source / "media_manifest.json").read_text()).items():
                file = safe_path(source / "data", key)
                if media.backend == "s3":
                    media.put_immutable(key, file, sha256=entry["sha256"])
                else:
                    destination = safe_path(store.root, key)
                    if destination.exists() and digest(destination) != entry["sha256"]:
                        raise ValueError(f"目标媒体存在异值：{key}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, destination)
            if media.backend != "s3":
                # 远端对象是权威内容；回到本地时还要恢复 job.json/检查点等逻辑视图。
                # 只复制 manifest 中已校验的对象，拒绝缓存或节点旧文件作为来源。
                for entry in origin.rows("SELECT * FROM shared_files WHERE size>=0"):
                    file = safe_path(source / "data", entry["object_key"])
                    if not file.is_file() or file.stat().st_size != entry["size"] or digest(file) != entry["sha256"]:
                        raise ValueError(f"权威文件对象缺失或摘要不一致：{entry['path']}")
                    destination = safe_path(store.root, entry["path"])
                    if destination.exists() and digest(destination) != entry["sha256"]:
                        raise ValueError(f"目标权威文件存在异值：{entry['path']}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, destination)
        if manifest["vectors"]:
            if not with_vectors:
                raise ValueError("bundle 包含向量，请传 --with-vectors，避免恢复后缺向量")
            load_vectors(store, source, config=config)
        # 元数据与迁移日志同事务，崩溃后不会把半库误认作可覆盖的空库。
        if not journal:
            if store.pg:
                with store.connect() as outer:
                    # copy_sqlite_to_pg 的事务由显式连接注入，避免双写窗口。
                    copy_sqlite_to_pg(origin, store, connection=outer)
                    outer.execute("INSERT INTO migration_runs(migration_id,source_sha256,status) VALUES(?,?,?)",
                                  (manifest["migration_id"], manifest["database_sha256"], "loaded"))
            else:
                with store.connect() as outer:
                    copy_sqlite_to_sqlite(origin, store, connection=outer)
                    outer.execute("INSERT INTO migration_runs(migration_id,source_sha256,status) VALUES(?,?,?)",
                                  (manifest["migration_id"], manifest["database_sha256"], "loaded"))
        result = verify_bundle(config, source)
        if not result["consistent"]:
            raise ValueError(f"迁移装载后校验失败：{result['differences']}")
    return result


def verify_bundle(config, source):
    manifest = inspect_bundle(source)
    context = open_context(config)
    actual = database_manifest(context.store)
    differences = [table for table in BASELINE_TABLES if actual[table] != manifest["tables"][table]]
    if manifest["media"]:
        for key, entry in json.loads((Path(source) / "media_manifest.json").read_text()).items():
            try:
                with context.media.open_read(key) as body:
                    hashed = hashlib.sha256()
                    while chunk := body.read(1024 * 1024):
                        hashed.update(chunk)
                if hashed.hexdigest() != entry["sha256"]:
                    differences.append(f"media:{key}")
            except FileNotFoundError:
                differences.append(f"media:{key}")
    if manifest["vectors"]:
        from mindex.maintenance.vector_bundle import verify_vectors
        differences += verify_vectors(context.store, Path(source), config=config)
    journal = _journal(context.store)
    if not differences and journal and journal["status"] != "activated":
        _record(context.store, manifest, "verified")
    return {"migration_id": manifest["migration_id"], "consistent": not differences, "differences": differences}


def activate_bundle(config, source):
    manifest = inspect_bundle(source)
    if not manifest["media"] and any(manifest["tables"][table]["rows"] for table in (
            "sources", "segments", "shared_files", "music_tracks", "person_references")):
        raise ValueError("bundle 缺少必要媒体，只能用于诊断，禁止激活；请重新 export --with-media")
    if not manifest["vectors"] and manifest["tables"]["representations"]["rows"]:
        raise ValueError("bundle 缺少必要向量，禁止激活；请重新 export --with-vectors")
    context = open_context(config)
    with offline(context, "migration-activate"):
        journal = _journal(context.store)
        if not journal or journal["migration_id"] != manifest["migration_id"]:
            raise ValueError("目标尚未装载该 migration_id")
        result = verify_bundle(config, source)
        if not result["consistent"]:
            raise ValueError("迁移对账失败，禁止激活")
        _record(context.store, manifest, "activated")
    return {**result, "activated": True}
