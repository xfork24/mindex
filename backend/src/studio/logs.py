"""Append-only, readable execution events; raw diagnostics stay in version directories."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from shared.db.fencing import current_attempt
from shared.i18n import add_message_metadata, message_metadata
from shared.storage import Store, dumps
from studio import studio
from studio.artifacts import safe_path

active_log: ContextVar[ExecutionLog | None] = ContextVar("studio_execution_log", default=None)


def log_path(config, job_id, build_id):
    return safe_path(studio.job_dir(config, job_id), f"builds/{build_id}/events.jsonl")


def read_events(config, job_id, build_id, after=0, limit=500):
    if config.queue.backend == "postgres":
        rows = Store.open(config).rows(
            "SELECT id,data FROM studio_events WHERE job_id=? AND build_id=? AND id>? ORDER BY id LIMIT ?",
            (job_id, build_id, after, limit + 1),
        )
        items = [add_message_metadata({**json.loads(row["data"]), "seq": row["id"]}) for row in rows[:limit]]
        return {
            "items": items,
            "next_seq": items[-1]["seq"] if items else after,
            "has_more": len(rows) > limit,
            "legacy": False,
        }
    path = log_path(config, job_id, build_id)
    items = []
    more = False
    if path.is_file():
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # A concurrent append may not have finished its final line yet.
                if event["seq"] <= after:
                    continue
                if len(items) == limit:
                    more = True
                    break
                items.append(add_message_metadata(event))
    return {
        "items": items,
        "next_seq": items[-1]["seq"] if items else after,
        "has_more": more,
        "legacy": not path.is_file(),
    }


class ExecutionLog:
    def __init__(self, config, job_id, build_id):
        self.config, self.job_id, self.build_id = config, job_id, build_id
        self.path = log_path(config, job_id, build_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self.seq = max(self.seq, json.loads(line)["seq"])
                except (ValueError, KeyError):
                    continue
        self.stage = self.shot_id = None

    def emit(self, message, level="info"):
        self.seq += 1
        event = {
            "seq": self.seq,
            "at": studio.now(),
            "stage": self.stage,
            "shot_id": self.shot_id,
            "level": level,
            "message": str(message)[:2000],
            **message_metadata(str(message)[:2000]),
        }
        if self.config.queue.backend == "postgres":
            store = Store.open(self.config)
            attempt = current_attempt()
            with store.connect() as db:
                cursor = db.execute(
                    "INSERT INTO studio_events(job_id,build_id,attempt_id,data) VALUES(?,?,?,?) RETURNING id",
                    (self.job_id, self.build_id, attempt.task.attempt_id if attempt else None, dumps(event)),
                )
                self.seq = cursor.fetchone()[0]
                if attempt and attempt.task.kind == "studio":
                    db.execute(
                        "UPDATE studio_tasks SET progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (
                            dumps(
                                {
                                    "latest_log": event["message"],
                                    "latest_seq": self.seq,
                                    "stage": self.stage,
                                    "shot_id": self.shot_id,
                                }
                            ),
                            attempt.task.ledger_id,
                        ),
                    )
            return
        with self.path.open("a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def diagnostic_event(kind, value):
    log = active_log.get()
    if log is None:
        return
    if kind == "model-request":
        path = urlsplit(value.get("url", "")).path
        log.emit(f"调用模型服务：{value.get('method', '')} {path}，第 {value.get('attempt', 0) + 1} 次尝试")
    elif kind == "query-vector-cache":
        log.emit("复用查询向量缓存" if value.get("hit") else "生成查询向量")
    elif kind in ("model-response", "tts-response"):
        log.emit(f"模型服务响应：HTTP {value.get('status', '未知')}")
    elif kind == "split-validation":
        log.emit("分镜校验未通过，正在修正", "warning")
    elif kind == "material-digest-error":
        log.emit("素材摘要生成失败，已按无摘要继续", "warning")
    elif kind == "command":
        executable = value.get("args", [""])[0]
        # 首参数可能是 MINDEX_FFMPEG 配置的绝对路径：按文件名识别。
        is_ffmpeg = Path(str(executable)).name == "ffmpeg"
        log.emit("正在处理音视频（ffmpeg）" if is_ffmpeg else "正在检查媒体信息")
    elif kind == "command-result":
        log.emit("媒体处理完成")
    elif kind == "command-error":
        log.emit("媒体处理失败，详细诊断已保存", "error")
