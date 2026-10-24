"""GET /api/studio/jobs/{job}/material-digest：返回字段、404、只读不加锁（需求 3.3–3.6、12.4）。

全部在 conftest 的临时数据目录中运行：用 Pipeline + IndexBuilder 构建真实生效索引，不触碰工作区 data/。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder
from fastapi.testclient import TestClient
from filelock import FileLock

from mindex.admin.curation import CurationPatch, Library
from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.studio import studio
from mindex.studio.material_digest import EMPTY_BUDGET, EMPTY_INDEX, EMPTY_SEGMENTS, build_digest


def tree_hashes(directory: Path) -> dict[str, str]:
    """目录下全部文件的 sha256（相对路径 → 哈希），用来断言请求前后逐字节一致。"""
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def indexed(config, video_file):
    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    source_id = pipeline.prepare(video_file)
    pipeline.analyze(source_id)
    IndexBuilder(config, embedder).build(activate=True)
    client = TestClient(create_app(config, embedder))
    return config, client, pipeline.store, source_id


def test_returns_digest_body_and_stats(indexed):
    config, client, store, source_id = indexed
    job_id = studio.create_job(config, "港口的一天。")["id"]
    response = client.get(f"/api/studio/jobs/{job_id}/material-digest")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"text", "sources", "stats", "empty_reason"}
    assert body["empty_reason"] is None and body["text"]
    expected = build_digest(config, store, studio.load_job(config, job_id))
    assert body == expected.public()
    stats = body["stats"]
    assert set(stats) == {"sources", "segments", "fingerprint", "content_index_id"}
    assert stats["sources"] == 1 and stats["segments"] > 0 and stats["fingerprint"]
    assert stats["content_index_id"] == store.active_index()["id"]
    assert [s["source_id"] for s in body["sources"]] == [source_id]


def test_scope_without_analyzed_segments_reports_reason(indexed):
    config, client, store, source_id = indexed
    job_id = studio.create_job(config, "素材已隐藏。", options={"source_ids": [source_id]})["id"]
    Library(store).patch("source", source_id, CurationPatch(hidden=True))
    body = client.get(f"/api/studio/jobs/{job_id}/material-digest").json()
    assert body["empty_reason"] == EMPTY_SEGMENTS
    assert body["text"] == "" and body["sources"] == []
    assert body["stats"]["sources"] == 0 and body["stats"]["segments"] == 0


def test_budget_zero_reason_takes_priority(indexed):
    config, client, _store, _source_id = indexed
    job_id = studio.create_job(config, "关闭摘要。")["id"]
    config.studio.material_digest_chars = 0
    body = client.get(f"/api/studio/jobs/{job_id}/material-digest").json()
    assert body["empty_reason"] == EMPTY_BUDGET and body["text"] == ""


def test_no_active_index_reason(config):
    client = TestClient(create_app(config))
    job_id = studio.create_job(config, "还没有索引。")["id"]
    response = client.get(f"/api/studio/jobs/{job_id}/material-digest")
    assert response.status_code == 200
    assert response.json()["empty_reason"] == EMPTY_INDEX


def test_unknown_job_returns_404(config):
    client = TestClient(create_app(config))
    response = client.get("/api/studio/jobs/20990101-000000-abcdef/material-digest")
    assert response.status_code == 404
    assert "找不到创作任务" in response.json()["detail"]


def test_read_only_and_works_while_lock_held(indexed):
    config, client, _store, _source_id = indexed
    job_id = studio.create_job(config, "只读预览。")["id"]
    directory = studio.job_dir(config, job_id)
    before = tree_hashes(directory)
    lock = FileLock(str(studio.lock_path(config)), timeout=0, thread_local=False)
    lock.acquire()
    try:
        first = client.get(f"/api/studio/jobs/{job_id}/material-digest")
        second = client.get(f"/api/studio/jobs/{job_id}/material-digest")
    finally:
        lock.release()
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    assert tree_hashes(directory) == before
