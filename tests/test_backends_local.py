"""阶段 0 本地后端抽象：BackendContext 与 db/media/locks/vectors/queue 的本地实现。

验收（plans/horizontal-scaling-plan.md §11 阶段 0）：新抽象不触发网络、无行为变化；
local 形态下 db 即 Store、媒体为单机文件、锁为 data_dir 文件锁、队列为进程内执行。
"""

from __future__ import annotations

import hashlib
import io
import random
import threading

import pytest
from filelock import Timeout

from shared.backends import BackendContext, FileLockProvider, InlineTaskQueue, LocalMediaStore, open_context
from shared.config import Config
from shared.storage import Store


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data")


def test_open_context_local_shape_and_store_open(config):
    ctx = open_context(config)
    assert isinstance(ctx, BackendContext)
    assert ctx.form == "local"
    assert isinstance(ctx.db, Store)
    assert ctx.store is ctx.db  # 本地形态下业务 Store 即 db
    assert ctx.data_dir == ctx.db.root
    assert ctx.media.backend == "local" and ctx.locks.backend == "file"
    assert ctx.vectors.backend == "chroma_local" and ctx.queue.backend == "inline"
    # Store.open 是集群调用链的规范入口：阶段 0 等价 Store(config.data_dir)
    assert Store.open(config).path == Store(config.data_dir).path


def test_library_id_minted_once_and_stable(config):
    first = open_context(config).library_id
    assert len(first) == 36  # UUID 字符串
    assert open_context(config).library_id == first  # 建库后从 settings 读取
    row = Store(config.data_dir).one("SELECT value FROM settings WHERE key='library.id'")
    assert row["value"] == first
    other = open_context(Config(data_dir=config.data_dir.parent / "data2"))
    assert other.library_id != first  # 不同素材库不同身份


