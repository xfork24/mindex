"""Append-only, readable execution events; raw diagnostics stay in version directories."""

from __future__ import annotations

import json
from contextvars import ContextVar

from . import studio
from .i18n import add_message_metadata, message_metadata
from .studio_artifacts import safe_path

active_log: ContextVar[ExecutionLog | None] = ContextVar("studio_execution_log", default=None)
LABELS = {
    "split": "拆分镜",
    "recall": "召回",
    "tts": "配音",
    "select": "选片与对齐",
    "render": "分镜渲染",
    "compose": "成片合成",
}


def log_path(config, job_id, build_id):
    return safe_path(studio.job_dir(config, job_id), f"builds/{build_id}/events.jsonl")


def read_events(config, job_id, build_id, after=0, limit=500):
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
        self.path = log_path(config, job_id, build_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.seq = 0
        if self.path.is_file():
            for line in self.path.read_text().splitlines():
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
        with self.path.open("a") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def diagnostic_event(kind, value):
    log = active_log.get()
    if log is None:
        return
    if kind == "model-request":
        log.emit(f"调用模型服务，第 {value.get('attempt', 0) + 1} 次尝试")
    elif kind in ("model-response", "tts-response"):
        log.emit(f"模型服务响应：HTTP {value.get('status', '未知')}")
    elif kind == "split-validation":
        log.emit("分镜校验未通过，正在修正", "warning")
    elif kind == "command":
        executable = value.get("args", [""])[0]
        log.emit("正在处理音视频（ffmpeg）" if str(executable).endswith("ffmpeg") else "正在检查媒体信息")
    elif kind == "command-result":
        log.emit("媒体处理完成")
    elif kind == "command-error":
        log.emit("媒体处理失败，详细诊断已保存", "error")
