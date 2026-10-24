"""智能助手会话事件日志（独立 SQLite：data_dir/agent.sqlite3）。

会话、密钥保险库与能力缺口不是业务数据：放在独立库里，不进入 ``mindex backup`` 的业务库快照，
保险库里的密钥也就不会随备份外流。

事件信封沿用 dsh：``{session_id, seq, time, type, data, ignorable?}``，``seq`` 在会话内从 0 连续递增，
``time`` 为 Unix 毫秒。核心事件类型与 dsh 一致；Mindex 扩展事件一律使用 ``plugin:mindex/...``。

发给模型的历史只从日志派生（``derive_messages``）：日志里没有的内容，模型永远看不到；
密钥明文在写入前替换为 ``secret://`` 引用，日志里永远没有明文。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from mindex.agent.llm.types import tool_calls, tool_message

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'idle',
  grants TEXT NOT NULL DEFAULT '[]', context TEXT,
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
"""

SECRET_PREFIX = "secret://"
OMITTED = "（较早的工具结果已省略以控制上下文长度；需要时请重新调用工具获取最新数据）"


def now_ms() -> int:
    return int(time.time() * 1000)


class SessionNotFound(Exception):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()) -> None:
        with self.connect() as db:
            db.execute(sql, args)


class SessionStore:
    def __init__(self, data_dir: Path):
        self.store = Database(Path(data_dir) / "agent.sqlite3")
        with self.store.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    # ------------------------------------------------------------ 会话
    def create(self, title: str = "") -> dict:
        sid = f"session-{uuid4().hex[:16]}"
        stamp = now_ms()
        self.store.execute(
            "INSERT INTO agent_sessions(id,title,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (sid, title[:60], "idle", stamp, stamp),
        )
        return self.get(sid)

    def get(self, sid: str) -> dict:
        row = self.store.one("SELECT * FROM agent_sessions WHERE id=?", (sid,))
        if row is None:
            raise SessionNotFound("找不到助手会话")
        return {**row, "grants": json.loads(row["grants"]), "context": json.loads(row["context"] or "null")}

    def list(self) -> list[dict]:
        rows = self.store.rows("SELECT id,title,status,created_at,updated_at FROM agent_sessions ORDER BY updated_at DESC")
        return rows

    def update(self, sid: str, **fields) -> None:
        if not fields:
            return
        values = {k: json.dumps(v, ensure_ascii=False) if k in ("grants", "context") else v for k, v in fields.items()}
        values["updated_at"] = now_ms()
        assignments = ",".join(f"{k}=?" for k in values)
        self.store.execute(f"UPDATE agent_sessions SET {assignments} WHERE id=?", (*values.values(), sid))

    def delete(self, sid: str) -> None:
        self.get(sid)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM agent_events WHERE session_id=?", (sid,))
            db.execute("DELETE FROM agent_secrets WHERE session_id=?", (sid,))
            db.execute("DELETE FROM agent_sessions WHERE id=?", (sid,))
            db.execute("COMMIT")

    # ------------------------------------------------------------ 事件
    def append(self, sid: str, kind: str, data, *, ignorable: bool = False) -> dict:
        data = self.redact(sid, data)
        stamp = now_ms()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT COALESCE(MAX(seq), -1) FROM agent_events WHERE session_id=?", (sid,)).fetchone()
            seq = row[0] + 1
            db.execute(
                "INSERT INTO agent_events(session_id,seq,time,type,data,ignorable) VALUES(?,?,?,?,?,?)",
                (sid, seq, stamp, kind, json.dumps(data, ensure_ascii=False), int(ignorable)),
            )
            db.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?", (stamp, sid))
            db.execute("COMMIT")
        event = {"session_id": sid, "seq": seq, "time": stamp, "type": kind, "data": data}
        if ignorable:
            event["ignorable"] = True
        return event

    def events(self, sid: str, after: int = -1, types: tuple[str, ...] | None = None) -> list[dict]:
        rows = self.store.rows(
            "SELECT * FROM agent_events WHERE session_id=? AND seq>? ORDER BY seq", (sid, after)
        )
        result = []
        for row in rows:
            if types and row["type"] not in types:
                continue
            event = {"session_id": sid, "seq": row["seq"], "time": row["time"], "type": row["type"],
                     "data": json.loads(row["data"])}
            if row["ignorable"]:
                event["ignorable"] = True
            result.append(event)
        return result

    def last_turn(self, sid: str) -> int:
        row = self.store.one(
            "SELECT MAX(json_extract(data,'$.turn')) AS turn FROM agent_events WHERE session_id=? AND type='turn/start'",
            (sid,),
        )
        return int(row["turn"]) if row and row["turn"] is not None else 0

    # ------------------------------------------------------------ 密钥
    def put_secret(self, sid: str, value: str, ttl: int) -> str:
        sid_ = f"{SECRET_PREFIX}{secrets.token_hex(12)}"
        stamp = now_ms()
        self.store.execute(
            "INSERT INTO agent_secrets(id,session_id,value,created_at,expires_at) VALUES(?,?,?,?,?)",
            (sid_, sid, value, stamp, stamp + ttl * 1000),
        )
        return sid_

    def resolve_secret(self, sid: str, ref: str) -> str:
        row = self.store.one("SELECT * FROM agent_secrets WHERE id=?", (ref,))
        if row is None or row["session_id"] != sid:
            raise ValueError("密钥引用无效或不属于本会话，请重新通过表单填写")
        if row["expires_at"] < now_ms():
            raise ValueError("密钥引用已过期，请重新通过表单填写")
        return row["value"]

    def redact(self, sid: str, data):
        values = [r["value"] for r in self.store.rows(
            "SELECT id, value FROM agent_secrets WHERE session_id=?", (sid,)) if len(r["value"]) >= 4]
        if not values:
            return data
        refs = {r["value"]: r["id"] for r in self.store.rows(
            "SELECT id, value FROM agent_secrets WHERE session_id=?", (sid,))}

        def scrub(value):
            if isinstance(value, str):
                for plain in values:
                    if plain in value:
                        value = value.replace(plain, refs[plain])
                return value
            if isinstance(value, dict):
                return {k: scrub(v) for k, v in value.items()}
            if isinstance(value, list):
                return [scrub(v) for v in value]
            return value

        return scrub(data)

    # ------------------------------------------------------------ 能力缺口
    def add_gap(self, sid: str | None, request: str, reason: str, suggestion: str = "") -> dict:
        with self.store.connect() as db:
            cursor = db.execute(
                "INSERT INTO agent_gaps(session_id,request,reason,suggestion,created_at) VALUES(?,?,?,?,?)",
                (sid, request, reason, suggestion, now_ms()),
            )
            gid = cursor.lastrowid
        return self.store.one("SELECT * FROM agent_gaps WHERE id=?", (gid,))

    def gaps(self, limit: int = 200) -> list[dict]:
        return self.store.rows("SELECT * FROM agent_gaps ORDER BY id DESC LIMIT ?", (limit,))


