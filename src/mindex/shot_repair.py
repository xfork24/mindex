"""Explicit, backed-up shot-boundary repair. Never deletes existing media."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import numpy as np

from .media import MediaError, atomic_media, command, probe
from .model_server import refine_cut_frame
from .storage import Store

VERSION = 'shot-boundary-v1'


def inspect_boundary(path, timestamp, origin=0., *, include_rgb=False):
    """Decode only a small neighborhood, retaining real input PTS (including VFR)."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'info', '-ss',
                             str(max(0, timestamp - .16)), '-copyts', '-i', str(path),
                             '-an', '-frames:v', '12', '-vf', 'scale=48:27,showinfo',
                             '-fps_mode', 'passthrough', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'],
                            capture_output=True, check=True, timeout=90)
    frames = np.frombuffer(result.stdout, np.uint8).reshape(-1, 27, 48, 3).astype(np.float32)
    log = result.stderr.decode()
    num, den = map(int, re.search(r'config in time_base:\s*(\d+)/(\d+)', log).groups())
    times = [int(pts) * num / den - origin for pts in re.findall(r'\bn:\s*\d+\s+pts:\s*(-?\d+)', log)][:len(frames)]
    if len(times) != len(frames) or len(times) < 3:
        raise MediaError('Cannot inspect shot boundary PTS')
    index = min(range(len(times)), key=lambda n: abs(times[n] - timestamp))
    changes = [0., *np.mean(np.diff(frames, axis=0) ** 2, axis=(1, 2, 3)).tolist()]
    # Need neighbors on both sides; a zero-strength first sample must never win.
    if not 0 < index < len(times) - 1:
        result = {'old': timestamp, 'new': timestamp, 'frame_delta': 0, 'mse': 0.,
                  'old_pts': times[index], 'reason': 'insufficient-neighbors'}
        if include_rgb:
            result['rgb'] = frames[index]
        return result
    candidate = refine_cut_frame(index, changes)
    result = {'old': timestamp, 'new': round(times[candidate], 6), 'frame_delta': candidate - index,
              'mse': changes[candidate], 'old_pts': times[index]}
    if include_rgb:
        result['rgb'] = frames[index]
        result['rgb_timestamp'] = times[index]
    return result


def audit(store, source_id=None, *, workers=4, progress=None):
    sources = [s for s in store.sources() if not source_id or s['id'] == source_id]
    if not sources:
        raise ValueError('Unknown source')
    report = {'version': VERSION, 'sources': [], 'physical_deletions': 0}
    for source in sources:
        plan_path = store.root / 'plans' / f"{source['id']}.json"
        plan_data = json.loads(plan_path.read_text()) if plan_path.exists() else {}
        if plan_data.get('boundary_repair', {}).get('version') == VERSION:
            shots = store.segments(source['id'], 'shot')
            report['sources'].append({'source_id': source['id'], 'name': source['name'],
                                      'original': source['path'], 'boundaries': [], 'status': 'verified',
                                      'already_corrected': True,
                                      'shots': [{**s, 'new_start': s['start'], 'new_end': s['end']} for s in shots]})
            continue
        path = store.resolve_media(source['path'])  # Explicit original-based repair only.
        raw = json.loads(command(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                  '-read_intervals', '%+#1', '-show_entries',
                                  'frame=best_effort_timestamp_time', '-of', 'json', str(path)]))
        origin = float(raw['frames'][0]['best_effort_timestamp_time'])
        shots = store.segments(source['id'], 'shot')
        def inspect(shot, path=path, origin=origin):
            return {**inspect_boundary(path, shot['start'], origin), 'shot_id': shot['id']}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            boundaries = []
            for item in pool.map(inspect, shots[1:]):
                boundaries.append(item)
                if progress:
                    progress(source['name'], len(boundaries), len(shots) - 1)
        changes = {r['old']: r['new'] for r in boundaries if abs(r['new'] - r['old']) > 1e-6}
        ranges = [(changes.get(s['start'], s['start']), changes.get(s['end'], s['end'])) for s in shots]
        if any(b <= a for a, b in ranges):
            raise MediaError('Boundary refinement would collapse a short shot')
        report['sources'].append({'source_id': source['id'], 'name': source['name'],
                                  'original': source['path'], 'pts_origin': origin, 'boundaries': boundaries,
                                  'shots': [{**s, 'new_start': a, 'new_end': b} for s, (a, b) in zip(shots, ranges)]})
    return report


def adjusted(segment, shots, changes):
    start, end = changes.get(segment['start'], segment['start']), changes.get(segment['end'], segment['end'])
    parent = shots.get(segment.get('parent_id'))
    if parent and segment['kind'] == 'moment':
        start, end = max(start, parent['new_start']), min(end, parent['new_end'])
    if end <= start:
        raise MediaError(f"Boundary correction collapses segment {segment['id']}")
    return start, end


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix('.partial')
    partial.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    partial.replace(path)


