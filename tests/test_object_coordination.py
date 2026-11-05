"""GC 重新取当前引用、持久删除意图、pin 及未知输出预算。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mindex.shared.backends import open_context
from mindex.shared.config import Config
from mindex.shared.object_coordination import ObjectCoordinator, ObjectDeleting


class Media:
    def __init__(self):
        self.objects = {"objects/key": b"old"}
        self.broken = False

    def stat(self, key):
        import hashlib

        if key not in self.objects:
            raise FileNotFoundError(key)
        data = self.objects[key]
        return SimpleNamespace(etag=hashlib.sha256(data).hexdigest(), size=len(data))

    def delete_if_match(self, key, *, etag, size, version_id=None):
        if self.broken:
            raise ConnectionError("模拟删除请求前断连")
        assert self.stat(key).etag == etag and self.stat(key).size == size
        del self.objects[key]


def test_gc_rereads_reference_after_candidate_mark(tmp_path):
    with open_context(Config(data_dir=tmp_path)) as context:
        coordinator, media = ObjectCoordinator(context.store), Media()
        assert not coordinator.collect(media, "objects/key", grace=10, now=100)
        context.store.execute("INSERT INTO shared_files(path,object_key,sha256,size,revision) VALUES(?,?,?,?,?)",
                              ("studio/a/input", "objects/key", "0" * 64, 3, "r"))
        assert not coordinator.collect(media, "objects/key", grace=10, now=120)
        assert media.objects["objects/key"] == b"old"


def test_deletion_intent_survives_network_failure_and_refuses_new_pins(tmp_path):
    with open_context(Config(data_dir=tmp_path)) as context:
        coordinator, media = ObjectCoordinator(context.store), Media()
        media.broken = True
        with pytest.raises(ConnectionError):
            coordinator.collect(media, "objects/key", grace=0)
        assert context.store.one("SELECT state FROM resource_objects")["state"] == "deleting"
        with pytest.raises(ObjectDeleting):
            coordinator.pin("objects/key", owner="reader", kind="download")
        media.broken = False
        assert coordinator.collect(media, "objects/key", grace=0)
        assert context.store.one("SELECT state FROM resource_objects")["state"] == "deleted"


def test_changed_object_identity_cancels_gc(tmp_path):
    with open_context(Config(data_dir=tmp_path)) as context:
        coordinator, media = ObjectCoordinator(context.store), Media()
        assert not coordinator.collect(media, "objects/key", grace=10, now=100)
        media.objects["objects/key"] = b"new"
        assert not coordinator.collect(media, "objects/key", grace=10, now=120)
        assert media.objects["objects/key"] == b"new"


def test_growing_reservation_refuses_before_writing(tmp_path):
    from mindex.shared.backends.staging import StagingFull
    from mindex.shared.jsonio import save_bytes
    from mindex.shared.work_budget import scope

    config = Config(data_dir=tmp_path, storage={"staging_quota_gb": 100 / 1024**3})
    with open_context(config) as context:
        target = context.staging.root / "result.bin"
        with pytest.raises(StagingFull), scope(context.store):
            save_bytes(target, b"x" * 101)
        assert not target.exists()
        assert context.staging.usage()["reserved"] == 0
        assert context.staging.usage()["pinned"] == 0


def test_external_encoder_stops_when_output_budget_is_exhausted(tmp_path):
    from mindex.shared.backends.staging import StagingFull
    from mindex.shared.media import atomic_media
    from mindex.shared.work_budget import scope

    config = Config(data_dir=tmp_path, storage={"staging_quota_gb": 1024 / 1024**3})
    with open_context(config) as context:
        target = context.staging.root / "result.mp4"
        with pytest.raises(StagingFull), scope(context.store):
            atomic_media(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "1",
                          "-c:v", "libx264"], target)
        assert not target.exists()
        assert context.staging.usage()["reserved"] == 0
        assert context.staging.usage()["cached"] == 0


def test_gc_while_pool_registry_is_locked_is_reentrant():
    import gc
    import weakref

    from mindex.shared.db.pg_backend import _POOLS_LOCK, PgBackend

    backend = PgBackend("gc-reentrant-isolated", library_id="test")
    ref = weakref.ref(backend)
    with _POOLS_LOCK:
        del backend
        gc.collect()
    assert ref() is None


def test_reference_paging_includes_integer_id_tables(tmp_path):
    from mindex.shared.resource_registry import _rows

    with open_context(Config(data_dir=tmp_path)) as context:
        with context.store.connect() as db:
            columns = [row["name"] for row in db.execute("PRAGMA table_info(voices)")]
            assert "id" in columns
            assert list(_rows(db, "voices", ("sample_path",), page=1)) == []


def test_download_pin_reuses_identity_and_retains_longest_expiry(tmp_path):
    with open_context(Config(data_dir=tmp_path)) as context:
        coordinator = ObjectCoordinator(context.store)
        first = coordinator.download("objects/key", ttl=600)
        second = coordinator.download("objects/key", ttl=1)
        assert first == second
        assert len(context.store.rows("SELECT * FROM resource_pins")) == 1
        with context.store.connect() as db:
            assert coordinator.pinned(db, "objects/key")
        context.store.execute("UPDATE resource_pins SET expires_at=0")
        assert coordinator.prune() == 1


@pytest.fixture
def pg_store(tmp_path, monkeypatch):
    import os

    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("需要隔离 MINDEX_TEST_POSTGRES_DSN")
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg

    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    with psycopg.connect(dsn) as db:
        migrate_pg(db)
    with open_context(Config(data_dir=tmp_path, database={"backend": "postgres"})) as context:
        yield context.store


@pytest.mark.requires_postgres
def test_pg_publication_and_download_cannot_race_conditional_delete(pg_store):
    import threading
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    coordinator, media = ObjectCoordinator(pg_store), Media()
    coordinator.download("objects/key", ttl=600)
    assert not coordinator.collect(media, "objects/key", grace=0)
    pg_store.execute("UPDATE resource_pins SET expires_at=0")
    entered, release = threading.Event(), threading.Event()
    delete = media.delete_if_match

    def blocked_delete(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return delete(*args, **kwargs)

    media.delete_if_match = blocked_delete
    with ThreadPoolExecutor(max_workers=3) as workers:
        collecting = workers.submit(coordinator.collect, media, "objects/key", grace=0)
        assert entered.wait(10)
        signing = workers.submit(coordinator.download, "objects/key", ttl=600)
        publishing = workers.submit(pg_store.execute,
            "INSERT INTO shared_files(path,object_key,sha256,size,revision) VALUES(?,?,?,?,?)",
            ("plans/current.json", "objects/key", "0" * 64, 3, "r"))
        try:
            with pytest.raises(TimeoutError):
                signing.result(timeout=.1)
        finally:
            release.set()
        assert collecting.result(timeout=10)
        for future in (signing, publishing):
            with pytest.raises(ObjectDeleting):
                future.result(timeout=10)
    assert not pg_store.rows("SELECT * FROM shared_files")
    assert not pg_store.rows("SELECT * FROM resource_pins WHERE expires_at>0")
    # 普通用户内容不作为对象引用拦截。
    pg_store.execute("INSERT INTO settings(key,value) VALUES('user-note','objects/key')")
