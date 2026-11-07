"""迁移只恢复账本意图；旧 attempt/远端执行从不直接变为成功。"""

from __future__ import annotations

import json

from shared import jsonio
from shared.db.queue_pg import PgTaskQueue
from shared.queue_dispatch import QUEUE_KINDS
from shared.storage import dumps


def export_execution(store, target):
    groups = store.rows("SELECT * FROM gpu_resource_groups")
    allocations = store.rows("SELECT * FROM gpu_allocations WHERE state IN ('active','unknown')")
    bindings = []
    if store.pg and allocations:
        ids = sorted({row["task_id"] for row in allocations})
        bindings = store.rows(
            "SELECT id,ledger_table,ledger_id FROM task_queue WHERE id IN ("
            + ",".join("?" for _ in ids)
            + ")",
            ids,
        )
    jsonio.save(
        target / "execution_manifest.json",
        {"format": 1, "groups": groups, "allocations": allocations, "bindings": bindings},
    )


def restore_execution(store, source, journal):

    path = source / "execution_manifest.json"
    runtime = json.loads(path.read_text()) if path.is_file() else {"groups": [], "allocations": []}
    summary = {}
    with store.connect() as db:
        # 与元数据对账分离的目标运行状态；重复恢复只按同一阶段提交一次。
        if journal.done("tasks"):
            return
        for group in runtime["groups"]:
            old = db.execute("SELECT capacity FROM gpu_resource_groups WHERE id=?", (group["id"],)).fetchone()
            if old and old[0] != group["capacity"]:
                raise ValueError("目标物理资源组容量与迁移记录不一致")
            db.execute(
                "INSERT OR IGNORE INTO gpu_resource_groups(id,capacity) VALUES(?,?)",
                (group["id"], group["capacity"]),
            )
        for row in runtime["allocations"]:
            db.execute(
                "INSERT INTO gpu_allocations(id,group_id,library_id,task_id,attempt_id,state,evidence) "
                "VALUES(?,?,?,?,?,'unknown',?) ON CONFLICT(group_id,attempt_id) DO NOTHING",
                (
                    row["id"],
                    row["group_id"],
                    store.library_id,
                    row["task_id"],
                    row["attempt_id"],
                    "迁移前远端执行未证明停止，请附依据核对后释放",
                ),
            )
        db.execute(
            "UPDATE external_calls SET state='needs_review',error='迁移前远端执行状态未知，请核对回执' "
            "WHERE state='dispatching'"
        )
        unknown = {
            row[0]
            for row in db.execute(
                "SELECT task_id FROM external_calls WHERE state IN ('dispatching','needs_review')"
            )
        }
        unknown_gpu = {(row["ledger_table"], row["ledger_id"]) for row in runtime.get("bindings", [])}
        for table, kind in QUEUE_KINDS.items():
            rows = db.execute(
                f"SELECT id,status FROM {table} WHERE status IN ('running','queued')"
            ).fetchall()
            for row in rows:
                task_id = row["id"]
                if task_id in unknown or (table, task_id) in unknown_gpu:
                    state, message = "failed", "迁移前远端执行结果未知，需核对后重试"
                elif store.pg and getattr(store, "queue_backend", "inline") == "postgres":
                    state, message = "queued", "迁移恢复：旧 attempt 已失效，等待新执行凭证"
                else:
                    state, message = "interrupted", "迁移恢复：旧 attempt 已失效，请显式重试"
                db.execute(
                    f"UPDATE {table} SET status=?,error=?,queue_attempt_id=NULL,queue_fencing_token=0 WHERE "
                    f"id=?",
                    (state, message, task_id),
                )
                if state == "queued":
                    PgTaskQueue(store.library_id).enqueue(
                        db,
                        kind,
                        {"ledger_table": table, "task_id": task_id},
                        ledger_table=table,
                        ledger_id=task_id,
                        dedupe_key=f"{table}:{task_id}",
                    )
                summary[table] = summary.get(table, 0) + 1
        journal.stage("tasks", "complete", connection=db, details=summary)
        # 在同一事务内登记恢复后预期指纹，不能把恢复任务状态误报为源业务数据漂移。
        # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
        from maintenance.migration import database_manifest

        restored = database_manifest(store, connection=db)
        db.execute(
            "UPDATE migration_runs SET restored_tables=? WHERE migration_id=?", (dumps(restored), journal.id)
        )
