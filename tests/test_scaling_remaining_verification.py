"""水平扩展剩余项 1–12 的功能/连通性验证（不做规模压测）。

每项先断言**当前已可用的行为**，再对尚未交付的能力用 ``pytest.xfail(reason)``
标注——xfail 的原因即缺口，能力交付后该断言会自然通过（届时删除 xfail 分支）。

需要 ``MINDEX_TEST_POSTGRES_DSN``；涉及对象存储的项另需 ``MINDEX_TEST_S3_ENDPOINT``。
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import time

import pytest

from mindex.shared.config import Config
from mindex.shared.storage import Store

PG = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
S3 = os.environ.get("MINDEX_TEST_S3_ENDPOINT", "")
ACCESS = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
SECRET = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pg(tmp_path, monkeypatch):
    """空库基线（每个用例独立 schema）。"""
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg

    if not PG:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", PG)
    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(PG) as conn:
        migrate_pg(conn)


@pytest.fixture
def s3_env(monkeypatch):
    if not S3:
        pytest.skip("需要 MINDEX_TEST_S3_ENDPOINT")
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", ACCESS)
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", SECRET)


def _bucket_store(tmp_path, monkeypatch, *, bucket, queue="inline", locks="file"):
    import boto3

    from mindex.shared.backends import open_context

    client = boto3.client("s3", endpoint_url=S3, aws_access_key_id=ACCESS,
                          aws_secret_access_key=SECRET, region_name="us-east-1")
    try:
        client.create_bucket(Bucket=bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
    config = Config(
        data_dir=tmp_path / "data",
        database={"backend": "postgres"},
        storage={"backend": "s3", "s3": {"endpoint_url": S3, "bucket": bucket,
                                         "prefix": "libs/"}},
        queue={"backend": queue},
        locks={"backend": locks},
    )
    config.embedding.dimensions = 8
    config.asr.aligner_revision = "test-revision"
    return config, open_context(config), client


# ---------------------------------------------------------------- 1. 锁接线


def test_item01_processing_lock_coordinates_via_pg(pg, tmp_path):
    from filelock import Timeout

    from mindex.shared.processing_lock import ProcessingLock

    first_config = Config(data_dir=tmp_path / "a", database={"backend": "postgres"},
                          locks={"backend": "postgres"})
    second_config = Config(data_dir=tmp_path / "b", database={"backend": "postgres"},
                           locks={"backend": "postgres"})
    first = ProcessingLock(first_config)
    first.acquire()
    second = ProcessingLock(second_config)
    try:
        try:
            second.acquire(blocking=False)
        except Timeout:
            return  # 期望：PG 租约让不同 data_dir 的进程也互斥
        second.release()
        pytest.xfail("ProcessingLock 仍按 data_dir 文件锁互斥；locks.backend=postgres 未接线")
    finally:
        first.release()


# ---------------------------------------------------------------- 2. 队列/账本补偿


def test_item02_ledger_enqueue_compensation_is_idempotent(pg, tmp_path):
    from mindex.shared.backends import open_context
    from mindex.shared.db.queue_pg import PgTaskQueue
    from mindex.shared.queue_dispatch import enqueue_backlog

    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"},
                    queue={"backend": "postgres"})
    context = open_context(config)
    context.store.execute(
        "INSERT INTO source_tasks(id,source_id,work,status,config,options) "
        "VALUES('t-strand','s1','detect','queued','{}','{}')")
    assert enqueue_backlog(config) == {"source_tasks": 1}  # 账本先落、入队前崩溃的补偿
    assert enqueue_backlog(config) == {"source_tasks": 1}  # 幂等：dedupe 命中
    queue = PgTaskQueue(context.library_id)
    with context.store.backend.connect() as db:
        claimed = queue.claim(db, "verify-worker", ["source"])
        assert claimed is not None and claimed.ledger_id == "t-strand"
        assert queue.complete(db, claimed)


# ---------------------------------------------------------------- 3. Studio 条件发布


def test_item03_studio_workspace_publish_revision_guard():
    from mindex.studio.workspace import versions

    assert callable(versions.require_revision)  # 方案预检已有修订校验
    assert "expected" in inspect.signature(versions.require_revision).parameters
    if "expected_revision" not in inspect.signature(versions.persist).parameters:
        pytest.xfail("workspace persist 无 expected_revision：并发写者可覆盖（条件发布未交付）")


# ---------------------------------------------------------------- 4. 检查点远端化


def test_item04_checkpoints_stay_on_node(pg, tmp_path, video_file, monkeypatch, s3_env):
    from conftest import FixtureAnalyzer, FixtureEmbedder

    from mindex.processing.pipeline import Pipeline

    config, context, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-checkpoints")
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(FixtureEmbedder(config)))
    with pipeline.lock():
        source = pipeline.prepare(video_file)
        pipeline.transcribe()
        pipeline.analyze(source)
    local_analysis = list((config.data_dir / "analysis").rglob("*"))
    local_plans = list((config.data_dir / "plans").rglob("*"))
    assert local_analysis and local_plans  # 当前：本地检查点存在、可断点续跑
    remote = set(context.media.iter_prefix("analysis/")) | set(context.media.iter_prefix("plans/"))
    if not remote:
        pytest.xfail("检查点（analysis/plans/transcripts）仍写节点本地目录，未进对象存储")


# ---------------------------------------------------------------- 5. uploads 远端化


def test_item05_upload_lands_locally(pg, tmp_path, video_file, monkeypatch, s3_env):
    from fastapi.testclient import TestClient

    from mindex.api import create_app

    config, context, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-uploads")
    app = create_app(config)
    client = TestClient(app)
    with video_file.open("rb") as handle:
        response = client.post("/api/admin/sources/upload",
                               files={"file": ("clip.mp4", handle, "video/mp4")})
    assert response.status_code == 201, response.text
    assert (config.data_dir / "sources").is_dir()  # 当前：原片落在节点本地 sources/
    uploaded = list(context.media.iter_prefix("sources/"))
    if not uploaded:
        pytest.xfail("上传仍写本地 sources/ 与 .uploads/，未走 S3 上传协议（multipart/complete/不可变 key）")


# ---------------------------------------------------------------- 6. GC pin


def test_item06_download_pin_blocks_purge(pg, tmp_path, monkeypatch, s3_env):
    import hashlib
    import io

    from mindex.maintenance.purge import purge_source
    from mindex.shared.media_delivery import DownloadPins

    _, context, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-gc")
    media = context.media
    payload = b"clip-bytes"
    media.put_immutable("clips/s1/shot/a.mp4", io.BytesIO(payload),
                        sha256=hashlib.sha256(payload).hexdigest())
    context.store.execute("INSERT INTO sources(id,name,path,media,layout_key) "
                          "VALUES('s1','素材','sources/s1/a.mp4','{}','k')")
    pins = DownloadPins(context.store, context.library_id)
    pins.pin("clips/s1/shot/a.mp4", 900)  # 预签名下载仍有效
    purge_source(context.store, "s1", media=media, pins=pins)
    assert "clips/s1/shot/a.mp4" in set(media.iter_prefix("clips/"))  # 下载保留期内不删


def test_item06_clean_does_not_collect_s3_orphans(pg, tmp_path, monkeypatch, s3_env):
    import hashlib
    import io

    from mindex.maintenance.clean import clean_library

    config, context, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-clean")
    payload = b"orphan"
    context.media.put_immutable("clips/orphan/x.mp4", io.BytesIO(payload),
                                sha256=hashlib.sha256(payload).hexdigest())
    clean_library(config, apply=True)
    leftover = set(context.media.iter_prefix("clips/"))
    if "clips/orphan/x.mp4" in leftover:
        pytest.xfail("clean 只扫描本地目录，S3 孤立对象未被回收（GC 远端化未交付）")


# ---------------------------------------------------------------- 7. persons PG


def test_item07_persons_review_queries_on_pg(pg, tmp_path):
    from mindex.persons.persons import Persons

    store = Store.open(Config(data_dir=tmp_path / "data", database={"backend": "postgres"}))
    try:
        Persons(store).review_summary()
    except Exception as exc:  # noqa: BLE001 — 当前为显式失败（json 函数不存在）
        pytest.xfail(f"persons 审核查询含 SQLite 专有 JSON（json_each/json_extract）：{type(exc).__name__}")


# ---------------------------------------------------------------- 8. agent 变更流


def test_item08_agent_changes_route_on_cluster(pg, tmp_path, monkeypatch, s3_env):
    from fastapi.testclient import TestClient

    from mindex.api import create_app

    config, _, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-agent",
                                 queue="postgres", locks="postgres")
    config.vector.backend = "chroma_server"
    monkeypatch.setenv("MINDEX_CHROMA_TOKEN", "")
    app = create_app(config)
    client = TestClient(app)
    assert client.get("/api/agent/status").status_code == 503  # 当前：网关指引
    response = client.get("/api/agent/changes")
    if response.status_code >= 500:
        pytest.xfail("cluster 业务 API 无变更流/outbox：agent 路由整体指向网关（outbox 未交付）")
    assert response.status_code == 200


# ---------------------------------------------------------------- 9. 指标


def test_item09_health_metrics_surface(pg, tmp_path, monkeypatch, s3_env):
    from fastapi.testclient import TestClient

    from mindex.api import create_app

    config, _, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-metrics",
                                 queue="postgres")
    app = create_app(config)
    health = TestClient(app).get("/api/health").json()
    assert health["backends"]["form"] == "pg-single"
    assert health["backends"]["queue"] == "postgres"
    assert "queue_stats" in health  # 队列深度/最老 pending 已暴露
    missing = [key for key in ("pool", "pool_wait", "lease_expired", "staging")
               if key not in json.dumps(health)]
    if missing:
        pytest.xfail(f"未暴露指标：{missing}（PG 池等待/租约到期/staging 水位）")


# ---------------------------------------------------------------- 10. GPU 容量


def test_item10_gpu_capacity_declaration():
    try:
        Config(gpu={"capacity": 1})
    except Exception as exc:  # noqa: BLE001 — 当前无 gpu 配置节
        pytest.xfail(f"无声明式 GPU 容量/准入配置（{type(exc).__name__}）；只有 worker --concurrency 槽位")


# ---------------------------------------------------------------- 11. 恢复演练（小规模）


def test_item11_small_restore_drill(pg, tmp_path, monkeypatch):

    from mindex.maintenance.pg_export import export_media_bundle, export_pg_to_sqlite
    from mindex.maintenance.vector_bundle import export_vectors, load_vectors
    from mindex.shared.backends import LocalMediaStore
    from mindex.shared.db.transfer import copy_sqlite_to_pg, table_digests
    from mindex.shared.storage import PgStore

    started = time.perf_counter()
    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"})
    config.embedding.dimensions = 8
    store = PgStore(config)
    # 种子：素材 + 分镜 + 媒体文件 + 一个 ready 索引的向量集合
    clip = config.data_dir / "clips" / "s1" / "shot" / "a.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"clip-bytes")
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) "
                  "VALUES('s1','素材','sources/s1/a.mp4','{}','k')")
    store.execute("INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
                  "VALUES('seg1','s1','shot',0,5,'clips/s1/shot/a.mp4','thumbnails/a.jpg','g','r')")
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('i1','{\"format\":2}','ready')")
    from mindex.processing.indexing import VectorIndex

    VectorIndex(config.data_dir, "i1", create=True).collection.upsert(
        ids=["rep1"], embeddings=[[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]],
        documents=["doc"], metadatas=[{"segment_id": "seg1", "view": "visual"}])
    bundle = tmp_path / "bundle"
    export_pg_to_sqlite(store, bundle)
    media_stats = export_media_bundle(store, LocalMediaStore(config.data_dir), bundle)
    vector_manifest = export_vectors(store, bundle, config=config)
    exported_seconds = time.perf_counter() - started
    assert media_stats["objects"] == 1 and vector_manifest["i1"]["vectors"] == 1
    # 清空目标并回灌
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg

    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(PG) as conn:
        migrate_pg(conn)
    restored = PgStore(config)
    copy_sqlite_to_pg(Store(bundle), restored)
    assert load_vectors(restored, bundle, config=config) == {"i1": 1}
    assert restored.one("SELECT id FROM sources WHERE id='s1'") is not None
    assert table_digests(Store(bundle))["sources"] == table_digests(restored)["sources"]
    from mindex.processing.indexing import VectorIndex as _VI

    assert _VI(config.data_dir, "i1").collection.count() == 1
    total_seconds = time.perf_counter() - started
    print(f"\n恢复演练（小规模）：导出 {exported_seconds:.2f}s / 全流程 {total_seconds:.2f}s")


# ---------------------------------------------------------------- 12. 桌面


def test_item12_desktop_logic_and_remote_config():
    root = os.path.dirname(os.path.dirname(__file__))
    result = subprocess.run(["node", "--test", "test/lib.test.js"], cwd=os.path.join(root, "desktop"),
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fail 0" in result.stdout
