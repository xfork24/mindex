"""切换模型后仍续跑已完成片段；强制重做只由 CLI --force 进入。"""
from contextlib import nullcontext

import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder
from typer.testing import CliRunner

from mindex import cli
from mindex.processing.pipeline import Pipeline


class CountingAnalyzer(FixtureAnalyzer):
    def __init__(self, config):
        super().__init__(FixtureEmbedder(config))
        self.group_calls = 0

    def group_scenes(self, units):
        self.group_calls += 1
        return super().group_scenes(units)


@pytest.mark.slow
def test_model_switch_reuses_completed_and_force_reanalyzes(config, video_file):
    first = CountingAnalyzer(config)
    pipeline = Pipeline(config, analyzer=first)
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    original_scene_ids = {s["id"] for s in pipeline.store.segments(sid, "scene")}
    original_profile = pipeline.annotation_profile(sid)
    original = {s['id']: pipeline.store.json_value('annotations', 'segment_id', s['id'], original_profile)
                for s in pipeline.store.segments(sid) if s['kind'] != 'scene'}
    changed = config.model_copy(deep=True)
    changed.analyzer.model = 'another-model'
    second = CountingAnalyzer(changed)
    resumed = Pipeline(changed, analyzer=second)
    resumed.analyze(sid)
    assert second.calls == second.group_calls == 0
    assert {s["id"] for s in resumed.store.segments(sid, "scene")} == original_scene_ids
    profile = resumed.annotation_profile(sid)
    for segment_id, previous in original.items():
        saved = resumed.store.json_value('annotations', 'segment_id', segment_id, profile)
        assert saved['annotation'] == previous['annotation']
        assert saved['model_identity'] == previous['model_identity']
        assert saved['content_version'] == previous['content_version']
        assert saved['reused_from_profile'] == original_profile
    assert resumed.snapshots(sid)
    # 单片段模型验收不应把跨模型续跑的复制记录当成该模型的实际输出。
    resumed._annotate(resumed.store.segments(sid)[0])
    assert second.calls == 1
    resumed.analyze(sid, force=True)
    assert second.calls > 0 and second.group_calls > 0
    for segment_id in original:
        saved = resumed.store.json_value('annotations', 'segment_id', segment_id, profile)
        assert saved['model_identity']['model'] == 'another-model'
        assert 'reused_from_profile' not in saved
    count = second.calls
    resumed.analyze(sid)
    assert second.calls == count


@pytest.mark.slow
def test_model_switch_only_analyzes_unfinished_segments(config, video_file):
    first = CountingAnalyzer(config)
    pipeline = Pipeline(config, analyzer=first)
    sid = pipeline.prepare(video_file)
    segment = pipeline.store.segments(sid)[0]
    pipeline._annotate(segment)
    changed = config.model_copy(deep=True)
    changed.analyzer.model = 'resume-model'
    second = CountingAnalyzer(changed)
    resumed = Pipeline(changed, analyzer=second)
    resumed.analyze(sid)
    profile = resumed.annotation_profile(sid)
    saved = resumed.store.json_value('annotations', 'segment_id', segment['id'], profile)
    assert saved['model_identity']['model'] == config.analyzer.model
    assert second.calls > 0
    assert resumed.snapshots(sid)


@pytest.mark.parametrize('force', [False, True])
def test_cli_analysis_force_is_explicit(config, tmp_path, monkeypatch, force):
    calls = []

    class FakePipeline:
        def __init__(self, config):
            pass

        def lock(self, *args):
            return nullcontext()

        def analyze(self, source, progress=None, *, force=False):
            calls.append((source, force))

    monkeypatch.setattr('mindex.processing.pipeline.Pipeline', FakePipeline)
    path = tmp_path / 'config.yaml'
    path.write_text(f'data_dir: {config.data_dir}\n')
    command = ['--config', str(path), 'analyze', '--source', 'source'] + (['--force'] if force else [])
    result = CliRunner().invoke(cli.app, command)
    assert result.exit_code == 0, result.output
    assert calls == [('source', force)]


@pytest.mark.slow
def test_ui_regeneration_always_resumes_after_model_switch(config, video_file, monkeypatch):
    from test_source_work import finished

    from mindex.admin.source_tasks import SourceTasks, WorkOptions
    from mindex.settings.runtime_settings import RuntimeConfig

    pipeline = Pipeline(config, analyzer=CountingAnalyzer(config))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    changed = config.model_copy(deep=True)
    changed.analyzer.model = 'ui-new-model'

    def unexpected(_):
        pytest.fail('UI 重试不应初始化模型或重新分析已完成片段')

    monkeypatch.setattr('mindex.processing.pipeline.make_analyzer', unexpected)
    manager = SourceTasks(RuntimeConfig(changed, pipeline.store), pipeline.store)
    task = finished(manager, manager.start(sid, 'analysis', WorkOptions()))
    assert task['status'] == 'complete', task
    with pytest.raises(ValueError):
        WorkOptions.model_validate({'force': True})
