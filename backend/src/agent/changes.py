"""全局改动与定位通道（``data_dir/agent.sqlite3`` 的 ``agent_changes`` 表）。

内置助手、MCP 服务端（同进程或 ``mindex mcp`` 独立进程）都往这里写两类记录：

- ``mutation``：数据被修改（``{domain, job_id?, plan_id?, ...}``），所有打开的页面据此刷新；
- ``focus``：页面定位描述，发起会话所在的标签页据此切换页面并高亮，其余页面只做页内被动高亮。

浏览器经 ``GET /api/agent/changes``（SSE）订阅；服务端轮询 SQLite，因此跨进程写入也能及时推送。
"""

from __future__ import annotations

import json
from pathlib import Path

from mindex.agent.schema import initialize
from mindex.agent.session_store import Database, now_ms

KINDS = ("mutation", "focus")
SOURCES = ("agent", "mcp")
KEEP = 5000


class ChangeFeed:
    def __init__(self, data_dir: Path):
        self.db = Database(Path(data_dir) / "agent.sqlite3")
        initialize(self.db)
        self._published = 0

    def publish(self, kind: str, data: dict, *, source: str = "agent", session_id: str | None = None,
                tab: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"未知改动类型 {kind}")
        if source not in SOURCES:
            raise ValueError(f"未知改动来源 {source}")
        stamp = now_ms()
        with self.db.connect() as db:
            cursor = db.execute(
                "INSERT INTO agent_changes(time,kind,source,session_id,tab,data) VALUES(?,?,?,?,?,?)",
                (stamp, kind, source, session_id, tab, json.dumps(data, ensure_ascii=False)),
            )
            seq = cursor.lastrowid
        self._published += 1
        if self._published % 200 == 0:
            self.prune()
        return {"seq": seq, "time": stamp, "kind": kind, "source": source, "session_id": session_id, "tab": tab,
                "data": data}

    def after(self, seq: int, limit: int = 200) -> list[dict]:
        rows = self.db.rows("SELECT * FROM agent_changes WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit))
        return [{**row, "data": json.loads(row["data"])} for row in rows]

    def publish_outbox(self, library_id, item):
        """事件与去重回执同一个 SQLite 事务，PG ack 失败可安全重放。"""
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            with db:
                receipt = db.execute(
                    "SELECT change_seq FROM agent_outbox_receipts WHERE library_id=? AND outbox_id=?",
                    (library_id, item["outbox_id"])).fetchone()
                if receipt:
                    return receipt[0]
                cursor = db.execute(
                    "INSERT INTO agent_changes(time,kind,source,session_id,tab,data) VALUES(?,?,?,?,?,?)",
                    (now_ms(), item["kind"], item["source"], item.get("session_id"), item.get("tab"),
                     json.dumps(item["data"], ensure_ascii=False)))
                seq = cursor.lastrowid
                db.execute("INSERT INTO agent_outbox_receipts(library_id,outbox_id,change_seq) VALUES(?,?,?)",
                           (library_id, item["outbox_id"], seq))
                db.execute("INSERT INTO agent_outbox_cursors(library_id,outbox_id,change_seq) VALUES(?,?,?) "
                           "ON CONFLICT(library_id) DO UPDATE SET outbox_id=excluded.outbox_id,change_seq=excluded.change_seq "
                           "WHERE excluded.outbox_id>agent_outbox_cursors.outbox_id", (library_id, item['outbox_id'], seq))
                return seq

    def cursor(self, library_id):
        return self.db.one('SELECT outbox_id,change_seq FROM agent_outbox_cursors WHERE library_id=?', (library_id,))

    def latest(self) -> int:
        row = self.db.one("SELECT COALESCE(MAX(seq), 0) AS seq FROM agent_changes")
        return int(row["seq"])

    def prune(self, keep: int = KEEP) -> None:
        self.db.execute("DELETE FROM agent_changes WHERE seq <= (SELECT MAX(seq) FROM agent_changes) - ?", (keep,))