def estimate_tokens(value) -> int:
    return len(json.dumps(value, ensure_ascii=False)) // 3 + 1


def derive_messages(events: list[dict], *, budget: int | None = None, keep_recent_results: int = 6) -> list[dict]:
    """从日志派生发给模型的消息序列。

    每个工具调用都要有对应结果：日志里缺失的（例如进程被杀）补一条错误结果，保证协议合法。
    超出预算时，先把较早的工具结果替换为省略说明，仍超出再从最早的完整用户轮次起整体丢弃。
    """
    messages: list[dict] = []
    answered: set[str] = set()
    for event in events:
        kind, data = event["type"], event["data"]
        if kind == "user/message":
            messages.append(data)
        elif kind == "assistant/message":
            messages.append(data["message"])
        elif kind == "tool/result":
            messages.append(data["message"])
            answered.add(data["message"]["toolCallId"])
    result: list[dict] = []
    for index, message in enumerate(messages):
        result.append(message)
        if message["role"] != "assistant":
            continue
        following = {m.get("toolCallId") for m in messages[index + 1:] if m["role"] == "tool"}
        for call in tool_calls(message):
            if call["id"] not in following:
                result.append(tool_message(call["id"], "Error: 该工具调用未完成（会话被中断）", is_error=True))
    # 工具结果必须紧跟其所属的 assistant 消息：中间插入的 user 消息（如页面上下文）移到结果之后。
    ordered: list[dict] = []
    pending_users: list[dict] = []
    open_calls: set[str] = set()
    for message in result:
        if message["role"] == "assistant":
            ordered.extend(pending_users)
            pending_users = []
            ordered.append(message)
            open_calls = {c["id"] for c in tool_calls(message)}
        elif message["role"] == "tool":
            ordered.append(message)
            open_calls.discard(message["toolCallId"])
            if not open_calls:
                ordered.extend(pending_users)
                pending_users = []
        elif open_calls:
            pending_users.append(message)
        else:
            ordered.append(message)
    ordered.extend(pending_users)
    if budget is None or estimate_tokens(ordered) <= budget:
        return ordered
    tool_indexes = [i for i, m in enumerate(ordered) if m["role"] == "tool"]
    for i in tool_indexes[:-keep_recent_results] if keep_recent_results else tool_indexes:
        ordered[i] = {**ordered[i], "content": [{"type": "text", "text": OMITTED}]}
        if estimate_tokens(ordered) <= budget:
            return ordered
    while estimate_tokens(ordered) > budget:
        starts = [i for i, m in enumerate(ordered) if m["role"] == "user" and m["source"].get("kind") != "context"]
        if len(starts) < 2:
            break
        ordered = ordered[starts[1]:]
    return ordered
