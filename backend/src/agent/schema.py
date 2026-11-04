"""``data_dir/agent.sqlite3`` 的表结构与版本化迁移（会话、附件、改动通道共用一个库、一条步骤列表）。

``SessionStore``、``AttachmentStore``、``ChangeFeed`` 构造时都调用 ``initialize``；步骤只追加，
不修改已发布的步骤（约定见 ``shared/migrations.py``）。
"""

from __future__ import annotations

from mindex.shared.migrations import execute_script, migrate

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
  grants TEXT NOT NULL DEFAULT '[]', context TEXT, loaded_domains TEXT NOT NULL DEFAULT '[]',
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_events (
  session_id TEXT NOT NULL, seq INTEGER NOT NULL, time INTEGER NOT NULL, type TEXT NOT NULL,
  data TEXT NOT NULL, ignorable INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(session_id, seq)
);
CREATE TABLE IF NOT EXISTS agent_secrets (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, value TEXT NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, request TEXT NOT NULL, reason TEXT NOT NULL,
  suggestion TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_attachments (
  id TEXT PRIMARY KEY, session_id TEXT, owner TEXT NOT NULL, filename TEXT NOT NULL,
  content_type TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_attachments_session ON agent_attachments(session_id);
CREATE INDEX IF NOT EXISTS agent_attachments_expires ON agent_attachments(expires_at);
CREATE TABLE IF NOT EXISTS agent_changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, time INTEGER NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
  session_id TEXT, tab TEXT, data TEXT NOT NULL
);
"""


def _v1(db) -> None:
    """引入版本号时的完整 schema；对 user_version=0 的旧库幂等。"""
    execute_script(db, SCHEMA_V1)
    # 旧库的 agent_sessions 没有 loaded_domains（CREATE TABLE IF NOT EXISTS 不会补列）。
    columns = {row[1] for row in db.execute("PRAGMA table_info(agent_sessions)")}
    if "loaded_domains" not in columns:
        db.execute("ALTER TABLE agent_sessions ADD COLUMN loaded_domains TEXT NOT NULL DEFAULT '[]'")


def _v2(db) -> None:
    """滚动操作窗口按事件类型读取最近记录，避免每步扫描完整会话。"""
    db.execute("CREATE INDEX IF NOT EXISTS agent_events_type ON agent_events(session_id, type, seq)")


def _v3(db) -> None:
    db.execute("ALTER TABLE agent_sessions ADD COLUMN runtime TEXT NOT NULL DEFAULT 'builtin'")
    db.execute("ALTER TABLE agent_events ADD COLUMN upstream_seq INTEGER")
    db.execute("CREATE UNIQUE INDEX agent_upstream ON agent_events(session_id, upstream_seq)")


def _v4(db) -> None:
    db.execute("CREATE TABLE agent_outbox_receipts (library_id TEXT NOT NULL, outbox_id INTEGER NOT NULL,"
               " change_seq INTEGER NOT NULL, PRIMARY KEY(library_id,outbox_id))")


STEPS = (_v1, _v2, _v3, _v4)


def initialize(database) -> int:
    """打开 ``database``（``session_store.Database``）对应的库：启用 WAL 并迁移到最新版本。"""
    with database.connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        return migrate(db, STEPS)
