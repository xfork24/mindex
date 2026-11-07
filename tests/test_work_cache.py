"""素材工作检查点与缓存失效回归。"""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.work_cache import WorkCache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    import admin.work_cache as module

    monkeypatch.setattr(module, "source_public", lambda row: dict(row))
    progress = {"completed": 1}
    monkeypatch.setattr(module, "attach_tasks", lambda store, source, work: work.update(task=dict(progress)))
    # 详情接口实时读取所属组；本夹具只验证工作缓存，成员查询为空。
    store = SimpleNamespace(
        root=tmp_path,
        path=tmp_path / "metadata.sqlite3",
        cache_identity=("test-library", "sqlite", "test-backend", ""),
        rows=lambda sql, args=(): [],
        one=lambda sql, args=(): None,
    )
    store.path.write_text("initial")

    class Library:
        calls = 0
        exists = True

        def source_row(self, sid):
            if not self.exists:
                raise ValueError("deleted")
            return {"id": sid, "name": "current"}

        def source(self, sid):
            self.calls += 1
            return {"id": sid, "work": {"coverage": self.calls}}

    library = Library()
    library.store = store
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(module, "ThreadPoolExecutor", lambda **kwargs: executor)
        yield WorkCache(library), library, progress, executor


def test_persisted_summary_survives_new_instance_and_reads_live_progress(cache):
    work_cache, library, progress, _ = cache
    assert work_cache.get("source")["work"]["coverage"] == 1
    progress["completed"] = 7
    result = WorkCache(library).get("source")
    assert library.calls == 1
    assert result["work"]["task"]["completed"] == 7
    assert result["name"] == "current"
    library.exists = False
    with pytest.raises(ValueError, match="deleted"):
        work_cache.get("source")


def test_database_change_returns_checkpoint_then_refreshes(cache):
    work_cache, library, _, executor = cache
    work_cache.get("source")
    library.store.path.write_text("updated database")
    result = work_cache.get("source")
    assert result["work"]["coverage"] == 1
    assert result["work_refreshing"] is True
    executor.submit(lambda: None).result()
    assert work_cache.get("source")["work"]["coverage"] == 2
    assert not work_cache.get("source")["work_refreshing"]


def test_expired_artifact_check_and_corrupt_checkpoint(cache):
    work_cache, library, _, executor = cache
    work_cache.path("source").parent.mkdir(parents=True)
    work_cache.path("source").write_text("invalid JSON")
    work_cache.get("source")
    work_cache.max_age = -1
    assert work_cache.get("source")["work_refreshing"]
    executor.submit(lambda: None).result()
    assert library.calls == 2


def test_manual_refresh_recomputes_and_persists(cache):
    work_cache, library, _, _ = cache
    work_cache.get("source")
    assert work_cache.refresh("source")["work"]["coverage"] == 2
    assert WorkCache(library).get("source")["work"]["coverage"] == 2
    assert library.calls == 2


def test_admin_cache_routes(cache, monkeypatch):

    import admin.work_cache as module
    from admin import admin
    from admin.admin import admin_router

    work_cache, library, _, _ = cache
    monkeypatch.setattr(admin, "Library", lambda store: library)
    monkeypatch.setattr(module, "WorkCache", lambda library: work_cache)
    app = FastAPI()
    app.include_router(admin_router(library.store))
    with TestClient(app) as client:
        result = client.get("/api/admin/sources/source?detail=work")
        assert result.status_code == 200
        assert result.json()["work"]["coverage"] == 1
        result = client.post("/api/admin/sources/source/work-cache/refresh")
        assert result.status_code == 200
        assert result.json()["work"]["coverage"] == 2
        assert result.json()["work_refreshing"] is False
        assert client.get("/api/admin/sources/source?detail=work").json()["work"]["coverage"] == 2
