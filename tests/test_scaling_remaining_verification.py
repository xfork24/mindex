"""水平扩展剩余项 1–12 的功能/连通性验证（不做规模压测）。

每项先断言**当前已可用的行为**，再对尚未交付的能力用 ``pytest.xfail(reason)``
标注——xfail 的原因即缺口，能力交付后该断言会自然通过（届时删除 xfail 分支）。

需要 ``MINDEX_TEST_POSTGRES_DSN``；涉及对象存储的项另需 ``MINDEX_TEST_S3_ENDPOINT``。
"""

from __future__ import annotations

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

    # 默认 pipeline 锁是共享的（并行处理），互斥语义要用 GPU 独占模式验证。
    first_config = Config(data_dir=tmp_path / "a", database={"backend": "postgres"},
                          locks={"backend": "postgres"}, enforce_gpu_lock=True)
    second_config = Config(data_dir=tmp_path / "b", database={"backend": "postgres"},
                           locks={"backend": "postgres"}, enforce_gpu_lock=True)
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


def test_item01b_resource_locks_and_index_write_coordinate_via_pg(pg, tmp_path):
    """同素材+同工作的资源锁与 index-write 锁在 PG 形态跨 data_dir 互斥。"""
    from filelock import Timeout

    from mindex.shared.processing_lock import ProcessingLock, index_write_lock

    base = {"backend": "postgres"}
    first_config = Config(data_dir=tmp_path / "a", database=base, locks={"backend": "postgres"})
    second_config = Config(data_dir=tmp_path / "b", database=base, locks={"backend": "postgres"})
    first = ProcessingLock(first_config, source_id="s1", work="detect")
    first.acquire()
    second = ProcessingLock(second_config, source_id="s1", work="detect")
    try:
        with pytest.raises(Timeout):
            second.acquire(blocking=False)
    finally:
        first.release()
    # 不同工作可并行（资源锁按 source+work 区分，不是整库串行）
    other = ProcessingLock(second_config, source_id="s1", work="transcribe")
    other.acquire()
    other.release()
    # index-write 跨 data_dir 互斥
    writer = index_write_lock(tmp_path / "a", config=first_config)
    writer.acquire()
    try:
        blocked = index_write_lock(tmp_path / "b", config=second_config)
        with pytest.raises(Timeout):
            blocked.acquire(timeout=0.2)
    finally:
        writer.release()


def test_item01c_recover_tasks_respects_pg_leases(pg, tmp_path):
    """任务恢复：无活跃租约的 queued/running 标记中断；被租约占用的保持原状。"""
    from mindex.shared.backends import open_context
    from mindex.shared.processing_lock import ProcessingLock, recover_tasks

    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"},
                    locks={"backend": "postgres"})
    store = open_context(config).store
    store.execute("INSERT INTO source_tasks(id,source_id,work,status,config,options) "
                  "VALUES('t-free','s1','detect','queued','{}','{}')")
    store.execute("INSERT INTO source_tasks(id,source_id,work,status,config,options) "
                  "VALUES('t-held','s2','detect','running','{}','{}')")
    holder = ProcessingLock(config, source_id="s2", work="detect")
    holder.acquire()  # 模拟活跃 worker 持有资源租约
    try:
        recover_tasks(store, "source_tasks")
    finally:
        holder.release()
    rows = {row["id"]: row["status"] for row in store.rows("SELECT id,status FROM source_tasks")}
    assert rows["t-free"] == "interrupted"  # 无活跃租约 → 标记中断
    assert rows["t-held"] == "running"      # 有活跃租约 → 不动


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


