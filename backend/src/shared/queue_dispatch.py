"""postgres 队列的入队与补偿（plans §7.2 阶段 3）。

- ``enqueue_task``：账本行入队（dedupe = 表:任务 ID，幂等；重复提交只产生一个
  有效工作）。接受 RuntimeConfig 或 Config。
- ``enqueue_backlog``：扫描四张账本表中 ``queued`` 状态的行补入队列——覆盖
  「账本已落、入队前崩溃」窗口与 worker 启动时的滞留恢复（durable outbox 的
  简化补偿；阶段 5 收紧为同事务）。
- payload 只携带账本表与任务 ID（引用数据），不序列化函数/路径/凭据。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

__all__ = ["QUEUE_KINDS", "enqueue_backlog", "enqueue_task", "queue_backend"]

# 账本表 → worker 领取 kind（与 worker_handlers._KIND_TABLES 一致）。
QUEUE_KINDS = {
    "source_tasks": "source",
    "dialogue_tasks": "dialogue",
    "person_tasks": "person",
    "topic_runs": "topic",
}


def queue_backend(runtime_or_config) -> str:
    config = runtime_or_config.current() if hasattr(runtime_or_config, "current") else runtime_or_config
    return config.queue.backend if getattr(config, "queue", None) is not None else "inline"


def _library_id(store) -> str:
    return getattr(store, "library_id", None) or (
        store.one("SELECT value FROM settings WHERE key='library.id'") or {"value": ""})["value"]


def enqueue_task(runtime_or_config, table: str, task_id: str, *, priority: int = 100) -> int | None:
    from mindex.shared.db.queue_pg import PgTaskQueue
    from mindex.shared.storage import Store

    config = runtime_or_config.current() if hasattr(runtime_or_config, "current") else runtime_or_config
    store = Store.open(config)
    queue = PgTaskQueue(_library_id(store))
    with store.backend.connect() as db:
        return queue.enqueue(
            db, QUEUE_KINDS[table], {"ledger_table": table, "task_id": task_id},
            ledger_table=table, ledger_id=task_id, dedupe_key=f"{table}:{task_id}",
            priority=priority)


def enqueue_backlog(runtime_or_config) -> dict[str, int]:
    """把 queued 账本行补入队列（幂等）。API/worker 启动时调用。"""
    if queue_backend(runtime_or_config) != "postgres":
        return {}
    from mindex.shared.db.queue_pg import PgTaskQueue
    from mindex.shared.storage import Store

    config = runtime_or_config.current() if hasattr(runtime_or_config, "current") else runtime_or_config
    store = Store.open(config)
    queue = PgTaskQueue(_library_id(store))
    counts: dict[str, int] = {}
    with store.backend.connect() as db:
        for table, kind in QUEUE_KINDS.items():
            rows = db.execute(f"SELECT id FROM {table} WHERE status='queued'").fetchall()
            for row in rows:
                task_id = row["id"]
                queue.enqueue(
                    db, kind, {"ledger_table": table, "task_id": task_id},
                    ledger_table=table, ledger_id=task_id, dedupe_key=f"{table}:{task_id}")
                counts[table] = counts.get(table, 0) + 1
    if counts:
        log.info("队列补偿扫描入队：%s", counts)
    return counts