def test_library_id_concurrent_first_open_wins(tmp_path):
    results: list[str] = []
    barrier = threading.Barrier(4)

    def open_later():
        barrier.wait()
        results.append(open_context(Config(data_dir=tmp_path / "data")).library_id)

    threads = [threading.Thread(target=open_later) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4  # 所有线程都必须成功，不能把线程异常当作通过
    assert len(set(results)) == 1  # 并发首次建库以先写者为准


def test_local_media_store_roundtrip(tmp_path):
    (tmp_path / "origin.bin").write_bytes(b"\x00video-bytes" * 3)
    payload = (tmp_path / "origin.bin").read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    store = LocalMediaStore(tmp_path)
    obj = store.put_immutable("clips/src/shot/a.mp4", tmp_path / "origin.bin", sha256=sha)
    assert obj.size == len(payload) and obj.sha256 == sha and obj.backend_version is None
    # 幂等：同 key 同内容重复写为 no-op（文件与 BytesIO 两种来源一致）
    assert store.put_immutable("clips/src/shot/a.mp4", io.BytesIO(payload), sha256=sha).size == len(payload)
    # 同 key 不同内容是键冲突（不可变键语义）
    other = b"different"
    with pytest.raises(ValueError):
        store.put_immutable(
            "clips/src/shot/a.mp4", io.BytesIO(other), sha256=hashlib.sha256(other).hexdigest()
        )
    assert store.stat("clips/src/shot/a.mp4", with_sha256=True).sha256 == sha
    with store.open_read("clips/src/shot/a.mp4") as handle:
        assert handle.read() == payload
    with store.materialize("clips/src/shot/a.mp4") as path:
        assert path.is_file()
    assert sorted(store.iter_prefix("clips/")) == ["clips/src/shot/a.mp4"]
    assert store.presign_get("clips/src/shot/a.mp4", expires=60) is None  # 本地无预签名
    store.delete_version("clips/src/shot/a.mp4")
    with pytest.raises(FileNotFoundError):
        store.stat("clips/src/shot/a.mp4")


def test_local_media_store_rejects_escape_and_bad_digest(tmp_path):
    store = LocalMediaStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.stat("../../etc/passwd")  # 相对路径不得逃出 root
    with pytest.raises(FileNotFoundError):
        store.stat("media.mp4", version="v1")  # 本地无版本概念
    with pytest.raises(ValueError):
        store.put_immutable(
            "bad.bin", io.BytesIO(b"mismatch"), sha256=hashlib.sha256(b"expected").hexdigest()
        )
    assert list(tmp_path.iterdir()) == []  # 失败写入不产生目标或残留临时文件


def test_file_lock_provider_exclusive_and_shared(tmp_path):
    provider = FileLockProvider(tmp_path)
    with provider.acquire("library", exclusive=True):
        with pytest.raises(Timeout):
            with provider.acquire("library", exclusive=True, timeout=0.1):
                pass
        with pytest.raises(Timeout):  # 独占期间共享也不可入
            with provider.acquire("library", exclusive=False, timeout=0.1):
                pass
    with provider.acquire("library", exclusive=False):
        with provider.acquire("library", exclusive=False, timeout=0.5):  # 共享可并存
            pass
    with pytest.raises(ValueError):
        provider.acquire("register", exclusive=False)  # register 只能独占
    with provider.acquire_ordered(["library", "studio"], timeout=0.1):
        with pytest.raises(Timeout):
            with provider.acquire("studio", timeout=0.05):
                pass
    with pytest.raises(ValueError), provider.acquire_ordered(["studio", "library"]):  # 逆序在进入时拒绝
        pass


def test_inline_queue_runs_in_thread():
    queue = InlineTaskQueue()
    done = threading.Event()
    thread_id: list[int] = []

    def job():
        thread_id.append(threading.get_ident())
        done.set()

    receipt = queue.enqueue(job, kind="source")
    assert receipt.backend == "inline" and receipt.task_id.startswith("inline-")
    assert done.wait(5)
    assert thread_id[0] != threading.get_ident()  # 与现有后台任务一样在守护线程执行
    thread = queue.threads[receipt.task_id]
    thread.join(5)
    assert not thread.is_alive()


def test_local_vector_store_wraps_vector_index(config):
    ctx = open_context(config)
    index_id = "i" * 12
    vectors = ctx.vectors.open(index_id, create=True)
    assert vectors.count() == 0
    rng = random.Random(7)
    entries = [
        (
            f"rep{j}",
            [rng.random() for _ in range(8)],
            {"id": f"seg{j}", "source_id": "src", "kind": "shot"},
            "visual",
            f"doc{j}",
        )
        for j in range(5)
    ]
    vectors.upsert_many(entries)
    vectors.flush()
    assert vectors.count() == 5
    assert vectors.ids() == {f"rep{j}" for j in range(5)}
    got = vectors.vectors(["rep0", "rep1"])
    assert got["rep0"] == pytest.approx(entries[0][1])
    # 查询原语：自查询最近邻是自身；where 是服务端过滤的下推边界
    hits = vectors.query(entries[0][1], count=3)
    assert hits and hits[0]["id"] == "rep0" and hits[0]["distance"] < 1e-6
    assert hits[0]["metadata"]["view"] == "visual" and hits[0]["metadata"]["segment_id"] == "seg0"
    assert vectors.query(entries[0][1], count=3, where={"view": "analysis"}) == []
    assert len(vectors.query(entries[0][1], count=3, where={"view": "visual"})) == 3
    vectors.delete(["rep4"])
    assert vectors.count() == 4
    reopen = ctx.vectors.open(index_id)
    assert reopen.count() == 4  # 本地持久化：重开集合仍可见


def test_local_vector_store_interops_with_vector_index(config):
    """本地实现与 processing 的 VectorIndex 读写同一集合（阶段 0 不改变现有调用方）。"""
    from processing.indexing import VectorIndex

    ctx = open_context(config)
    index_id = "k" * 12
    rng = random.Random(11)
    entries = [
        (
            f"vid{j}",
            [rng.random() for _ in range(8)],
            {"id": f"seg{j}", "source_id": "src", "kind": "shot"},
            "visual",
            f"doc{j}",
        )
        for j in range(3)
    ]
    ctx.vectors.open(index_id, create=True).upsert_many(entries)
    legacy = VectorIndex(config.data_dir, index_id)  # 现有读取方
    assert legacy.ids() == {f"vid{j}" for j in range(3)}
    assert legacy.vectors(["vid0"])["vid0"] == pytest.approx(entries[0][1])


def test_store_open_matches_direct_construction(tmp_path):
    """阶段 0 的 Store.open 与直构零行为差异：同一目录、可读同样的表。"""
    direct = Store(tmp_path / "data")
    direct.execute("INSERT INTO settings VALUES('probe','1')")
    assert (
        Store.open(Config(data_dir=tmp_path / "data")).one("SELECT value FROM settings WHERE key='probe'")[
            "value"
        ]
        == "1"
    )
