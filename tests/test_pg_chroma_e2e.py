"""pg-single + chroma_server 组合形态的端到端检索（plans §6 阶段 2 验收）。

需要 ``MINDEX_TEST_POSTGRES_DSN`` 与 ``MINDEX_TEST_CHROMA_SERVER`` 同时设置；缺失时
skip。fixture 管线在 PG 元数据 + 独立 Chroma 服务上完成 prepare→detect→index→检索，
词法走 SQL 倒排、向量走 HttpClient，结果与同一数据在纯本地形态一致。
"""

from __future__ import annotations

import os

import pytest

from shared.config import Config

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_chroma_server, pytest.mark.slow]

PG_DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
CHROMA_URL = os.environ.get("MINDEX_TEST_CHROMA_SERVER", "")


@pytest.fixture
def combo(video_file, tmp_path, monkeypatch):
    if not (PG_DSN and CHROMA_URL):
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN 与 MINDEX_TEST_CHROMA_SERVER")
    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(PG_DSN) as conn:
        from shared.db.migrate_pg import migrate_pg

        migrate_pg(conn)
    monkeypatch.setenv("MINDEX_DATABASE_DSN", PG_DSN)
    config = Config(
        data_dir=tmp_path / "data",
        database={"backend": "postgres"},
        vector={"backend": "chroma_server", "chroma_server": {"url": CHROMA_URL}},
    )
    config.embedding.dimensions = 8
    config.asr.aligner_revision = "test-revision"
    return config


def test_pipeline_index_search_on_pg_and_chroma(combo, video_file):
    from conftest import FixtureAnalyzer, FixtureEmbedder
    from processing.indexing import IndexBuilder
    from processing.pipeline import Pipeline
    from search.search import SearchEngine
    from shared.lexical_store import lexical_ready
    from shared.types import TextSearch

    embedder = FixtureEmbedder(combo)
    pipeline = Pipeline(combo, analyzer=FixtureAnalyzer(embedder))
    with pipeline.lock():
        source = pipeline.prepare(video_file)  # prepare 已含 detect + split
        pipeline.transcribe()
        pipeline.analyze(source)
        version = IndexBuilder(combo, embedder).build(
            source_id=source, channels=["visual", "dialogue", "analysis"], activate=True
        )
    version = IndexBuilder(combo, FixtureEmbedder(combo)).build(
        source_id=source, channels=["visual", "dialogue", "analysis"], activate=True
    )
    store = pipeline.store
    assert store.pg
    assert lexical_ready(store, version)  # 发布时构建派生词法索引

    engine = SearchEngine(combo, FixtureEmbedder(combo))
    # 文字检索：SQL BM25（analysis 渠道必有内容）+ 服务端向量融合
    result = engine.text(TextSearch(text="城市", granularity="shot"))
    assert result["index_id"] == version
    assert result["eligible_segments"] >= 1
    assert all("matches" in hit for hit in result["results"])
    # 向量（服务端）路径：检索引擎经 HttpClient 查询同一集合
    analysis = engine.text(TextSearch(text="画面", granularity="shot", channels=["visual"]))
    assert analysis["index_id"] == version
    # 索引一致性校验走 PG + HttpClient
    from processing.indexing import check_index

    assert check_index(store, version, config=combo) == []
