"""PostgreSQL 至少一次任务队列（plans §7.2 阶段 3）。

- **账本是用户可见状态来源**：``source_tasks`` 等表记录进度/结果；本表只存调度
  状态（``ledger_id`` 引用账本行），入队与账本写入在**同一事务**（调用方把
  ``enqueue`` 放进自己的 ``store.connect()`` 块；跨事务场景用 outbox，阶段 5）。
- claim：单条短事务 ``FOR UPDATE SKIP LOCKED``，按 priority、run_after、id 取任务；
  attempts+1 并分配新 attempt_id/fencing_token；到期 running 先条件回收再重排。
- 至少一次交付：崩溃/超时由回收重派；只有原 owner+attempt 能续期/完成/失败
  （旧 worker 恢复后不能提交别人的 attempt）。副作用方按 attempt_id 写新产物、
  以 fencing 校验提交（plans §7.1/§7.2）。
- 重试：有上限指数退避 + 抖动；超上限置 failed 保留可人工重试。
- 取消：pending 原子转 cancelled；running 持久化 cancel_requested，由持有 worker
  传播终止；完成状态不早于账本一致。
- payload 只放可 JSON 序列化的引用数据（表名/任务 ID/参数），不序列化函数、
  局部路径、数据库连接或凭据。
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindex.shared.db.pg_backend import PgConnection

log = logging.getLogger(__name__)

__all__ = ["TASK_TTL_SECONDS", "ClaimedTask", "PgTaskQueue"]

TASK_TTL_SECONDS = 120.0  # running 心跳到期；恢复 = TTL + 回收周期 + 调度等待
_BASE_BACKOFF_SECONDS = 5.0
_MAX_BACKOFF_SECONDS = 300.0
# 到期回收后的重排延迟：短于失败退避（任务已死，尽快重派），抖动避免同时回收
# 的一批任务同时唤醒（plans §7.2）。
_RECLAIM_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class ClaimedTask:
    id: int
    kind: str
    owner: str
    ledger_table: str | None
    ledger_id: str | None
    payload: dict
    attempts: int
    max_attempts: int
    attempt_id: str
    fencing_token: int
    cancel_requested: bool
    execution: object | None = field(default=None, compare=False, repr=False)


class PgTaskQueue:
    """队列原语；连接由调用方事务（或 worker 循环）提供。"""

    backend = "postgres"

    def __init__(self, library_id: str):
        self.library_id = library_id

    # ------------------------------------------------------------ 入队
    def enqueue(self, db: PgConnection, kind: str, payload: dict, *,
                ledger_table: str | None = None, ledger_id: str | None = None,
                dedupe_key: str | None = None, priority: int = 100,
                max_attempts: int = 3, restart: bool = False) -> int | None:
        """在调用方事务内入队；dedupe 冲突返回已有任务 id（重复提交只产生一个有效工作）。"""
        key = dedupe_key or f"{kind}:{ledger_table}:{ledger_id}:{uuid.uuid4().hex[:8]}"
        if restart:
            db.execute(
                "UPDATE task_queue SET status='pending',owner=NULL,attempt_id=NULL,expires_at=NULL,"
                " cancel_requested=FALSE,error=NULL,attempts=0,run_after=clock_timestamp(),"
                " updated_at=clock_timestamp() WHERE library_id=%s AND dedupe_key=%s"
                " AND status IN ('failed','cancelled','succeeded')", (self.library_id, key))
        row = db.execute(
            "INSERT INTO task_queue(library_id,kind,payload,ledger_table,ledger_id,"
            "dedupe_key,priority,max_attempts) VALUES(%s,%s,%s::jsonb,%s,%s,%s,%s,%s)"
            " ON CONFLICT (library_id,dedupe_key) DO UPDATE SET updated_at=clock_timestamp()"
            " RETURNING id, (xmax = 0) AS inserted",
            (self.library_id, kind, _json(payload), ledger_table, ledger_id,
             key, priority, max_attempts)).fetchone()
        if not row["inserted"]:
            log.info("队列去重命中：%s（%s）", key, kind)
        return int(row["id"])

    # ------------------------------------------------------------ 领取
    def reclaim_expired(self, db: PgConnection) -> int:
        """到期 running 回收：重置为 pending 并短暂延迟重排（不认定成功）。

        重排时刻以**过期时刻**为基准加抖动：久死的任务立即可领，刚过期的任务
        留出短暂缓冲，同一批回收不会同时唤醒。
        """
        rows = db.execute(
            "WITH expired AS (SELECT id FROM task_queue WHERE library_id=%s AND status='running'"
            " AND expires_at < clock_timestamp() FOR UPDATE SKIP LOCKED)"
            "UPDATE task_queue SET status=CASE WHEN cancel_requested THEN 'cancelled'"
            " WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,"
            " owner=NULL, attempt_id=NULL,expires_at=NULL,"
            " run_after=expires_at + make_interval(secs => %s * (0.5 + random())),"
            " error=CASE WHEN cancel_requested THEN '用户已取消任务' ELSE '任务租约已到期，执行已中断' END,"
            " updated_at=clock_timestamp()"
            " WHERE id IN (SELECT id FROM expired)"
            " RETURNING ledger_table,ledger_id,status,error",
            (self.library_id, _RECLAIM_DELAY_SECONDS)).fetchall()
        for row in rows:
            self._ledger(db, row, {"pending": "queued"}.get(row["status"], row["status"]),
                         error=row["error"], clear=True)
        return len(rows)

    def claim(self, db: PgConnection, worker: str, kinds: list[str], *,
              ttl: float = TASK_TTL_SECONDS) -> ClaimedTask | None:
        """领取一个任务（优先级→到期时间→id）；没有可用任务返回 None。"""
        self.reclaim_expired(db)
        row = db.execute(
            "WITH picked AS ("
            " SELECT id FROM task_queue WHERE library_id=%s AND status='pending'"
            " AND kind = ANY(%s) AND run_after <= clock_timestamp()"
            " AND NOT cancel_requested"
            " ORDER BY priority, run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED)"
            " UPDATE task_queue t SET status='running', owner=%s, attempts=t.attempts+1,"
            " attempt_id=%s, fencing_token=t.fencing_token+1,"
            " expires_at=clock_timestamp() + make_interval(secs => %s),"
            " updated_at=clock_timestamp(), cancel_requested=FALSE"
            " FROM picked WHERE t.id=picked.id AND t.library_id=%s"
            " RETURNING t.id, t.kind, t.ledger_table, t.ledger_id, t.payload, t.attempts,"
            " t.max_attempts, t.attempt_id, t.fencing_token, t.cancel_requested",
            (self.library_id, list(kinds), worker, uuid.uuid4().hex, ttl,
             self.library_id)).fetchone()
        if row is None:
            return None
        self._ledger(db, row, "running", attempt=row["attempt_id"], token=row["fencing_token"])
        return ClaimedTask(
            id=int(row["id"]), kind=row["kind"], owner=worker,
            ledger_table=row["ledger_table"], ledger_id=row["ledger_id"],
            payload=dict(row["payload"]), attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]), attempt_id=row["attempt_id"],
            fencing_token=int(row["fencing_token"]),
            cancel_requested=bool(row["cancel_requested"]))

    # ------------------------------------------------------------ 持有者操作
    def _ledger(self, db, row, status, *, error=None, clear=False, attempt=None, token=None):
        from mindex.shared.db.scaling_schema import ALL_LEDGER_TABLES

        table, identifier = row["ledger_table"], row["ledger_id"]
        if table not in ALL_LEDGER_TABLES or not identifier:
            return
        bindings = ",queue_attempt_id=NULL" if clear else ""
        args = [status, error]
        if attempt is not None:
            bindings += ",queue_attempt_id=%s,queue_fencing_token=%s"
            args.extend((attempt, token))
        args.append(identifier)
        db.execute(f"UPDATE {table} SET status=%s,error=%s,updated_at=CURRENT_TIMESTAMP{bindings} WHERE id=%s", args)

    def renew(self, db: PgConnection, task: ClaimedTask, *, ttl: float = TASK_TTL_SECONDS) -> bool:
        """续期心跳；owner/attempt/token 不匹配或非 running 返回 False。"""
        cursor = db.execute(
            "UPDATE task_queue SET expires_at=clock_timestamp() + make_interval(secs => %s),"
            " updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='running' AND owner=%s"
            " AND attempt_id=%s AND fencing_token=%s AND expires_at >= clock_timestamp()",
            (ttl, self.library_id, task.id, task.owner, task.attempt_id, task.fencing_token))
        return cursor.rowcount > 0

    def complete(self, db: PgConnection, task: ClaimedTask) -> bool:
        """只有原 owner+attempt 能完成；队列与账本完成由调用方放同一事务。"""
        row = db.execute(
            "UPDATE task_queue SET status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'succeeded' END,"
            " expires_at=NULL, updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='running' AND owner=%s"
            " AND attempt_id=%s AND fencing_token=%s AND expires_at >= clock_timestamp()"
            " RETURNING ledger_table,ledger_id,status",
            (self.library_id, task.id, task.owner, task.attempt_id, task.fencing_token)).fetchone()
        if row:
            self._ledger(db, row, "complete" if row["status"] == "succeeded" else "cancelled")
        return row is not None

    def finish_cancelled(self, db: PgConnection, task: ClaimedTask) -> bool:
        row = db.execute(
            "UPDATE task_queue SET status='cancelled', expires_at=NULL, updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='running' AND owner=%s"
            " AND attempt_id=%s AND fencing_token=%s AND expires_at >= clock_timestamp()"
            " RETURNING ledger_table,ledger_id",
            (self.library_id, task.id, task.owner, task.attempt_id, task.fencing_token)).fetchone()
        if row:
            self._ledger(db, row, "cancelled")
        return row is not None

    def fail(self, db: PgConnection, task: ClaimedTask, error: str, *,
             backoff: float | None = None) -> str:
        """失败：可重试则退避重排，超上限置 failed（可人工重试）。返回新状态。

        ``backoff`` 缺省为有上限指数退避 + 抖动（plans §7.2）。
        """
        delay = backoff if backoff is not None else _backoff(task.attempts)
        row = db.execute(
            "UPDATE task_queue SET status='failed', error=%s, expires_at=NULL,"
            " updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='running' AND owner=%s"
            " AND attempt_id=%s AND fencing_token=%s AND expires_at >= clock_timestamp()"
            " AND attempts >= max_attempts"
            " RETURNING ledger_table,ledger_id",
            (str(error)[:2000], self.library_id, task.id, task.owner,
             task.attempt_id, task.fencing_token)).fetchone()
        if row:
            self._ledger(db, row, "failed", error=str(error)[:2000])
            return "failed"
        row = db.execute(
            "UPDATE task_queue SET status='pending', error=%s, owner=NULL, attempt_id=NULL,"
            " run_after=clock_timestamp() + make_interval(secs => %s),"
            " updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='running' AND owner=%s"
            " AND attempt_id=%s AND fencing_token=%s AND expires_at >= clock_timestamp()"
            " RETURNING ledger_table,ledger_id",
            (str(error)[:2000], delay, self.library_id, task.id,
             task.owner, task.attempt_id, task.fencing_token)).fetchone()
        if row is None:
            raise RuntimeError("任务不属于当前 attempt（旧 worker 不得提交他人重派的任务）")
        self._ledger(db, row, "queued", error=str(error)[:2000], clear=True)
        return "pending"

    # ------------------------------------------------------------ 取消
    def cancel(self, db: PgConnection, task_id: int) -> str:
        """pending 原子转 cancelled；running 持久化意图（持有方轮询传播）。"""
        row = db.execute(
            "UPDATE task_queue SET status='cancelled', updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='pending'"
            " RETURNING ledger_table,ledger_id", (self.library_id, task_id)).fetchone()
        if row:
            self._ledger(db, row, "cancelled", clear=True)
            return "cancelled"
        row = db.execute(
            "UPDATE task_queue SET cancel_requested=TRUE, updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='running'"
            " RETURNING id", (self.library_id, task_id)).fetchone()
        if row:
            return "cancel_requested"
        row = db.execute(
            "SELECT status FROM task_queue WHERE library_id=%s AND id=%s",
            (self.library_id, task_id)).fetchone()
        return row["status"] if row else "missing"

    def cancel_requested(self, db: PgConnection, task: ClaimedTask) -> bool:
        row = db.execute(
            "SELECT cancel_requested FROM task_queue WHERE library_id=%s AND id=%s",
            (self.library_id, task.id)).fetchone()
        return bool(row and row["cancel_requested"])

    def retry(self, db: PgConnection, task_id: int) -> bool:
        """人工重试 failed：清错误重排（attempts 保留历史）。"""
        row = db.execute(
            "UPDATE task_queue SET status='pending', error=NULL, attempts=0,"
            " run_after=clock_timestamp(), updated_at=clock_timestamp()"
            " WHERE library_id=%s AND id=%s AND status='failed'"
            " RETURNING ledger_table,ledger_id",
            (self.library_id, task_id)).fetchone()
        if row:
            self._ledger(db, row, "queued", clear=True)
        return row is not None

    # ------------------------------------------------------------ 观测
    def stats(self, db: PgConnection) -> dict:
        rows = db.execute(
            "SELECT status, count(*) AS n, min(run_after) AS oldest"
            " FROM task_queue WHERE library_id=%s GROUP BY status", (self.library_id,)).fetchall()
        return {row["status"]: {"count": int(row["n"]),
                                "oldest": str(row["oldest"]) if row["oldest"] else None}
                for row in rows}


def _backoff(attempts: int) -> float:
    jitter = random.uniform(0.8, 1.2)
    return min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1))) * jitter


def _json(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
