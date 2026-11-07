"""会话记录的 loaded_domains 列：新库建表、旧库迁移与 JSON 往返。"""

from __future__ import annotations

import sqlite3

from agent.session_store import SessionStore

OLD_SCHEMA = """
CREATE TABLE agent_sessions (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
  grants TEXT NOT NULL DEFAULT '[]', context TEXT,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
"""


def columns(path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[1] for row in db.execute("PRAGMA table_info(agent_sessions)")}


def test_new_session_defaults_to_empty_and_round_trips(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create("测试")
    assert session["loaded_domains"] == []
    store.update(session["id"], loaded_domains=["studio", "search"], grants=["edit_shots"])
    again = SessionStore(tmp_path).get(session["id"])  # 模拟服务重启
    assert again["loaded_domains"] == ["studio", "search"]
    assert again["grants"] == ["edit_shots"]
    assert set(store.list()[0]) == {"id", "title", "status", "created_at", "updated_at", "runtime"}


def test_old_schema_is_migrated_without_losing_rows(tmp_path):
    path = tmp_path / "agent.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(OLD_SCHEMA)
        db.execute(
            "INSERT INTO agent_sessions(id,title,status,grants,context,created_at,updated_at) "
            "VALUES('session-old','旧会话','idle','[\"edit_shots\"]',NULL,1,1)"
        )
    assert "loaded_domains" not in columns(path)
    store = SessionStore(tmp_path)
    assert "loaded_domains" in columns(path)
    old = store.get("session-old")
    assert old["loaded_domains"] == []
    assert old["grants"] == ["edit_shots"]
    store.update("session-old", loaded_domains=["library"])
    assert store.get("session-old")["loaded_domains"] == ["library"]
    SessionStore(tmp_path)  # 再次初始化不应重复 ALTER
    assert SessionStore(tmp_path).get("session-old")["loaded_domains"] == ["library"]
