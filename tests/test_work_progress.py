"""工作台处理/发布分阶段进度：真实管线、索引、SQLite 与检查点。"""

import json
import logging
import threading
from types import SimpleNamespace

import pytest

from admin.source_tasks import publication_attempt, publish_work
from conftest import FixtureAnalyzer, FixtureEmbedder
from processing.channel_index import snapshots_for
from processing.indexing import IndexBuilder
from processing.pipeline import Pipeline
from shared.processing_lock import index_write_lock
from shared.task_progress import save_task_progress

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


@pytest.mark.parametrize("work", ["shots", "dialogue", "analysis"])
def test_publish_work_reports_scope_counts_for_every_channel(config, video_file, work):
    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, embedder).build(source_id=sid, channels=["visual"], activate=True)
    pipeline.store.execute(
        "INSERT INTO channel_data(source_id,channel,profile,status,data) "
        "VALUES(?,'dialogue','fixture','complete',?)",
        (
            sid,
            json.dumps(
                {
                    "source": "subtitle",
                    "status": "complete",
                    "content_version": "dialogue1",
                    "utterances": [{"start": 0, "end": 10, "text": "工程对白"}],
                }
            ),
        ),
    )
    pipeline.analyze(sid)
    events, saved = [], []
    result = publication_attempt(
        config, sid, work, embedder=embedder, progress=events.append, save=saved.append
    )
    assert result["status"] == "complete", result
    assert events[0]["stage"] == "index_wait" and "total" not in events[0]
    assert events[-1]["stage"] == "published"
    assert all(event["phase"] == "publish" for event in events)
    expected = len(snapshots_for(pipeline, sid, ["moment", "shot"]))
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
    from persons.persons import Persons

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, activate=True)
    shot = pipeline.store.segments(sid, "shot")[0]
    # More than one coverage checkpoint for a shot counts as one mirror of the
    # published person coverage; another source is outside this task's count.
    for key, source, shot_id in [
        ("a", sid, shot["id"]),
        ("b", sid, shot["id"]),
        ("c", "other", "other-shot"),
    ]:
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
    repeated = []
    again = publish_work(config, sid, "persons", FixtureEmbedder(config), progress=repeated.append)
    assert again == result
    assert repeated[-1]["completed"] == repeated[-1]["total"] == 1


@pytest.mark.parametrize("table", ["source_tasks", "dialogue_tasks", "person_tasks"])
def test_processing_progress_survives_publication_updates_and_retry(config, table):
    from shared.storage import Store

    store = Store(config.data_dir)
    if table == "source_tasks":
        store.execute(
            "INSERT INTO source_tasks(id,source_id,work,status,config,options) "
            "VALUES('t','s','analysis','running','{}','{}')"
        )
    else:
        store.execute(
            f"INSERT INTO {table}(id,source_id,status,config,options) VALUES('t','s','running','{{}}','{{}}')"
        )
    processing = {"stage": "analyze", "completed": 8, "total": 8, "unit": "segments"}
    save_task_progress(store, table, "t", processing)
    for event in [
        {"stage": "index_wait", "phase": "publish"},
        {"stage": "embedding", "phase": "publish", "completed": 2, "total": 4, "unit": "segments"},
        {"stage": "index_wait", "phase": "publish"},
    ]:
        save_task_progress(store, table, "t", event)
        progress = json.loads(store.one(f"SELECT progress FROM {table} WHERE id='t'")["progress"])
        assert progress["processing_progress"] == {**processing, "phase": "process"}
        assert "processing_progress" not in progress["processing_progress"]


