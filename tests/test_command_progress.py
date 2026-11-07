"""Long offline CLI commands expose progress without changing output or checkpoint semantics."""

import logging
import threading
from contextlib import nullcontext

import pytest
from typer.testing import CliRunner

import cli
from shared.command_progress import CommandProgress, duration


def test_heartbeat_during_blocked_work_and_cleanup(caplog):
    pulse = threading.Event()

    class Heartbeat(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("测试 进程心跳"):
                pulse.set()

    root = logging.getLogger()
    handler = Heartbeat()
    root.addHandler(handler)
    try:
        with caplog.at_level(logging.INFO), CommandProgress("测试", interval=0.01) as reporter:
            reporter.update(
                {
                    "stage": "analyze",
                    "completed": 12,
                    "total": 100,
                    "source_name": "素材A",
                    "source_number": 2,
                    "source_total": 5,
                    "source_id": "source-a",
                    "kind": "moment",
                    "start": 3.0,
                    "end": 7.0,
                    "segment_id": "segment-a",
                }
            )
            logging.getLogger("models.qwen").info("LLM 请求开始 test-model")
            assert pulse.wait(2), "Heartbeat must continue while the work thread waits"
        assert not reporter._thread.is_alive()
        text = caplog.text
        assert "12/100" in text and "素材 2/5" in text and "3.00–7.00s" in text
        assert "LLM 请求开始 test-model" in text and "距最近工作日志/进度" in text
        assert "测试 完成" in text
    finally:
        root.removeHandler(handler)


def test_failure_preserves_exception_and_stops_heartbeat(caplog):
    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="timeout"):
        with CommandProgress("索引更新", interval=0.01) as reporter:
            reporter.update({"stage": "embedding", "index_id": "index-a", "completed": 2, "total": 9})
            raise RuntimeError("timeout")
    assert not reporter._thread.is_alive()
    assert "失败" in caplog.text and "index-a" in caplog.text and "检查点保留" in caplog.text


def test_duration_handles_multiday_runs():
    assert duration(90061) == "1天 01:01:01"


@pytest.mark.parametrize("command,force", [("analyze", False), ("analyze", True), ("index", False)])
def test_cli_wires_progress_and_keeps_index_stdout(config, monkeypatch, caplog, command, force):
    from processing import indexing, pipeline

    calls = []

    class Pipeline:
        def __init__(self, config):
            pass

        def lock(self, source, work):
            return nullcontext()

        def analyze(self, source, *, progress, force=False):
            calls.append(("analyze", source, force))
            progress({"stage": "analyze", "completed": 1, "total": 1})

    class IndexBuilder:
        def __init__(self, config):
            pass

        def build(self, *, progress, **kwargs):
            calls.append(("index", kwargs))
            progress({"stage": "embedding", "completed": 2, "total": 2, "index_id": "index-fixture"})
            progress({"stage": "published", "completed": 2, "total": 2, "index_id": "index-fixture"})
            return "index-fixture"

    monkeypatch.setattr(cli, "load_config", lambda *_: config)
    monkeypatch.setattr("settings.runtime_settings.apply_runtime_overrides", lambda c: c)
    monkeypatch.setattr(pipeline, "Pipeline", Pipeline)
    monkeypatch.setattr(indexing, "IndexBuilder", IndexBuilder)
    args = [command] if command == "analyze" else ["index", "--channel", "analysis", "--activate"]
    if force:
        args.append("--force")
    with caplog.at_level(logging.INFO):
        result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert calls[0][0] == command
    assert "开始" in caplog.text and "完成" in caplog.text
    if command == "index":
        assert result.stdout.strip() == "index-fixture"
        assert calls[0][1]["channels"] == ["analysis"] and calls[0][1]["activate"]
    else:
        assert calls == [("analyze", None, force)]


def test_interrupt_is_not_reported_as_success(caplog):
    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        with CommandProgress("分析", interval=0.01) as reporter:
            reporter.update({"stage": "scene_grouping", "completed": 32, "total": 100})
            raise KeyboardInterrupt()
    assert "已中断" in caplog.text and "组织连续场景" in caplog.text
    assert "分析 完成" not in caplog.text and not reporter._thread.is_alive()


@pytest.mark.slow
def test_real_cli_analysis_reports_checkpoints_and_resume(config, video_file, monkeypatch, caplog):
    from conftest import FixtureAnalyzer, FixtureEmbedder
    from processing import pipeline

    analyzer = FixtureAnalyzer(FixtureEmbedder(config))
    pipeline.Pipeline(config).prepare(video_file)
    monkeypatch.setattr(pipeline, "make_analyzer", lambda *_: analyzer)
    monkeypatch.setattr(cli, "load_config", lambda *_: config)
    monkeypatch.setattr("settings.runtime_settings.apply_runtime_overrides", lambda c: c)
    runner = CliRunner()
    with caplog.at_level(logging.INFO):
        first = runner.invoke(cli.app, ["analyze"])
    assert first.exit_code == 0, first.output
    assert "素材 1/1" in caplog.text and "分析检查点已保存" in caplog.text
    assert "场景分组开始" in caplog.text and "分析场景" in caplog.text and "素材完成 1/1" in caplog.text
    count = analyzer.calls
    caplog.clear()
    with caplog.at_level(logging.INFO):
        resumed = runner.invoke(cli.app, ["analyze"])
    assert resumed.exit_code == 0, resumed.output
    assert analyzer.calls == count
    assert "分析检查点命中" in caplog.text and "场景计划检查点命中" in caplog.text
