"""Resumable, explicitly applied clip migration and recovery."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from mindex.processing.subtitle_archive import archive_subtitles, load_archive
from mindex.shared.shot_media import VERSION, ShotMedia
from mindex.shared.storage import Store


def protected_digest(store):
    digest = hashlib.sha256()
    with store.connect() as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                  if r[0] not in {'media_references', 'clip_migration_files'} and not r[0].startswith('sqlite_')]
        for table in tables:
            identifier = '"' + table.replace('"', '""') + '"'
            digest.update(table.encode() + b'\0')
            for row in db.execute(f'SELECT * FROM {identifier} ORDER BY rowid'):
                digest.update(json.dumps(tuple(row), ensure_ascii=False, sort_keys=True).encode() + b'\n')
    return digest.hexdigest()


def fixed_hash_paths(store):
    """Conservatively retain media named by historical byte-fixed JSON manifests."""
    pinned = set()

    def visit(value):
        if isinstance(value, dict):
            if (any(key in value for key in ('sha256', 'content_hash', 'video_sha256'))
                    and isinstance(value.get('path'), str)):
                pinned.add(value['path'])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    roots = [store.root, store.root.parent / 'artifacts']
    for root in roots:
        for path in root.rglob('*.json'):
            if 'node_modules' in path.parts or path.stat().st_size > 16 * 1024**2:
                continue
            try:
                visit(json.loads(path.read_text()))
            except (ValueError, OSError, UnicodeError):
                continue
    return pinned


def inventory(store, source_id=None):
    plans = []
    pinned = fixed_hash_paths(store)
    for source in store.sources():
        if source_id and source['id'] != source_id:
            continue
        segments = [s for s in store.segments(source['id']) if s['kind'] in {'moment', 'scene'}]
        by_path = {s['path']: s for s in segments}
        for row in store.rows('SELECT snapshot FROM index_members'):
            snapshot = json.loads(row['snapshot'])
            if snapshot.get('source_id') == source['id'] and snapshot.get('kind') in {'moment', 'scene'}:
                by_path.setdefault(snapshot['path'], snapshot)
        files, seen = [], set()
        for path, segment in by_path.items():
            full = (store.root / path).resolve()
            if not full.is_relative_to(store.root):
                raise ValueError('Clip path outside library')
            size = 0
            if full.is_file():
                stat = full.stat()
                inode = (stat.st_dev, stat.st_ino)
                if inode not in seen:
                    size = stat.st_size
                    seen.add(inode)
            files.append({'path': path, 'start': segment['start'], 'end': segment['end'],
                          'bytes': size, 'exists': full.is_file(), 'fixed_hash': path in pinned})
        plans.append({'source_id': source['id'], 'files': files})
    return plans


@contextmanager
def original_unavailable(store, source):
    """Deny archived source access to the application for migration verification."""
    from unittest.mock import patch

    original_resolve = Store.resolve_media
    archived = (store.root / source['path']).resolve()

    def resolve(instance, relative):
        if (instance.root / relative).resolve() == archived:
            raise PermissionError('Archived source unavailable during shot verification')
        return original_resolve(instance, relative)

    with patch.object(Store, 'resolve_media', resolve):
        yield


def migrate(config, source_id=None, *, apply=False, verify=False):
    store = Store.__new__(Store)
    store.root = Path(config.data_dir).resolve()
    store.path = store.root / 'metadata.sqlite3'
    if not store.path.is_file():
        raise FileNotFoundError(store.path)
    plans = inventory(store, source_id)
    if source_id and not plans:
        raise ValueError('Unknown source')
    report = {'version': VERSION, 'apply': apply, 'sources': plans,
              'released_bytes': 0, 'physical_deletion_enabled': False}
    if not apply:
        return report
    from mindex.shared.processing_lock import ProcessingLock, index_write_lock

    with ProcessingLock(config, deleting=True), index_write_lock(store.root):
        stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
        backup = store.root / 'backups' / f'clips-{stamp}.sqlite3'
        backup.parent.mkdir(parents=True, exist_ok=True)
        with store.connect() as db, sqlite3.connect(backup) as target:
            db.backup(target)
        report['backup'] = str(backup)
        store = Store(config.data_dir)
        before = protected_digest(store)
        service = ShotMedia(store)
        for plan in report['sources']:
            sid = plan['source_id']
            try:
                source = store.one('SELECT * FROM sources WHERE id=?', (sid,))
                archive_subtitles(store, source)
                with store.connect() as db:
                    for item in plan['files']:
                        service.register(item['path'], sid, item['start'], item['end'], db=db)
                        item['status'] = 'registered'
                        db.execute('INSERT OR REPLACE INTO clip_migration_files'
                                   '(path,source_id,version,status,dependency_digest) VALUES(?,?,?,?,?)',
                                   (item['path'], sid, VERSION, 'registered', hashlib.sha256(
                                       json.dumps(service.identity(sid, item['start'], item['end']),
                                                  sort_keys=True).encode()).hexdigest()))
                if verify:
                    with original_unavailable(store, source), tempfile.TemporaryDirectory(dir=store.root) as directory:
                        load_archive(store, sid)
                        service.references(sid)
                        service.frame(sid, 0, Path(directory) / 'frame.png')
                        duration = json.loads(source['media'])['duration']
                        service.audio(sid, Path(directory) / 'audio.wav', 0, min(duration, 1))
                        representatives = plan['files'][::max(1, len(plan['files']) // 3)][:3]
                        for item in representatives:
                            service.materialize(sid, item['start'], item['end'])
                        # All references were coverage/dependency checked before commit, above.
                        for item in plan['files']:
                            item['status'] = 'verified'
                            store.execute('UPDATE clip_migration_files SET status=? WHERE path=?',
                                          ('verified', item['path']))
                    plan['original_access_denied_during_verification'] = True
                plan['status'] = 'verified' if verify else 'registered'
                # User acceptance is a separate gate: migration never deletes media.
                plan['deletion_pending_user_acceptance'] = bool(verify)
                for item in plan['files']:
                    if item['fixed_hash']:
                        item['status'] = 'retained-fixed-hash'
                    elif verify:
                        item['status'] = 'ready-for-deletion'
            except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                plan['status'] = 'failed'
                plan['error'] = str(exc)
        after = protected_digest(store)
        report.update(protected_before=before, protected_after=after)
        if before != after:
            raise RuntimeError('Protected library data changed during migration')
        destination = store.root / 'migration-reports' / f'clips-{stamp}.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        report['report'] = str(destination)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def materialize_all(config):
    store = Store(config.data_dir)
    from mindex.shared.processing_lock import ProcessingLock

    restored = []
    with ProcessingLock(config, deleting=True):
        entries = {row['path']: row for row in store.rows('SELECT * FROM media_references ORDER BY path')}
        for plan in inventory(store):
            for item in plan['files']:
                entries.setdefault(item['path'], {**item, 'source_id': plan['source_id']})
        for row in entries.values():
            destination = (store.root / row['path']).resolve()
            if not destination.is_relative_to(store.root):
                raise ValueError('Clip path outside library')
            if destination.exists():
                continue
            media = ShotMedia(store).materialize(row['source_id'], row['start'], row['end'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.mp4', delete=False) as handle:
                partial = Path(handle.name)
            try:
                shutil.copyfile(media, partial)
                partial.replace(destination)
            finally:
                partial.unlink(missing_ok=True)
            restored.append(row['path'])
    return {'restored': restored}


def verify_archives(config, source_id=None):
    """Explicit archive maintenance; business validation never calls this."""
    from mindex.shared.media import content_hash

    store = Store.__new__(Store)
    store.root = Path(config.data_dir).resolve()
    store.path = store.root / 'metadata.sqlite3'
    results = []
    for source in store.sources():
        if source_id and source['id'] != source_id:
            continue
        try:
            intact = content_hash(store.resolve_media(source['path'])) == source['id']
            results.append({'source_id': source['id'], 'intact': intact})
        except OSError as exc:
            results.append({'source_id': source['id'], 'intact': False, 'error': str(exc)})
    return {'archives': results}


def prune(config, migration_report, *, apply=False):
    """Delete only freshly verified, reconstructible moment/scene entities after acceptance."""
    import os

    from mindex.shared.processing_lock import SharedFileLock, index_write_lock

    store = Store(config.data_dir)
    report_path = Path(migration_report)
    migration = json.loads(report_path.read_text())
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    destination = store.root / 'migration-reports' / f'prune-{stamp}.json'
    journal = destination.with_suffix('.jsonl')
    result = {'apply': apply, 'migration_report': str(report_path), 'files': [],
              'deleted_files': 0, 'deleted_logical_bytes': 0, 'released_allocated_bytes': 0,
              'report': str(destination), 'journal': str(journal)}
    # Explicit pipeline exclusivity also covers installations without enforce_gpu_lock.
    with SharedFileLock(store.root / 'pipeline.lock', exclusive=True), \
            SharedFileLock(store.root / "locks" / "library.lock", exclusive=True), index_write_lock(store.root):
        before = protected_digest(store)
        result['protected_before'] = before
        pinned = fixed_hash_paths(store)
        active_shots = {(store.root / row['path']).resolve()
                        for row in store.rows("SELECT path FROM segments WHERE kind='shot'")}
        service = ShotMedia(store)
        seen, inodes = set(), {}
        for source in migration['sources']:
            if source.get('status') != 'verified':
                raise ValueError(f"Source not verified: {source['source_id']}")
            sid = source['source_id']
            load_archive(store, sid)
            service.references(sid)
            for item in source['files']:
                relative = item['path']
                if relative in seen:
                    continue
                seen.add(relative)
                path = store.root / relative
                resolved = path.resolve()
                if not resolved.is_relative_to(store.root / 'clips') or path.is_symlink():
                    raise ValueError(f'Unsafe clip path: {relative}')
                if resolved in active_shots:
                    raise ValueError(f'Active shot cannot be deleted: {relative}')
                row = store.one('SELECT * FROM clip_migration_files WHERE path=?', (relative,))
                ref = store.one('SELECT * FROM media_references WHERE path=?', (relative,))
                digest = hashlib.sha256(json.dumps(service.identity(sid, item['start'], item['end']),
                                                   sort_keys=True).encode()).hexdigest()
                if (not row or row['status'] not in {'verified', 'deleted'} or
                        row['source_id'] != sid or row['dependency_digest'] != digest or not ref or
                        (ref['source_id'], ref['start'], ref['end']) != (sid, item['start'], item['end'])):
                    raise ValueError(f'Stale or missing verified reference: {relative}')
                entry = {'path': relative, 'source_id': sid, 'dependency_digest': digest}
                if relative in pinned or str(resolved) in pinned:
                    entry['status'] = 'retained-fixed-hash'
                elif not path.exists():
                    entry['status'] = 'already-absent'
                else:
                    stat = path.stat()
                    entry.update(status='ready', identity=[stat.st_dev, stat.st_ino, stat.st_size,
                                                          stat.st_mtime_ns], bytes=stat.st_size)
                    inode = (stat.st_dev, stat.st_ino)
                    group = inodes.setdefault(inode, {'paths': 0, 'links': stat.st_nlink,
                                                       'allocated': stat.st_blocks * 512})
                    group['paths'] += 1
                result['files'].append(entry)
        result['estimated_released_allocated_bytes'] = sum(
            g['allocated'] for g in inodes.values() if g['paths'] == g['links'])
        if not apply:
            return result
        backup = store.root / 'backups' / f'pre-prune-{stamp}.sqlite3'
        backup.parent.mkdir(parents=True, exist_ok=True)
        with store.connect() as db, sqlite3.connect(backup) as target:
            db.backup(target)
        result['backup'] = str(backup)
        result['free_bytes_before'] = shutil.disk_usage(store.root).free
        destination.parent.mkdir(parents=True, exist_ok=True)

        def save():
            partial = destination.with_suffix('.partial')
            partial.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            partial.replace(destination)

        save()
        try:
            with journal.open('a') as log:
                for entry in result['files']:
                    if entry['status'] != 'ready':
                        continue
                    path = store.root / entry['path']
                    stat = path.stat()
                    if [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns] != entry['identity']:
                        raise ValueError(f"File changed after preflight: {entry['path']}")
                    log.write(json.dumps({**entry, 'status': 'deleting'}) + '\n')
                    log.flush()
                    os.fsync(log.fileno())
                    last_link = stat.st_nlink == 1
                    path.unlink()
                    entry['status'] = 'deleted'
                    result['deleted_files'] += 1
                    result['deleted_logical_bytes'] += stat.st_size
                    if last_link:
                        result['released_allocated_bytes'] += stat.st_blocks * 512
                    store.execute('UPDATE clip_migration_files SET status=? WHERE path=?',
                                  ('deleted', entry['path']))
                    log.write(json.dumps({'path': entry['path'], 'status': 'deleted'}) + '\n')
                    log.flush()
                    os.fsync(log.fileno())
            result['protected_after'] = protected_digest(store)
            if result['protected_after'] != before:
                raise RuntimeError('Protected library data changed during cleanup')
        finally:
            result['free_bytes_after'] = shutil.disk_usage(store.root).free
            save()
    return result