def apply(config, report, *, progress=None, video_encoder="libx264"):
    """Stage new shots first, then atomically commit source metadata. Old paths survive."""
    from .processing_lock import ProcessingLock, index_write_lock
    from .shot_media import ShotMedia

    if video_encoder not in {'libx264', 'h264_videotoolbox'}:
        raise ValueError('Unsupported repair video encoder')
    encoder_args = (['-c:v', 'libx264', '-threads', '4', '-preset', 'fast', '-crf', '20']
                    if video_encoder == 'libx264' else ['-c:v', 'h264_videotoolbox', '-q:v', '65'])
    store = Store(config.data_dir)
    stamp = report.setdefault('run_id', datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ'))
    journal = store.root / 'shot-repairs' / stamp / 'report.json'
    with ProcessingLock(config, deleting=True), index_write_lock(store.root):
        if all(s.get('status') == 'verified' for s in report['sources']):
            for source in report['sources']:
                ShotMedia(store).references(source['source_id'])
            report['no_changes'] = True
            return report
        backup = store.root / 'backups' / f'shot-boundaries-{stamp}.sqlite3'
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            with sqlite3.connect(store.path) as src, sqlite3.connect(backup) as dst:
                src.backup(dst)
        report['backup'] = str(backup)
        _write_json(journal, report)
        for source in report['sources']:
            if source.get('status') == 'verified':
                ShotMedia(store).references(source['source_id'])
                continue
            sid = source['source_id']
            shots = {s['id']: s for s in source['shots']}
            changes = {r['old']: r['new'] for r in source['boundaries'] if abs(r['new'] - r['old']) > 1e-6}
            current_shots = {s['id']: s for s in store.segments(sid, 'shot')}
            committed = any(s.get('repaired_path') for s in shots.values()) and all(
                (current_shots[s['id']]['start'], current_shots[s['id']]['end'], current_shots[s['id']]['path']) ==
                (s['new_start'], s['new_end'], s.get('repaired_path', s['path'])) for s in shots.values())
            if committed:
                _commit_plan(store, source, shots, journal, stamp)
                ShotMedia(store).references(sid)
                source['status'] = 'verified'
                _write_json(journal, report)
                continue
            # Fingerprint inputs before staging; never apply an audit to changed coordinates.
            for shot in shots.values():
                current = store.one('SELECT * FROM segments WHERE id=?', (shot['id'],))
                if (current['start'], current['end']) != (shot['start'], shot['end']):
                    raise MediaError('Stale boundary audit; current shot coordinates changed')
            staged = []
            for number, shot in enumerate(shots.values(), 1):
                from .shot_media import ShotMedia
                old_info = ShotMedia(store)._info(store.resolve_media(shot['path']))
                truncated = old_info['duration'] + 1 / old_info['fps'] + 1e-6 < shot['end'] - shot['start']
                if (shot['start'], shot['end']) == (shot['new_start'], shot['new_end']) and not truncated:
                    continue
                relative = f"clips/{sid}/shot-repaired/{stamp}/{shot['id']}.mp4"
                output = store.root / relative
                if not output.exists():
                    # Seek before the cut, then trim using original PTS: exact input seeks can
                    # discard the incoming frame on streams with fractional timestamps/edit lists.
                    a = shot['new_start'] + source.get('pts_origin', 0.)
                    b = shot['new_end'] + source.get('pts_origin', 0.)
                    vf = f'trim=start={max(0,a-1e-6):.6f}:end={b-1e-6:.6f},setpts=PTS-STARTPTS,pad=ceil(iw/2)*2:ceil(ih/2)*2'
                    af = f'atrim=start={a:.6f}:end={b:.6f},asetpts=PTS-STARTPTS'
                    atomic_media(['-ss', str(max(0, shot['new_start'] - .1)), '-copyts',
                                  '-i', str(store.root / source['original']), '-map', '0:v:0',
                                  '-map', '0:a:0?', '-vf', vf,
                                  '-af', af, '-fps_mode', 'vfr', *encoder_args, '-pix_fmt', 'yuv420p',
                                  '-c:a', 'aac', '-movflags', '+faststart'], output,
                                 timeout=max(600, (shot['new_end'] - shot['new_start']) * 20))
                _info, held_frames = ensure_duration(output, shot['new_end'] - shot['new_start'], encoder_args)
                shot['held_tail_frames'] = held_frames
                shot['repaired_path'] = relative
                shot.setdefault('encoder', video_encoder)
                staged.append(shot)
                if progress:
                    progress(source['name'], number, len(shots))
                _write_json(journal, report)
            snapshots_changed = 0
            paths = {}
            for segment in store.segments(sid):
                a, b = adjusted(segment, shots, changes)
                if (a, b) != (segment['start'], segment['end']) or shots.get(segment['id'], {}).get('repaired_path'):
                    paths[segment['path']] = (a, b)
            # Historical-only media also receive corrected bounds, without recreating vector entries.
            with store.connect() as db:
                for segment in store.segments(sid):
                    a, b = adjusted(segment, shots, changes)
                    shot = shots.get(segment['id'], {})
                    db.execute('UPDATE segments SET start=?,end=?,path=? WHERE id=?',
                               (a, b, shot.get('repaired_path', segment['path']), segment['id']))
                for row in db.execute('SELECT rowid,snapshot FROM index_members').fetchall():
                    segment = json.loads(row['snapshot'])
                    if segment.get('source_id') != sid:
                        continue
                    a, b = adjusted(segment, shots, changes)
                    if (a, b) != (segment['start'], segment['end']):
                        paths[segment['path']] = (a, b)
                        segment.update(start=a, end=b)
                        db.execute('UPDATE index_members SET snapshot=? WHERE rowid=?',
                                   (json.dumps(segment, ensure_ascii=False, sort_keys=True), row['rowid']))
                        snapshots_changed += 1
                service = ShotMedia(store)
                for path, (a, b) in paths.items():
                    # References must see this same transaction's corrected shots.
                    refs = _transaction_refs(db, sid, a, b)
                    db.execute('INSERT OR REPLACE INTO media_references(path,source_id,start,end,version,refs) '
                               'VALUES(?,?,?,?,?,?)', (path, sid, a, b, VERSION, json.dumps(refs)))
                # Regenerate every registered dependency, even if its absolute bounds stayed fixed.
                for row in db.execute('SELECT * FROM media_references WHERE source_id=?', (sid,)).fetchall():
                    a, b = paths.get(row['path'], (row['start'], row['end']))
                    refs = _transaction_refs(db, sid, a, b)
                    db.execute('UPDATE media_references SET start=?,end=?,version=?,refs=? WHERE path=?',
                               (a, b, VERSION, json.dumps(refs), row['path']))
                for row in db.execute('SELECT rowid,data FROM scene_plans WHERE source_id=?', (sid,)).fetchall():
                    ranges = json.loads(row['data'])
                    corrected = [[changes.get(a, a), changes.get(b, b)] for a, b in ranges]
                    db.execute('UPDATE scene_plans SET data=? WHERE rowid=?',
                               (json.dumps(corrected), row['rowid']))
                db.execute('UPDATE clip_migration_files SET status=? WHERE source_id=?', ('requires-reverification', sid))
            source.update(status='committed', staged_shots=len(staged), snapshots_changed=snapshots_changed,
                          legacy_paths_redirected=len(paths))
            _write_json(journal, report)
            _commit_plan(store, source, shots, journal, stamp)
            service.references(sid)
            source.update(status='verified', staged_shots=len(staged), snapshots_changed=snapshots_changed,
                          legacy_paths_redirected=len(paths))
            _write_json(journal, report)
        report['report'] = str(journal)
        _write_json(journal, report)
    return report


def _transaction_refs(db, sid, start, end):
    cursor, refs = start, []
    for row in db.execute("SELECT * FROM segments WHERE source_id=? AND kind='shot' ORDER BY start", (sid,)):
        a, b = max(start, row['start']), min(end, row['end'])
        if b <= a:
            continue
        if abs(a - cursor) > 1e-6:
            raise MediaError('Repaired references have incomplete coverage')
        refs.append({'shot_id': row['id'], 'path': row['path'], 'start': a - row['start'],
                     'end': b - row['start'], 'offset': a - start, 'absolute_start': a})
        cursor = b
    if abs(cursor - end) > 1e-6:
        raise MediaError('Repaired references have incomplete coverage')
    return refs


def _commit_plan(store, source, shots, journal, stamp):
    sid = source['source_id']
    plan = store.root / 'plans' / f'{sid}.json'
    if plan.exists():
        old = plan.read_bytes()
        saved = journal.parent / f'{sid}-plan.json'
        if not saved.exists():
            saved.write_bytes(old)
        data = json.loads(old)
        data['shots'] = [{'start': s['new_start'], 'end': s['new_end']} for s in shots.values()]
        data['boundary_repair'] = {'version': VERSION, 'run_id': stamp}
        _write_json(plan, data)


def ensure_duration(output, duration, encoder_args):
    """Preserve the final displayed frame across a short VFR gap at an exclusive cut."""
    info = probe(output)
    tolerance = 1 / info['fps'] + 1e-6
    held = 0
    if duration - info['duration'] > tolerance:
        held = round((duration - info['duration']) * info['fps'])
        if not 1 <= held <= 2:
            raise MediaError('Repaired shot duration mismatch')
        filled = output.with_suffix('.filled.mp4')
        atomic_media(['-i', str(output), '-map', '0:v:0', '-map', '0:a:0?',
                      '-vf', f'tpad=stop_mode=clone:stop={held}', '-fps_mode', 'vfr',
                      *encoder_args, '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart'], filled)
        checked = probe(filled)
        if abs(checked['duration'] - duration) > 1 / checked['fps'] + 1e-6:
            raise MediaError('Repaired shot duration mismatch after VFR hold')
        saved = output.with_suffix(f'.unfilled-{datetime.now(UTC).strftime("%H%M%S%f")}.mp4')
        output.rename(saved)
        filled.replace(output)
        info = checked
    if abs(info['duration'] - duration) > 1 / info['fps'] + 1e-6:
        raise MediaError('Repaired shot duration mismatch')
    return info, held