def test_pipeline_reports_before_first_analysis_and_each_stage(config, video_file):
    embedder = FixtureEmbedder(config)
    events = []

    class Analyzer(FixtureAnalyzer):
        def analyze(self, path, dialogue):
            assert (
                events[-1]["stage"] in {"analyze", "scenes"} and events[-1]["completed"] < events[-1]["total"]
            )
            return super().analyze(path, dialogue)

    pipeline = Pipeline(config, analyzer=Analyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid, progress=events.append)
    assert events[0]["stage"] == "analyze" and events[0]["completed"] == 0
    assert {e["stage"] for e in events} == {"analyze", "scene_grouping", "scenes"}
    assert all(e["unit"] == ("scenes" if e["stage"] == "scenes" else "segments") for e in events)
    assert events[-1]["completed"] == events[-1]["total"]


def test_shot_moment_and_scene_publication_are_independent(config, video_file):
    from admin.curation import Library
    from admin.source_tasks import PublishOptions, SourceTasks
    from settings.runtime_settings import RuntimeConfig

    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    publish_work(config, sid, "shots", embedder)
    shot_moment = publish_work(config, sid, "analysis", embedder)["index_id"]
    assert {s["kind"] for s in pipeline.store.snapshots(shot_moment)} == {"moment", "shot"}
    work = Library(pipeline.store).source(sid)["work"]
    assert work["shots"]["search"]["current"]
    assert work["analysis"]["search"]["current"]
    assert work["shots"]["scene_search"]["indexed_segments"] == 0
    assert work["analysis"]["can_publish_scene"]
    events = []
    scene = publish_work(config, sid, "analysis", embedder, scope="scene", progress=events.append)["index_id"]
    snapshots = pipeline.store.snapshots(scene)
    assert {s["kind"] for s in snapshots} == {"moment", "shot", "scene"}
    assert all("analysis" in s["indexed_views"] for s in snapshots)
    expected = len([s for s in snapshots if s["kind"] == "scene"])
    assert all(e["total"] == expected for e in events if "total" in e)
    work = Library(pipeline.store).source(sid)["work"]
    assert work["analysis"]["scene_search"]["current"]
    assert work["analysis"]["scene_search"]["indexed_segments"] == expected
    again = publish_work(config, sid, "shots", embedder)["index_id"]
    assert {s["id"] for s in pipeline.store.snapshots(again)} == {s["id"] for s in snapshots}
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store, embedder)
    # Capture queued options without launching a thread: the scope is durable for retry.
    manager.launch = lambda *args: args[-1].release()
    task = manager.start(sid, "publish", PublishOptions(work="analysis", scope="scene"))
    all_work = Library(pipeline.store).source(sid)["work"]
    work = all_work["analysis"]
    assert work["scene_publish_task"]["id"] == task["id"]
    assert work["scene_publish_task"]["options"]["scope"] == "scene"
    assert work["publish_task"] is None
    assert all_work["shots"]["scene_publish_task"] is None
    assert all_work["shots"]["publish_task"] is None
    # A shot/moment update has its own owner too; shared vector coverage must not
    # copy this execution's progress or logs onto the other work cards.
    shot_moment_task = manager.start(sid, "publish", PublishOptions(work="shots", scope="shot_moment"))
    all_work = Library(pipeline.store).source(sid)["work"]
    assert all_work["shots"]["publish_task"]["id"] == shot_moment_task["id"]
    assert all_work["analysis"]["publish_task"] is None
    assert all_work["dialogue"]["publish_task"] is None
    assert all_work["analysis"]["scene_publish_task"]["id"] == task["id"]


