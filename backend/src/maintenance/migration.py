"""同版本 local↔PG 业务迁移：清单校验、空目标保护与同 migration_id 续跑。"""
from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

from mindex.maintenance.backup_db import database as bundle_database
from mindex.maintenance.backup_db import schema, snapshot, store_at
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
        from mindex.shared.migration_state import access

        with lock_backend.acquire_ordered(context.store, ORDER, exclusive=True), access(context.store):
            yield


def _value(value):
    if isinstance(value, bytes | memoryview):
        return {"bytes": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def database_manifest(store, *, connection=None):
    from mindex.shared.db.dialect import _baseline_columns

    result = {}
    columns = _baseline_columns()
    with store.connect() if connection is None else nullcontext(connection) as db:
        for table in BASELINE_TABLES:
            names = sorted(c for c in columns[table] if c != "rowid")
            query = ",".join(f'"{c}"' for c in names)
            order = "id" if table in {"runs", "voices", "model_profiles", "studio_events"} else "rowid"
            hashed, count = hashlib.sha256(), 0
            cursor = None
            while True:
                boundary, args = ("", ()) if cursor is None else (f" WHERE {order}>?", (cursor,))
                rows = db.execute(f'SELECT {order} AS _seq_,{query} FROM "{table}"' + boundary +
                                  f" ORDER BY {order} LIMIT ?", (*args, 512)).fetchall()
                if not rows:
                    break
                for row in rows:
                    hashed.update(json.dumps([_value(x) for x in row], ensure_ascii=False,
                                             separators=(",", ":")).encode("utf-8") + b"\n")
                    count += 1
                cursor = rows[-1][0]
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
    with bundle_database(f"{database.as_uri()}?mode=ro", uri=True) as db:
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
    if manifest.get('execution_manifest_sha256') and digest(source / 'execution_manifest.json') != manifest['execution_manifest_sha256']:
        raise ValueError('执行状态清单摘要不一致')
    if manifest.get("vectors"):
        from mindex.maintenance.vector_bundle import validate_vectors
        if digest(source / "vectors_manifest.json") != manifest.get("vectors_manifest_sha256"):
            raise ValueError("向量清单摘要不一致")
        validate_vectors(source)
    return manifest


def export_bundle(config, target, *, with_media=False, with_vectors=False, legacy_namespace=False):
    from mindex.maintenance.pg_export import export_media_bundle, export_pg_to_sqlite
    from mindex.maintenance.vector_bundle import export_vectors

    with open_context(config) as context:
        media = context.media
        if legacy_namespace:
            if config.database.backend != "postgres" or (
                    config.storage.backend != "s3" and config.vector.backend != "chroma_server"):
                raise ValueError("--legacy-namespace 仅用于显式导出旧 PG 共享后端；本地库不需要此参数")
            if config.storage.backend == "s3":
                from mindex.shared.backends.s3 import S3MediaStore

                media = S3MediaStore(config.storage.s3, staging=context.staging,
                                     cache_dir=context.staging.root / "legacy-export")
        if with_media and context.store.one("SELECT 1 FROM upload_sessions WHERE transport='s3' "
                                             "AND state IN ('pending','completing') LIMIT 1"):
            context.close()
            raise ValueError("原生 S3 multipart 尚未完成，无法跨后端迁移分片；请完成或显式取消后重新导出")
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
            from mindex.maintenance.migration_runtime import export_execution
            from mindex.maintenance.migration_secrets import redact_metadata

            export_execution(context.store, target)
            redact_metadata(target / "metadata.sqlite3")
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
            from mindex.maintenance.migration_secrets import scan_bundle

            manifest['execution_manifest_sha256'] = digest(target / 'execution_manifest.json')
            manifest['unknown_secret_locations'] = scan_bundle(target)
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


def _empty(store, *, include_runtime=True):
    for table in BASELINE_TABLES:
        if table == "settings":
            seeds = {"runtime.protocol": "2", "migration.person-roles-v2": "complete", "migration.person-role-only-v3": "complete"}
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
    if include_runtime:
        for table in ('gpu_allocations', 'resource_objects', 'resource_pins'):
            if store.one(f'SELECT 1 FROM {table} LIMIT 1'):
                return False
    if store.pg and store.one("SELECT 1 FROM task_queue LIMIT 1"):
        return False
    return True


def _target_context(config, manifest):
    expected = config.server.library_id
    if expected is not None and expected != manifest['library_id']:
        raise ValueError('目标配置期望的库身份与迁移源不一致，请修正配置后再迁移')
    temporary = config.model_copy(deep=True)
    temporary.server.library_id = None  # 仅迁移工具可打开待绑定的空目标。
    return open_context(temporary)


def _rebind(store, library_id):
    store.library_id = library_id
    if store.pg:
        store.backend.library_id = library_id
        hashed = hashlib.sha256(library_id.encode()).digest()
        store.backend.library_key = (int.from_bytes(hashed[:4], 'big', signed=True),
                                     int.from_bytes(hashed[4:8], 'big', signed=True))


def preflight(config, source):
    from mindex.maintenance.migration_journal import MigrationJournal

    source = Path(source).resolve()
    manifest = inspect_bundle(source)
    with _target_context(config, manifest) as context:
        rows = context.store.rows('SELECT * FROM migration_runs')
        journal = MigrationJournal(context.store, manifest, source)
        if rows:
            row = journal.row()
            if (row is None or len(rows) != 1 or row['manifest_sha256'] != journal.manifest_sha256
                    or row['source_library_id'] != manifest['library_id']):
                raise ValueError('目标已有不同迁移/源清单/库身份的记录，拒绝覆盖')
            if row['status'] == 'activated':
                raise ValueError('目标已激活并可能有新写入，禁止重放旧迁移')
        elif not _empty(context.store):
            raise ValueError('迁移目标非空，拒绝覆盖已有数据；请新建空数据库/目录')
        return {'ok': True, 'migration_id': manifest['migration_id'], 'resume': bool(rows),
                'library_id': manifest['library_id'], 'media': manifest['media'], 'vectors': manifest['vectors']}


def _matches(media, key, entry):
    try:
        with media.open_read(key) as body:
            hashed, size = hashlib.sha256(), 0
            while chunk := body.read(1024 * 1024):
                size += len(chunk)
                hashed.update(chunk)
    except FileNotFoundError:
        return False
    if hashed.hexdigest() != entry['sha256'] or size != entry['size']:
        raise ValueError(f'目标媒体存在异值，拒绝覆盖：{key}')
    return True


def _copy_local(file, destination, journal):
    import os
    import tempfile

    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f'.migration-{journal.id}-'
    for partial in destination.parent.glob(prefix + '*.partial'):
        partial.unlink()
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=prefix, suffix='.partial', delete=False) as output:
        temporary = Path(output.name)
        try:
            with file.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _load_media(store, media, origin, source, journal):
    manifest = json.loads((source / 'media_manifest.json').read_text())
    for key, entry in manifest.items():
        journal.object('media', key, entry['sha256'], state='pending')
        if not _matches(media, key, entry):
            file = safe_path(source / 'data', key)
            if media.backend == 's3':
                media.put_immutable(key, file, sha256=entry['sha256'])
            else:
                _copy_local(file, safe_path(store.root, key), journal)
            if not _matches(media, key, entry):
                raise ValueError(f'目标媒体写入后不可读：{key}')
        journal.object('media', key, entry['sha256'], state='verified')
    if media.backend != 's3':
        for entry in origin.rows('SELECT * FROM shared_files WHERE size>=0'):
            key = entry['path']
            file = safe_path(source / 'data', entry['object_key'])
            if not file.is_file() or digest(file) != entry['sha256'] or file.stat().st_size != entry['size']:
                raise ValueError(f'权威文件对象缺失或摘要不一致：{key}')
            destination = safe_path(store.root, key)
            journal.object('logical', key, entry['sha256'], state='pending')
            if destination.exists():
                if digest(destination) != entry['sha256']:
                    raise ValueError(f'目标权威文件存在异值：{key}')
            else:
                _copy_local(file, destination, journal)
            journal.object('logical', key, entry['sha256'], state='verified')


