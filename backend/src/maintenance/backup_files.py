"""备份与迁移共用的文件、选择与校验辅助函数。"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

import yaml
from filelock import FileLock, Timeout

from mindex.maintenance.backup_db import (
    NON_LIBRARY_SOURCE_ID,
    SECRET_FIELDS,
    TABLES,
    filter_database,
    json_container,
    redact,
    rewrite_database,
    snapshot,
    store_at,
    walk,
)
from mindex.shared import locks
from mindex.shared.config import Config, fingerprint
from mindex.shared.storage import Store, dumps

# 向量备份/恢复子进程的超时：大库也足够宽裕，只防止进程挂死后永远等待。
VECTOR_PROCESS_TIMEOUT_SECONDS = 6 * 3600

EXCLUDED_DIRS = {'chroma', 'locks', 'models', 'model-cache', '.cache', '__pycache__'}


MODEL_SUFFIXES = {'.gguf', '.safetensors', '.onnx', '.pt', '.pth'}


TEXT_SUFFIXES = {'.json', '.yaml', '.yml', '.txt', '.log', '.jsonl'}


def verification_mode(value):
    if value not in {'full', 'basic'}:
        raise ValueError('校验模式必须为 full 或 basic')
    return value


def verification_report(value, *, exporting=False):
    skipped = ['全文件 SHA256 复算']
    skipped += (['向量文档和元数据归属复核', '向量批次重复解析'] if exporting else
                ['向量逐项数值、文档和归属复核', '逐历史索引分镜媒体探测'])
    return {'verification': value, 'skipped_checks': skipped if value == 'basic' else []}


@contextmanager
def temporary_directory(**kwargs):
    """Finder 等进程可能在删除期间重建目录项；清理不能遮住真正的迁移错误。"""
    path = tempfile.mkdtemp(**kwargs)
    try:
        yield path
    finally:
        for attempt in range(3):
            try:
                shutil.rmtree(path)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                if exc.errno == errno.ENOTEMPTY and attempt < 2:
                    continue
                warnings.warn(f'临时目录清理失败，请手动清理 {path}：{exc}', RuntimeWarning, stacklevel=2)
                break


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


def finder_metadata(path):
    """只忽略 Finder 文件及格式合法、有对应主文件的 AppleDouble 旁路文件。"""
    if path.name == '.DS_Store':
        return True
    if not path.name.startswith('._') or not path.with_name(path.name[2:]).exists():
        return False
    with path.open('rb') as stream:
        header = stream.read(26)
        if len(header) != 26:
            return False
        magic, version, _, count = struct.unpack('>II16sH', header)
        size = path.stat().st_size
        if magic != 0x00051607 or version != 0x00020000 or 26 + count * 12 > size:
            return False
        for _ in range(count):
            _, offset, length = struct.unpack('>III', stream.read(12))
            if offset < 26 + count * 12 or offset + length > size:
                return False
    return True


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
    if path.name == 'agent.sqlite3' and len(path.parts) == 1:
        return '智能助手会话与密钥保险库属于本机运行状态，不进入备份'
    if path.parts[:1] == ('agent-attachments',):
        return '智能助手附件是会话内临时暂存，过期由 mindex clean 回收，不进入备份'
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
                for name in locks.ORDER:
                    safe_path(root, locks.PATHS[name])
                stack.enter_context(locks.acquire_ordered(root, locks.ORDER, exclusive=True))
            yield
    except Timeout as exc:
        raise ValueError('迁移锁被占用；请停止 Web 和全部后台写入任务后重试') from exc


def vector_worker(action, root, bundle, **options):
    request = {'action': action, 'root': str(root), 'bundle': str(bundle), **options}
    # 子进程退出后才能移动目录；不触碰 Chroma 的私有 shutdown API。
    with temporary_directory(prefix='mindex-vector-') as temporary:
        path = Path(temporary) / 'request.json'
        path.write_text(dumps(request), encoding='utf-8')
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'mindex.maintenance.backup_vectors', str(path)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', check=False,
                timeout=VECTOR_PROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            hours = VECTOR_PROCESS_TIMEOUT_SECONDS // 3600
            raise ValueError(f'向量迁移进程超过 {hours} 小时仍未结束，已终止；请检查向量库后重试') from None
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
        state = json.loads(path.read_text(encoding="utf-8"))
        refs = set(state.get('source_ids') or [])
        historical = set()
        for artifact in path.parent.rglob('*.json'):
            if finder_metadata(artifact):
                continue
            text = artifact.read_text(encoding="utf-8")
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
        if finder_metadata(path):
            continue
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
            sid = ((row.get('source_id') if table not in NON_LIBRARY_SOURCE_ID else None) or
                   (row.get('id') if table == 'sources' else None))
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
    for relative in dict.fromkeys(paths):
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


def _copy_payload_file(origin, destination, *, transform, include_secrets, secrets, selected,
                       allow_legacy_representations=False):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with origin.open('rb') as stream:
        sqlite = stream.read(16) == b'SQLite format 3\x00'
    if sqlite:
        # 历史 SQLite 文件可以早于当前 schema；仅升级隔离副本，不写回历史原件。
        with temporary_directory(prefix='.mindex-history-', dir=destination.parent) as temporary:
            working = Path(temporary) / 'metadata.sqlite3'
            snapshot(origin.resolve(), working, allow_legacy=True)
            Store(working.parent)
            snapshot(working, destination)
        maps = filter_database(destination, selected, allow_legacy_representations=allow_legacy_representations)
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


def _resolve_dependency(item, base_dir, data_root):
    """定位配置依赖文件，容忍数据目录移动/改名后残留的失效绝对路径。

    依次尝试：(1) 原值（相对 base_dir 解析或本身绝对）；(2) 若原值包含
    config-dependencies/<digest>/<name> 片段，则在当前 data_root 下按该片段定位；
    (3) 把原值当作相对 data_root.parent 的路径解析。全部失败才返回 None。
    """
    expanded = Path(item).expanduser()
    primary = (base_dir / expanded).resolve()
    if primary.is_file():
        return primary
    posix = expanded.as_posix()
    marker = 'config-dependencies/'
    if data_root is not None and marker in posix:
        suffix = posix[posix.index(marker):]
        candidate = (data_root / suffix).resolve()
        if candidate.is_file():
            return candidate
    if data_root is not None and not expanded.is_absolute():
        candidate = (data_root.parent / expanded).resolve()
        if candidate.is_file():
            return candidate
    return None


def _config_dependencies(config, base_dir, payload, data_root=None):
    paths, missing, env = {}, [], set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'api_key_env' and item:
                    env.add(item)
                elif key == 'model_root' and item:
                    missing.append({'path': item, 'reason': '模型目录需在目标机器补充'})
                elif key in {'deployment_manifest', 'font_file'} and item:
                    original = _resolve_dependency(item, base_dir, data_root)
                    if original is None:
                        attempted = (base_dir / Path(item).expanduser()).resolve()
                        raise ValueError(
                            f'配置依赖文件缺失：{attempted}'
                            f'（也未能在数据目录 {data_root} 内按 config-dependencies 片段定位 {item}）'
                        )
                    relative = f'config-dependencies/{digest(original)[:24]}/{original.name}'
                    destination = payload / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _copy_payload_file(original, destination, transform=lambda v: v, include_secrets=False,
                                       secrets=[], selected=None)
                    paths[str(original)] = relative
                    paths[item] = relative
                    if key == 'deployment_manifest':
                        pinned = json.loads(original.read_text(encoding="utf-8"))
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
    from mindex.models.compatibility import load_baseline, location
    from mindex.models.deployment import verified_file

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
            if finder_metadata(gate):
                continue
            marker = gate.with_suffix('.sha256')
            verified_file(gate, marker.read_text(encoding="utf-8").strip())
            paths.extend((gate, marker))
        for path in paths:
            relative = path.relative_to(root).as_posix()
            safe_path(root, relative)
            required.add(relative)
    return required


def immutable_evidence(relative):
    # 清单描述模型服务器上的路径；不能当作客户端数据路径重定位。
    return relative.startswith(('capabilities/v2/', 'config-dependencies/', 'deployments/'))


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
