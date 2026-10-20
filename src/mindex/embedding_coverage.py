"""Read embedding checkpoints by actual reusable inputs, independently of publication."""
import json
from collections import Counter, defaultdict
from types import SimpleNamespace

from .channel_index import normalize_snapshot, representation_key
from .config import VideoConfig
from .indexing import IndexBuilder


def embedding_coverage(store, source_id, active, fresh, segments, published):
    old = {s['id']: normalize_snapshot(s) for s in published}
    segment_map = {s['id']: s for s in segments}
    spec = active['spec'] if active else None
    versions = store.rows("SELECT id,spec FROM index_versions WHERE status IN ('ready','building')")
    compatible = {r['id'] for r in versions if spec and all(
        json.loads(r['spec']).get(k) == spec.get(k) for k in ('space', 'preprocessing')
    )}
    cached, published_keys, known = set(), defaultdict(set), set()
    # A complete member certifies all its vector writes have finished. Cache keys
    # also allow a moment to reuse an identical shot's vector (and vice versa).
    if compatible:
        marks = ','.join('?' for _ in compatible)
        for r in store.rows(
            "SELECT r.index_id,r.segment_id,r.view,r.cache_key FROM representations r "
            "JOIN index_members m ON m.index_id=r.index_id AND m.segment_id=r.segment_id "
            f"WHERE m.complete=1 AND r.index_id IN ({marks})", tuple(compatible)
        ):
            known.add((r['segment_id'], r['view']))
            if r['cache_key']:
                cached.add(r['cache_key'])
                if active and r['index_id'] == active['id']:
                    published_keys[(r['segment_id'], r['view'])].add(r['cache_key'])
    builder = SimpleNamespace(config=SimpleNamespace(video=VideoConfig.model_validate(spec['preprocessing']).model_copy(update=spec['preprocessing']))) if spec else None
    result = {}
    for name, channel in (('shots', 'visual'), ('dialogue', 'dialogue'), ('analysis', 'analysis')):
        view = 'video' if channel == 'visual' else channel
        result[name] = {}
        for field, kinds in (('search', {'moment'}), ('extra_search', {'shot', 'scene'})):
            candidates = [s for s in fresh if s['kind'] in kinds and channel in s['channel_versions']
                          and (channel == 'visual' or s['views'].get(channel))]
            by_kind = {k: {'expected_segments': 0, 'indexed_segments': 0, 'embedded_segments': 0} for k in sorted(kinds)}
            reasons, pending, unpublished = Counter(), [], 0
            for candidate in candidates:
                sid = candidate['id']
                saved = old.get(sid)
                keys = set()
                if builder:
                    segment = {**segment_map[sid], **candidate}
                    keys = {representation_key(builder, spec['space'], segment, v, doc, bounds)
                            for _, v, doc, bounds in IndexBuilder._representations(builder, segment) if v == view}
                version_matches = bool(saved) and (
                    saved['channel_versions'].get(channel) == candidate['channel_versions'][channel]
                    or (not saved['channel_versions'] and
                        (channel == 'visual' or saved['views'].get(channel) == candidate['views'].get(channel)))
                )
                indexed = bool(saved and view in saved['indexed_views']) and (
                    (bool(keys) and keys <= published_keys[(sid, view)])
                    if channel == 'visual' else version_matches
                )
                # Legacy indexes do not always have cache keys; preserve their
                # published coverage, but never treat changed inputs as reusable.
                if saved and not saved['channel_versions'] and version_matches and view in saved['indexed_views']:
                    indexed = True
                embedded = indexed or bool(keys and keys <= cached)
                counts = by_kind[candidate['kind']]
                counts['expected_segments'] += 1
                counts['indexed_segments'] += indexed
                counts['embedded_segments'] += embedded
                if embedded and not indexed:
                    unpublished += 1
                if not embedded:
                    reason = 'inputs_changed' if (sid, view) in known else 'not_embedded'
                    reasons[reason] += 1
                    segment = segment_map[sid]
                    pending.append({k: segment[k] for k in ('id', 'kind', 'start', 'end')} | {'reason': reason})
            expected = len(candidates)
            indexed = sum(c['indexed_segments'] for c in by_kind.values())
            result[name][field] = {
                'current': bool(expected) and indexed == expected,
                'expected_segments': expected,
                'indexed_segments': indexed,
                'embedded_segments': sum(c['embedded_segments'] for c in by_kind.values()),
                'by_kind': by_kind,
                'pending_reasons': [{'code': code, 'count': count} for code, count in sorted(reasons.items())],
                'pending_segments': pending,
                'unpublished_segments': unpublished,
                'index_id': active['id'] if active else None,
            }
    return result