def load_bundle(config, source, *, with_vectors=False):
    from mindex.maintenance.migration_journal import MigrationJournal
    from mindex.maintenance.migration_runtime import restore_execution
    from mindex.maintenance.vector_bundle import load_vectors
    from mindex.shared.db.transfer import copy_sqlite_to_pg, copy_sqlite_to_sqlite

    source = Path(source).resolve()
    manifest = inspect_bundle(source)
    if manifest['vectors'] and not with_vectors:
        raise ValueError('bundle 包含向量，请传 --with-vectors，避免恢复后缺向量')
    with _target_context(config, manifest) as context, offline(context, 'migration-load'):
        store = context.store
        journal = MigrationJournal(store, manifest, source)
        journal.prepare()  # 必须在任何目标对象、集合或身份写入之前持久提交。
        if journal.done('metadata'):
            if database_manifest(store) != journal.expected_tables():
                raise ValueError('同 migration_id 的目标内容已变化，拒绝覆盖异值')
        elif not _empty(store, include_runtime=False):
            raise ValueError('迁移元数据尚未落账，但目标内容已变化，拒绝覆盖异值')
        if not journal.done('identity'):
            with store.connect() as db:
                db.execute("UPDATE settings SET value=? WHERE key='library.id'", (manifest['library_id'],))
                journal.stage('identity', 'complete', connection=db)
        _rebind(store, manifest['library_id'])
        media = context.media
        if config.storage.backend == 's3':
            from mindex.shared.backends.s3 import S3MediaStore
            from mindex.shared.object_coordination import ObjectCoordinator

            media = S3MediaStore(config.storage.s3, library_id=store.library_id, staging=context.staging,
                                 cache_dir=context.staging.root / 'libraries' / store.library_id / 's3')
            media.coordinator = ObjectCoordinator(store)
            context._resources.callback(media.close)
            store.media = media
        origin = store_at(source)
        journal.stage('media', 'running')
        if manifest['media']:
            _load_media(store, media, origin, source, journal)
        journal.stage('media', 'complete')
        journal.stage('vectors', 'running')
        if manifest['vectors']:
            load_vectors(store, source, config=config, journal=journal)
        journal.stage('vectors', 'complete')
        if not journal.done('metadata'):
            with store.connect() as outer:
                if store.pg:
                    copy_sqlite_to_pg(origin, store, connection=outer)
                else:
                    copy_sqlite_to_sqlite(origin, store, connection=outer)
                journal.stage('metadata', 'complete', connection=outer)
                outer.execute("UPDATE migration_runs SET status='loaded' WHERE migration_id=?", (journal.id,))
        if not journal.done('tasks'):
            journal.stage('tasks', 'running')
        restore_execution(store, source, journal)
        journal.stage('validation', 'running')
        result = verify_bundle(config, source)
        if not result['consistent']:
            raise ValueError(f"迁移装载后校验失败：{result['differences']}")
        return result


