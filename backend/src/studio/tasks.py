"""Studio 可恢复执行入口：API 只提交描述；worker 按冻结配置执行。"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from uuid import uuid4

from settings.runtime_settings import RuntimeConfig
from shared import files, lock_backend
from shared.db.queue_pg import PgTaskQueue
from shared.directory_files import Snapshot, bind, snapshot
from shared.external_calls import scope
from shared.queue_dispatch import ledger_transaction
from shared.storage import Store, dumps
from shared.task_config import freeze_config, restore_config
from studio import steps as studio_steps
from studio import studio, workspace


@contextmanager
def publication(config, job_id):
    if config.queue.backend != "postgres":
        yield
        return
    if config.storage.backend != "s3":
        raise ValueError("Studio 持久执行需要共享 S3 存储")
    with bind(Store.open(config), f"studio/{job_id}/", deferred=True):
        yield


def submit(config, job_id, kind, options, *, build_id=None):
    store = Store.open(config)
    task_id = uuid4().hex
    view = snapshot(store, f"studio/{job_id}/")
    if build_id:
        entry = view.entries.get(f"studio/{job_id}/workspace.json")
        if entry is not None:
            with store.media.open_read(entry["object_key"]) as source:
                document = json.load(source)
            record = next(b for b in document["builds"] if b["id"] == build_id)
            record["task_id"] = task_id
            view.put(f"studio/{job_id}/workspace.json", json.dumps(document, ensure_ascii=False).encode())
    frozen = freeze_config(config, store, studio=True)
    with ledger_transaction(store, "studio_tasks", task_id) as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute(
            "SELECT id FROM studio_tasks WHERE job_id=? AND status IN ('queued','running')", (job_id,)
        ).fetchone()
        if active:
            raise ValueError("此创作任务已有排队或运行中的执行")
        revision = view.commit(db)
        db.execute(
            "INSERT INTO studio_tasks(id,job_id,kind,build_id,status,config,options) "
            "VALUES(?,?,?,?,'queued',?,?)",
            (task_id, job_id, kind, build_id, frozen, dumps(options)),
        )
    view.revision, view.dirty = revision, False
    return task_id


def handle(config, task):

    store = Store.open(config)
    row = store.one("SELECT * FROM studio_tasks WHERE id=?", (task.ledger_id,))
    job_id = row["job_id"]
    attempt = task.execution.attempt
    # 只串行同一任务的提交；素材处理不持有全库 Studio 独占锁。
    attempt.retain(
        (store.library_id, "studio", job_id),
        lock_backend.resource(store, "studio-job:" + job_id, timeout=None),
    )
    snapshot(store, f"studio/{job_id}/")  # 租约与目录状态持续到终态事务
    attempt.terminals[(store.library_id, "studio", row["id"])] = terminal_writer(store, row, task)
    cfg = restore_config(row["config"], RuntimeConfig(config, store), store)
    options = json.loads(row["options"])
    if row["kind"] == "build":
        before = workspace.load(cfg, job_id)
        submitted = next(b for b in before["builds"] if b["id"] == row["build_id"])
        if submitted.get("plan_snapshot") is not None and workspace.revision(
            submitted["plan_snapshot"]
        ) != workspace.revision(workspace.plan(before, submitted["plan_id"])):
            raise ValueError("提交后的方案已变化，请重新预检并提交，旧构建不会调用模型")
        workspace.run_build(cfg, job_id, row["build_id"])
        w = workspace.load(cfg, job_id)
        result = next(b for b in w["builds"] if b["id"] == row["build_id"])
        # 构建失败是有记录的业务终态；提交失败版本与诊断，不自动重做外部调用。
        state = "complete" if result["status"] == "complete" else result["status"]
        unknown = store.one(
            "SELECT id FROM external_calls WHERE task_id=? AND state IN "
            "('dispatching','needs_review') LIMIT 1",
            (row["id"],),
        )
        if unknown:
            state = "needs_review"
            result["status"] = state
            workspace.persist(cfg, job_id, w)
    else:
        for entry in files.entries(store, f"studio/{job_id}/"):
            relative = entry["path"].removeprefix(f"studio/{job_id}/")
            if not relative.startswith(("versions/", "attempts/", "builds/", "migration-v2/")):
                files.hydrate(store, store.root / entry["path"])
        steps = options["steps"]
        for index, step in enumerate(steps):
            attempt.check()
            studio.begin_step(cfg, job_id, step)
            studio.set_execution(cfg, job_id, "running", step, steps[index + 1 :])

            with scope(store, [job_id, "legacy", step]):
                studio_steps.run_step(
                    cfg, job_id, step, mode=options["mode"], shot_ids=options.get("shot_ids"), started=True
                )
        result = studio.set_execution(cfg, job_id, "complete", None, [])
        state = "complete"

    # 结果与 manifest、队列和账本完成同一终态事务提交。
    def terminal(db):
        db.execute(
            "UPDATE studio_tasks SET result=? WHERE id=?",
            (dumps({"status": state, "result": result}), row["id"]),
        )
        if state != "complete":
            db.execute("UPDATE studio_tasks SET status=? WHERE id=?", (state, row["id"]))

    attempt.defer((store.library_id, "studio-result", row["id"]), terminal)


def terminal_writer(store, row, task):
    def finish(db, outcome, error):
        if outcome == "succeeded":
            return

        view = Snapshot(store, f"studio/{row['job_id']}/", db=db)
        key = view.prefix + ("workspace.json" if row["kind"] == "build" else "job.json")
        entry = view.entries.get(key)
        if entry is None:
            return
        with store.media.open_read(entry["object_key"]) as source:
            state = json.load(source)
        if row["kind"] == "build":
            build = next(b for b in state["builds"] if b["id"] == row["build_id"])
            status = "running" if outcome == "failed" and task.attempts < task.max_attempts else outcome
            build.update(status=status, error=error)
            if status != "running":
                build["finished_at"] = studio.now()
                for node in build["nodes"]:
                    if node["status"] in {"pending", "running"}:
                        node.update(status="blocked", error=error or "任务已取消")
        else:
            status = "running" if outcome == "failed" and task.attempts < task.max_attempts else outcome
            state["execution"].update(status=status, error=error)
            if status != "running":
                state["execution"].update(current_step=None, remaining_steps=[])
                for step in state["steps"].values():
                    if step["status"] == "running":
                        step.update(
                            status="cancelled" if outcome == "cancelled" else "failed",
                            error=error,
                            finished_at=studio.now(),
                        )
        view.put(key, json.dumps(state, ensure_ascii=False).encode())
        view.commit(db)

    return finish


def cancel(config, kind, job_id, key=None):

    store = Store.open(config)
    queue = PgTaskQueue(store.library_id)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT * FROM studio_tasks WHERE job_id=? AND kind=? "
            "AND (build_id=? OR build_id IS NULL) AND status IN ('queued','running')",
            (job_id, kind, key),
        ).fetchall()
        for row in rows:
            queued = db.execute(
                "SELECT id,status FROM task_queue WHERE library_id=? AND ledger_table='studio_tasks' "
                "AND ledger_id=? FOR UPDATE",
                (store.library_id, row["id"]),
            ).fetchone()
            if queued:
                queue.cancel(db, queued["id"])
                if queued["status"] == "pending":
                    terminal_writer(store, row, None)(db, "cancelled", "用户已取消任务")


def wait(config, task_id):

    store = Store.open(config)
    while True:
        row = store.one("SELECT status,result,error FROM studio_tasks WHERE id=?", (task_id,))
        if row["status"] not in {"queued", "running"}:
            if row["result"]:
                return json.loads(row["result"])
            raise studio.StudioError(row["error"] or "创作执行未完成，请查看任务账本")
        time.sleep(0.2)


def retry(config, job_id, task_id):
    store = Store.open(config)
    with publication(config, job_id):
        row = store.one("SELECT * FROM studio_tasks WHERE id=? AND job_id=?", (task_id, job_id))
        if row is None or row["status"] not in {
            "failed",
            "partial",
            "interrupted",
            "cancelled",
            "needs_review",
        }:
            raise ValueError("只有失败、中断、取消或已核对的任务可以重试")
        if store.one(
            "SELECT id FROM external_calls WHERE task_id=? AND state IN ('dispatching','needs_review') LIMIT "
            "1",
            (task_id,),
        ):
            raise ValueError("存在未知外部调用结果，请先核对所有调用回执")
        view = snapshot(store, f"studio/{job_id}/")
        if row["kind"] == "build":
            w = workspace.load(config, job_id)
            build = next(b for b in w["builds"] if b["id"] == row["build_id"])
            build.update(status="running", error=None)
            workspace.persist(config, job_id, w)
        with ledger_transaction(store, "studio_tasks", task_id, restart=True) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT id FROM studio_tasks WHERE job_id=? AND id<>? AND status IN ('queued','running')",
                (job_id, task_id),
            ).fetchone():
                raise ValueError("此创作任务已有排队或运行中的执行")
            revision = view.commit(db)
            db.execute(
                "UPDATE studio_tasks SET status='queued',error=NULL,result=NULL,queue_attempt_id=NULL,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )
        view.revision, view.dirty = revision, False
    return {"task_id": task_id, "status": "queued"}
