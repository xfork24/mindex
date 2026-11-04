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

if TYPE_CHECKING:
    from mindex.shared.storage import Store

log = logging.getLogger(__name__)

__all__ = ["export_vectors", "load_vectors"]

_BATCH = 500


def _collection(store: Store, index_id: str, *, create: bool, config=None):
    from mindex.processing.indexing import VectorIndex, vector_client

    client = vector_client(config) if config is not None else None
    return VectorIndex(store.root, index_id, create=create, client=client).collection


def export_vectors(store: Store, target: Path, *, config=None) -> dict:
    """把全部 ready 索引版本的向量原样导出到 ``target/vectors/``；返回逐集合清单。"""
    directory = Path(target) / "vectors"
    directory.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    for row in store.rows("SELECT id FROM index_versions WHERE status='ready' ORDER BY id"):
        index_id = row["id"]
        try:
            collection = _collection(store, index_id, create=False, config=config)
            total = collection.count()
        except Exception as exc:  # noqa: BLE001 — 集合缺失（被清理）时跳过并记录
            log.warning("索引 %s 的向量集合不可用，跳过导出：%s", index_id, exc)
            manifest[index_id] = {"vectors": 0, "skipped": str(exc).splitlines()[0]}
            continue
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
                    digest.update(line.encode())
                    written += 1
        manifest[index_id] = {"vectors": written, "sha256": digest.hexdigest(),
                              "file": path.name}
    (Path(target) / "vectors_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("向量导出完成：%d 个集合 → %s", len(manifest), directory)
    return manifest


def load_vectors(store: Store, source: Path, *, config=None) -> dict[str, int]:
    """从 ``source/vectors/*.jsonl`` 重建集合；返回逐集合写入条数（不重新嵌入）。"""
    directory = Path(source) / "vectors"
    counts: dict[str, int] = {}
    if not directory.is_dir():
        return counts
    import chromadb  # noqa: F401 — 确保依赖就绪（错误信息更直接）

    for path in sorted(directory.glob("*.jsonl")):
        index_id = path.stem
        collection = _collection(store, index_id, create=True, config=config)
        ids, embeddings, documents, metadatas = [], [], [], []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            ids.append(item["id"])
            embeddings.append(list(_from_float32(item["embedding"])))
            documents.append(item.get("document"))
            metadatas.append(item.get("metadata"))
            if len(ids) >= _BATCH:
                _upsert(collection, ids, embeddings, documents, metadatas)
                counts[index_id] = counts.get(index_id, 0) + len(ids)
                ids, embeddings, documents, metadatas = [], [], [], []
        if ids:
            _upsert(collection, ids, embeddings, documents, metadatas)
            counts[index_id] = counts.get(index_id, 0) + len(ids)
    log.info("向量装载完成：%s", counts)
    return counts


def _upsert(collection, ids, embeddings, documents, metadatas) -> None:
    collection.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)


def _float32_bytes(embedding) -> bytes:
    import numpy as np

    return np.asarray(embedding, dtype=np.float32).tobytes()


def _from_float32(encoded: str) -> list[float]:
    import numpy as np

    # chroma 只接受 Python float 或 numpy.ndarray：转成 float 列表（值仍是 float32 精度）。
    return np.frombuffer(base64.b64decode(encoded), dtype=np.float32).astype("float64").tolist()
