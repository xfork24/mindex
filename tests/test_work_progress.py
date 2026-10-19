"""工作台处理/发布分阶段进度：真实管线、索引、SQLite 与检查点。"""

import json
import threading

import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder

from mindex.channel_index import snapshots_for
from mindex.indexing import IndexBuilder
from mindex.pipeline import Pipeline
from mindex.processing_lock import index_write_lock
from mindex.source_tasks import publication_attempt, publish_work
from mindex.task_progress import save_task_progress


@pytest.mark.parametrize("work", ["shots", "dialogue", "analysis"])
def test_publish_work_reports_scope_counts_for_every_channel(config, video_file, work):
    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, embedder).build(source_id=sid, channels=["visual"], activate=True)
    pipeline.store.execute(
        "INSERT INTO channel_data(source_id,channel,profile,status,data) VALUES(?,'dialogue','fixture','complete',?)",
        (sid, json.dumps({"source": "subtitle", "status": "complete", "content_version": "dialogue1",
                         "utterances": [{"start": 0, "end": 10, "text": "工程对白"}]})),
    )
    pipeline.analyze(sid)
    events, saved = [], []
    result = publication_attempt(config, sid, work, embedder=embedder, progress=events.append, save=saved.append)
    assert result["status"] == "complete", result
    assert events[0]["stage"] == "index_wait" and "total" not in events[0]
    assert events[-1]["stage"] == "published"
    assert all(event["phase"] == "publish" for event in events)
    expected = len(snapshots_for(pipeline, sid, ["moment"]))
    counted = [e for e in events if "total" in e]
    assert counted and all(e["total"] == expected and e["unit"] == "segments" for e in counted)
    assert counted[-1]["completed"] == expected
    assert result["progress"] == events[-1] and saved[-1] == result


def test_index_lock_wait_is_reported_before_build_starts(config, video_file):
    sid = Pipeline(config).prepare(video_file)
    waiting = threading.Event()
    events, errors = [], []

    def notify(value):
        events.append(value)
        if value["stage"] == "index_wait":
            waiting.set()

    def build():
        try:
            IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, progress=notify)
        except Exception as exc:  # noqa: BLE001 — 后台异常返回测试线程断言
            errors.append(exc)

    with index_write_lock(config.data_dir):
        thread = threading.Thread(target=build)
        thread.start()
        assert waiting.wait(2)
        assert [e["stage"] for e in events] == ["index_wait"]
    thread.join(10)
    assert not thread.is_alive() and not errors
    assert events[1]["stage"] == "index_prepare" and events[-1]["stage"] == "indexed"


def test_person_publish_counts_unique_source_shots(config, video_file):
    from mindex.persons import Persons

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, activate=True)
    shot = pipeline.store.segments(sid, "shot")[0]
    # More than one coverage checkpoint for a shot counts as one mirror of the
    # published person coverage; another source is outside this task's count.
    for key, source, shot_id in [("a", sid, shot["id"]), ("b", sid, shot["id"]), ("c", "other", "other-shot")]:
        pipeline.store.execute(
            "INSERT INTO person_coverage(cache_id,source_id,data) VALUES(?,?,?)",
            (key, source, json.dumps({"shot_id": shot_id, "start": 0, "end": 1})),
        )
    events = []
    result = publish_work(config, sid, "persons", FixtureEmbedder(config), progress=events.append)
    assert result["release_id"]
    assert events[-2:] == [
        {"stage": "person_index", "completed": 0, "total": 1, "unit": "shots"},
        {"stage": "published", "completed": 1, "total": 1, "unit": "shots"},
    ]
    assert Persons(pipeline.store).published(result["content_id"])[1]["coverage"]


@pytest.mark.parametrize("table", ["source_tasks", "dialogue_tasks", "person_tasks"])
def test_processing_progress_survives_publication_updates_and_retry(config, table):
    from mindex.storage import Store

    store = Store(config.data_dir)
    if table == "source_tasks":
        store.execute("INSERT INTO source_tasks(id,source_id,work,status,config,options) VALUES('t','s','analysis','running','{}','{}')")
    else:
        store.execute(f"INSERT INTO {table}(id,source_id,status,config,options) VALUES('t','s','running','{{}}','{{}}')")
    processing = {"stage": "analyze", "completed": 8, "total": 8, "unit": "segments"}
    save_task_progress(store, table, "t", processing)
    for event in [{"stage": "index_wait", "phase": "publish"},
                  {"stage": "embedding", "phase": "publish", "completed": 2, "total": 4, "unit": "segments"},
                  {"stage": "index_wait", "phase": "publish"}]:
        save_task_progress(store, table, "t", event)
        progress = json.loads(store.one(f"SELECT progress FROM {table} WHERE id='t'")["progress"])
        assert progress["processing_progress"] == {**processing, "phase": "process"}
        assert "processing_progress" not in progress["processing_progress"]


def test_pipeline_reports_before_first_analysis_and_each_stage(config, video_file):
    embedder = FixtureEmbedder(config)
    events = []

    class Analyzer(FixtureAnalyzer):
        def analyze(self, path, dialogue):
            assert events[-1]["stage"] in {"analyze", "scenes"} and events[-1]["completed"] < events[-1]["total"]
            return super().analyze(path, dialogue)

    pipeline = Pipeline(config, analyzer=Analyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid, progress=events.append)
    assert events[0]["stage"] == "analyze" and events[0]["completed"] == 0
    assert {e["stage"] for e in events} == {"analyze", "scene_grouping", "scenes"}
    assert all(e["unit"] == ("scenes" if e["stage"] == "scenes" else "segments") for e in events)
    assert events[-1]["completed"] == events[-1]["total"]


def test_moment_and_extra_publication_are_independent(config, video_file):
    from mindex.curation import Library
    from mindex.runtime_settings import RuntimeConfig
    from mindex.source_tasks import PublishOptions, SourceTasks

    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    moment = publish_work(config, sid, 'analysis', embedder)['index_id']
    assert {s['kind'] for s in pipeline.store.snapshots(moment)} == {'moment'}
    work = Library(pipeline.store).source(sid)['work']
    assert work['shots']['search']['current']
    assert work['analysis']['search']['current']
    assert work['shots']['extra_search']['indexed_segments'] == 0
    assert work['analysis']['can_publish_extra']
    events = []
    extra = publish_work(config, sid, 'analysis', embedder, scope='extra', progress=events.append)['index_id']
    snapshots = pipeline.store.snapshots(extra)
    assert {s['kind'] for s in snapshots} == {'moment', 'shot', 'scene'}
    assert all('analysis' in s['indexed_views'] for s in snapshots)
    expected = len([s for s in snapshots if s['kind'] != 'moment'])
    assert all(e['total'] == expected for e in events if 'total' in e)
    work = Library(pipeline.store).source(sid)['work']
    assert work['analysis']['extra_search']['current']
    assert work['analysis']['extra_search']['indexed_segments'] == expected
    again = publish_work(config, sid, 'shots', embedder)['index_id']
    assert {s['id'] for s in pipeline.store.snapshots(again)} == {s['id'] for s in snapshots}
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store, embedder)
    # Capture queued options without launching a thread: the scope is durable for retry.
    manager.launch = lambda *args: args[-1].release()
    task = manager.start(sid, 'publish', PublishOptions(work='analysis', scope='extra'))
    work = Library(pipeline.store).source(sid)['work']['analysis']
    assert work['extra_publish_task']['id'] == task['id']
    assert work['extra_publish_task']['options']['scope'] == 'extra'
    assert work['publish_task'] is None
