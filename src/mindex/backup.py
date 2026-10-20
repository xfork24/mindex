"""离线、可验证的库迁移；发布前的一切工作只发生在同文件系统暂存区。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

import yaml
from filelock import FileLock, Timeout

from .backup_db import (
    SECRET_FIELDS,
    TABLES,
    filter_database,
    interrupt_tasks,
    json_container,
    merge_database,
    redact,
    remap_local_refs,
    rewrite_database,
    snapshot,
    store_at,
    validate_database,
    walk,
)
from .config import Config, fingerprint
from .processing_lock import SharedFileLock
from .storage import Store, dumps

FORMAT_VERSION = 1
EXCLUDED_DIRS = {'chroma', 'locks', 'models', 'model-cache', '.cache', '__pycache__'}
MODEL_SUFFIXES = {'.gguf', '.safetensors', '.onnx', '.pt', '.pth'}
TEXT_SUFFIXES = {'.json', '.yaml', '.yml', '.txt', '.log', '.jsonl'}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(root, relative):
    if not isinstance(relative, str) or '\\' in relative:
        raise ValueError(f'无效备份路径：{relative}')
    parts = PurePosixPath(relative)
    if parts.is_absolute() or not parts.parts or any(p in {'.', '..'} for p in parts.parts):
        raise ValueError(f'备份路径越界：{relative}')
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'备份路径越界：{relative}')
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f'备份不接受符号链接：{relative}')
    return path


def files_under(root):
    if not root.exists():
        return []
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f'数据目录不接受符号链接：{path.relative_to(root)}')
        for name in names:
            path = Path(directory) / name
            if not path.is_file():
                raise ValueError(f'数据目录包含非普通文件：{path.relative_to(root)}')
            files.append(path)
    return sorted(files)


def excluded(relative):
    path = Path(relative)
    if path.name == '.DS_Store':
        return 'macOS Finder 目录显示元数据'
    if set(path.parts) & EXCLUDED_DIRS:
        return '向量内部文件、模型目录或锁目录（向量经 API 单独导出）'
    if path.name.startswith('metadata.sqlite3') and path.name in {
        'metadata.sqlite3', 'metadata.sqlite3-wal', 'metadata.sqlite3-shm',
    }:
        return '数据库通过 SQLite backup API 单独保存'
    if path.suffix in MODEL_SUFFIXES:
        return '模型权重需单独补充'
    if path.name == '.env' or path.name.startswith('.env.'):
        return '环境凭据文件不属于备份；请在目标环境另行配置'
    if path.name.endswith(('-wal', '-shm', '-journal')):
        return 'SQLite 事务旁路文件由一致快照替代，禁止原样携带'
    if path.suffix in {'.lock', '.partial', '.tmp', '.temp'} or path.name.startswith('.tmp'):
        return '锁或未完成临时文件'
    if any(part.startswith(('asr-', 'align-', 'embed-', 'query-', 'dialogue-')) for part in path.parts[:-1]):
        return '未完成临时工作目录'
    return None


@contextmanager
def offline(root):
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ExitStack() as stack:
            migration_lock = root.parent / f'.{root.name}.migration.lock'
            if migration_lock.is_symlink():
                raise ValueError('迁移锁不能是符号链接')
            stack.enter_context(FileLock(str(migration_lock), timeout=0))
            if root.exists():
                for relative in ('pipeline.lock', 'locks/library.lock', 'sources/.register.lock',
                                 'index-write.lock', 'studio/studio.lock'):
                    safe_path(root, relative)
                stack.enter_context(SharedFileLock(root / 'pipeline.lock', exclusive=True))
                stack.enter_context(SharedFileLock(root / 'locks/library.lock', exclusive=True))
                (root / 'sources').mkdir(exist_ok=True)
                stack.enter_context(FileLock(str(root / 'sources/.register.lock'), timeout=0))
                stack.enter_context(FileLock(str(root / 'index-write.lock'), timeout=0))
                (root / 'studio').mkdir(exist_ok=True)
                stack.enter_context(FileLock(str(root / 'studio/studio.lock'), timeout=0))
            yield
    except Timeout as exc:
        raise ValueError('迁移锁被占用；请停止 Web 和全部后台写入任务后重试') from exc


def vector_worker(action, root, bundle, **options):
    request = {'action': action, 'root': str(root), 'bundle': str(bundle), **options}
    # 子进程退出后才能移动目录；不触碰 Chroma 的私有 shutdown API。
    with tempfile.TemporaryDirectory(prefix='mindex-vector-') as temporary:
        path = Path(temporary) / 'request.json'
        path.write_text(dumps(request), encoding='utf-8')
        result = subprocess.run(
            [sys.executable, '-m', 'mindex.backup_vectors', str(path)],
            capture_output=True, text=True, check=False,
        )
    try:
        response = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError('向量迁移进程异常退出：' + result.stderr[-1000:]) from None
    if result.returncode or 'error' in response:
        raise ValueError('向量迁移失败：' + response.get('error', result.stderr[-1000:]))
    return response['result']


def source_list(root, *, original_names=False):
    name = 's.name' if original_names else "COALESCE(NULLIF(c.display_name,''),s.name)"
    return store_at(root).rows(
        f'SELECT s.id,{name} AS name,s.path,s.prepared FROM sources s '
        'LEFT JOIN source_curation c ON c.id=s.id ORDER BY name,s.id'
    )


def selection(available, all_sources, sources):
    if all_sources and sources:
        raise ValueError('--all 和 --source 不能同时使用')
    known = {row['id'] for row in available}
    if all_sources:
        return None
    selected = set(sources or [])
    if not selected:
        raise ValueError('请指定 --all 或重复使用 --source 素材ID')
    if selected - known:
        raise ValueError('找不到素材：' + '、'.join(sorted(selected - known)))
    return selected


def _strings(value):
    result = []
    walk(value, lambda item: result.append(item) if isinstance(item, str) else item)
    return result


def job_selection(root, selected, known):
    included, omitted = {}, []
    store = store_at(root)
    versions = {row['id']: set() for row in store.rows('SELECT id FROM index_versions')}
    tokens = {row['id']: row['source_id'] for row in store.rows('SELECT id,source_id FROM segments')}
    for row in store.rows('SELECT index_id,snapshot FROM index_members'):
        member = json.loads(row['snapshot'])
        versions.setdefault(row['index_id'], set()).add(member['source_id'])
        tokens[member['id']] = member['source_id']
    for row in store.rows('SELECT id,content_id FROM search_releases'):
        versions[row['id']] = versions.get(row['content_id'], set())
    for path in sorted((root / 'studio').glob('*/job.json')):
        state = json.loads(path.read_text())
        refs = set(state.get('source_ids') or [])
        historical = set()
        for artifact in path.parent.rglob('*.json'):
            text = artifact.read_text()
            refs.update(s for s in known if s in text)
            refs.update(s for token, s in tokens.items() if token in text)
            historical.update(v for v in versions if v in text)
        valid_history = selected is None or all(versions[v] & selected for v in historical)
        if refs <= known and valid_history and (selected is None or state.get('source_ids') and refs <= selected):
            included[path.parent.name] = sorted(refs or known)
        else:
            omitted.append({'job_id': path.parent.name, 'reason': '素材依赖不完整或无法确定范围',
                            'sources': sorted(refs)})
    return included, omitted


def file_owners(root, all_sources):
    store = store_at(root)
    owners = {}
    tokens = {r['id']: {r['id']} for r in all_sources}
    for row in store.rows('SELECT id,source_id FROM segments'):
        tokens[row['id']] = {row['source_id']}
    for table in ('source_tasks', 'dialogue_tasks', 'person_tasks'):
        for row in store.rows(f'SELECT id,source_id FROM {table}'):
            if row['source_id']:
                tokens[row['id']] = {row['source_id']}
    for path in files_under(root):
        relative = path.relative_to(root).as_posix()
        assigned = set()
        for part in Path(relative).parts:
            assigned.update(tokens.get(part, set()))
            assigned.update(tokens.get(Path(part).stem, set()))
        if path.suffix in {'.json', '.yaml', '.yml'} and path.stat().st_size < 16 * 1024**2:
            text = path.read_text(encoding='utf-8')
            assigned.update(s['id'] for s in all_sources if s['id'] in text)
        owners[relative] = assigned
    # 注册路径可以不使用约定目录布局。
    for table in sorted(TABLES):
        for row in store.rows(f'SELECT * FROM {table}'):
            sid = row.get('source_id') or (row.get('id') if table == 'sources' else None)
            if not sid:
                continue
            for value in row.values():
                if not isinstance(value, str):
                    continue
                try:
                    values = _strings(json.loads(value))
                except ValueError:
                    values = [value]
                for item in values:
                    if item in owners:
                        owners[item].add(sid)
    return owners


def referenced_files(root, database, *, available_files=None):
    result = set()
    available = (set(available_files) if available_files is not None else
                 {path.relative_to(root).as_posix() for path in files_under(root)})
    store = store_at(database.parent)
    for table in sorted(TABLES):
        for row in store.rows(f'SELECT * FROM {table}'):
            for value in row.values():
                if not isinstance(value, str):
                    continue
                try:
                    values = _strings(json.loads(value))
                except ValueError:
                    values = [value]
                for item in values:
                    if not item or '\n' in item or len(item) > 4096:
                        continue
                    path = Path(item)
                    if path.is_absolute():
                        if not path.is_relative_to(root):
                            continue
                        item = path.relative_to(root).as_posix()
                    # 描述/日志也可能包含斜杠、小数点；仅对实际文件清单中的路径做检查。
                    # 必需媒体的缺失与越界由 validate_media 单独严格校验。
                    if item in available:
                        safe_path(root, item)
                        result.add(item)
    return result


def validate_media(root):
    store = store_at(root)
    virtual = {r['path'] for r in store.rows('SELECT path FROM media_references')}
    paths = []
    for row in store.rows('SELECT path FROM sources'):
        paths.append(row['path'])
    for row in store.rows('SELECT path,thumbnail FROM segments'):
        paths.extend(v for v in row.values() if v and v not in virtual)
    for row in store.rows('SELECT snapshot FROM index_members'):
        value = json.loads(row['snapshot'])
        paths.extend(value[k] for k in ('path', 'thumbnail', 'source_path') if value.get(k) and value[k] not in virtual)
    for row in store.rows('SELECT path FROM person_references'):
        paths.append(row['path'])
    for row in store.rows('SELECT sample_path FROM voices WHERE sample_path IS NOT NULL'):
        paths.append(row['sample_path'])
    for row in store.rows('SELECT refs FROM media_references'):
        paths.extend(item['path'] for item in json.loads(row['refs']))
    for relative in paths:
        if not safe_path(root, relative).is_file():
            raise ValueError(f'必需媒体文件缺失：{relative}')


def _saved_secrets(root):
    result = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower().split('.')[-1] in SECRET_FIELDS and isinstance(item, str) and item:
                    result.add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for table in sorted(TABLES):
        for row in store_at(root).rows(f'SELECT * FROM {table}'):
            visit(row)
            if table == 'settings' and row['key'].lower().split('.')[-1] in SECRET_FIELDS and row['value']:
                result.add(row['value'])
            for value in row.values():
                parsed = json_container(value)
                if parsed is not None:
                    visit(parsed)
    return sorted(result, key=len, reverse=True)


def _copy_payload_file(origin, destination, *, transform, include_secrets, secrets, selected):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with origin.open('rb') as stream:
        sqlite = stream.read(16) == b'SQLite format 3\x00'
    if sqlite:
        # 历史 SQLite 文件可以早于当前 schema；仅升级隔离副本，不写回历史原件。
        with tempfile.TemporaryDirectory(prefix='.mindex-history-', dir=destination.parent) as temporary:
            working = Path(temporary) / 'metadata.sqlite3'
            snapshot(origin.resolve(), working, allow_legacy=True)
            Store(working.parent)
            snapshot(working, destination)
        maps = filter_database(destination, selected)
        ids = {k: v for mapping in maps.values() for k, v in mapping.items()}
        rewrite_database(destination, lambda s: transform(ids.get(s, s)), secrets=include_secrets)
        return
    if origin.suffix in TEXT_SUFFIXES:
        text = origin.read_text(encoding='utf-8')
        if origin.suffix in {'.json', '.yaml', '.yml'}:
            original = json.loads(text) if origin.suffix == '.json' else yaml.safe_load(text)
            value = walk(original if include_secrets else redact(original), transform)
            if not include_secrets:
                def sanitize(item):
                    if not isinstance(item, str):
                        return item
                    for secret in secrets:
                        if len(secret) >= 8 or item == secret:
                            item = item.replace(secret, '[已移除密钥]')
                    return item
                value = walk(value, sanitize)
            if value != original:
                text = dumps(value) if origin.suffix == '.json' else yaml.safe_dump(value, allow_unicode=True)
        elif not include_secrets:
            for secret in secrets:
                text = text.replace(secret, '[已移除密钥]')
        destination.write_text(text, encoding='utf-8')
    else:
        shutil.copy2(origin, destination)


def _config_dependencies(config, base_dir, payload):
    paths, missing, env = {}, [], set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'api_key_env' and item:
                    env.add(item)
                elif key == 'model_root' and item:
                    missing.append({'path': item, 'reason': '模型目录需在目标机器补充'})
                elif key in {'deployment_manifest', 'font_file'} and item:
                    original = Path(item).expanduser()
                    original = (base_dir / original).resolve()
                    if not original.is_file():
                        raise ValueError(f'配置依赖文件缺失：{original}')
                    relative = f'config-dependencies/{digest(original)[:24]}/{original.name}'
                    destination = payload / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _copy_payload_file(original, destination, transform=lambda v: v, include_secrets=False,
                                       secrets=[], selected=None)
                    paths[str(original)] = relative
                    paths[item] = relative
                    if key == 'deployment_manifest':
                        pinned = json.loads(original.read_text())
                        missing.append({'path': str(original), 'reason': '部署清单已保存；权重和服务进程需另行部署',
                                        'file_identity': pinned.get('file_identity')})
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(config)
    return paths, missing, sorted(env)


def compatibility_files(root, database):
    """已建索引所依赖的固定输入与验证证据；不调用模型或生成通过状态。"""
    from .compatibility import load_baseline, location
    from .deployment import verified_file

    required, seen = set(), set()
    for row in store_at(database.parent).rows('SELECT spec FROM index_versions'):
        space = json.loads(row['spec']).get('space', {})
        space_id = fingerprint(space)
        if space.get('schema') != 2 or space_id in seen:
            continue
        seen.add(space_id)
        config = Config(data_dir=root)
        baseline = load_baseline(config, space)
        directory = location(config, space)
        paths = [directory / 'baseline.json', directory / 'baseline.sha256']
        paths.extend(safe_path(root, sample['path']) for sample in baseline['samples'])
        for gate in (directory / 'deployments').glob('*.json'):
            marker = gate.with_suffix('.sha256')
            verified_file(gate, marker.read_text().strip())
            paths.extend((gate, marker))
        for path in paths:
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            required.add(relative)
    return required


def immutable_evidence(relative):
    # 清单描述模型服务器上的路径；不能当作客户端数据路径重定位。
    return relative.startswith(('capabilities/v2/', 'config-dependencies/', 'deployments/'))


def build_bundle(config, output, *, selected, include_secrets, config_path):
    root = config.data_dir.resolve()
    payload = output / 'data'
    payload.mkdir(parents=True)
    known = source_list(root)
    chosen = {s['id'] for s in known} if selected is None else selected
    jobs, omitted = job_selection(root, selected, {s['id'] for s in known})
    snapshot(store_at(root).path, payload / 'metadata.sqlite3')
    maps = filter_database(payload / 'metadata.sqlite3', selected)
    ids = {k: v for mapping in maps.values() for k, v in mapping.items()}
    transform = lambda value: ids.get(value, value) if isinstance(value, str) else value
    secrets = _saved_secrets(root)
    rewrite_database(payload / 'metadata.sqlite3', transform, secrets=include_secrets)
    base = yaml.safe_load(config_path.read_text()) if config_path.is_file() else config.model_dump(mode='json')
    base = base or {}
    if not include_secrets:
        base = redact(base)
        # Service.api_key 允许字符串；被删除的 YAML 字段直接去掉。
        def clean(value):
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if v is not None or k != 'api_key'}
            return value
        base = clean(base)
    configs = [config.model_dump(mode='json'), base]
    for table in ('model_profiles', 'dialogue_tasks', 'source_tasks', 'person_tasks'):
        configs.extend(json.loads(row['config']) for row in store_at(payload).rows(f'SELECT config FROM {table}'))
    for path in (root / 'studio').glob('*/job.json'):
        if path.parent.name in jobs:
            configs.append(json.loads(path.read_text()))
    deps, missing, env = _config_dependencies(configs, config_path.parent, payload)
    owners = file_owners(root, known)
    references = referenced_files(root, payload / 'metadata.sqlite3', available_files=owners)
    technical = compatibility_files(root, payload / 'metadata.sqlite3')
    exclusions = []
    global_dirs = ('voices/', 'config-dependencies/')
    for origin in files_under(root):
        relative = origin.relative_to(root).as_posix()
        reason = excluded(relative)
        assigned = owners.get(relative, set())
        if relative in technical:
            reason = None
        elif selected is not None and not reason:
            if relative.startswith('studio/'):
                parts = Path(relative).parts
                if len(parts) < 3 or parts[1] not in jobs:
                    reason = '创作任务依赖不完整或未知'
            elif assigned - chosen:
                reason = '文件引用未选素材'
            elif not (assigned or relative in references or relative.startswith(global_dirs)):
                reason = '部分备份不包含归属未知的历史文件或缓存'
        if reason:
            exclusions.append({'path': relative, 'reason': reason})
            continue
        if relative in technical or immutable_evidence(relative):
            destination = payload / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            # 证据摘要和部署 ID 绑定原始字节，禁止重写已验证内容。
            if origin.suffix == '.json' and not include_secrets:
                text = origin.read_text()
                value = json.loads(text)
                if redact(value) != value or any(secret in text for secret in secrets):
                    raise ValueError(f'兼容性证据含凭据，不能脱敏后沿用验证：{relative}')
            shutil.copy2(origin, destination)
            continue
        _copy_payload_file(origin, payload / relative, transform=transform, include_secrets=include_secrets,
                           secrets=secrets, selected=selected)
    (output / 'config').mkdir()
    (output / 'config/base.yaml').write_text(yaml.safe_dump(base, allow_unicode=True), encoding='utf-8')
    validate_database(payload)
    validate_media(payload)
    collections = vector_worker(
        'dump', payload, output, origin=str(root),
        full=selected is None,
        original_indexes={v: k for k, v in maps['indexes'].items()},
        original_representations={v: k for k, v in maps['representations'].items()},
    )
    manifest = {
        'format': 'mindex-backup', 'version': FORMAT_VERSION, 'created_at': datetime.now(UTC).isoformat(),
        'full': selected is None, 'includes_secrets': include_secrets,
        'origin': {'data_dir': str(root), 'config': str(config_path)},
        'sources': [s for s in known if s['id'] in chosen], 'collections': collections,
        'id_maps': maps, 'path_map': deps, 'jobs': jobs, 'excluded_jobs': omitted,
        'excluded': exclusions, 'required_environment': env, 'missing_dependencies': missing,
        'files': [],
    }
    for path in files_under(output):
        if path.name == '.DS_Store':
            continue
        relative = path.relative_to(output).as_posix()
        manifest['files'].append({'path': relative, 'bytes': path.stat().st_size, 'sha256': digest(path),
                                  'sources': sorted(owners.get(relative.removeprefix('data/'), set()) & chosen)})
    (output / 'manifest.json').write_text(dumps(manifest), encoding='utf-8')
    return manifest


def inspect_bundle(bundle):
    try:
        return _inspect_bundle(bundle)
    except json.JSONDecodeError as exc:
        raise ValueError(f'备份 JSON 无效：{exc}') from exc
    except (KeyError, TypeError, AttributeError, IndexError, sqlite3.DatabaseError) as exc:
        raise ValueError(f'备份结构或数据库无效：{exc}') from exc


def _inspect_bundle(bundle):
    bundle = bundle.resolve()
    manifest = json.loads(safe_path(bundle, 'manifest.json').read_text())
    if (not isinstance(manifest, dict) or manifest.get('format') != 'mindex-backup'
            or type(manifest.get('version')) is not int or manifest.get('version') != FORMAT_VERSION):
        raise ValueError('不支持的备份格式或版本')
    listed = set()
    for item in manifest['files']:
        path = safe_path(bundle, item['path'])
        if path.name.endswith(('-wal', '-shm', '-journal')):
            raise ValueError('备份不接受 SQLite 事务旁路文件')
        if item['path'] in listed:
            raise ValueError(f'清单包含重复路径：{item["path"]}')
        listed.add(item['path'])
        if not path.is_file() or path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise ValueError(f'备份文件缺失或校验失败：{item["path"]}')
    if not {'data/metadata.sqlite3', 'config/base.yaml'} <= listed:
        raise ValueError('备份缺少数据库或基础配置')
    actual = {p.relative_to(bundle).as_posix() for p in files_under(bundle)} - {'manifest.json'}
    unregistered = sorted(path for path in actual - listed if PurePosixPath(path).name != '.DS_Store')
    if unregistered:
        raise ValueError('备份含未登记文件：' + '、'.join(unregistered))
    indexes = {r['id'] for r in store_at(bundle / 'data').rows('SELECT id FROM index_versions')}
    if {item['index_id'] for item in manifest['collections']} != indexes:
        raise ValueError('向量集合与数据库的索引版本不一致')
    if len(manifest['collections']) != len(indexes):
        raise ValueError('备份含重复向量集合')
    for collection in manifest['collections']:
        if any(name not in listed for name in collection['batches']):
            raise ValueError('备份缺少向量批次')
        identifiers = set()
        count = 0
        for name in collection['batches']:
            rows = json.loads(safe_path(bundle, name).read_text())
            count += len(rows)
            identifiers.update(r['id'] for r in rows)
        if count != collection['count'] or len(identifiers) != count:
            raise ValueError('向量批次数量或 ID 不一致')
        expected = {r['id'] for r in store_at(bundle / 'data').rows(
            'SELECT id FROM representations WHERE index_id=?', (collection['index_id'],)
        )}
        if identifiers != expected:
            raise ValueError('备份缺少必需向量或包含未知表示')
    for relative in manifest['path_map'].values():
        safe_path(bundle / 'data', relative)
        if 'data/' + relative not in listed:
            raise ValueError('备份缺少配置依赖')
    current_sources = {row['id']: row for row in source_list(bundle / 'data')}
    original_sources = {row['id']: row for row in source_list(bundle / 'data', original_names=True)}
    # v1 早期备份的清单使用原始文件名；不改变其数据库中的人工命名。
    recorded_sources = manifest['sources']
    if (len(recorded_sources) != len(current_sources)
            or len({row['id'] for row in recorded_sources}) != len(recorded_sources)
            or any(row not in (current_sources.get(row['id']), original_sources.get(row['id']))
                   for row in recorded_sources)):
        raise ValueError('备份素材清单与数据库不一致')
    validate_database(bundle / 'data')
    validate_media(bundle / 'data')
    manifest['sources'] = list(current_sources.values())
    return manifest


def summary(manifest):
    return {key: manifest[key] for key in (
        'version', 'full', 'includes_secrets', 'sources', 'collections', 'id_maps', 'jobs', 'excluded_jobs',
        'required_environment', 'missing_dependencies',
    )} | {'files': len(manifest['files']), 'bytes': sum(f['bytes'] for f in manifest['files']),
         'excluded': manifest['excluded'], 'configuration': '基础 YAML 与 SQLite 运行时覆盖层分别保存'}


def _combined_maps(original, current, selected):
    combined = {category: dict(mapping) for category, mapping in current.items()}
    for category, mapping in original.items():
        for old, intermediate in mapping.items():
            if selected is None or intermediate in current[category]:
                combined[category][old] = current[category].get(intermediate, intermediate)
    return combined


def export_backup(config, destination, *, all_sources=False, sources=None, include_secrets=False,
                  dry_run=False, config_path=None, progress=None):
    root, destination = config.data_dir.resolve(), destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('导出目标必须不存在')
    if destination.resolve().is_relative_to(root):
        raise ValueError('不能备份到数据目录内部')
    config_path = (config_path or Path('config.local.yaml')).resolve()
    selected = selection(source_list(root), all_sources, sources)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with offline(root), tempfile.TemporaryDirectory(prefix='.mindex-export-', dir=destination.parent) as temporary:
        if progress:
            progress('正在创建 SQLite 一致快照、计算素材依赖并导出向量…')
        staging = Path(temporary) / 'bundle'
        staging.mkdir()
        manifest = build_bundle(config, staging, selected=selected, include_secrets=include_secrets,
                                config_path=config_path)
        inspect_bundle(staging)
        if not dry_run:
            # 再检查一次，避免构建期间意外覆盖他人创建的目录。
            if destination.exists():
                raise ValueError('导出目标已存在')
            _sync_tree(staging)
            staging.rename(destination)
            _fsync_directory(destination.parent)
        return summary(manifest) | {'ok': True, 'dry_run': dry_run, 'destination': str(destination)}


def _relocator(origin, target, path_map, ids):
    def transform(value):
        if not isinstance(value, str):
            return value
        if value in ids:
            return ids[value]
        if value in path_map:
            return str(target / path_map[value])
        if value == origin or value.startswith(origin + '/'):
            return str(target) + value[len(origin):]
        return value
    return transform


def _filter_payload(bundle, incoming, selected, manifest):
    snapshot(bundle / 'data/metadata.sqlite3', incoming / 'metadata.sqlite3')
    maps = filter_database(incoming / 'metadata.sqlite3', selected)
    kept = {s['id'] for s in source_list(incoming)}
    jobs, omitted = job_selection(bundle / 'data', selected, {s['id'] for s in manifest['sources']})
    owners = {f['path'].removeprefix('data/'): set(f['sources']) for f in manifest['files']
              if f['path'].startswith('data/')}
    references = referenced_files(bundle / 'data', incoming / 'metadata.sqlite3')
    technical = compatibility_files(bundle / 'data', incoming / 'metadata.sqlite3')
    for item in manifest['files']:
        if not item['path'].startswith('data/') or item['path'] == 'data/metadata.sqlite3':
            continue
        relative = item['path'].removeprefix('data/')
        assigned = owners.get(relative, set())
        if selected is not None and relative not in technical:
            if relative.startswith('studio/'):
                if Path(relative).parts[1] not in jobs:
                    continue
            elif assigned - kept or not (assigned or relative in references or relative.startswith(
                ('voices/', 'config-dependencies/')
            )):
                continue
        target = safe_path(incoming, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(safe_path(bundle, item['path']), target)
    return maps, omitted


def _merge_published_persons(store, origins, target):
    values = {'format': 1, 'entities': [], 'evidence': [], 'coverage': []}
    for category, key in [('entities', 'id'), ('evidence', 'id'), ('coverage', 'cache_id')]:
        unique = {}
        for origin in origins:
            person = origin.get('person_version')
            row = store.one('SELECT data FROM person_versions WHERE id=?', (person,)) if person else None
            for item in json.loads(row['data']).get(category, []) if row else []:
                if item[key] in unique and unique[item[key]] != item:
                    raise ValueError(f'已发布人物数据冲突：{category}/{item[key]}')
                unique[item[key]] = item
        values[category] = sorted(unique.values(), key=lambda item: item[key])
    person = fingerprint(values) if any(o.get('person_version') for o in origins) else None
    if person:
        store.execute('INSERT OR IGNORE INTO person_versions(id,data) VALUES(?,?)', (person, dumps(values)))
    release = fingerprint([target, person])
    store.execute('INSERT OR IGNORE INTO search_releases(id,content_id,person_id) VALUES(?,?,?)',
                  (release, target, person))
    store.activate_release(release)


def union_index(stage, originals):
    store = store_at(stage)
    first, second = originals
    check_compatible(first, second, store, store)
    members = {}
    for version in originals:
        for member in store.rows('SELECT * FROM index_members WHERE index_id=?', (version['id'],)):
            if member['segment_id'] in members:
                raise ValueError('联合索引含重复素材表示')
            members[member['segment_id']] = member
    spec = dict(first['spec'])
    spec['manifest'] = fingerprint([json.loads(members[key]['snapshot']) for key in sorted(members)])
    identifier = fingerprint(spec)
    store.execute('INSERT INTO index_versions(id,spec,status) VALUES(?,?,?)', (identifier, dumps(spec), 'ready'))
    for member in members.values():
        store.execute('INSERT INTO index_members VALUES(?,?,?,?)',
                      (identifier, member['segment_id'], member['snapshot'], member['complete']))
    vector_worker('union', stage, stage, index_id=identifier, origins=[v['id'] for v in originals])
    _merge_published_persons(store, originals, identifier)
    return identifier


def check_compatible(first, second, first_store, second_store):
    # 合并既有向量只约束嵌入空间及索引结构；分析/ASR 等来源配置保留作历史记录。
    for key in ('space', 'format', 'preprocessing', 'mode'):
        if first['spec'].get(key) != second['spec'].get(key):
            raise ValueError(f'活动索引不兼容：{key}')


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_tree(root):
    for path in files_under(root):
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
    for directory, _, _ in os.walk(root, topdown=False):
        _fsync_directory(Path(directory))


def _write_journal(path, value):
    temporary = path.with_suffix('.tmp')
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError('迁移日志不能是符号链接')
    with temporary.open('w', encoding='utf-8') as stream:
        stream.write(dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    _fsync_directory(path.parent)


def journal_path(root):
    return root.parent / f'.{root.name}.migration.json'


def recover_import(root, *, config_path):
    root, config_path = root.resolve(), config_path.resolve()
    with offline(root):
        return _recover(root, config_path)


def _recover(root, config_path):
    journal = journal_path(root)
    if journal.is_symlink():
        raise ValueError('迁移日志不能是符号链接')
    if not journal.exists():
        return {'ok': True, 'recovered': False}
    value = json.loads(journal.read_text())
    if value['target'] != str(root) or value['config'] != str(config_path):
        raise ValueError('中断日志的目标与本次命令不同；请用原 --data-dir 和 --config 恢复')
    # 日志中的临时路径必须是本次目标的同级保留名称，不能用日志删除任意路径。
    paths = {}
    for key in ('stage', 'old', *(['work'] if value.get('work') else [])):
        path = Path(value[key])
        if path.parent != root.parent or not path.name.startswith(f'.{root.name}.migration-') or path.is_symlink():
            raise ValueError('中断日志包含不安全路径')
        paths[key] = path
    config_old = config_path.parent / value['config_old']
    config_new = config_path.parent / value['config_new']
    for path in (config_old, config_new):
        if (path.parent != config_path.parent or not path.name.startswith(f'.{config_path.name}.migration-')
                or path.is_symlink()):
            raise ValueError('中断日志包含不安全配置路径')
    if value['phase'] != 'committed':
        if paths['old'].exists():
            if root.exists():
                shutil.rmtree(root)
            paths['old'].rename(root)
        elif not value['had_data'] and not paths['stage'].exists() and root.exists():
            shutil.rmtree(root)
        if value['write_config']:
            if config_old.exists():
                config_old.replace(config_path)
            elif not value['had_config'] and not config_new.exists():
                config_path.unlink(missing_ok=True)
    for path in paths.values():
        if path.exists():
            shutil.rmtree(path)
    config_old.unlink(missing_ok=True)
    config_new.unlink(missing_ok=True)
    journal.unlink()
    _fsync_directory(root.parent)
    return {'ok': True, 'recovered': True, 'result': '已完成清理' if value['phase'] == 'committed' else '已回滚旧库'}


def _switch(stage, root, config_path, config_text, *, work=None):
    token = uuid4().hex
    old = root.parent / f'.{root.name}.migration-old-{token}'
    config_old = config_path.parent / f'.{config_path.name}.migration-old-{token}'
    config_new = config_path.parent / f'.{config_path.name}.migration-new-{token}'
    config_path.parent.mkdir(parents=True, exist_ok=True)
    value = {'target': str(root), 'config': str(config_path), 'stage': str(stage), 'old': str(old),
             'work': str(work) if work else None,
             'config_old': config_old.name, 'config_new': config_new.name,
             'had_data': root.exists(), 'had_config': config_path.exists(),
             'write_config': config_text is not None, 'phase': 'prepared'}
    _sync_tree(stage)
    try:
        if config_text is not None:
            if config_path.exists():
                shutil.copy2(config_path, config_old)
                with config_old.open('rb') as stream:
                    os.fsync(stream.fileno())
            with config_new.open('w', encoding='utf-8') as stream:
                stream.write(config_text)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(config_path.parent)
        _write_journal(journal_path(root), value)
        if root.exists():
            root.rename(old)
        _fsync_directory(root.parent)
        stage.rename(root)
        _fsync_directory(root.parent)
        if config_text is not None:
            config_new.replace(config_path)
            _fsync_directory(config_path.parent)
        value['phase'] = 'committed'
        _write_journal(journal_path(root), value)
    except BaseException:
        if journal_path(root).exists():
            _recover(root, config_path)
        else:
            config_old.unlink(missing_ok=True)
            config_new.unlink(missing_ok=True)
        raise
    _recover(root, config_path)


def import_backup(config, bundle, *, all_sources=False, sources=None, skip_existing=False,
                  restore_config=False, dry_run=False, config_path=None, progress=None):
    bundle, root = bundle.resolve(), config.data_dir.resolve()
    config_path = (config_path or Path('config.local.yaml')).resolve()
    if bundle.is_relative_to(root) or root.is_relative_to(bundle):
        raise ValueError('备份目录与目标数据目录不能相互包含')
    if config_path.is_relative_to(root):
        raise ValueError('基础配置必须放在数据目录外')
    if progress:
        progress('正在校验备份格式、校验和及媒体依赖…')
    manifest = inspect_bundle(bundle)
    selected = selection(manifest['sources'], all_sources, sources)
    if journal_path(root).exists():
        raise ValueError('发现中断迁移；请先运行 backup recover（使用相同的 --data-dir 和 --config）')
    with offline(root), tempfile.TemporaryDirectory(prefix=f'.{root.name}.migration-work-', dir=root.parent) as tmp:
        work = Path(tmp)
        incoming, stage = work / 'incoming', root.parent / f'.{root.name}.migration-stage-{uuid4().hex}'
        incoming.mkdir()
        existing = source_list(root) if (root / 'metadata.sqlite3').is_file() else []
        known = {s['id'] for s in existing}
        chosen = {s['id'] for s in manifest['sources']} if selected is None else selected
        duplicates = chosen & known
        if duplicates and not skip_existing:
            raise ValueError('素材已存在；使用 --skip-existing 保留目标及衍生数据：' + '、'.join(sorted(duplicates)))
        if duplicates:
            selected = chosen - duplicates
        full_restore = not existing
        write_config = full_restore or restore_config
        maps, omitted = _filter_payload(bundle, incoming, selected, manifest)
        ids = {k: v for mapping in maps.values() for k, v in mapping.items()}
        relocate = _relocator(manifest['origin']['data_dir'], root, manifest['path_map'], ids)
        rewrite_database(incoming / 'metadata.sqlite3', relocate, secrets=True)
        for path in files_under(incoming):
            if path.suffix in {'.json', '.yaml', '.yml'} and not immutable_evidence(path.relative_to(incoming).as_posix()):
                _copy_payload_file(path, path, transform=relocate, include_secrets=True, secrets=[], selected=selected)
        base = yaml.safe_load((bundle / 'config/base.yaml').read_text()) or {}
        base = walk(base, relocate)
        base['data_dir'] = str(root)
        Config.model_validate(base)
        config_text = yaml.safe_dump(base, allow_unicode=True) if write_config else None
        current_base = yaml.safe_load(config_path.read_text()) if config_path.is_file() else {}
        # 差异仅输出字段名和脱敏值，--json 不暴露现有密钥。
        config_diff = {'before': redact(current_base), 'after': redact(base)} if write_config else {}
        report = {'ok': True, 'dry_run': dry_run, 'sources': source_list(incoming),
                  'skipped_sources': sorted(duplicates), 'excluded_jobs': omitted,
                  'config_action': '恢复备份配置' if write_config else '保留目标配置',
                  'config_diff': config_diff, 'id_maps': _combined_maps(manifest['id_maps'], maps, selected),
                  'target': str(root),
                  'required_environment': manifest['required_environment'],
                  'missing_dependencies': manifest['missing_dependencies']}
        report['dependencies'] = {'historical_indexes': [r['id'] for r in store_at(incoming).rows(
            'SELECT id FROM index_versions ORDER BY id')],
            'files': len(files_under(incoming)), 'jobs': sorted(p.name for p in (incoming / 'studio').glob('*')
                                                            if p.is_dir())}
        if progress:
            progress('配置处理：' + report['config_action'])
            if config_diff:
                progress('配置差异（已脱敏）：' + dumps(config_diff))
            progress('正在暂存并合并数据；通过 API 重建向量集合…')
        incoming_active = store_at(incoming).active_index()
        target_store = store_at(root) if (root / 'metadata.sqlite3').is_file() else None
        prior_active = target_store.active_index() if target_store else None
        if prior_active and incoming_active:
            check_compatible(prior_active, incoming_active, target_store, store_at(incoming))
        try:
            stage.mkdir()
            prior_active = None
            if (root / 'metadata.sqlite3').exists():
                # 目标也通过 backup API + Chroma API 重建，绝不拷贝内部 HNSW 文件。
                snapshot(root / 'metadata.sqlite3', stage / 'metadata.sqlite3')
                prior_active = store_at(root).active_index()
                for path in files_under(root):
                    relative = path.relative_to(root).as_posix()
                    if relative.split('/')[0] in {'chroma', 'locks'} or path.name in {
                        'metadata.sqlite3', 'metadata.sqlite3-wal', 'metadata.sqlite3-shm',
                    } or path.suffix == '.lock':
                        continue
                    destination = stage / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
                target_vectors = work / 'target-vectors'
                target_vectors.mkdir()
                collections = vector_worker('dump', stage, target_vectors, origin=str(root),
                                            original_indexes={}, original_representations={}, full=True)
                vector_worker('load', stage, target_vectors, collections=collections,
                              index_map={c['index_id']: c['index_id'] for c in collections}, rep_map={})
                report['integer_id_maps'] = merge_database(stage, incoming, restore_config=write_config)
                for path in files_under(incoming):
                    relative = path.relative_to(incoming).as_posix()
                    if relative.startswith('metadata.sqlite3'):
                        continue
                    if path.suffix in {'.json', '.yaml', '.yml'} and not immutable_evidence(relative):
                        value = (json.loads(path.read_text()) if path.suffix == '.json'
                                 else yaml.safe_load(path.read_text()))
                        remapped = remap_local_refs(value, report['integer_id_maps'])
                        if remapped != value:
                            path.write_text(dumps(remapped) if path.suffix == '.json'
                                            else yaml.safe_dump(remapped, allow_unicode=True), encoding='utf-8')
                    destination = safe_path(stage, relative)
                    if destination.exists():
                        if digest(path) != digest(destination):
                            raise ValueError(f'文件内容冲突：{relative}')
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)
            else:
                if root.exists() and any(p for p in files_under(root) if p.suffix != '.lock'):
                    raise ValueError('目标不是空数据目录且没有有效数据库')
                shutil.copytree(incoming, stage, dirs_exist_ok=True)
            index_map = maps['indexes'] if selected is not None else {
                c['index_id']: c['index_id'] for c in manifest['collections']
            }
            vector_worker('load', stage, bundle, collections=manifest['collections'],
                          index_map=index_map, rep_map=maps['representations'])
            if prior_active and incoming_active:
                report['active_index'] = union_index(stage, [prior_active, incoming_active])
            elif not prior_active and incoming_active:
                if incoming_active.get('search_release_id'):
                    store_at(stage).activate_release(incoming_active['search_release_id'])
                else:
                    store_at(stage).activate(incoming_active['id'])
                report['active_index'] = incoming_active['id']
            else:
                report['active_index'] = prior_active['id'] if prior_active else None
            interrupt_tasks(stage / 'metadata.sqlite3')
            for path in (stage / 'studio').glob('*/job.json'):
                value = json.loads(path.read_text())
                def interrupted(item):
                    if isinstance(item, dict):
                        return {key: ('interrupted' if key == 'status' and v in ('queued', 'running')
                                      else interrupted(v)) for key, v in item.items()}
                    if isinstance(item, list):
                        return [interrupted(v) for v in item]
                    return item
                path.write_text(dumps(interrupted(value)), encoding='utf-8')
            validate_database(stage)
            validate_media(stage)
            if progress:
                progress('正在核对外键、历史成员、表示、向量数量与媒体引用…')
            vector_worker('verify', stage, stage)
            report['searchable_sources'] = sorted({s['source_id'] for s in store_at(stage).snapshots(
                report['active_index'])}) if report['active_index'] else []
            if not dry_run:
                if progress:
                    progress('校验通过，正在持久化并切换数据和配置…')
                _switch(stage, root, config_path, config_text, work=work)
            return report
        finally:
            if stage.exists() and not journal_path(root).exists():
                shutil.rmtree(stage)
