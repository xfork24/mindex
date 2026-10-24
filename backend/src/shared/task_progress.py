"""持久化任务当前进度，同时保留处理阶段，供自动检索更新时并列显示。"""

import json

from mindex.shared.storage import dumps
from mindex.shared.task_cancel import CANCEL_MESSAGE, TaskCancelled, registry_for

# 索引已激活（或已就绪）后的进度事件；此后取消已无回退意义。
POST_COMMIT_STAGES = frozenset({"published", "indexed"})
TASK_TABLES = frozenset({"source_tasks", "dialogue_tasks", "person_tasks"})
CANCELLABLE = frozenset({"queued", "running"})
# 可重试的任务终态；cancelled 与失败、中断一样复用已有检查点。
RETRYABLE = frozenset({"failed", "interrupted", "cancelled"})


def _table(table):
    if table not in TASK_TABLES:
        raise ValueError("无效的任务表")
    return table


def begin_task(store, table, task_id):
    """queued→running；排队期间已被取消（状态或标记先到）则抛 TaskCancelled，不开始工作。"""
    _table(table)
    store.execute(
        f"UPDATE {table} SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='queued'",
        (task_id,),
    )
    row = store.one(f"SELECT status FROM {table} WHERE id=?", (task_id,))
    if row and row["status"] == "cancelled":
        raise TaskCancelled()
    registry_for(store.root).check(task_id)


def mark_cancelled(store, table, task_id):
    _table(table)
    store.execute(
        f"UPDATE {table} SET status='cancelled',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (CANCEL_MESSAGE, task_id),
    )


def request_task_cancel(store, table, task_id):
    """返回取消前的状态（None 表示任务不存在）；仅 queued/running 写取消标记。

    先写标记再改 queued→cancelled：刚启动的线程要么看到 cancelled 状态，要么在
    begin_task 的检查点看到标记，不会把已取消的任务重新置为 running。
    """
    _table(table)
    row = store.one(f"SELECT status FROM {table} WHERE id=?", (task_id,))
    if not row:
        return None
    if row["status"] in CANCELLABLE:
        registry_for(store.root).request(task_id)
        store.execute(
            f"UPDATE {table} SET status='cancelled',error=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='queued'",
            (CANCEL_MESSAGE, task_id),
        )
    return row["status"]


def save_task_progress(store, table, task_id, value):
    _table(table)
    row = store.one(f"SELECT progress FROM {table} WHERE id=?", (task_id,))
    old = json.loads(row["progress"]) if row else {}
    phase = value.get("phase", "process")
    current = {**value, "phase": phase}
    if phase == "publish":
        processing = old.get("processing_progress") or (old if old.get("phase") != "publish" else None)
        if processing:
            current["processing_progress"] = processing
    store.execute(
        f"UPDATE {table} SET progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (dumps(current), task_id),
    )
    # 进度上报即取消检查点；激活之后的收尾阶段不再中断，避免“已激活却标记取消”。
    if value.get("stage") not in POST_COMMIT_STAGES:
        registry_for(store.root).check(task_id)


def task_logs(table):
    """Keep only the latest application log from this task's worker thread."""
    import logging
    import threading
    from functools import wraps

    def decorate(run):
        @wraps(run)
        def wrapped(manager, task_id, *args, **kwargs):
            worker = threading.get_ident()

            class LatestLog(logging.Handler):
                def emit(self, record):
                    if record.thread != worker or not record.name.startswith("mindex."):
                        return
                    try:
                        row = manager.store.one(f"SELECT progress FROM {table} WHERE id=?", (task_id,))
                        if not row:
                            return
                        progress = json.loads(row["progress"] or "{}")
                        lines = record.getMessage().splitlines()
                        progress["latest_log"] = next((line.strip() for line in reversed(lines) if line.strip()), "")[:2000]
                        manager.store.execute(
                            f"UPDATE {table} SET progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                            (dumps(progress), task_id),
                        )
                    except Exception:  # noqa: BLE001 — logging must never fail a processing task.
                        return

            handler = LatestLog()
            logger = logging.getLogger()
            logger.addHandler(handler)
            try:
                return run(manager, task_id, *args, **kwargs)
            finally:
                logger.removeHandler(handler)

        return wrapped

    return decorate


def reset_task_attempt(store, table, task_id):
    """Clear attempt diagnostics while keeping completed processing checkpoints."""
    _table(table)
    # 上一次尝试遗留的取消标记会立即中断新的执行。
    registry_for(store.root).clear(task_id)
    row = store.one(f'SELECT progress,result FROM {table} WHERE id=?', (task_id,))
    old = json.loads(row['progress'] or '{}') if row else {}
    processing = old.get('processing_progress') if row and row['result'] else None
    progress = {}
    if processing:
        clean = {key: value for key, value in processing.items()
                 if key not in {'latest_log', 'message', 'message_key', 'message_params', 'error', 'error_message'}}
        progress = {'phase': 'publish', 'processing_progress': clean}
    store.execute(
        f"UPDATE {table} SET status='queued',error=NULL,progress=?,publication='{{}}',updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (dumps(progress), task_id),
    )
