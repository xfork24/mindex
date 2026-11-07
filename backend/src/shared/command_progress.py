"""Timestamped CLI progress and a local-process heartbeat for long offline commands."""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

STAGES = {
    "starting": "初始化",
    "lock_wait": "获取处理锁",
    "analyze": "分析 moment/shot",
    "scene_grouping": "组织连续场景",
    "scenes": "分析 scene",
    "index_wait": "等待索引写锁",
    "index_prepare": "准备索引/校验模型",
    "embedding": "更新嵌入",
    "index_check": "校验索引",
    "publish": "发布索引",
    "published": "索引已发布",
    "indexed": "索引已就绪",
}


def duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}天 " if days else "") + f"{hours:02}:{minutes:02}:{seconds:02}"


class CommandProgress:
    """One command, one worker thread. Heartbeats never claim remote inference is healthy."""

    def __init__(self, name, *, interval=30):
        self.name = name
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = {"stage": "starting"}
        self._latest = "尚无工作日志"
        self._started = time.monotonic()
        self._changed = self._started

    def __enter__(self):
        self._started = self._changed = time.monotonic()
        owner = threading.get_ident()
        reporter = self

        class Activity(logging.Handler):
            def emit(self, record):
                if record.thread != owner or record.levelno < logging.INFO:
                    return
                if not record.name.startswith("mindex.") or record.name == __name__:
                    return
                lines = record.getMessage().splitlines()
                with reporter._lock:
                    reporter._latest = next((line for line in lines if line.strip()), "")[:400]
                    reporter._changed = time.monotonic()

        self._handler = Activity()
        logging.getLogger().addHandler(self._handler)
        self._thread = threading.Thread(target=self._heartbeat, name="command-progress", daemon=True)
        self._thread.start()
        log.info("%s 开始；每 %g 秒输出进程心跳，已完成的检查点可在重试时复用", self.name, self.interval)
        return self

    def update(self, value):
        with self._lock:
            if self._state == value:
                return
            self._state = dict(value)
            self._changed = time.monotonic()
            text = self._describe()
        log.info("%s · %s", self.name, text)

    def _describe(self):
        state = self._state
        fields = [STAGES.get(state.get("stage"), state.get("stage", "处理中"))]
        if state.get("source_name"):
            fields.append(
                f"素材 {state.get('source_number', '?')}/{state.get('source_total', '?')} "
                f"{state['source_name']} [{state.get('source_id', '')}]"
            )
        if "completed" in state and "total" in state:
            fields.append(f"{state['completed']}/{state['total']} {state.get('unit', '')}")
        if state.get("kind"):
            fields.append(
                f"{state['kind']} {state.get('start', 0):.2f}–{state.get('end', 0):.2f}s "
                f"[{state.get('segment_id', '')}]"
            )
        if state.get("skipped"):
            fields.append(f"已跳过 {state['skipped']}")
        if state.get("index_id"):
            fields.append(f"索引 {state['index_id']}")
        if state.get("message"):
            fields.append(str(state["message"]))
        return " · ".join(fields)

    def heartbeat(self):
        with self._lock:
            now = time.monotonic()
            text, latest = self._describe(), self._latest
            elapsed, idle = duration(now - self._started), duration(now - self._changed)
        log.info(
            "%s 进程心跳 · 总用时 %s · %s · 距最近工作日志/进度 %s · 最近：%s",
            self.name,
            elapsed,
            text,
            idle,
            latest,
        )

    def _heartbeat(self):
        while not self._stop.wait(self.interval):
            self.heartbeat()

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        self._thread.join(timeout=1)
        logging.getLogger().removeHandler(self._handler)
        elapsed = duration(time.monotonic() - self._started)
        if exc_type is None:
            log.info("%s 完成 · 总用时 %s", self.name, elapsed)
        else:
            with self._lock:
                current = self._describe()
            log.error(
                "%s %s · 总用时 %s · %s · %s；已写入的检查点保留，可重试继续",
                self.name,
                "已中断" if issubclass(exc_type, KeyboardInterrupt) else "失败",
                elapsed,
                current,
                exc,
            )
        return False
