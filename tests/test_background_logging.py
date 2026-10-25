"""后台线程顶层失败必须写出 traceback，而不只是把 str(exc) 存进任务表。"""

from __future__ import annotations

import logging
import time

import pytest

from mindex.admin.dialogue_tasks import DialogueOptions, DialogueTasks
from mindex.admin.source_tasks import SourceTasks, WorkOptions
from mindex.processing.pipeline import Pipeline
from mindex.settings.runtime_settings import RuntimeConfig


def finished(manager, task):
    for _ in range(1000):
        value = manager.get(task["id"])
        if value["status"] in {"complete", "failed", "interrupted"} and task["id"] not in manager.threads:
            return value
        time.sleep(0.01)
    pytest.fail("后台任务未完成")


def traceback_record(caplog, logger, text):
    return next(
        (r for r in caplog.records if r.name == logger and r.exc_info and text in str(r.exc_info[1])),
        None,
    )


def test_failing_source_task_logs_traceback(config, video_file, monkeypatch, caplog):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)

    def analyze(self, source_id, progress):
        raise RuntimeError("模拟分析崩溃")

    monkeypatch.setattr(Pipeline, "analyze", analyze)
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    with caplog.at_level(logging.ERROR, logger="mindex.admin.source_tasks"):
        task = finished(manager, manager.start(sid, "analysis", WorkOptions(publish=False)))
    assert task["status"] == "failed" and "模拟分析崩溃" in task["error"]
    record = traceback_record(caplog, "mindex.admin.source_tasks", "模拟分析崩溃")
    assert record is not None and record.levelno == logging.ERROR
    assert record.exc_info[2] is not None  # 带 traceback


def test_failing_dialogue_task_logs_traceback(config, video_file, monkeypatch, caplog):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)

    def explode(*args, **kwargs):
        raise RuntimeError("模拟对白崩溃")

    monkeypatch.setattr("mindex.admin.dialogue_tasks.execute_dialogue", explode)
    manager = DialogueTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    with caplog.at_level(logging.ERROR, logger="mindex.admin.dialogue_tasks"):
        task = finished(manager, manager.start(sid, DialogueOptions(publish=False)))
    assert task["status"] == "failed" and "模拟对白崩溃" in task["error"]
    record = traceback_record(caplog, "mindex.admin.dialogue_tasks", "模拟对白崩溃")
    assert record is not None and record.exc_info[2] is not None
