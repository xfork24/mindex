"""备份的数据库边界、依赖裁剪及不可变版本映射。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from mindex.shared.config import fingerprint
from mindex.shared.storage import Store, dumps, migrate_metadata

# 新业务表必须在这里明确归属，避免升级后部分备份悄悄携带无关素材。
TABLES = {
    'media_groups', 'media_group_members', 'clip_migration_files', 'media_references', 'sources', 'segments', 'source_curation',
    'segment_curation', 'transcripts', 'annotations', 'scene_plans', 'index_versions',
    'index_members', 'representations', 'channel_data', 'dialogue_tasks', 'source_tasks',
    'settings', 'model_profiles', 'voices', 'runs', 'person_entities', 'person_sources',
    'person_references', 'person_imports', 'person_features', 'person_evidence', 'person_coverage',
    'person_tasks', 'person_versions', 'search_releases', 'script_templates',
    'topic_profiles', 'topic_sources', 'topic_items', 'topic_runs', 'topic_cards', 'topic_gaps',
    'topic_calendar_events', 'brand_templates', 'delivery_templates', 'delivery_platforms', 'music_tracks', 'tool_sources',
}
# 这些表的 source_id 指向选题档案/选题数据源，而不是素材库 sources；
# 部分备份不按素材裁剪它们，也不做素材依赖校验（与 script_templates 一样整体保留）。
NON_LIBRARY_SOURCE_ID = frozenset({'topic_items', 'topic_runs', 'topic_calendar_events'})


def library_source_id(table, columns):
    """表的 source_id 列是否引用素材库 sources。"""
    return 'source_id' in columns and table not in NON_LIBRARY_SOURCE_ID
SECRET_FIELDS = {
    'api_key', 'apikey', 'access_token', 'refresh_token', 'secret', 'password',
    'authorization', 'bearer_token', 'client_secret',
}


@contextmanager
def database(path, **kwargs):
    db = sqlite3.connect(path, **kwargs)
    try:
        with db:
            yield db
    finally:
        db.close()


def store_at(root: Path) -> Store:
    """读取旧库不运行 schema migration，也不创建不存在的库。"""
    store = Store.__new__(Store)
    store.root, store.path = root.resolve(), root / 'metadata.sqlite3'
    if store.path.is_symlink():
        raise ValueError('数据库不能是符号链接')
    if not store.path.is_file():
        raise ValueError(f'找不到数据库：{store.path}')
    return store


def schema(db, *, require_current=True):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    unknown = names - TABLES - {'sqlite_sequence'}
    missing = TABLES - names
    if unknown or (missing if require_current else not {'sources', 'settings', 'index_versions'} <= names):
        raise ValueError(f'不支持的数据库结构（请先升级应用）：未知表 {sorted(unknown)}，缺少表 {sorted(missing)}')
    if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone():
        raise ValueError('备份数据库不接受自定义触发器或视图')
    return names & TABLES


def snapshot(origin, destination, *, allow_legacy=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with database(f'{origin.as_uri()}?mode=ro', uri=True) as src, database(destination) as dst:
        schema(src, require_current=not allow_legacy)
        src.backup(dst)
        dst.execute('PRAGMA journal_mode=DELETE')


def upgrade_database(path):
    """把隔离副本按版本化迁移升级到当前 schema（旧备份可能是 user_version=0 的旧结构）。"""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row  # 迁移步骤按列名读取
    try:
        migrate_metadata(db)
    finally:
        db.close()


def walk(value, transform):
    """按 transform 改写结构中的字符串；布尔、数字与 null 原样保留。

    transform 的契约是字符串改写（路径迁移、ID 重映射），对 JSON 标量直接调用会出错。
    """
    if isinstance(value, dict):
        return {key: walk(item, transform) for key, item in value.items()}
    if isinstance(value, list):
        return [walk(item, transform) for item in value]
    return transform(value) if isinstance(value, str) else value


def json_container(value):
    """自由文本可能以括号开头；只把有效 JSON 对象或数组视为结构化数据。"""
    if not isinstance(value, str) or not value.lstrip().startswith(('{', '[')):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def raw_representation_ids(member, spec):
    if 'representation_ids' in member:
        return member['representation_ids']
    from mindex.shared.media import windows

    duration = member['end'] - member['start']
    preprocessing = spec['preprocessing']
    bounds = ([(0, duration)] if duration <= preprocessing['short_shot_seconds'] else
              windows(0, duration, preprocessing['window_seconds'], preprocessing['stride_seconds']))
    return [fingerprint([member['id'], 'video', start, end]) for start, end in bounds] + [
        fingerprint([member['id'], view, document]) for view, document in member['views'].items()
    ]


def representation_manifest(member, spec, index_id, representations, *, allow_legacy_representations=False):
    """为修复过时间边界的旧版快照保存显式表示谱系，不推测原始窗口。"""
    canonical = {fingerprint([index_id, base]): base for base in raw_representation_ids(member, spec)}
    missing = [row for row in representations if row['id'] not in canonical]
    if not missing:
        return member, {row['id']: canonical[row['id']] for row in representations}
    if not allow_legacy_representations or spec.get('format', 1) != 1 or 'representation_ids' in member:
        raise ValueError(f'表示不在成员清单中：{missing[0]["id"]}')
    for row in representations:
        if (row['view'] != 'video' and
                member.get('views', {}).get(row['view']) != row['document']):
            raise ValueError(f'表示与历史成员渠道不一致：{row["id"]}')
    # 不能直接把旧表示 ID 当作 base：随后全库重映射也会替换这些字符串。
    bases = {row['id']: fingerprint(['legacy-representation-v1', index_id, row['id']])
             for row in sorted(representations, key=lambda row: row['id'])}
    return {**member, 'representation_ids': list(bases.values())}, bases


def redact(value):
    if isinstance(value, dict):
        return {
            key: ('' if key.lower().split('.')[-1] in SECRET_FIELDS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def rewrite_database(path, transform, *, secrets=False):
    """重写所有文本/JSON，包括任务快照；VACUUM 清除密钥的旧页及空闲页。"""
    with database(path) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA secure_delete=ON')
        for table in sorted(schema(db)):
            columns = [r['name'] for r in db.execute(f'PRAGMA table_info({table})')]
            for row in db.execute(f'SELECT rowid AS _rowid_,* FROM {table}').fetchall():
                changes = {}
                for key in columns:
                    value = row[key]
                    if not secrets and (
                        key in SECRET_FIELDS or
                        table == 'settings' and str(row['key']).lower().split('.')[-1] in SECRET_FIELDS
                    ):
                        changes[key] = None if key != 'value' else ''
                        continue
                    if not isinstance(value, str):
                        continue
                    # SQL 文本 ID 可能恰好是合法 JSON 数字（如 1e999）。
                    # 仅解析容器，以及 settings.value 明确编码的 JSON 字符串。
                    parsed = json_container(value)
                    if parsed is None and table == 'settings' and key == 'value' and value.lstrip().startswith('"'):
                        try:
                            parsed = json.loads(value)
                        except ValueError:
                            pass
                    if parsed is None:
                        updated = transform(value)
                    else:
                        parsed = parsed if secrets else redact(parsed)
                        # 空间/预处理契约是身份的一部分，不重写其中的旧版本追溯字段。
                        updated = dumps(parsed if table == 'index_versions' and key == 'spec'
                                        else walk(parsed, transform))
                    if updated != value:
                        changes[key] = updated
                if changes:
                    db.execute(
                        f'UPDATE {table} SET ' + ','.join(f'{k}=?' for k in changes) + ' WHERE rowid=?',
                        (*changes.values(), row['_rowid_']),
                    )
        db.commit()
        db.execute('VACUUM')


def person_dependency_ids(entities, works, evidence):
    """作品演员表与所选证据的闭包；共享角色的其他作品不扩展其整份演员表。"""
    by_id = {item['id']: item for item in entities}
    kept = set(works)
    for item in entities:
        if item.get('kind') in {'role', 'stage'} and set(item.get('work_ids', [])) & works:
            kept.add(item['id'])
    for item in evidence:
        kept.update(item.get(key) for key in ('actor_id', 'role_id', 'stage_id') if item.get(key))
    pending = list(kept)
    while pending:
        item = by_id.get(pending.pop(), {})
        dependencies = [*item.get('actor_ids', []), *item.get('work_ids', [])]
        if item.get('actor_id'):
            dependencies.append(item['actor_id'])
        for identifier in dependencies:
            if identifier not in kept:
                kept.add(identifier)
                pending.append(identifier)
    return kept


def prune_person_catalogue(db):
    works = {r['work_id'] for r in db.execute('SELECT work_id FROM person_sources') if r['work_id']}
    entities = [{'id': r['id'], 'kind': r['kind'], **json.loads(r['data'])}
                for r in db.execute('SELECT * FROM person_entities')]
    evidence = [dict(r) for r in db.execute('SELECT * FROM person_evidence')]
    versions = []
    for row in db.execute('SELECT * FROM person_versions').fetchall():
        value = json.loads(row['data'])
        evidence.extend(value.get('evidence', []))
        ids = person_dependency_ids(value.get('entities', []), works, value.get('evidence', []))
        value['entities'] = [item for item in value.get('entities', []) if item['id'] in ids]
        versions.append((row, value))
    kept = person_dependency_ids(entities, works, evidence)
    for row, value in versions:
        kept.update(item['id'] for item in value['entities'])
        db.execute('UPDATE person_versions SET data=? WHERE id=?', (dumps(value), row['id']))
    for item in entities:
        if item['id'] not in kept:
            db.execute('DELETE FROM person_entities WHERE id=?', (item['id'],))
    imports, digests = set(), set()
    for row in db.execute('SELECT * FROM person_references').fetchall():
        role = row['role_id'] or row['stage_id']
        if row['actor_id'] not in kept or (role and role not in kept):
            db.execute('DELETE FROM person_references WHERE id=?', (row['id'],))
        else:
            digests.add(row['digest'])
            provenance = json.loads(row['data'])
            if provenance.get('import_id'):
                imports.add(provenance['import_id'])
    for row in db.execute('SELECT * FROM person_imports').fetchall():
        value = json.loads(row['data'])
        if value.get('work_id') not in works and row['id'] not in imports:
            db.execute('DELETE FROM person_imports WHERE id=?', (row['id'],))
    for row in db.execute('SELECT * FROM person_features WHERE source_id IS NULL').fetchall():
        keys = {fingerprint(['reference-v1', digest, json.loads(row['space'])]) for digest in digests}
        if row['kind'] != 'reference' or row['id'] not in keys:
            db.execute('DELETE FROM person_features WHERE id=?', (row['id'],))


def filter_database(path, selected: set[str] | None, *, allow_legacy_representations=False):
    """返回旧→新版本/表示映射；None 表示保留完整库。"""
    maps = {'indexes': {}, 'representations': {}, 'persons': {}, 'releases': {}}
    if selected is None:
        return maps
    with database(path) as db:
        db.row_factory = sqlite3.Row
        tables = schema(db)
        known = {r[0] for r in db.execute('SELECT id FROM sources')}
        removed = known - selected
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('CREATE TEMP TABLE chosen(id TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO chosen VALUES(?)', [(s,) for s in selected])
        for table in sorted(tables - {'sources', 'segments', 'index_members', 'representations'}):
            columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            if library_source_id(table, columns):
                db.execute(f'DELETE FROM {table} WHERE source_id IS NOT NULL AND source_id NOT IN chosen')
                if table in {'dialogue_tasks', 'runs'}:
                    db.execute(f'DELETE FROM {table} WHERE source_id IS NULL')
        db.execute('DELETE FROM media_groups WHERE NOT EXISTS '
                   '(SELECT 1 FROM media_group_members m WHERE m.group_id=media_groups.id)')
        db.execute('DELETE FROM source_curation WHERE id NOT IN chosen')
        db.execute('DELETE FROM sources WHERE id NOT IN chosen')
        db.execute('DELETE FROM segments WHERE source_id NOT IN chosen')
        db.execute('DELETE FROM segment_curation WHERE id NOT IN (SELECT id FROM segments)')
        db.execute('DELETE FROM annotations WHERE segment_id NOT IN (SELECT id FROM segments)')
        db.execute("DELETE FROM index_members WHERE json_extract(snapshot,'$.source_id') NOT IN chosen")
        db.execute('DELETE FROM representations WHERE NOT EXISTS (SELECT 1 FROM index_members m '
                   'WHERE m.index_id=representations.index_id AND m.segment_id=representations.segment_id)')
        for row in db.execute('SELECT * FROM person_imports').fetchall():
            if any(s in row['data'] for s in removed):
                db.execute('DELETE FROM person_imports WHERE id=?', (row['id'],))
        for version in db.execute('SELECT * FROM index_versions').fetchall():
            old = version['id']
            members = db.execute('SELECT * FROM index_members WHERE index_id=? ORDER BY segment_id', (old,)).fetchall()
            if not members:
                db.execute('DELETE FROM index_versions WHERE id=?', (old,))
                continue
            spec = json.loads(version['spec'])
            prepared = []
            by_segment = {}
            for rep in db.execute('SELECT * FROM representations WHERE index_id=? ORDER BY id', (old,)):
                by_segment.setdefault(rep['segment_id'], []).append(rep)
            for member in members:
                reps = by_segment.get(member['segment_id'], [])
                value, bases = representation_manifest(json.loads(member['snapshot']), spec, old, reps,
                                                       allow_legacy_representations=allow_legacy_representations)
                db.execute('UPDATE index_members SET snapshot=? WHERE index_id=? AND segment_id=?',
                           (dumps(value), old, member['segment_id']))
                prepared.append((reps, bases, value))
            spec['manifest'] = fingerprint([value for _, _, value in prepared])
            # 即使某个历史版本本来只含所选素材，也建立迁移版本，保留清晰的旧新映射。
            spec['migration'] = {'origin': old, 'sources': sorted(selected)}
            new = fingerprint(spec)
            maps['indexes'][old] = new
            db.execute('UPDATE index_versions SET id=?,spec=? WHERE id=?', (new, dumps(spec), old))
            for reps, bases, _ in prepared:
                for rep in reps:
                    rid = fingerprint([new, bases[rep['id']]])
                    maps['representations'][rep['id']] = rid
                    db.execute('UPDATE representations SET id=?,index_id=? WHERE id=?', (rid, new, rep['id']))
            db.execute('UPDATE index_members SET index_id=? WHERE index_id=?', (new, old))
        for row in db.execute('SELECT * FROM person_versions').fetchall():
            value = json.loads(row['data'])
            for key in ('evidence', 'coverage'):
                value[key] = [item for item in value.get(key, []) if item.get('source_id') in selected]
            db.execute('UPDATE person_versions SET data=? WHERE id=?', (dumps(value), row['id']))
        prune_person_catalogue(db)
        for row in db.execute('SELECT * FROM person_versions').fetchall():
            value = json.loads(row['data'])
            new = fingerprint(value)
            maps['persons'][row['id']] = new
            db.execute('DELETE FROM person_versions WHERE id=?', (row['id'],))
            db.execute('INSERT OR IGNORE INTO person_versions VALUES(?,?,?)', (new, dumps(value), row['created_at']))
        for row in db.execute('SELECT * FROM search_releases').fetchall():
            db.execute('DELETE FROM search_releases WHERE id=?', (row['id'],))
            content = maps['indexes'].get(row['content_id'])
            if not content:
                continue
            person = maps['persons'].get(row['person_id'], row['person_id'])
            new = fingerprint([content, person])
            maps['releases'][row['id']] = new
            db.execute('INSERT OR IGNORE INTO search_releases VALUES(?,?,?,?)',
                       (new, content, person, row['created_at']))
        ids = {k: v for mapping in maps.values() for k, v in mapping.items()}
        for row in db.execute('SELECT key,value FROM settings').fetchall():
            if row['key'] == 'active' and row['value'] not in maps['indexes']:
                db.execute('DELETE FROM settings WHERE key IN (?,?)', ('active', 'active.release'))
            elif row['key'] == 'active.release' and row['value'] not in maps['releases'] or row['key'].startswith('index_report.'):
                db.execute('DELETE FROM settings WHERE key=?', (row['key'],))
    rewrite_database(path, lambda s: ids.get(s, s), secrets=True)
    return maps


def interrupt_tasks(path):
    with database(path) as db:
        for table in ('dialogue_tasks', 'source_tasks', 'person_tasks'):
            db.execute(f"UPDATE {table} SET status='interrupted',error='迁移时任务已中断；请手动重试' "
                       "WHERE status IN ('queued','running')")
        db.execute("UPDATE runs SET status='interrupted' WHERE status IN ('queued','running')")
        db.execute("UPDATE topic_runs SET status='interrupted',error='迁移时任务已中断；请手动重试' "
                   "WHERE status IN ('queued','running')")


def validate_database(root, *, basic=False, legacy=False):
    store = store_at(root)
    with store.connect() as db:
        # legacy=True 只要求核心表：旧结构备份在导入暂存区会按版本化迁移升级到当前结构。
        schema(db, require_current=not legacy)
        check = 'quick_check' if basic else 'integrity_check'
        if db.execute(f'PRAGMA {check}').fetchone()[0] != 'ok':
            raise ValueError('SQLite 完整性检查失败')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('SQLite 外键检查失败')
        for table in sorted(TABLES):
            columns = {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}
            if library_source_id(table, columns) and db.execute(
                f'SELECT 1 FROM {table} t WHERE t.source_id IS NOT NULL AND NOT EXISTS '
                '(SELECT 1 FROM sources s WHERE s.id=t.source_id)'
            ).fetchone():
                raise ValueError(f'{table} 存在缺失的素材依赖')
        if 'media_group_members' in schema(db, require_current=not legacy):
            invalid = db.execute(
                "SELECT 1 FROM media_group_members m JOIN media_groups g ON g.id=m.group_id "
                "WHERE (g.type='source' AND m.source_id IS NULL) OR (g.type='music' AND m.music_id IS NULL) LIMIT 1"
            ).fetchone()
            if invalid:
                raise ValueError('组成员类型与组类型不符')
        if basic and db.execute(
            "SELECT 1 FROM index_members m WHERE json_extract(snapshot,'$.id') IS NOT segment_id "
            "OR NOT EXISTS (SELECT 1 FROM sources s WHERE s.id=json_extract(m.snapshot,'$.source_id')) LIMIT 1"
        ).fetchone():
            raise ValueError('历史索引成员的身份或素材依赖不一致')
        for row in (() if basic else db.execute('SELECT segment_id,snapshot FROM index_members')):
            member = json.loads(row['snapshot'])
            if (member['id'] != row['segment_id'] or not db.execute(
                'SELECT 1 FROM sources WHERE id=?', (member['source_id'],)
            ).fetchone()):
                raise ValueError('历史索引成员的身份或素材依赖不一致')
        for table, query in {
            'representations': 'SELECT 1 FROM representations r LEFT JOIN index_members m ON '
                'm.index_id=r.index_id AND m.segment_id=r.segment_id WHERE m.index_id IS NULL',
            'index_members': 'SELECT 1 FROM index_members m LEFT JOIN index_versions v ON '
                'v.id=m.index_id WHERE v.id IS NULL',
            'search_releases': 'SELECT 1 FROM search_releases r LEFT JOIN index_versions v ON '
                'v.id=r.content_id LEFT JOIN person_versions p ON p.id=r.person_id '
                'WHERE v.id IS NULL OR (r.person_id IS NOT NULL AND p.id IS NULL)',
        }.items():
            if db.execute(query).fetchone():
                raise ValueError(f'{table} 存在缺失依赖')
        for key, table in [('active', 'index_versions'), ('active.release', 'search_releases')]:
            if db.execute(f'SELECT 1 FROM settings s WHERE s.key=? AND NOT EXISTS '
                          f'(SELECT 1 FROM {table} t WHERE t.id=s.value)', (key,)).fetchone():
                raise ValueError(f'活动指针无效：{key}')


def merge_database(target, incoming, *, restore_config):
    """仅写暂存库；同 ID 内容冲突一律拒绝。整数 ID 在关联引用前分配。"""
    src, dst = store_at(incoming), store_at(target)
    remaps = {'model_profiles': {}, 'voices': {}, 'runs': {}, 'media_groups': {}}
    with dst.connect() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        for table in ('model_profiles', 'voices', 'runs'):
            rows = src.rows(f'SELECT * FROM {table} ORDER BY id')
            for row in rows:
                old = row['id']
                equivalent = None
                if table != 'runs':
                    query = 'name=?' + (' AND role=?' if table == 'model_profiles' else '')
                    args = (row['name'], row['role']) if table == 'model_profiles' else (row['name'],)
                    existing = db.execute(f'SELECT * FROM {table} WHERE {query}', args).fetchone()
                    if existing:
                        ignored = {'id', 'created_at', 'updated_at'}
                        if all(same_value(existing[k], v) for k, v in row.items() if k not in ignored):
                            equivalent = existing['id']
                        else:
                            # name 是本地唯一展示名；整数 ID 不具有跨库身份。
                            suffix = fingerprint({k: v for k, v in row.items() if k not in ignored})[:8]
                            row['name'] = f'{row["name"]}（导入 {suffix}）'
                            renamed = db.execute(f'SELECT * FROM {table} WHERE {query}',
                                                 (row['name'], row['role']) if table == 'model_profiles'
                                                 else (row['name'],)).fetchone()
                            if renamed:
                                if any(not same_value(renamed[k], v) for k, v in row.items() if k not in ignored):
                                    raise ValueError(f'配置或音色名称冲突：{row["name"]}')
                                equivalent = renamed['id']
                if equivalent is None:
                    values = {k: v for k, v in row.items() if k != 'id'}
                    cursor = db.execute(f'INSERT INTO {table}(' + ','.join(values) + ') VALUES('
                                        + ','.join('?' for _ in values) + ')', tuple(values.values()))
                    equivalent = cursor.lastrowid
                remaps[table][old] = equivalent
        # 组的稳定 ID 保留；同类型同名组沿用目标库 ID，并映射任务与成员引用。
        for row in src.rows('SELECT * FROM media_groups ORDER BY id'):
            existing = db.execute('SELECT * FROM media_groups WHERE id=?', (row['id'],)).fetchone()
            if existing:
                canonical_name = f"{row['name']}（导入 {row['id'][:8]}）"
                if any(not same_value(existing[k], v) for k, v in row.items()
                       if k != 'updated_at' and not (k == 'name' and existing[k] == canonical_name)):
                    raise ValueError(f"同 ID 组内容冲突：{row['id']}")
                remaps['media_groups'][row['id']] = row['id']
                continue
            named = db.execute('SELECT * FROM media_groups WHERE type=? AND name=?',
                               (row['type'], row['name'])).fetchone()
            if named:
                def members(database, group_id):
                    return {(item['source_id'], item['music_id']) for item in database.execute(
                        'SELECT source_id,music_id FROM media_group_members WHERE group_id=?', (group_id,))}

                incoming_members = {(item['source_id'], item['music_id']) for item in src.rows(
                    'SELECT source_id,music_id FROM media_group_members WHERE group_id=?', (row['id'],))}
                if named['description'] == row['description'] and members(db, named['id']) == incoming_members:
                    remaps['media_groups'][row['id']] = named['id']
                    continue
                # 同名不等于同一范围：保留导入组身份，避免把已有成员并入任务范围。
                row['name'] += f"（导入 {row['id'][:8]}）"
            db.execute('INSERT INTO media_groups(id,type,name,description,updated_at) VALUES(?,?,?,?,?)',
                       tuple(row[k] for k in ('id', 'type', 'name', 'description', 'updated_at')))
            remaps['media_groups'][row['id']] = row['id']
        # sources 必须先于带外键的依赖行；其余关联由最终的全库检查兜底。
        for table in ['sources', *sorted(TABLES - {'sources', 'model_profiles', 'voices', 'runs', 'media_groups'})]:
            info = db.execute(f'PRAGMA table_info({table})').fetchall()
            keys = [r['name'] for r in info if r['pk']]
            for row in src.rows(f'SELECT * FROM {table}'):
                if table == 'media_group_members':
                    from mindex.shared.media_groups import membership_id

                    row['group_id'] = remaps['media_groups'].get(row['group_id'], row['group_id'])
                    row['id'] = membership_id(row['group_id'], row['source_id'] or row['music_id'])
                for column, value in row.items():
                    parsed = json_container(value)
                    if parsed is not None:
                        row[column] = dumps(remap_local_refs(parsed, remaps))
                if table == 'settings':
                    if row['key'] in {'active', 'active.release'}:
                        continue
                    if not restore_config:
                        continue
                    if row['key'].startswith('active.profile.'):
                        row['value'] = str(remaps['model_profiles'].get(int(row['value']), int(row['value'])))
                    db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (row['key'], row['value']))
                    continue
                existing = db.execute(f'SELECT * FROM {table} WHERE ' + ' AND '.join(f'{k}=?' for k in keys),
                                      tuple(row[k] for k in keys)).fetchone()
                if existing:
                    ignored = {'created_at', 'updated_at'}
                    if any(not same_value(existing[k], v) for k, v in row.items() if k not in ignored):
                        raise ValueError(f'同 ID 对象内容冲突：{table}/{[row[k] for k in keys]}')
                else:
                    db.execute(f'INSERT INTO {table}(' + ','.join(row) + ') VALUES('
                               + ','.join('?' for _ in row) + ')', tuple(row.values()))
    return remaps


def same_value(first, second):
    if first == second:
        return True
    if isinstance(first, str) and isinstance(second, str):
        left, right = json_container(first), json_container(second)
        if left is not None and right is not None:
            return left == right
    return False


def remap_local_refs(value, remaps):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {'source_group_ids', 'music_group_ids', 'group_ids'} and isinstance(item, list):
                result[key] = [remaps.get('media_groups', {}).get(group_id, group_id) for group_id in item]
                continue
            if key == '_credential_profiles' and isinstance(item, dict):
                profiles = {}
                for role, reference in item.items():
                    pointer = reference.get('value') if isinstance(reference, dict) else reference
                    mapped = remaps['model_profiles'].get(int(pointer), pointer) if pointer else pointer
                    mapped = str(mapped) if isinstance(pointer, str) else mapped
                    profiles[role] = {**reference, 'value': mapped} if isinstance(reference, dict) else mapped
                result[key] = profiles
                continue
            table = {'profile_id': 'model_profiles', 'model_profile_id': 'model_profiles',
                     'voice_pk': 'voices', 'run_id': 'runs', 'audit_id': 'runs'}.get(key)
            if table and isinstance(item, (int, str)) and str(item).isdigit():
                mapped = remaps[table].get(int(item), item)
                result[key] = str(mapped) if isinstance(item, str) else mapped
            else:
                result[key] = remap_local_refs(item, remaps)
        return result
    if isinstance(value, list):
        return [remap_local_refs(item, remaps) for item in value]
    return value
