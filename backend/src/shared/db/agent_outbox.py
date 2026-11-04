"""智能助手变更 outbox（plans §9 阶段 5）：跨 API/进程的改动先写 PG，再由网关转入 SSE。

- 业务 API（cluster 形态）或 `mindex mcp --base-url` 报告改动时写 outbox；
- 单实例 agent-gateway 轮询 outbox，按 id 顺序幂等转入本地 ChangeFeed（SSE 推送），
  避免每个 API 各自维护 SSE 事件流导致分裂；
- 至少一次：drain 在单事务内标记 consumed 并返回行；网关随后发布到 ChangeFeed，
  崩溃窗口可能丢失一次提示（SSE 属提示性通道，业务状态仍以元数据库为准）。
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

# 与 agent.changes.KINDS/SOURCES 保持一致。shared 不得反向依赖 agent 包
# （tests/test_import_boundaries.py 的包边界规则）；漂移由 agent 侧测试兜底。
KINDS = ("mutation", "focus")
SOURCES = ("agent", "mcp")

__all__ = ["drain", "publish"]


def publish(store, kind: str, data: dict, *, source: str = "mcp",
            session_id: str | None = None, tab: str | None = None) -> int:
    """写入 outbox（在 cluster 业务 API 上代替本地 ChangeFeed.publish）。返回 outbox id。"""
    if kind not in KINDS:
        raise ValueError(f"未知改动类型 {kind}")
    if source not in SOURCES:
        raise ValueError(f"未知改动来源 {source}")
    library_id = getattr(store, "library_id", None) or (
        store.one("SELECT value FROM settings WHERE key='library.id'") or {"value": ""})["value"]
    with store.connect() as db:
        row = db.execute(
            "INSERT INTO agent_outbox(library_id,kind,source,session_id,tab,data) "
            "VALUES(?,?,?,?,?,?) RETURNING id",
            (library_id, kind, source, session_id, tab,
             json.dumps(data, ensure_ascii=False))).fetchone()
        return int(row[0])


def drain(store, *, limit: int = 200) -> list[dict]:
    """取走未消费的改动（单事务标记 consumed）；返回按 id 升序的条目。"""
    library_id = getattr(store, "library_id", None) or (
        store.one("SELECT value FROM settings WHERE key='library.id'") or {"value": ""})["value"]
    with store.connect() as db:
        rows = db.execute(
            "WITH picked AS (SELECT id FROM agent_outbox WHERE library_id=? AND consumed_at IS NULL"
            " ORDER BY id LIMIT ? FOR UPDATE SKIP LOCKED)"
            " UPDATE agent_outbox o SET consumed_at=clock_timestamp() FROM picked"
            " WHERE o.id=picked.id RETURNING o.id,o.kind,o.source,o.session_id,o.tab,o.data",
            (library_id, limit)).fetchall()
    items = []
    for row in rows:
        data = row["data"]
        items.append({
            "outbox_id": int(row["id"]), "kind": row["kind"], "source": row["source"],
            "session_id": row["session_id"], "tab": row["tab"],
            "data": json.loads(data) if isinstance(data, str) else data,
        })
    items.sort(key=lambda item: item["outbox_id"])
    return items