def test_completed_processing_and_both_embedding_scopes_reuse_results(config, video_file, monkeypatch):
    from admin.curation import Library

    embedder = FixtureEmbedder(config)
    analyzer = FixtureAnalyzer(embedder)
    pipeline = Pipeline(config, analyzer=analyzer)
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    for scope in ("shot_moment", "scene"):
        publish_work(config, sid, "shots", embedder, scope=scope)
        publish_work(config, sid, "analysis", embedder, scope=scope)

    def unexpected(*args, **kwargs):
        pytest.fail("completed work must reuse cached model results")

    monkeypatch.setattr(pipeline.detector, "detect", unexpected)
    monkeypatch.setattr(analyzer, "analyze", unexpected)
    monkeypatch.setattr(analyzer, "group_scenes", unexpected)
    for method in ("text", "video", "image"):
        monkeypatch.setattr(embedder, method, unexpected)
    pipeline.detect_source(sid)
    pipeline.split(sid)
    pipeline.analyze(sid)
    for scope in ("shot_moment", "scene"):
        events = []
        publish_work(config, sid, "analysis", embedder, scope=scope, progress=events.append)
        assert events[-1]["completed"] == events[-1]["total"]
    work = Library(pipeline.store).source(sid)["work"]
    for name in ("shots", "analysis"):
        assert work[name]["status"] == "complete"
        for field in ("search", "scene_search"):
            coverage = work[name][field]
            assert coverage["current"]
            assert coverage["indexed_segments"] == coverage["expected_segments"]
            assert all(c["indexed_segments"] == c["expected_segments"] for c in coverage["by_kind"].values())


def test_scene_embedding_progress_survives_unpublished_index(config, video_file):
    from admin.curation import Library

    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    publish_work(config, sid, "shots", embedder)
    shot_moment = publish_work(config, sid, "analysis", embedder)["index_id"]
    visual_scene = publish_work(config, sid, "shots", embedder, scope="scene")["index_id"]
    scene = publish_work(config, sid, "analysis", embedder, scope="scene")["index_id"]
    # A final validation/publication failure leaves reusable, completed vectors.
    pipeline.store.activate(shot_moment)
    pipeline.store.execute("UPDATE index_versions SET status='building' WHERE id=?", (scene,))
    work = Library(pipeline.store).source(sid)["work"]
    for name in ("shots", "analysis"):
        coverage = work[name]["scene_search"]
        assert not coverage["current"] and coverage["indexed_segments"] == 0
        assert coverage["embedded_segments"] == coverage["expected_segments"] > 0
        assert all(c["embedded_segments"] == c["expected_segments"] for c in coverage["by_kind"].values())
    # An incomplete member is not reported as a completed embedding checkpoint.
    pipeline.store.execute(
        "UPDATE index_members SET complete=0 WHERE index_id IN (?,?)", (scene, visual_scene)
    )
    work = Library(pipeline.store).source(sid)["work"]
    assert work["shots"]["scene_search"]["by_kind"]["scene"]["embedded_segments"] == 0
    assert (
        work["shots"]["scene_search"]["embedded_segments"]
        < work["shots"]["scene_search"]["expected_segments"]
    )


def test_empty_person_release_is_not_full_shot_coverage(config, video_file):
    from admin.curation import Library
    from persons.persons import Persons

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, activate=True)
    Persons(pipeline.store).publish(source_id=sid)
    coverage = Library(pipeline.store).source(sid)["work"]["persons"]["search"]
    assert coverage["expected_segments"] > 0
    assert coverage["indexed_segments"] == 0
    assert not coverage["current"]


@pytest.mark.parametrize("kind", ["shots", "dialogue", "persons"])
def test_retry_clears_only_attempt_diagnostics_and_preserves_result(config, video_file, monkeypatch, kind):
    from admin.dialogue_tasks import DialogueOptions, DialogueTasks
    from admin.source_tasks import SourceTasks, WorkOptions
    from persons.person_processing import PersonOptions, PersonTasks
    from settings.runtime_settings import RuntimeConfig

    class IdleThread:
        def __init__(self, *args, **kwargs):
            self.lock = kwargs["args"][-1]

        def start(self):
            self.lock.release()

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    monkeypatch.setattr(threading, "Thread", IdleThread)
    runtime = RuntimeConfig(config, pipeline.store)
    manager, options, table = {
        "shots": (SourceTasks(runtime, pipeline.store), WorkOptions(), "source_tasks"),
        "dialogue": (DialogueTasks(runtime, pipeline.store), DialogueOptions(), "dialogue_tasks"),
        "persons": (PersonTasks(runtime, pipeline.store), PersonOptions(), "person_tasks"),
    }[kind]
    task = manager.start(sid, kind, options) if kind == "shots" else manager.start(sid, options)
    pipeline.store.execute(
        f"UPDATE {table} SET status='failed',error='old error',progress=?,publication=?,result=? WHERE id=?",
        (
            json.dumps({"latest_log": "old log", "stage": "embedding"}),
            json.dumps({"status": "failed", "error": "old publication error"}),
            json.dumps({"saved": True}),
            task["id"],
        ),
    )
    manager.retry(task["id"])
    row = pipeline.store.one(f"SELECT * FROM {table} WHERE id=?", (task["id"],))
    assert row["status"] == "queued" and row["error"] is None
    assert json.loads(row["progress"]) == json.loads(row["publication"]) == {}
    assert json.loads(row["result"]) == {"saved": True}


