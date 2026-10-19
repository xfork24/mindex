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
