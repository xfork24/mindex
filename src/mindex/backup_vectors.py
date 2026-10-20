"""迁移专用 Chroma 子进程；只读写显式向量，不创建模型客户端。"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from .backup_db import raw_representation_ids, store_at
from .config import fingerprint
from .storage import dumps

BATCH_SIZE = 256


def run(request):
    from .indexing import VectorIndex, check_index

    root, bundle = Path(request['root']), Path(request['bundle'])
    store = store_at(root)
    action = request['action']
    if action == 'dump':
        origin = Path(request['origin'])
        collections = []
        for version in store.rows('SELECT id FROM index_versions ORDER BY id'):
            identifier = version['id']
            if not identifier or any(character not in '0123456789abcdef' for character in identifier):
                raise ValueError('索引 ID 不是有效的内容指纹')
            old = request['original_indexes'].get(identifier, identifier)
            reps = store.rows('SELECT * FROM representations WHERE index_id=? ORDER BY id', (identifier,))
            by_id = {row['id']: row for row in reps}
            snapshots = {row['segment_id']: json.loads(row['snapshot']) for row in store.rows(
                'SELECT segment_id,snapshot FROM index_members WHERE index_id=?', (identifier,))}
            originals = request['original_representations']
            wanted = {originals.get(r['id'], r['id']): r['id'] for r in reps}
            if not wanted:
                collections.append({'index_id': identifier, 'count': 0, 'batches': []})
                continue
            vectors = VectorIndex(origin, old)
            available = vectors.ids()
            if set(wanted) - available:
                raise ValueError(f'索引 {old} 缺少必需向量：{len(set(wanted) - available)}')
            if request.get('full') and set(wanted) != available:
                raise ValueError(f'索引 {old} 含未登记的向量')
            files = []
            for offset in range(0, len(wanted), BATCH_SIZE):
                ids = list(wanted)[offset:offset + BATCH_SIZE]
                batch = vectors.collection.get(ids=ids, include=['embeddings', 'documents', 'metadatas'])
                if set(batch['ids']) != set(ids) or batch['embeddings'] is None:
                    raise ValueError(f'索引 {old} 向量读取不完整')
                for rid, metadata, document in zip(batch['ids'], batch['metadatas'], batch['documents'], strict=True):
                    rep = by_id[wanted[rid]]
                    member = snapshots[rep['segment_id']]
                    expected = {'source_id': member['source_id'], 'segment_id': member['id'],
                                'granularity': member['kind'], 'view': rep['view']}
                    if not metadata or any(metadata.get(key) != value for key, value in expected.items()):
                        raise ValueError(f'向量元数据归属不一致：{rid}')
                    if document != (rep['document'] or rep['view']):
                        raise ValueError(f'向量文档与表示不一致：{rid}')
                rows = [
                    {'id': wanted[rid], 'embedding': vector.tolist(), 'document': document, 'metadata': metadata}
                    for rid, vector, document, metadata in zip(
                        batch['ids'], batch['embeddings'], batch['documents'], batch['metadatas'], strict=True
                    )
                ]
                relative = f'vectors/{identifier}/{offset // BATCH_SIZE:06d}.json'
                path = bundle / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(dumps(rows), encoding='utf-8')
                files.append(relative)
            collections.append({'index_id': identifier, 'count': len(wanted), 'batches': files})
        return collections
    if action == 'load':
        for item in request['collections']:
            identifier = request['index_map'].get(item['index_id'])
            if identifier is None:
                continue
            vectors = VectorIndex(root, identifier, create=True)
            wanted = {r['id'] for r in store.rows('SELECT id FROM representations WHERE index_id=?', (identifier,))}
            seen = set()
            for relative in item['batches']:
                rows = json.loads((bundle / relative).read_text())
                batch = []
                for row in rows:
                    rid = request['rep_map'].get(row['id'], row['id'])
                    if rid not in wanted:
                        continue
                    if rid in seen:
                        raise ValueError(f'重复向量：{rid}')
                    seen.add(rid)
                    batch.append({**row, 'id': rid})
                if batch:
                    vectors.collection.upsert(
                        ids=[r['id'] for r in batch], embeddings=[r['embedding'] for r in batch],
                        documents=[r['document'] for r in batch], metadatas=[r['metadata'] for r in batch],
                    )
            if seen != wanted:
                raise ValueError(f'索引 {identifier} 向量不完整')
            vectors.flush()
        return {'ok': True}
    if action == 'union':
        target = request['index_id']
        vectors = VectorIndex(root, target, create=True)
        for origin in request['origins']:
            original = VectorIndex(root, origin)
            spec = store.index_version(origin)['spec']
            snapshots = {s['id']: s for s in store.snapshots(origin)}
            records = store.rows('SELECT * FROM representations WHERE index_id=? ORDER BY id', (origin,))
            for offset in range(0, len(records), BATCH_SIZE):
                batch = records[offset:offset + BATCH_SIZE]
                raw = original.collection.get(ids=[row['id'] for row in batch],
                                               include=['embeddings', 'documents', 'metadatas'])
                if set(raw['ids']) != {row['id'] for row in batch}:
                    raise ValueError(f'联合索引缺少向量：{origin}')
                mapped, values = {}, []
                for row in batch:
                    snapshot = snapshots[row['segment_id']]
                    raw_ids = raw_representation_ids(snapshot, spec)
                    base = next((raw for raw in raw_ids if fingerprint([origin, raw]) == row['id']), None)
                    if base is None:
                        raise ValueError(f'表示不在成员清单中：{row["id"]}')
                    rid = fingerprint([target, base])
                    mapped[row['id']] = rid
                    values.append((rid, target, row['segment_id'], row['view'], row['document'], row['cache_key']))
                vectors.collection.upsert(
                    ids=[mapped[rid] for rid in raw['ids']],
                    embeddings=[v.tolist() for v in raw['embeddings']],
                    documents=raw['documents'], metadatas=raw['metadatas'],
                )
                with store.connect() as db:
                    db.executemany('INSERT INTO representations(id,index_id,segment_id,view,document,cache_key) '
                                   'VALUES(?,?,?,?,?,?)', values)
        vectors.flush()
        return {'ok': True}
    if action == 'verify':
        for version in store.rows('SELECT id,status,spec FROM index_versions'):
            vectors = VectorIndex(root, version['id'])
            wanted = {r['id'] for r in store.rows(
                'SELECT id FROM representations WHERE index_id=?', (version['id'],)
            )}
            if vectors.ids() != wanted:
                raise ValueError(f'索引 {version["id"]} 的向量和表示不一致')
            dimension = json.loads(version['spec'])['space'].get('dimensions')
            representations = {r['id']: r for r in store.rows(
                'SELECT * FROM representations WHERE index_id=?', (version['id'],)
            )}
            snapshots = {json.loads(m['snapshot'])['id']: json.loads(m['snapshot']) for m in store.rows(
                'SELECT snapshot FROM index_members WHERE index_id=?', (version['id'],)
            )}
            ids = sorted(wanted)
            for offset in range(0, len(ids), BATCH_SIZE):
                batch = vectors.collection.get(ids=ids[offset:offset + BATCH_SIZE],
                                               include=['embeddings', 'metadatas', 'documents'])
                for rid, vector, metadata, document in zip(
                    batch['ids'], batch['embeddings'], batch['metadatas'], batch['documents'], strict=True
                ):
                    if (dimension and len(vector) != dimension) or not all(math.isfinite(n) for n in vector):
                        raise ValueError(f'向量维度或数值无效：{rid}')
                    rep = representations[rid]
                    member = snapshots[rep['segment_id']]
                    expected = {'segment_id': member['id'], 'source_id': member['source_id'],
                                'granularity': member['kind'], 'view': rep['view']}
                    if not metadata or any(metadata.get(key) != value for key, value in expected.items()):
                        raise ValueError(f'向量元数据归属不一致：{rid}')
                    if document != (rep['document'] or rep['view']):
                        raise ValueError(f'向量文档与表示不一致：{rid}')
            if version['status'] == 'ready':
                issues = check_index(store, version['id'])
                if issues:
                    raise ValueError('索引检查失败：' + '; '.join(issues[:5]))
        return {'ok': True}
    raise ValueError('不支持的向量操作')


if __name__ == '__main__':
    try:
        result = run(json.loads(Path(sys.argv[1]).read_text()))
        print(dumps({'result': result}))
    except Exception as exc:  # noqa: BLE001 — 子进程边界统一返回结构化失败，主进程拒绝发布
        print(dumps({'error': str(exc)}))
        sys.exit(1)
