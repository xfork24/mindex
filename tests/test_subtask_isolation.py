"""The ten workbench subtasks own their execution, output and checkpoints."""
import json
from contextlib import ExitStack

import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder
from filelock import Timeout

from mindex.admin.dialogue_tasks import DialogueOptions
from mindex.admin.source_tasks import PublishOptions, SourceTasks, WorkOptions, publish_work
from mindex.persons.person_processing import PersonOptions
from mindex.processing.channel_index import normalize_snapshot
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.shared.processing_lock import recover_tasks


def test_processing_defaults_never_chain_embeddings():
    assert not WorkOptions().publish
    assert not DialogueOptions().publish
    assert not PersonOptions().publish


def test_all_five_content_embedding_subtasks_are_isolated(config, video_file, monkeypatch):
    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    pipeline.store.execute(
        "INSERT INTO channel_data(source_id,channel,profile,status,data) VALUES(?,'dialogue','fixture','complete',?)",
        (sid, json.dumps({'source': 'subtitle', 'status': 'complete', 'content_version': 'dialogue1',
                         'utterances': [{'start': 0, 'end': 10, 'text': '对白专用文本'}]})),
    )
    calls = []
    monkeypatch.setattr(embedder, 'video', lambda path: calls.append(('video', str(path))) or [1., 0., 0., 0., 0., 0., 0., 0.])
    monkeypatch.setattr(embedder, 'text', lambda texts, **kwargs: [calls.append(('text', text)) or [1., 0., 0., 0., 0., 0., 0., 0.] for text in texts])
    previous = {}
    for owner, scope, view in [('shots', 'moment', 'video'), ('analysis', 'moment', 'analysis'),
                               ('dialogue', 'moment', 'dialogue'), ('shots', 'extra', 'video'),
                               ('analysis', 'extra', 'analysis')]:
        calls.clear()
        events = []
        index = publish_work(config, sid, owner, embedder, scope=scope, progress=events.append)['index_id']
        current = {s['id']: normalize_snapshot(s) for s in pipeline.store.snapshots(index)}
        kinds = {'moment'} if scope == 'moment' else {'shot', 'scene'}
        for segment in current.values():
            before = previous.get(segment['id'])
            for other in {'video', 'analysis', 'dialogue'} - {view}:
                assert (other in segment['indexed_views']) == bool(before and other in before['indexed_views'])
                if other != 'video':
                    assert segment['views'].get(other) == (before['views'].get(other) if before else None)
            if segment['kind'] in kinds:
                assert view in segment['indexed_views']
        if scope == 'moment':
            assert calls, (owner, scope)
        assert all(kind == ('video' if view == 'video' else 'text') for kind, _ in calls)
        if view != 'video':
            assert all(('对白专用文本' in text) == (view == 'dialogue') for _, text in calls)
        expected = sum(s['kind'] in kinds and view in s['indexed_views'] for s in current.values())
        assert events[-1]['completed'] == events[-1]['total'] == expected
        previous = current
    # Each completed subtask reuses its own vectors without a new model call.
    calls.clear()
    for owner, scope in [('shots', 'moment'), ('analysis', 'moment'), ('dialogue', 'moment'),
                         ('shots', 'extra'), ('analysis', 'extra')]:
        publish_work(config, sid, owner, embedder, scope=scope)
    assert not calls


def test_person_publication_does_not_build_content_embeddings(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    monkeypatch.setattr(IndexBuilder, 'build', lambda *args, **kwargs: pytest.fail('person publication must not build content embeddings'))
    with pytest.raises(ValueError, match='请先发布内容索引'):
        publish_work(config, sid, 'persons')
    assert pipeline.store.active_index() is None


def test_subtask_locks_and_restart_recovery_are_independent(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    with ExitStack() as locks:
        for owner, scope in [('shots', 'moment'), ('shots', 'extra'), ('analysis', 'moment'),
                             ('analysis', 'extra'), ('dialogue', 'moment'), ('persons', 'moment')]:
            options = PublishOptions(work=owner, scope=scope)
            locks.enter_context(manager.lock(sid, 'publish', options))
            with pytest.raises(Timeout), manager.lock(sid, 'publish', options):
                pass
            pipeline.store.execute(
                "INSERT INTO source_tasks(id,source_id,work,status,config,options) VALUES(?,?,'publish','running','{}',?)",
                (owner + scope, sid, options.model_dump_json()),
            )
        recover_tasks(pipeline.store, 'source_tasks')
        assert all(t['status'] == 'running' for t in pipeline.store.rows('SELECT status FROM source_tasks'))
    recover_tasks(pipeline.store, 'source_tasks')
    assert all(t['status'] == 'interrupted' for t in pipeline.store.rows('SELECT status FROM source_tasks'))


@pytest.mark.parametrize('enforced', [False, True])
def test_automatic_embedding_uses_separate_child_tasks_and_logs(config, video_file, monkeypatch, enforced):
    import logging
    import threading

    from test_source_work import finished

    config.enforce_gpu_lock = enforced
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    entered, release = threading.Event(), threading.Event()

    def publish(config, source_id, owner, *args, scope='moment', progress=None, **kwargs):
        assert owner == 'shots' and source_id == sid
        progress({'stage':'embedding','completed':0,'total':1})
        logging.getLogger('mindex.isolation').warning('own child %s %s', owner, scope)
        entered.set()
        assert release.wait(10)
        return {'index_id':scope}

    monkeypatch.setattr('mindex.admin.source_tasks.publish_work', publish)
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    try:
        parent = finished(manager, manager.start(sid, 'shots', WorkOptions(publish=True)))
        assert parent['status'] == 'complete'
        assert parent['progress']['phase'] == 'process'
        assert 'own child' not in parent['progress'].get('latest_log', '')
        children = parent['publication']['task_ids']
        assert len(children) == 2 and parent['id'] not in children
        assert entered.wait(3)
        # A restarted app must preserve queued leases while a child owns the GPU.
        SourceTasks(manager.runtime, pipeline.store)
        for identifier in children:
            child = manager.get(identifier)
            assert child['status'] in ('queued','running')
            assert child['options']['work'] == 'shots'
        release.set()
        for identifier in children:
            child = finished(manager, manager.get(identifier))
            assert child['status'] == 'complete'
            assert child['progress']['phase'] == 'publish'
            assert child['progress']['latest_log'] == f"own child shots {child['options']['scope']}"
        assert manager.get(parent['id'])['progress'] == parent['progress']
    finally:
        release.set()
