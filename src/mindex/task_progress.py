"""持久化任务当前进度，同时保留处理阶段，供自动检索更新时并列显示。"""

import json

from .storage import dumps


def save_task_progress(store, table, task_id, value):
    if table not in {"source_tasks", "dialogue_tasks", "person_tasks"}:
        raise ValueError("无效的任务表")
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
    if table not in {'source_tasks', 'dialogue_tasks', 'person_tasks'}:
        raise ValueError('无效的任务表')
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
