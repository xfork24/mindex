"""postgres 队列的入队与补偿（plans §7.2 阶段 3）。

- ``enqueue_task``：账本行入队（dedupe = 表:任务 ID，幂等；重复提交只产生一个
  有效工作）。接受 RuntimeConfig 或 Config。
- ``ledger_transaction``：创建/重试账本与入队同事务。
- ``enqueue_backlog``：仅修复历史 queued 行与异常滞留，不承担正常写入的原子性。
- payload 只携带账本表与任务 ID（引用数据），不序列化函数/路径/凭据。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import cast

from mindex.shared.db.pg_backend import PgBackend

log = logging.getLogger(__name__)

__all__ = ["QUEUE_KINDS", "enqueue_backlog", "enqueue_task", "queue_backend"]

# 账本表 → worker 领取 kind（与 worker_handlers._KIND_TABLES 一致）。
QUEUE_KINDS = {
    "source_tasks": "source",
    "dialogue_tasks": "dialogue",
    "person_tasks": "person",
    "topic_runs": "topic",
    "index_tasks": "index",
    "studio_tasks": "studio",
}


def validate_index_options(store, options):
    from mindex.shared.task_config import IndexOptions

    parsed = IndexOptions.model_validate(options or {})
    if parsed.action == "activate" and not parsed.index_id:
        raise ValueError("激活任务必须指定 index_id")
    if parsed.action == "activate-release" and not parsed.release_id:
        raise ValueError("激活检索组合必须指定 release_id")
    if parsed.action == "activate-release" and not store.one(
            "SELECT id FROM search_releases WHERE id=?", (parsed.release_id,)):
        raise ValueError("找不到检索发布记录")
    if parsed.index_id is not None:
        version = store.index_version(parsed.index_id)
        if parsed.action == "lexical" and version["status"] != "ready":
            raise ValueError("词法回填需要就绪的索引")
    return parsed


def insert_index_task(store, task_id, config, options):
    from mindex.shared.task_config import freeze_config

    parsed = validate_index_options(store, options)
    insert_ledger(store, "index_tasks", task_id,
        "INSERT INTO index_tasks(id,source_id,status,config,options) VALUES(?,?,'queued',?,?)",
        (task_id, parsed.source_id, freeze_config(config, store), parsed.model_dump_json()))


def wait_index_task(store, task_id, *, progress=None):
    import time

    from fastapi import HTTPException

    from mindex.shared.background_tasks import public_task

    while True:
        row = store.one("SELECT * FROM index_tasks WHERE id=?", (task_id,))
        if row is None:
            raise HTTPException(404, "找不到索引任务")
        task = public_task(row)
        if task["status"] not in {"queued", "running"}:
            break
        if progress:
            progress(task["progress"])
        time.sleep(.2)
    if task["status"] != "complete":
        error = task["error"] or "索引任务未完成，请查看任务状态"
        code = 409 if task["status"] == "cancelled" or error.startswith("Timeout:") else (
            503 if error.startswith("ModelError:") else 422)
        raise HTTPException(code, error)
    return task["result"]


def run_index_task(config, store, options, *, progress=None):
    """底层构建器只依赖共享队列提交协议；执行编排由 worker 的上层 runner 负责。"""
    from uuid import uuid4

    from mindex.shared import lock_backend

    task_id = uuid4().hex
    with lock_backend.resource(store, "index-task:" + task_id, timeout=0):
        insert_index_task(store, task_id, config, options)
    return wait_index_task(store, task_id, progress=progress)


@contextmanager
def ledger_transaction(store, table, task_id, *, restart=False):
    """账本创建/重试与调度同事务；本地形态只执行账本写入。"""
    if table not in QUEUE_KINDS:
        raise ValueError("无效的任务表")
    with store.connect() as db:
        yield db
        if getattr(store, "queue_backend", "inline") == "postgres":
            from mindex.shared.db.queue_pg import PgTaskQueue

            PgTaskQueue(store.library_id).enqueue(
                db, QUEUE_KINDS[table], {"ledger_table": table, "task_id": task_id},
                ledger_table=table, ledger_id=task_id, dedupe_key=f"{table}:{task_id}", restart=restart)


def insert_ledger(store, table, task_id, sql, args):
    with ledger_transaction(store, table, task_id) as db:
        db.execute(sql, args)


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
    with cast(PgBackend, store.backend).connect() as db:
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
    with cast(PgBackend, store.backend).connect() as db:
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
