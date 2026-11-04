"""独立 Chroma 服务的向量后端（plans §6.2 阶段 2 验收）。

经 ``MINDEX_TEST_CHROMA_SERVER``（服务 URL，如 http://localhost:55500）门控，缺失时
整文件 skip；提交的集群实现必须在 CI 专项启用（plans §12）。覆盖：集合创建/写入/
读取/查询/删除与本地实现同协议、HttpClient 进程内共享、配置组合校验
（chroma_server 需要 postgres 元数据库）。
"""

from __future__ import annotations

import os
import random

import pytest

from mindex.shared.backends.vectors import ChromaServerVectorStores as ServerStores

pytestmark = pytest.mark.requires_chroma_server

URL = os.environ.get("MINDEX_TEST_CHROMA_SERVER", "")


@pytest.fixture
def server():
    if not URL:
        pytest.skip("需要 MINDEX_TEST_CHROMA_SERVER（独立 Chroma 服务）")
    from mindex.shared.config import ChromaServerConfig

    return ServerStores(ChromaServerConfig(url=URL, token_env="MINDEX_TEST_CHROMA_TOKEN_UNUSED"))


def test_server_store_roundtrip_and_query(server):
    index_id = "srv" + f"{random.randint(0, 999999):06d}"
    store = server.open(index_id, create=True)
    assert store.count() == 0
    rng = random.Random(3)
    entries = [
        (f"rep{j}", [rng.random() for _ in range(8)],
         {"id": f"seg{j}", "source_id": "src", "kind": "shot"}, "visual", f"doc{j}")
        for j in range(6)
    ]
    store.upsert_many(entries)
    assert store.count() == 6
    assert store.ids() == {f"rep{j}" for j in range(6)}
    got = store.vectors(["rep0"])
    assert got["rep0"] == pytest.approx(entries[0][1])
    hits = store.query(entries[0][1], count=3)
    assert hits[0]["id"] == "rep0" and hits[0]["distance"] < 1e-6
    assert hits[0]["metadata"]["segment_id"] == "seg0"
    filtered = store.query(entries[0][1], count=6, where={"view": "analysis"})
    assert filtered == []
    assert len(store.query(entries[0][1], count=3, where={"view": "visual"})) == 3
    store.delete(["rep5"])
    assert store.count() == 5
    reopen = server.open(index_id)  # 同名集合重开仍可见（服务端持久）
    assert reopen.count() == 5


def test_http_client_shared_per_url_token(monkeypatch):
    import chromadb

    from mindex.shared.backends.vectors import chroma_http_client
    from mindex.shared.config import ChromaServerConfig

    section = ChromaServerConfig(url=URL or "http://127.0.0.1:59999", token_env="MINDEX_NO_TOKEN")
    monkeypatch.delenv("MINDEX_NO_TOKEN", raising=False)
    calls = []
    def client(**kwargs):
        calls.append(kwargs)
        return object()
    monkeypatch.setattr(chromadb, "HttpClient", client)
    monkeypatch.setattr("mindex.shared.backends.vectors._HTTP_CLIENTS", {})
    assert chroma_http_client(section) is chroma_http_client(section)  # 进程内共享
    assert len(calls) == 1


def test_chroma_server_requires_postgres(tmp_path):
    from mindex.shared.backends import open_context
    from mindex.shared.config import Config

    config = Config(data_dir=tmp_path / "data", vector={"backend": "chroma_server"})
    with pytest.raises(RuntimeError, match="postgres"):
        open_context(config)
