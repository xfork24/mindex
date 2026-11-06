"""素材操作按实际输入复用；任务、配置和片段 ID 变化不触发模型重算。"""
import json

import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder
from typer.testing import CliRunner

from mindex import cli
from mindex.processing.dialogue import DialogueProcessor
from mindex.processing.indexing import IndexBuilder, VectorIndex, check_index
from mindex.processing.pipeline import Pipeline
from mindex.shared.types import Annotation


@pytest.mark.slow
def test_repeated_preparation_does_not_regenerate_valid_media(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)

    def unexpected(*args, **kwargs):
        pytest.fail('已有媒体与缩略图不应重新生成')

    monkeypatch.setattr('mindex.processing.pipeline.cut', unexpected)
    monkeypatch.setattr('mindex.processing.pipeline.thumbnail', unexpected)
    pipeline.detect_source(sid)
    pipeline.split(sid)


@pytest.mark.slow
def test_scene_publish_ignores_historical_generations_and_reuses_interval_vectors(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    source = pipeline.store.one('SELECT * FROM sources WHERE id=?', (sid,))
    for a, b in [(0, 5), (5, 10), (0, 10)]:
        pipeline._segment(source, 'scene', a, b, 'old', None, 'fixture')
    embedder = FixtureEmbedder(config)
    builder = IndexBuilder(config, embedder)
    old = builder.build(source_id=sid, kinds=['scene'], channels=['visual'], activate=True)
    assert len(pipeline.store.snapshots(old)) == 3
    current = [pipeline._segment(source, 'scene', a, b, 'new', None, 'fixture') for a, b in [(0, 5), (5, 10)]]
    pipeline.store.put_json('scene_plans', 'source_id', sid, 'new', [[0, 5], [5, 10]])

    def unexpected(*args, **kwargs):
        pytest.fail('相同原片区间应复用已有向量，不调用模型')

    monkeypatch.setattr(embedder, 'video', unexpected)
    new = builder.build(source_id=sid, kinds=['scene'], channels=['visual'], activate=True)
    assert {s['id'] for s in pipeline.store.snapshots(new)} == {s['id'] for s in current}
    assert len(pipeline.store.snapshots(old)) == 3
    assert not check_index(pipeline.store, new)
    monkeypatch.setattr(VectorIndex, 'upsert_many', unexpected)
    assert builder.build(source_id=sid, kinds=['scene'], channels=['visual'], activate=True) == new


@pytest.mark.slow
def test_model_alias_change_does_not_change_published_analysis(config, video_file):
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(FixtureEmbedder(config)))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    builder = IndexBuilder(config, FixtureEmbedder(config))
    old = builder.build(source_id=sid, channels=['analysis'], activate=True)
    config.analyzer.model = 'model-alias-changed'
    pipeline.analyze(sid)
    assert builder.build(source_id=sid, channels=['analysis'], activate=True) == old


@pytest.mark.slow
@pytest.mark.parametrize('entry', ['dialogue', 'transcribe'])
def test_completed_asr_survives_model_change_and_force_recomputes(config, video_file, monkeypatch, entry):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    info = json.loads(pipeline.store.one('SELECT media FROM sources WHERE id=?', (sid,))['media'])
    pipeline.store.execute('UPDATE sources SET media=? WHERE id=?', (json.dumps({**info, 'audio': True}), sid))
    config.asr.backend = 'minimax'

    class ASR:
        calls = 0

        def raw(self, path, **kwargs):
            self.calls += 1
            return {'raw_response': {'text': '你好。', 'segments': [
                {'text': '你好。', 'start': 0.1, 'end': 0.5, 'speaker': 0}]}}

    transcriber = ASR()
    monkeypatch.setattr('mindex.shared.shot_media.ShotMedia.audio', lambda self, sid, path, *args: path.write_bytes(b'fixture'))
    processor = DialogueProcessor(config, transcriber)
    original = processor.process(sid, method='asr')['sources'][0]
    config.asr.model = 'another-model'
    if entry == 'dialogue':
        reused = processor.process(sid, method='asr')['sources'][0]
        assert reused['model_identity'] == original['model_identity'] and reused['reused']
        assert transcriber.calls == 1
        processor.process(sid, method='asr', force=True)
    else:
        pipeline.transcribe(sid)
        assert transcriber.calls == 1
        monkeypatch.setattr('mindex.processing.dialogue.make_transcriber', lambda _: transcriber)
        pipeline.transcribe(sid, force=True)
    assert transcriber.calls == 2


@pytest.mark.parametrize('command', ['transcribe', 'enrich-dialogue'])
@pytest.mark.parametrize('force', [False, True])
def test_cli_asr_force_is_explicit(config, monkeypatch, command, force):
    from contextlib import nullcontext

    calls = []

    class FakePipeline:
        def __init__(self, config):
            pass

        def lock(self, *args):
            return nullcontext()

        def transcribe(self, source, *, force=False):
            calls.append((source, force))

    def execute(config, source, *, force=False, **kwargs):
        calls.append((source, force))
        return {}

    monkeypatch.setattr('mindex.processing.pipeline.Pipeline', FakePipeline)
    monkeypatch.setattr('mindex.admin.dialogue_tasks.execute_dialogue', execute)
    monkeypatch.setattr(cli, 'load_config', lambda *_: config)
    monkeypatch.setattr('mindex.settings.runtime_settings.apply_runtime_overrides', lambda config: config)
    args = ['transcribe', '--source', 'source'] if command == 'transcribe' else ['enrich-dialogue', 'source', '--no-publish']
    result = CliRunner().invoke(cli.app, args + (['--force'] if force else []))
    assert result.exit_code == 0, result.exception
    assert calls == [('source', force)]