def test_item03_studio_workspace_conditional_publish(tmp_path):
    """条件发布：陈旧 expected_revision 被拒；并发写者不丢更新；无 expected 兼容旧调用。"""
    from mindex.shared.config import Config as _Config
    from mindex.studio.workspace import versions

    config = _Config(data_dir=tmp_path / "data")
    job = "20260101-120000-abcdef"
    base = {"plans": {}, "versions": {}, "builds": {}, "active_plan": None, "updated_at": ""}
    versions.persist(config, job, dict(base))
    first = versions.current_revision(config, job)
    assert first
    with pytest.raises(versions.RevisionConflict):
        versions.persist(config, job, {**base, "active_plan": "stale"}, expected_revision="not-current")
    versions.persist(config, job, {**base, "active_plan": "p1"}, expected_revision=first)
    assert versions.current_revision(config, job) != first
    # 并发写者：只有携带当前修订的那个能成功（另一个收到 RevisionConflict）
    current = versions.current_revision(config, job)
    results = []

    def writer(name):
        try:
            versions.persist(config, job, {**base, "active_plan": name}, expected_revision=current)
            results.append(("ok", name))
        except versions.RevisionConflict:
            results.append(("conflict", name))

    import threading

    threads = [threading.Thread(target=writer, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert [kind for kind, _ in results].count("ok") == 1, results  # 恰好一个成功
    assert [kind for kind, _ in results].count("conflict") == 1
    # 旧调用（不带 expected）仍可用
    versions.persist(config, job, dict(base))


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
    assert local_analysis and local_plans  # 本地快速路径仍在（写穿透）
    remote = {row["object_key"] for row in context.store.rows(
        "SELECT path,object_key FROM shared_files") if row["path"].startswith(("analysis/", "plans/", "transcripts/"))}
    assert remote  # 检查点已镜像到对象存储（权威副本）
    # 新节点（不同 data_dir、同一 PG+S3）无需共享目录即可读回分镜计划
    other = Config(data_dir=tmp_path / "other-node", database={"backend": "postgres"},
                   storage=config.storage.model_copy(deep=True))
    other.embedding.dimensions = 8
    other.asr.aligner_revision = "test-revision"
    from mindex.processing.pipeline import Pipeline as _Pipeline

    node = _Pipeline(other)
    source_row = node.store.one("SELECT * FROM sources WHERE id=?", (source,))
    assert node._read_plan(source_row)  # 从 S3 物化检查点
    assert (other.data_dir / "plans").exists()  # 新节点从权威指针读取后形成可丢弃缓存


# ---------------------------------------------------------------- 5. uploads 远端化


def test_item05_upload_lands_in_object_storage(pg, tmp_path, video_file, monkeypatch, s3_env):
    """S3 形态：上传直接写内容寻址的不可变对象，不落节点本地；重复上传幂等。"""
    from fastapi.testclient import TestClient

    from mindex.api import create_app

    config, context, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-uploads")
    app = create_app(config)
    client = TestClient(app)
    for _ in range(2):  # 重复上传同一内容：幂等（键已存在且摘要一致）
        with video_file.open("rb") as handle:
            response = client.post("/api/admin/sources/upload",
                                   files={"file": ("clip.mp4", handle, "video/mp4")})
        assert response.status_code == 201, response.text
    uploaded = list(context.media.iter_prefix("sources/"))
    assert len(uploaded) == 1 and uploaded[0].endswith("/clip.mp4")
    assert context.store.one("SELECT id,path FROM sources")["path"] == uploaded[0]
    # 原片不落节点本地；临时上传目录已清理
    assert not (config.data_dir / "sources").exists() or not any(
        (config.data_dir / "sources").rglob("*.mp4"))
    # 处理入口仍可经 media 物化读取（resolve_media 走远端）
    local = context.store.resolve_media(uploaded[0])
    assert local.is_file() and local.stat().st_size == video_file.stat().st_size


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


def test_item06_clean_collects_s3_orphans_respecting_pins(pg, tmp_path, monkeypatch, s3_env):
    """S3 孤儿回收：未引用且过宽限的对象被删；被引用/被 pin/未过宽限的保留。"""
    import hashlib
    import io

    from mindex.maintenance.clean import clean_library
    from mindex.shared.media_delivery import DownloadPins

    config, context, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-clean")
    config.clean.s3_orphan_grace_hours = 0  # 测试不等待宽限

    def put(key: str, payload: bytes):
        context.media.put_immutable(key, io.BytesIO(payload),
                                    sha256=hashlib.sha256(payload).hexdigest())

    put("clips/orphan/x.mp4", b"orphan")
    put("clips/s1/shot/kept.mp4", b"kept")
    put("clips/s1/shot/pinned.mp4", b"pinned")
    context.store.execute("INSERT INTO sources(id,name,path,media,layout_key) "
                          "VALUES('s1','素材','sources/s1/a.mp4','{}','k')")
    context.store.execute(
        "INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
        "VALUES('seg1','s1','shot',0,5,'clips/s1/shot/kept.mp4','thumbnails/a.jpg','g','r')")
    pins = DownloadPins(context.store, context.library_id)
    pins.pin("clips/s1/shot/pinned.mp4", 900)
    report = clean_library(config, apply=True)
    keys = set(context.media.iter_prefix(""))
    assert "clips/orphan/x.mp4" not in keys          # 未引用 + 过宽限 → 回收
    assert "clips/s1/shot/kept.mp4" in keys          # 被 segments 引用 → 保留
    assert "clips/s1/shot/pinned.mp4" in keys        # 下载 pin → 保留
    assert report["s3_orphans"]["deleted"] >= 1


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
    assert client.get("/api/agent/status").status_code == 503  # 会话/SSE 仍由网关提供
    # 变更上报：写入共享 outbox，由网关幂等转入 SSE（plans §9）
    posted = client.post("/api/agent/changes", json={
        "kind": "mutation", "data": {"domain": "search", "segment_id": "seg1"}})
    assert posted.status_code == 201, posted.text
    assert posted.json()["forwarded"] is True
    invalid = client.post("/api/agent/changes", json={"kind": "unknown", "data": {}})
    assert invalid.status_code == 422

    from mindex.agent.changes import ChangeFeed
    from mindex.agent.outbox_bridge import OutboxBridge
    from mindex.shared.db.agent_outbox import drain
    feed = ChangeFeed(tmp_path / "gateway")
    bridge = OutboxBridge(app.state.store, feed)
    assert bridge.drain_once() == 1  # 网关转入 ChangeFeed
    assert [(item["kind"], item["source"], item["data"]) for item in feed.after(0)] == [
        ("mutation", "mcp", {"domain": "search", "segment_id": "seg1"})]
    assert bridge.drain_once() == 0  # 幂等：已消费不再重放
    assert drain(app.state.store) == []


# ---------------------------------------------------------------- 9. 指标


def test_item09_health_metrics_surface(pg, tmp_path, monkeypatch, s3_env):
    from fastapi.testclient import TestClient

    from mindex.api import create_app

    config, _, _ = _bucket_store(tmp_path, monkeypatch, bucket="mindex-verify-metrics",
                                 queue="postgres")
    app = create_app(config)
    client = TestClient(app)
    health = client.get("/api/health").json()
    assert health["backends"]["form"] == "pg-single"
    assert health["backends"]["queue"] == "postgres"
    assert "queue_stats" in health  # 队列深度/最老 pending
    metrics = health["metrics"]
    assert {"pool", "pool_wait", "lease_expired"} <= set(metrics)  # PG 池等待/租约到期
    assert metrics["lease_expired"] == 0 and metrics["lease_holders"] == 0
    # s3 存储形态固定启用 staging 预算：水位可见
    assert metrics["staging"]["quota"] > 0
    assert set(client.get("/api/metrics").json()) >= {"form", "pool", "queue"}


# ---------------------------------------------------------------- 10. GPU 容量


def test_item10_gpu_capacity_admission(pg, tmp_path):
    """capacity=1 时两个 worker 并发执行 GPU 类别任务：实际并发不超过 1。"""
    import threading
    import time as _time

    from mindex.shared.backends import open_context
    from mindex.shared.db.queue_pg import PgTaskQueue
    from mindex.worker_runtime import Worker

    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"},
                    queue={"backend": "postgres"}, gpu={"capacity": 1, "queues": ["selftest"]})
    context = open_context(config)
    queue = PgTaskQueue(context.library_id)
    with context.db.connect() as db:
        for index in range(2):
            queue.enqueue(db, "selftest", {"i": index}, dedupe_key=f"gpu-{index}")

    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def handler(_config, task):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        _time.sleep(0.5)
        with lock:
            active["now"] -= 1

    workers = [Worker(config, queues=["selftest"], once=True,
                      extra_handlers={"selftest": handler}) for _ in range(2)]
    threads = [threading.Thread(target=worker.run) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert active["max"] == 1  # 容量 1：第二个任务等槽位释放
    with context.db.connect() as db:
        assert queue.stats(db)["succeeded"]["count"] == 2


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
