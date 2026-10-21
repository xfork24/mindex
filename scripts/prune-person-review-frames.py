"""Remove accepted/rejected frame evidence outside a per-shot PTS sampling grid.

Dry run by default. Whole-shot review markers without frame/cache provenance and
published snapshots are retained. Run with --apply --vacuum to reclaim DB space.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import av

from mindex.shared.processing_lock import SharedFileLock


def sampled_pts(path, start, end, interval):
    result = set()
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        stream.codec_context.thread_count = 2
        origin = float(stream.start_time * stream.time_base) if stream.start_time else 0
        index = 0
        for frame in container.decode(stream):
            if frame.pts is None:
                raise ValueError(f"Missing PTS: {path}")
            t = float(frame.pts * stream.time_base) - origin
            if t >= end - start:
                break
            if t < 0:
                continue
            if index % interval == 0:
                result.add(round(t + start, 6))
            index += 1
    if not result:
        raise ValueError(f"No valid frames: {path}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--interval', type=int, default=15)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--vacuum', action='store_true')
    args = parser.parse_args()
    if args.interval < 1:
        parser.error('interval must be >= 1')
    root = args.data_dir.resolve()
    with SharedFileLock(root / 'pipeline.lock', exclusive=True), SharedFileLock(root / 'locks/library.lock', exclusive=True):
        db = sqlite3.connect(root / 'metadata.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        # A write reservation prevents concurrent database changes during planning.
        db.execute('BEGIN IMMEDIATE')
        shots = db.execute("SELECT DISTINCT s.id,s.source_id,s.path,s.start,s.end FROM segments s JOIN person_evidence e "
                           "ON e.shot_id=s.id AND e.source_id=s.source_id WHERE s.kind='shot' "
                           "AND e.status IN ('accepted','rejected') ORDER BY s.source_id,s.start").fetchall()
        db.execute('CREATE TEMP TABLE prune_evidence(id TEXT PRIMARY KEY)')
        counts = {'accepted': 0, 'rejected': 0, 'kept_frame_evidence': 0, 'kept_shot_markers': 0, 'shots': len(shots)}
        executor = ThreadPoolExecutor(max_workers=4)
        pending = [executor.submit(sampled_pts, root / shot['path'], shot['start'], shot['end'], args.interval)
                   for shot in shots]
        for i, shot in enumerate(shots):
            pts = pending[i].result()
            rows = db.execute("SELECT id,status,start,json_extract(data,'$.cache_id') cache_id, "
                              "json_extract(data,'$.image_path') image_path FROM person_evidence "
                              "WHERE source_id=? AND shot_id=? AND status IN ('accepted','rejected')",
                              (shot['source_id'], shot['id']))
            removed = []
            for row in rows:
                if not row['cache_id']:
                    counts['kept_shot_markers'] += 1
                elif round(row['start'], 6) in pts:
                    counts['kept_frame_evidence'] += 1
                else:
                    removed.append((row['id'],))
                    counts[row['status']] += 1
            db.executemany('INSERT INTO prune_evidence VALUES(?)', removed)
            if (i + 1) % 25 == 0:
                print(json.dumps({'processed_shots': i + 1, **counts}), flush=True)
        executor.shutdown()
        if args.apply:
            db.execute('DELETE FROM person_evidence WHERE id IN (SELECT id FROM prune_evidence)')
            # Invalidate match results so retries cannot resurrect removed reviews.
            db.execute("DELETE FROM person_features WHERE kind='matching'")
            for row in db.execute("SELECT id,config FROM model_profiles WHERE role='person'").fetchall():
                config = json.loads(row['config'])
                config['frame_interval'] = args.interval
                db.execute('UPDATE model_profiles SET config=? WHERE id=?', (json.dumps(config), row['id']))
            assert db.execute('SELECT COUNT(*) FROM person_evidence WHERE id IN (SELECT id FROM prune_evidence)').fetchone()[0] == 0
            db.commit()
            if args.vacuum:
                print('Compacting database...', flush=True)
                db.execute('VACUUM')
        else:
            db.rollback()
        counts['applied'] = args.apply
        print(json.dumps(counts, ensure_ascii=False), flush=True)
        report_dir = root / 'reports'
        report_dir.mkdir(exist_ok=True)
        (report_dir / 'person-review-frame-pruning.json').write_text(json.dumps(counts, ensure_ascii=False, indent=2))
        db.close()


if __name__ == '__main__':
    main()