def test_ui_options_reject_force(config):
    """force 只属于显式 CLI 重算：API/页面入口拒绝，人物选项仍在模型层拒绝。"""
    from fastapi.testclient import TestClient

    from mindex.api import create_app
    from mindex.persons.person_processing import PersonOptions

    with pytest.raises(ValueError):
        PersonOptions.model_validate({'force': True})
    with TestClient(create_app(config)) as client:
        for path in ('/api/admin/sources/source/shots', '/api/admin/sources/source/analysis',
                     '/api/admin/sources/source/dialogue'):
            response = client.post(path, json={'force': True})
            assert response.status_code == 422, (path, response.text)


def test_analysis_provenance_does_not_change_content_version(config):
    from mindex.processing.channel_index import snapshots_for
    from mindex.shared.storage import Store

    store = Store(config.data_dir)
    store.execute('INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)',
                  ('source', 'source', 'source.mp4', '{"duration":1}', 'layout'))
    store.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)',
                  ('shot', 'source', 'shot', 0, 1, 'source.mp4', 'source.jpg', 'layout', None, 'fixture'))
    annotation = {'annotation': Annotation(description='树林 / Woodland').model_dump()}
    store.put_json('annotations', 'segment_id', 'shot', 'old', annotation)
    before = snapshots_for(IndexBuilder(config), 'source', ['shot'])
    store.put_json('annotations', 'segment_id', 'shot', 'new', {**annotation, 'reused_from_profile': 'old'})
    after = snapshots_for(IndexBuilder(config), 'source', ['shot'])
    assert after == before


@pytest.mark.slow
def test_index_check_validates_subtitle_dependency_once_per_source(config, video_file, monkeypatch):
    from mindex.processing import subtitle_archive

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    version = IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, channels=['visual'])
    original = subtitle_archive.load_archive
    checked = []

    def load(store, source_id):
        checked.append(source_id)
        return original(store, source_id)

    monkeypatch.setattr(subtitle_archive, 'load_archive', load)
    assert check_index(pipeline.store, version) == []
    assert checked == [sid]
    (pipeline.store.root / pipeline.store.segments(sid, 'shot')[0]['path']).unlink()
    assert check_index(pipeline.store, version)


@pytest.mark.slow
def test_index_check_reuses_probe_cache_but_rejects_changed_media(config, video_file, monkeypatch):
    from mindex.shared import shot_media

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    version = IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, channels=['visual'])
    original = shot_media.probe
    checked = []

    def probe(path):
        checked.append(path)
        return original(path)

    monkeypatch.setattr(shot_media, 'probe', probe)
    assert IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, channels=['visual']) == version
    assert check_index(pipeline.store, version) == []
    assert checked == [], '已有有效探测结果，不应重复启动 ffprobe'
    shot = pipeline.store.root / pipeline.store.segments(sid, 'shot')[0]['path']
    shot.write_bytes(b'truncated')
    assert check_index(pipeline.store, version), '媒体变化必须失效缓存并重新校验'
    assert checked and set(checked) == {shot}


@pytest.mark.slow
def test_cached_publish_can_resume_after_committed_batch_without_media_work(config, video_file, monkeypatch):
    from mindex.processing import channel_index

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    builder = IndexBuilder(config, FixtureEmbedder(config))
    original = builder.build(source_id=sid, kinds=['moment'], channels=['visual'], activate=True)
    source = pipeline.store.one('SELECT * FROM sources WHERE id=?', (sid,))
    template = pipeline.store.segments(sid, 'moment')[0]
    for number in range(10):
        pipeline._segment(source, 'scene', template['start'], template['end'],
                          f'alias-{number}', None, 'fixture')

    def unexpected(*args, **kwargs):
        pytest.fail('已有相同区间向量，不应切片、探测媒体或请求模型')

    monkeypatch.setattr(builder.embedder, 'video', unexpected)
    monkeypatch.setattr('mindex.shared.shot_media.ShotMedia.inference_video', unexpected)
    monkeypatch.setattr('mindex.shared.shot_media.probe', unexpected)
    monkeypatch.setattr(channel_index, 'REUSED_METADATA_BATCH_SIZE', 4)
    saved = []

    class Cancelled(Exception):
        pass

    def progress(value):
        if value['stage'] == 'embedding' and value['completed']:
            count = pipeline.store.one("SELECT count(*) n FROM index_members WHERE index_id=? "
                                       "AND complete=1 AND json_extract(snapshot,'$.kind')='scene'",
                                       (value['index_id'],))['n']
            assert count == value['completed'], '进度只能报告已提交的元数据'
            assert 0 < count < 10
            saved.append(value['index_id'])
            raise Cancelled()

    with pytest.raises(Cancelled):
        builder.build(source_id=sid, kinds=['scene'], channels=['visual'], progress=progress)
    resumed = builder.build(source_id=sid, kinds=['scene'], channels=['visual'])
    assert saved == [resumed]
    assert len(pipeline.store.snapshots(original)) == len(pipeline.store.segments(sid, 'moment'))
    assert check_index(pipeline.store, resumed) == []
    report = json.loads(pipeline.store.one('SELECT value FROM settings WHERE key=?',
                                          ('index_report.' + resumed,))['value'])
    assert report['embedded_vectors'] == 0 and report['cached_metadata_batches'] > 0