def test_latest_worker_log_is_one_line_thread_scoped_and_phase_scoped(config):

    from shared.storage import Store
    from shared.task_progress import task_logs

    store = Store(config.data_dir)
    store.execute(
        "INSERT INTO source_tasks(id,source_id,work,status,config,options) "
        "VALUES('log','s','shots','running','{}','{}')"
    )
    logger = logging.getLogger("processing.test_task_log")
    before = logger.level
    logger.setLevel(logging.INFO)

    @task_logs("source_tasks")
    def run(manager, task_id):
        save_task_progress(store, "source_tasks", task_id, {"stage": "split"})
        logger.info("first line\nlatest processing line")
        thread = threading.Thread(target=lambda: logger.warning("another task"))
        thread.start()
        thread.join()
        row = json.loads(store.one("SELECT progress FROM source_tasks WHERE id='log'")["progress"])
        assert row["latest_log"] == "latest processing line"
        save_task_progress(store, "source_tasks", task_id, {"stage": "embedding", "phase": "publish"})
        logger.info("latest embedding line")

    root = logging.getLogger()
    handlers = list(root.handlers)
    try:
        run(SimpleNamespace(store=store), "log")
    finally:
        logger.setLevel(before)
    value = json.loads(store.one("SELECT progress FROM source_tasks WHERE id='log'")["progress"])
    assert value["latest_log"] == "latest embedding line"
    assert value["processing_progress"]["latest_log"] == "latest processing line"
    assert root.handlers == handlers


@pytest.mark.parametrize("scope", ["shot_moment", "scene"])
def test_analysis_coverage_uses_published_vectors_despite_metadata_changes(config, video_file, scope):
    from admin.curation import Library

    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    sid = pipeline.prepare(video_file)
    pipeline.analyze(sid)
    index = publish_work(config, sid, "analysis", embedder, scope=scope)["index_id"]
    field = "search" if scope == "shot_moment" else "scene_search"
    snapshots = pipeline.store.snapshots(index, sid)
    for snapshot in snapshots:
        row = pipeline.store.one(
            "SELECT rowid,data FROM annotations WHERE segment_id=? ORDER BY rowid DESC LIMIT 1",
            (snapshot["id"],),
        )
        data = json.loads(row["data"])
        data["model_identity"] = {"model": "changed-provenance"}
        data["dialogue_input_version"] = "changed-input-version"
        data["input_fingerprint"] = "changed-input-fingerprint"
        pipeline.store.execute(
            "UPDATE annotations SET data=? WHERE rowid=?", (json.dumps(data), row["rowid"])
        )
    coverage = Library(pipeline.store).source(sid)["work"]["analysis"][field]
    assert coverage["expected_segments"] > 0
    assert coverage["current"]
    assert coverage["indexed_segments"] == coverage["embedded_segments"] == coverage["expected_segments"]
    assert coverage["unpublished_segments"] == 0

    # Actual text changes require a new vector, even if its metadata was unchanged.
    row = pipeline.store.one(
        "SELECT rowid,data FROM annotations WHERE segment_id=? ORDER BY rowid DESC LIMIT 1",
        (snapshots[0]["id"],),
    )
    data = json.loads(row["data"])
    data["annotation"]["description"] = "全新的分析文本，需要更新检索向量"
    pipeline.store.execute("UPDATE annotations SET data=? WHERE rowid=?", (json.dumps(data), row["rowid"]))
    coverage = Library(pipeline.store).source(sid)["work"]["analysis"][field]
    assert not coverage["current"]
    assert coverage["embedded_segments"] == coverage["expected_segments"] - 1
    assert coverage["pending_reasons"] == [{"code": "inputs_changed", "count": 1}]

    # Building reusable vectors elsewhere does not publish them into the active index.
    IndexBuilder(config, embedder).build(
        source_id=sid,
        kinds=("moment", "shot") if scope == "shot_moment" else ("scene",),
        channels=["analysis"],
        activate=False,
    )
    coverage = Library(pipeline.store).source(sid)["work"]["analysis"][field]
    assert not coverage["current"]
    assert coverage["embedded_segments"] == coverage["expected_segments"]
    assert coverage["unpublished_segments"] == 1


