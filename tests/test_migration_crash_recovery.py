"""迁移崩溃窗口与激活准入：元数据和恢复日志必须原子提交。"""
from __future__ import annotations

import subprocess
import sys

import pytest

from mindex.maintenance.migration import activate_bundle, export_bundle, load_bundle
from mindex.shared.backends import open_context
from mindex.shared.config import Config


def seed(tmp_path):
    origin = Config(data_dir=tmp_path / "origin")
    store = open_context(origin).store
    store.execute("INSERT INTO settings(key,value) VALUES('recover','expected')")
    store.execute("INSERT INTO runs(id,stage,status) VALUES(42,'restore-test','complete')")
    bundle = tmp_path / "bundle"
    export_bundle(origin, bundle)
    target = Config(data_dir=tmp_path / "target")
    open_context(target)
    return bundle, target


def test_process_death_before_metadata_commit_keeps_prewrite_binding(tmp_path):
    bundle, target = seed(tmp_path)
    script = """
import os, sys
from pathlib import Path
from mindex.shared.config import Config
from mindex.shared.db import transfer
from mindex.maintenance.migration import load_bundle
original = transfer.copy_sqlite_to_sqlite
def crash(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(23)
transfer.copy_sqlite_to_sqlite = crash
load_bundle(Config(data_dir=Path(sys.argv[1])), Path(sys.argv[2]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(target.data_dir), str(bundle)],
                            capture_output=True, text=True, check=False, timeout=15)
    assert result.returncode == 23, result.stderr
    store = open_context(target).store
    assert store.one("SELECT 1 FROM runs") is None
    assert store.one("SELECT status FROM migration_runs")["status"] == "preparing"
    assert store.one("SELECT source_library_id FROM migration_runs")["source_library_id"] == store.library_id
    assert load_bundle(target, bundle)["consistent"]
    assert store.one("SELECT id FROM runs")["id"] == 42


def test_failure_after_metadata_commit_keeps_resumable_journal(tmp_path, monkeypatch):
    from mindex.maintenance import migration

    bundle, target = seed(tmp_path)
    original = migration.verify_bundle
    def fail(*args, **kwargs):
        raise RuntimeError("模拟数据提交后校验进程退出")
    monkeypatch.setattr(migration, "verify_bundle", fail)
    with pytest.raises(RuntimeError):
        load_bundle(target, bundle)
    store = open_context(target).store
    assert store.one("SELECT status FROM migration_runs")["status"] == "loaded"
    assert store.one("SELECT id FROM runs")["id"] == 42
    monkeypatch.setattr(migration, "verify_bundle", original)
    assert load_bundle(target, bundle)["consistent"]


def test_metadata_only_bundle_with_sources_cannot_activate(tmp_path):
    origin = Config(data_dir=tmp_path / "origin")
    open_context(origin).store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key) VALUES('s','素材','sources/s/a.mp4','{}','layout')")
    bundle = tmp_path / "bundle"
    export_bundle(origin, bundle)
    target = Config(data_dir=tmp_path / "target")
    assert load_bundle(target, bundle)["consistent"]
    with pytest.raises(ValueError, match="必要媒体"):
        activate_bundle(target, bundle)


def test_resume_rejects_changed_database_before_recopying_media(tmp_path):
    origin = Config(data_dir=tmp_path / "origin")
    store = open_context(origin).store
    key = "sources/s/a.mp4"
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('s','素材',?,'{}','layout')", (key,))
    file = origin.data_dir / key
    file.parent.mkdir(parents=True)
    file.write_bytes(b"restore-source")
    bundle = tmp_path / "bundle"
    export_bundle(origin, bundle, with_media=True)
    target = Config(data_dir=tmp_path / "target")
    assert load_bundle(target, bundle)["consistent"]
    from mindex.shared.db.maintenance_gate import MaintenanceBusy
    from mindex.shared.migration_state import access

    target_store = open_context(target).store
    with pytest.raises(MaintenanceBusy):
        target_store.execute("INSERT INTO settings(key,value) VALUES('new-write','keep')")
    # 显式模拟数据库管理员绕过业务准入后的异值，恢复仍须拒绝覆盖。
    with access(target_store):
        target_store.execute("INSERT INTO settings(key,value) VALUES('new-write','keep')")
    (target.data_dir / key).unlink()
    with pytest.raises(ValueError, match="已变化"):
        load_bundle(target, bundle)
    assert not (target.data_dir / key).exists()
