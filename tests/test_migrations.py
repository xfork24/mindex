"""版本化迁移：新库、旧库（user_version=0）幂等升级、重复打开不重跑、agent 库与备份暂存副本。"""

from __future__ import annotations

import sqlite3

import pytest

from mindex.agent import schema as agent_schema
from mindex.agent.attachments import AttachmentStore
from mindex.agent.changes import ChangeFeed
from mindex.agent.session_store import SessionStore
from mindex.maintenance.backup_db import upgrade_database
from mindex.shared.migrations import execute_script, migrate, user_version
from mindex.shared.storage import METADATA_STEPS, Store


def columns(db, table):
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# 引入版本号之前的旧库形态：缺少后来新增的列，user_version 为 0。
LEGACY_METADATA = """
CREATE TABLE sources (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
  media TEXT NOT NULL, layout_key TEXT NOT NULL, prepared INTEGER NOT NULL DEFAULT 0);
CREATE TABLE representations (id TEXT PRIMARY KEY, index_id TEXT NOT NULL, segment_id TEXT NOT NULL,
  view TEXT NOT NULL, document TEXT NOT NULL);
CREATE TABLE dialogue_tasks (id TEXT PRIMARY KEY, source_id TEXT, status TEXT NOT NULL,
  config TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
  error TEXT, result TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE clip_migration_files (path TEXT PRIMARY KEY, source_id TEXT NOT NULL, version TEXT NOT NULL,
  status TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE person_references (id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, stage_id TEXT,
  path TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE person_tasks (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, status TEXT NOT NULL,
  config TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}', error TEXT, result TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
INSERT INTO sources VALUES ('s1', '旧素材', 'sources/s1/a.mp4', '{}', 'layout', 1);
INSERT INTO representations VALUES ('r1', 'i1', 'seg1', 'visual', '文档');
INSERT INTO person_references VALUES ('ref1', 'actor1', NULL, 'p.png', 'd', '{}');
INSERT INTO settings VALUES ('active', 'i1');
"""


def test_fresh_metadata_database_reaches_latest_version(tmp_path):
    store = Store(tmp_path)
    with store.connect() as db:
        assert user_version(db) == len(METADATA_STEPS)
        assert {"sources", "segments", "person_evidence", "search_releases"} <= tables(db)
        assert "layout_config" in columns(db, "sources")
        assert "cache_key" in columns(db, "representations")
        assert "role_id" in columns(db, "person_references")


def test_legacy_metadata_database_upgrades_with_data_preserved(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_METADATA)
        assert user_version(db) == 0
    store = Store(tmp_path)
    with store.connect() as db:
        assert user_version(db) == len(METADATA_STEPS)
        assert "layout_config" in columns(db, "sources")
        assert "publication" in columns(db, "dialogue_tasks")
        assert "publication" in columns(db, "person_tasks")
        assert "dependency_digest" in columns(db, "clip_migration_files")
        assert "cache_key" in columns(db, "representations")
        assert "role_id" in columns(db, "person_references")
        assert "person_evidence" in tables(db)
    assert store.one("SELECT name FROM sources WHERE id='s1'")["name"] == "旧素材"
    assert store.one("SELECT document FROM representations")["document"] == "文档"
    assert store.one("SELECT actor_id,role_id FROM person_references") == {"actor_id": "actor1", "role_id": None}
    assert store.one("SELECT value FROM settings WHERE key='active'")["value"] == "i1"
    assert store.one("SELECT value FROM settings WHERE key='migration.person-role-only-v3'")


def test_reopen_does_not_rerun_steps(tmp_path):
    calls = []

    def step(name):
        def run(db):
            calls.append(name)
            db.execute(f"CREATE TABLE {name} (id INTEGER)")
        return run

    steps = [step("first"), step("second")]
    path = tmp_path / "x.sqlite3"
    for _ in range(3):
        db = sqlite3.connect(path)
        try:
            assert migrate(db, steps) == 2
        finally:
            db.close()
    assert calls == ["first", "second"]
    # 追加步骤后只执行新增的那一步。
    steps.append(step("third"))
    db = sqlite3.connect(path)
    try:
        assert migrate(db, steps) == 3
    finally:
        db.close()
    assert calls == ["first", "second", "third"]


