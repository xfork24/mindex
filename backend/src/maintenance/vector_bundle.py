"""向量集合的原样导出与装载（plans §10 阶段 5）。

- **导出**：对每个 ready 索引版本分页读取全部向量（id/embedding/metadata/document），
  embedding 以 float32 原始字节的 base64 保存（不经 JSON 浮点文本往返），逐集合写
  ``vectors/<index_id>.jsonl`` + 摘要清单。
- **装载**：在目标库按同一 id/embedding/metadata/document 重建集合并分批 upsert；
  不重新嵌入（plans §10：物理 ANN 图可能变化，召回按 ANN 容差验收）。chroma 在
  cosine 空间写入时会重新归一化并做 float32 舍入，装载后 embedding 与源集合存在
  约 1e-7 的元素级差异——不承诺逐位相等，id/文档/元数据逐项保真。
- 集合名/余弦空间/embedding_function=None 与在线构建一致；chroma_server 形态经
  注入的 client 读写同一远端集合。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from mindex.maintenance.backup_files import digest as file_digest
from mindex.maintenance.backup_files import safe_path

if TYPE_CHECKING:
    from mindex.shared.storage import Store

log = logging.getLogger(__name__)

__all__ = ["export_vectors", "load_vectors"]

_BATCH = 500


def _collection(store: Store, index_id: str, *, create: bool, config=None, legacy_namespace=False):
    from mindex.processing.indexing import VectorIndex, vector_client

    client = vector_client(config, library_id=store.library_id or None) if config is not None else None
    if legacy_namespace and config is not None and config.vector.backend == "chroma_server":
        from mindex.shared.backends.vectors import chroma_http_client

        client = chroma_http_client(config.vector.chroma_server)
    return VectorIndex(store.root, index_id, create=create, client=client).collection


def export_vectors(store: Store, target: Path, *, config=None, legacy_namespace=False) -> dict:
    """把全部 ready 索引版本的向量原样导出到 ``target/vectors/``；返回逐集合清单。"""
    directory = Path(target) / "vectors"
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for row in store.rows("SELECT id FROM index_versions WHERE status='ready' ORDER BY id"):
        index_id = row["id"]
        try:
            collection = _collection(store, index_id, create=False, config=config,
                                     legacy_namespace=legacy_namespace)
            total = collection.count()
        except Exception as exc:
            raise ValueError(f"就绪索引 {index_id} 的向量集合不可用，不能生成完整备份") from exc
        digest = hashlib.sha256()
        written = 0
        path = directory / f"{index_id}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for offset in range(0, total, _BATCH):
                batch = collection.get(limit=_BATCH, offset=offset,
                                       include=["embeddings", "documents", "metadatas"])
                embeddings = batch.get("embeddings")
                documents = batch.get("documents") or [None] * len(batch["ids"])
                metadatas = batch.get("metadatas") or [None] * len(batch["ids"])
                for identifier, embedding, document, metadata in zip(
                        batch["ids"], embeddings if embeddings is not None else [], documents, metadatas):
                    line = json.dumps({
                        "id": identifier,
                        # float32 原始字节 base64：避免十进制文本往返误差
                        "embedding": base64.b64encode(
                            _float32_bytes(embedding)).decode("ascii"),
                        "document": document,
                        "metadata": metadata,
                    }, ensure_ascii=False, sort_keys=True)
                    handle.write(line + "\n")
                    digest.update((line + "\n").encode())
                    written += 1
        if written != total or collection.count() != total:
            raise ValueError(f"向量集合 {index_id} 在导出期间变化或缺少 embedding")
        manifest[index_id] = {"vectors": written, "sha256": digest.hexdigest(),
                              "file": path.name, "metadata": collection.metadata}
    (Path(target) / "vectors_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("向量导出完成：%d 个集合 → %s", len(manifest), directory)
    return manifest


def load_vectors(store: Store, source: Path, *, config=None, journal=None) -> dict[str, int]:
    """从 ``source/vectors/*.jsonl`` 重建集合；返回逐集合写入条数（不重新嵌入）。"""
    manifest = validate_vectors(source)
    counts = {}
    # 全部文件校验通过后才接触目标集合。
    for index_id, entry in manifest.items():
        path = safe_path(Path(source) / "vectors", entry["file"])
        if journal:
            journal.object('collection', index_id, entry['sha256'], state='pending')
        collection = _collection(store, index_id, create=True, config=config)
        if entry.get("metadata") != collection.metadata:
            raise ValueError(f"目标向量集合身份不一致：{index_id}")
        # 先验证全部已有值与集合闭包，再追加缺失 ID，绝不盲目 upsert 已有值。
        before, matched = collection.count(), 0
        for batch in _batches(path):
            existing = collection.get(ids=[item['id'] for item in batch], include=["embeddings", "documents", "metadatas"])
            _compare_batch(index_id, batch, existing, require_all=False)
            matched += len(existing['ids'])
        if matched != before:
            raise ValueError(f"目标向量集合存在清单外数据，拒绝写入：{index_id}")
        for number, batch in enumerate(_batches(path)):
            batch_sha = hashlib.sha256(json.dumps(batch, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            batch_key = f'{index_id}/{number}'
            if journal:
                journal.object('vector-batch', batch_key, batch_sha, state='pending')
            ids = [item["id"] for item in batch]
            existing = collection.get(ids=ids, include=["embeddings", "documents", "metadatas"])
            _compare_batch(index_id, batch, existing, require_all=False)
            present = set(existing['ids'])
            missing = [item for item in batch if item['id'] not in present]
            if missing:
                _upsert(collection, [item['id'] for item in missing], [_from_float32(item['embedding']) for item in missing],
                        [item.get('document') for item in missing], [item.get('metadata') for item in missing])
            actual = collection.get(ids=ids, include=["embeddings", "documents", "metadatas"])
            _compare_batch(index_id, batch, actual)
            if journal:
                journal.object('vector-batch', batch_key, batch_sha, state='verified')
        if collection.count() != entry["vectors"] or collection.metadata != entry.get('metadata'):
            raise ValueError(f"目标向量集合存在清单外数据或身份变化：{index_id}")
        if journal:
            journal.object('collection', index_id, entry['sha256'], state='verified')
        counts[index_id] = entry["vectors"]
    return counts


def _batches(path):
    batch = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            batch.append(item)
            if len(batch) == _BATCH:
                yield batch
                batch = []
    if batch:
        yield batch


def validate_vectors(source):
    source = Path(source)
    manifest_path = source / "vectors_manifest.json"
    if not manifest_path.is_file():
        if (source / "vectors").exists():
            raise ValueError("缺少 vectors_manifest.json")
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for index_id, entry in manifest.items():
        if "skipped" in entry:
            raise ValueError(f"向量备份不完整：{index_id}")
        path = safe_path(source / "vectors", entry["file"])
        if not path.is_file() or file_digest(path) != entry["sha256"]:
            raise ValueError(f"向量文件摘要不一致：{index_id}")
        identifiers, dimensions = set(), None
        for batch in _batches(path):
            for item in batch:
                identifier = item["id"]
                if identifier in identifiers:
                    raise ValueError(f"向量备份含重复 ID：{identifier}")
                identifiers.add(identifier)
                embedding = _from_float32(item["embedding"])
                if not embedding or (dimensions is not None and len(embedding) != dimensions):
                    raise ValueError(f"向量维数不一致：{index_id}")
                import math
                if not all(math.isfinite(value) for value in embedding):
                    raise ValueError(f"向量含非有限值：{identifier}")
                dimensions = len(embedding)
        if len(identifiers) != entry["vectors"]:
            raise ValueError(f"向量条数与清单不一致：{index_id}")
    return manifest


def _compare_batch(index_id, batch, actual, *, require_all=True):
    import numpy as np
    by_id = {identifier: (vector, document, metadata)
             for identifier, vector, document, metadata in zip(
                 actual["ids"], actual["embeddings"], actual["documents"], actual["metadatas"])}
    for item in batch:
        if item["id"] not in by_id:
            if require_all:
                raise ValueError(f"目标缺少向量：{index_id}/{item['id']}")
            continue
        vector, document, metadata = by_id[item["id"]]
        expected = _from_float32(item["embedding"])
        if (document != item.get("document") or metadata != item.get("metadata")
                or len(vector) != len(expected) or not np.allclose(vector, expected, atol=1e-6, rtol=0)):
            raise ValueError(f"目标向量存在异值：{index_id}/{item['id']}")


def verify_vectors(store, source, *, config=None):
    manifest = validate_vectors(source)
    differences = []
    for index_id, entry in manifest.items():
        try:
            collection = _collection(store, index_id, create=False, config=config)
            if collection.count() != entry["vectors"] or collection.metadata != entry.get("metadata"):
                raise ValueError("集合身份或条数不一致")
            for batch in _batches(safe_path(Path(source) / "vectors", entry["file"])):
                actual = collection.get(ids=[item["id"] for item in batch],
                                        include=["embeddings", "documents", "metadatas"])
                _compare_batch(index_id, batch, actual)
        except Exception:  # noqa: BLE001 — 对账收集差异，由 CLI 以非零退出
            differences.append(f"vectors:{index_id}")
    return differences


def _upsert(collection, ids, embeddings, documents, metadatas) -> None:
    collection.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)


def _float32_bytes(embedding) -> bytes:
    import numpy as np

    return np.asarray(embedding, dtype="<f4").tobytes()


def _from_float32(encoded: str) -> list[float]:
    import numpy as np

    # chroma 只接受 Python float 或 numpy.ndarray：转成 float 列表（值仍是 float32 精度）。
    return np.frombuffer(base64.b64decode(encoded, validate=True), dtype="<f4").astype("float64").tolist()