def test_visual_coverage_checks_vector_inputs_instead_of_stale_version_tags(config, video_file):
    from admin.curation import Library

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    embedder = FixtureEmbedder(config)
    index = publish_work(config, sid, "shots", embedder)["index_id"]
    snapshot = pipeline.store.snapshots(index, sid)[0]
    snapshot["channel_versions"]["visual"] = "historical-tag-before-boundary-repair"
    pipeline.store.execute(
        "UPDATE index_members SET snapshot=? WHERE index_id=? AND segment_id=?",
        (json.dumps(snapshot), index, snapshot["id"]),
    )
    coverage = Library(pipeline.store).source(sid)["work"]["shots"]["search"]
    assert coverage["current"]
    assert coverage["embedded_segments"] == coverage["expected_segments"]
    # A genuinely different interval cannot reuse that old vector.
    pipeline.store.execute("UPDATE segments SET start=start+0.1 WHERE id=?", (snapshot["id"],))
    coverage = Library(pipeline.store).source(sid)["work"]["shots"]["search"]
    assert not coverage["current"]
    assert coverage["embedded_segments"] == coverage["expected_segments"] - 1
    assert coverage["pending_reasons"] == [{"code": "inputs_changed", "count": 1}]


@pytest.mark.parametrize("table", ["source_tasks", "dialogue_tasks", "person_tasks"])
def test_retry_clears_logs_without_losing_completed_processing_stage(config, table):
    from shared.storage import Store
    from shared.task_progress import reset_task_attempt

    store = Store(config.data_dir)
    columns = "id,source_id,status,config,options" + (",work" if table == "source_tasks" else "")
    values = "'retry-log','s','failed','{}','{}'" + (",'shots'" if table == "source_tasks" else "")
    store.execute(f"INSERT INTO {table}({columns}) VALUES({values})")
    processing = {
        "phase": "process",
        "stage": "split",
        "completed": 3,
        "total": 3,
        "latest_log": "old processing diagnostic",
        "message": "old message",
    }
    store.execute(
        f"UPDATE {table} SET result=?,progress=?,error=? WHERE id=?",
        (
            json.dumps({"saved": True}),
            json.dumps(
                {"phase": "publish", "processing_progress": processing, "latest_log": "old publication error"}
            ),
            "old error",
            "retry-log",
        ),
    )
    reset_task_attempt(store, table, "retry-log")
    save_task_progress(store, table, "retry-log", {"phase": "publish", "stage": "embedding"})
    row = store.one(f"SELECT * FROM {table} WHERE id=?", ("retry-log",))
    progress = json.loads(row["progress"])
    assert progress["processing_progress"] == {
        "phase": "process",
        "stage": "split",
        "completed": 3,
        "total": 3,
    }
    assert "old" not in row["progress"]
    assert row["error"] is None