def test_failed_step_rolls_back_and_resumes(tmp_path):
    calls = []

    def good(db):
        calls.append("good")
        db.execute("CREATE TABLE a (id INTEGER)")

    def bad(db):
        calls.append("bad")
        db.execute("CREATE TABLE b (id INTEGER)")
        raise RuntimeError("中断")

    path = tmp_path / "x.sqlite3"
    db = sqlite3.connect(path)
    with pytest.raises(RuntimeError):
        migrate(db, [good, bad])
    assert user_version(db) == 1
    assert tables(db) == {"a"}  # 失败步骤的表结构与版本号一起回滚
    db.close()

    def fixed(db):
        calls.append("fixed")
        db.execute("CREATE TABLE b (id INTEGER)")

    db = sqlite3.connect(path)
    assert migrate(db, [good, fixed]) == 2
    db.close()
    assert calls == ["good", "bad", "fixed"]


def test_store_reopen_skips_schema_checks(tmp_path, monkeypatch):
    Store(tmp_path)
    import mindex.shared.storage as storage

    def fail(db):
        raise AssertionError("已是最新版本时不应重跑迁移")

    monkeypatch.setattr(storage, "METADATA_STEPS", (fail,))
    Store(tmp_path)


def test_execute_script_stays_inside_transaction(tmp_path):
    db = sqlite3.connect(tmp_path / "x.sqlite3")
    db.execute("BEGIN")
    execute_script(db, "CREATE TABLE a (v TEXT DEFAULT ';');\n-- 注释; 继续\nINSERT INTO a VALUES ('x;y');")
    assert db.in_transaction
    db.rollback()
    assert tables(db) == set()
    db.close()


def test_agent_database_fresh_legacy_and_shared_version(tmp_path):
    fresh = tmp_path / "fresh"
    SessionStore(fresh)
    AttachmentStore(fresh)
    ChangeFeed(fresh)
    with sqlite3.connect(fresh / "agent.sqlite3") as db:
        assert user_version(db) == len(agent_schema.STEPS)
        assert {"agent_sessions", "agent_events", "agent_attachments", "agent_changes"} <= tables(db)

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    with sqlite3.connect(legacy / "agent.sqlite3") as db:
        db.executescript("""
            CREATE TABLE agent_sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'idle', grants TEXT NOT NULL DEFAULT '[]', context TEXT,
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
            CREATE TABLE agent_changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER NOT NULL,
              kind TEXT NOT NULL, source TEXT NOT NULL, session_id TEXT, tab TEXT, data TEXT NOT NULL);
            INSERT INTO agent_sessions(id,title,created_at,updated_at) VALUES('session-old','旧会话',1,1);
            INSERT INTO agent_changes(time,kind,source,data) VALUES(1,'mutation','agent','{}');
        """)
    # 任一模块先打开都会把整库升级到同一版本。
    ChangeFeed(legacy)
    with sqlite3.connect(legacy / "agent.sqlite3") as db:
        assert user_version(db) == len(agent_schema.STEPS)
        assert "loaded_domains" in columns(db, "agent_sessions")
        assert "agent_attachments" in tables(db)
        assert db.execute("SELECT count(*) FROM agent_changes").fetchone()[0] == 1
    assert SessionStore(legacy).get("session-old")["loaded_domains"] == []


def test_agent_reopen_does_not_rerun_steps(tmp_path, monkeypatch):
    SessionStore(tmp_path)

    def fail(db):
        raise AssertionError("已是最新版本时不应重跑迁移")

    monkeypatch.setattr(agent_schema, "STEPS", (fail,))
    SessionStore(tmp_path)
    AttachmentStore(tmp_path)
    ChangeFeed(tmp_path)


def test_backup_upgrade_sets_latest_version_on_isolated_copy(tmp_path):
    path = tmp_path / "metadata.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_METADATA)
    upgrade_database(path)
    with sqlite3.connect(path) as db:
        assert user_version(db) == len(METADATA_STEPS)
        assert "cache_key" in columns(db, "representations")
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # 隔离副本不切换 WAL