@pytest.mark.slow
def test_pending_raw_asr_reuses_chunks_before_initializing_model(config, video_file, monkeypatch):
    from mindex.shared.jsonio import save

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    source = pipeline.store.one('SELECT * FROM sources WHERE id=?', (sid,))
    info = json.loads(source['media'])
    pipeline.store.execute('UPDATE sources SET media=? WHERE id=?', (json.dumps({**info, 'audio': True}), sid))
    config.asr.backend = 'openai'
    old = config.data_dir / 'transcripts' / sid / 'old-profile'
    save(old / 'raw/0.json', {'text': '你好', 'language': 'Chinese', 'start': 0, 'duration': info['duration']})
    save(old / 'aligned-0.json', {'revision': config.asr.aligner_revision, 'utterances': [
        {'start': 0, 'end': .5, 'text': '你好'}]})

    def unexpected(*args, **kwargs):
        pytest.fail('已有原始转写检查点，不应初始化 ASR 或提取音频')

    monkeypatch.setattr('mindex.processing.pipeline.make_transcriber', unexpected)
    monkeypatch.setattr('mindex.shared.shot_media.ShotMedia.audio', unexpected)
    pipeline.transcribe_raw(sid)
    cached = config.data_dir / 'transcripts' / sid / config.asr_key
    assert json.loads((cached / 'raw/0.json').read_text())['reused_from_profile'] == 'old-profile'
    assert (cached / 'aligned-0.json').is_file()


def test_asr_chunks_reject_different_context_before_reuse(config):
    from mindex.shared.jsonio import save

    processor = DialogueProcessor(config)
    old = config.data_dir / 'dialogue/source/old/0.json'
    cache = config.data_dir / 'dialogue/source/new'
    chunk = {'core_start': 0, 'core_end': 30, 'context_start': 0, 'context_end': 32,
             'words': [{'text': 'hello', 'start': 1, 'end': 2}]}
    save(old, chunk)
    processor._reuse_chunks('source', cache, 2, 60, None)
    assert not (cache / '0.json').exists()
    save(old, {**chunk, 'context_end': 31})
    processor._reuse_chunks('source', cache, 2, 60, None)
    assert json.loads((cache / '0.json').read_text())['words'] == chunk['words']


@pytest.mark.slow
def test_forced_raw_asr_invalidates_alignment_and_promotes_new_dialogue(config, video_file, monkeypatch):
    import sys
    from pathlib import Path

    from mindex.processing.dialogue import current_dialogue
    from mindex.shared.jsonio import save

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    info = json.loads(pipeline.store.one('SELECT media FROM sources WHERE id=?', (sid,))['media'])
    pipeline.store.execute('UPDATE sources SET media=? WHERE id=?', (json.dumps({**info, 'audio': True}), sid))
    config.asr.backend = 'openai'
    config.asr.aligner_revision = 'test-revision'
    DialogueProcessor(config).save(sid, 'old', {'status': 'complete', 'source': 'asr',
        'utterances': [{'start': 0, 'end': 1, 'text': 'old words'}]})
    cache = config.data_dir / 'transcripts' / sid / config.asr_key
    save(cache / 'aligned-0.json', {'revision': config.asr.aligner_revision,
                                   'utterances': [{'start': 0, 'end': 1, 'text': 'stale words'}]})
    calls = []

    class ASR:
        def raw(self, path):
            calls.append('asr')
            return {'text': 'new words', 'language': 'English'}

    def align(args, **kwargs):
        calls.append('align')
        raw = json.loads(Path(args[args.index('--raw') + 1]).read_text())
        save(Path(args[args.index('--output') + 1]), {'revision': config.asr.aligner_revision,
             'utterances': [{'start': 0, 'end': 1, 'text': raw['text']}]})

    pipeline._transcriber = ASR()
    monkeypatch.setattr('mindex.shared.shot_media.ShotMedia.audio', lambda self, sid, path, *args: path.write_bytes(b'fixture'))
    monkeypatch.setattr('mindex.shared.media.command', align)
    pipeline.transcribe_raw(sid, force=True)
    assert not (cache / 'aligned-0.json').exists()
    pipeline.align(Path(sys.executable), sid)
    assert current_dialogue(pipeline.store, sid)['utterances'][0]['text'] == 'new words'
    assert calls == ['asr', 'align']
