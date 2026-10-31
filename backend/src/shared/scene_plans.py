"""按当前场景计划选择实际区间，保留历史记录供快照回退使用。"""
from __future__ import annotations

import json


def select_scenes(segments, plan, profile):
    scenes = [s for s in segments if s['kind'] == 'scene' and s.get('parent_id') is None]
    selected = []
    for start, end in plan:
        matches = [s for s in scenes if abs(s['start'] - start) < 1e-6 and abs(s['end'] - end) < 1e-6]
        if matches:
            selected.append(min(matches, key=lambda s: (s['generation'] != profile, s['id'])))
    return selected


def current_segments(store, source_id, segments=None):
    segments = segments if segments is not None else store.segments(source_id)
    row = store.one('SELECT profile,data FROM scene_plans WHERE source_id=? ORDER BY rowid DESC LIMIT 1',
                    (source_id,))
    if row is None:
        return segments
    plan = json.loads(row['data'])
    selected = {s['id'] for s in select_scenes(segments, plan, row['profile'])}
    return [s for s in segments if s['kind'] != 'scene' or s['id'] in selected]
