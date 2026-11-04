"""向量集合原样导出与装载（plans §10 阶段 5）。

不变量：id / document / metadata 逐项相等；embedding 以 float32 原始字节导出，
装载后与源集合逐元素差 ≤1e-6（chroma 在 cosine 空间写入时会重新归一化并做
float32 舍入）。plan 明确物理 ANN 图可能变化、召回按 ANN 容差验收，因此不做
逐位相等承诺。同时覆盖空集合与缺失集合的跳过语义。
"""

from __future__ import annotations

import random

from mindex.maintenance.vector_bundle import export_vectors, load_vectors
from mindex.shared.config import Config
from mindex.shared.storage import Store, dumps


def _seed(config: Config, index_id: str = "idx-vectors"):
    from mindex.processing.indexing import VectorIndex

    store = Store(config.data_dir)
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES(?,?,'ready')",
                  (index_id, dumps({"format": 2, "space": {}, "preprocessing": {}})))
    collection = VectorIndex(store.root, index_id, create=True).collection
    rng = random.Random(11)
    rows = [
        (f"rep{j}", [rng.random() for _ in range(8)], f"doc{j}",
         {"segment_id": f"seg{j}", "source_id": "src", "granularity": "shot", "view": "visual"})
        for j in range(7)
    ]
    collection.upsert(
        ids=[r[0] for r in rows], embeddings=[r[1] for r in rows],
        documents=[r[2] for r in rows], metadatas=[r[3] for r in rows])
    return store, index_id, rows


def test_export_load_roundtrip_preserves_ids_vectors_metadata(tmp_path):
    config = Config(data_dir=tmp_path / "origin")
    config.embedding.dimensions = 8
    store, index_id, rows = _seed(config)
    manifest = export_vectors(store, tmp_path / "bundle")
    assert manifest[index_id]["vectors"] == 7
    # float32 字节存盘：体积确定、可校验
    lines = (tmp_path / "bundle" / "vectors" / f"{index_id}.jsonl").read_text().splitlines()
    assert len(lines) == 7

    target = Config(data_dir=tmp_path / "target")
    target.embedding.dimensions = 8
    target_store = Store(target.data_dir)
    counts = load_vectors(target_store, tmp_path / "bundle")
    assert counts == {index_id: 7}

    from mindex.processing.indexing import VectorIndex

    # 不变量：导出端「已存储的 float32 向量」经 base64 往返后逐位不变
    # （chroma 在 cosine 空间会归一化存储，故基准取源集合实际返回值，而非原始输入）。
    origin_collection = VectorIndex(store.root, index_id).collection
    source = origin_collection.get(ids=[r[0] for r in rows],
                                   include=["embeddings", "documents", "metadatas"])
    baseline = {identifier: (embedding.tolist(), document, metadata)
                for identifier, embedding, document, metadata
                in zip(source["ids"], source["embeddings"], source["documents"], source["metadatas"])}

    collection = VectorIndex(target_store.root, index_id).collection
    assert collection.count() == 7
    got = collection.get(ids=[r[0] for r in rows], include=["embeddings", "documents", "metadatas"])
    by_id = {identifier: (embedding.tolist(), document, metadata)
             for identifier, embedding, document, metadata
             in zip(got["ids"], got["embeddings"], got["documents"], got["metadatas"])}
    for identifier, (source_embedding, document, metadata) in baseline.items():
        stored_embedding, stored_document, stored_metadata = by_id[identifier]
        assert stored_document == document
        assert stored_metadata == metadata
        for a, b in zip(stored_embedding, source_embedding):
            assert abs(a - b) <= 1e-6  # 归一化 + float32 舍入容差（远小于 ANN 容差）


def test_missing_ready_collection_fails_export(tmp_path):
    import pytest
    config = Config(data_dir=tmp_path / "origin")
    store = Store(config.data_dir)
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('gone','{}','ready')")
    with pytest.raises(ValueError, match="不能生成完整备份"):
        export_vectors(store, tmp_path / "bundle")


def test_load_without_vectors_directory_is_noop(tmp_path):
    store = Store(tmp_path / "target")
    assert load_vectors(store, tmp_path / "empty") == {}
