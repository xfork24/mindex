"""VectorIndex 对 chroma 元数据/HNSW 段不一致的自愈与强制落盘。

chroma 的元数据段每笔提交，HNSW 段仅在累计 sync_threshold（默认 1000）个元素时
才把 id_map 与索引写入磁盘；未冲过阈值的尾部向量只存在于内存与 WAL，进程重启或
跨进程访问后可能变成「元数据有、HNSW 无」的孤儿，向量复用时 chroma 报
「Error executing plan: Internal error: Error finding id」。
"""

import pickle

from mindex.processing.indexing import VectorIndex


def make_index(root, name="ix-vector-index-test"):
    return VectorIndex(root, name, create=True)


def make_segment(number):
    return {"id": f"seg-{number}", "source_id": "src-1", "kind": "shot"}


def test_vector_returns_none_when_hnsw_missing_id(tmp_path, monkeypatch):
    """复用读取遇 HNSW 缺 id 的库内不一致时降级为 None，由上层重新嵌入。"""
    index = make_index(tmp_path)
    index.upsert("rep-1", [0.1, 0.2], make_segment(1), "video", "视频")
    assert index.vector("rep-1") is not None

    def raise_missing(self, *args, **kwargs):
        raise RuntimeError("Error executing plan: Internal error: Error finding id")

    monkeypatch.setattr(type(index.collection), "get", raise_missing)
    assert index.vector("rep-1") is None


def test_vector_reraises_other_chroma_errors(tmp_path, monkeypatch):
    """其他 chroma 错误不吞，照常抛出。"""
    index = make_index(tmp_path)
    index.upsert("rep-1", [0.1, 0.2], make_segment(1), "video", "视频")

    def raise_other(self, *args, **kwargs):
        raise RuntimeError("别的错误")

    monkeypatch.setattr(type(index.collection), "get", raise_other)
    try:
        index.vector("rep-1")
    except RuntimeError as exc:
        assert "别的错误" in str(exc)
    else:
        raise AssertionError("应当原样抛出非缺 id 的 chroma 错误")


def test_flush_persists_hnsw_id_map(tmp_path):
    """flush 以幂等重写冲过 chroma 落盘阈值，HNSW id_map 完整落盘。"""
    index = make_index(tmp_path)
    for i in range(5):
        index.upsert(f"rep-{i}", [float(i), 0.1], make_segment(i), "video", "视频")

    chroma = tmp_path / "chroma"
    pickle_files = [p / "index_metadata.pickle" for p in chroma.iterdir() if p.is_dir()]
    assert pickle_files and not any(p.exists() for p in pickle_files)  # 未冲阈值：只在内存与 WAL

    index.flush()

    saved = set()
    for path in pickle_files:
        if path.exists():
            with open(path, "rb") as handle:
                saved.update(pickle.load(handle)["id_to_label"])
    assert saved == {f"rep-{i}" for i in range(5)}


def test_flush_skips_empty_collection(tmp_path):
    """空集合 flush 直接跳过，不触发任何 chroma 读写。"""
    index = make_index(tmp_path, "ix-empty-flush")
    index.flush()  # 不应抛错