def verify_bundle(config, source):
    from mindex.maintenance.migration_journal import MigrationJournal
    from mindex.maintenance.migration_secrets import scan_bundle

    source = Path(source).resolve()
    manifest = inspect_bundle(source)
    with _target_context(config, manifest) as context:
        store = context.store
        journal = MigrationJournal(store, manifest, source)
        matching = journal.row() is not None
        expected = journal.expected_tables() if matching else manifest['tables']
        actual = database_manifest(store)
        differences = [table for table in BASELINE_TABLES if actual[table] != expected[table]]
        if matching and not journal.done('tasks'):
            differences.append('runtime:tasks_not_restored')
        if manifest['media']:
            for key, entry in json.loads((source / 'media_manifest.json').read_text()).items():
                try:
                    if not _matches(context.media, key, entry):
                        differences.append(f'media:{key}')
                except ValueError:
                    differences.append(f'media:{key}')
        if manifest['vectors']:
            from mindex.maintenance.vector_bundle import verify_vectors

            differences += verify_vectors(store, source, config=config)
        secrets = scan_bundle(source)
        if not differences and matching and journal.row()['status'] != 'activated':
            # CLI verify 本身是维护操作；只更新该迁移的验证账本。
            from mindex.shared.migration_state import access

            with access(store):
                journal.stage('validation', 'complete')
                _record(store, manifest, 'verified')
        return {'migration_id': manifest['migration_id'], 'consistent': not differences,
                'differences': differences, 'unknown_secret_locations': secrets}


def activate_bundle(config, source):
    from mindex.maintenance.migration_journal import MigrationJournal

    source = Path(source).resolve()
    manifest = inspect_bundle(source)
    if not manifest['media'] and any(manifest['tables'][table]['rows'] for table in (
            'sources', 'segments', 'shared_files', 'music_tracks', 'person_references')):
        raise ValueError('bundle 缺少必要媒体，只能用于诊断，禁止激活；请重新 export --with-media')
    if not manifest['vectors'] and manifest['tables']['representations']['rows']:
        raise ValueError('bundle 缺少必要向量，禁止激活；请重新 export --with-vectors')
    with _target_context(config, manifest) as context, offline(context, 'migration-activate'):
        journal = MigrationJournal(context.store, manifest, source)
        row = journal.row()
        if not row or row['manifest_sha256'] != journal.manifest_sha256:
            raise ValueError('目标尚未装载该 migration_id/源清单')
        if row['status'] == 'activated':
            return {**verify_bundle(config, source), 'activated': True, 'already_activated': True}
        result = verify_bundle(config, source)
        if not result['consistent']:
            raise ValueError('迁移对账失败，禁止激活')
        if result['unknown_secret_locations']:
            raise ValueError('未知秘密载体阻止正式激活：' + ', '.join(result['unknown_secret_locations']))
        unknown_groups = {r['group_id'] for r in context.store.rows("SELECT group_id FROM gpu_allocations WHERE state='unknown'")}
        if unknown_groups - set(config.gpu.groups):
            raise ValueError('迁移前 GPU 执行未核对，目标必须保留原物理资源组声明或先附依据核对')
        if context.form == 'cluster':
            from mindex.shared.admission import validate_serving

            validate_serving(context)
            from mindex.shared.gpu_resources import required_groups

            for row in context.store.rows("SELECT DISTINCT kind FROM task_queue WHERE status='pending'"):
                required_groups(config, row['kind'], cluster=True)
        with context.store.connect() as db:
            journal.stage('activation', 'complete', connection=db)
            db.execute("UPDATE migration_runs SET status='activated' WHERE migration_id=?", (journal.id,))
    return {**result, 'activated': True}
