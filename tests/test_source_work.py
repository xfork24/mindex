"""隔离素材的上传、任务、发布、检索和历史状态集成验收。"""

import asyncio
import io
import threading
import time

import pytest
from conftest import FixtureAnalyzer, FixtureEmbedder
from fastapi import UploadFile
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.dialogue import DialogueProcessor, current_dialogue
from mindex.dialogue_tasks import DialogueOptions, DialogueTasks
from mindex.indexing import IndexBuilder, check_index
from mindex.pipeline import Pipeline
from mindex.runtime_settings import RuntimeConfig
from mindex.source_tasks import SourceTasks, WorkOptions
from mindex.storage import Store


def finished(manager, task):
    for _ in range(1000):
        value = manager.get(task["id"])
        if value["status"] in {"complete", "failed", "interrupted"} and task["id"] not in manager.threads:
            return value
        time.sleep(0.01)
    pytest.fail("后台任务未完成")


def test_upload_register_duplicate_invalid_limit_and_interruption(config, video_file, monkeypatch):
    client = TestClient(create_app(config))
    monkeypatch.setattr("mindex.pipeline.make_detector", lambda *_: pytest.fail("登记不启动分镜检测"))
    raw = video_file.read_bytes()
    response = client.post("/api/admin/sources/upload", files={"file": ("../中文原片.mp4", raw)})
    assert response.status_code == 201, response.text
    sid = response.json()["source_id"]
    assert not response.json()["duplicate"]
    source = client.get(f"/api/admin/sources/{sid}").json()
    assert source["original_name"] == "中文原片.mp4"
    assert all(w["status"] == "not_processed" for w in source["work"].values())
    assert not source["work"]["analysis"]["can_start"]
    assert source["work"]["shots"]["can_start"]
    assert Store(config.data_dir).resolve_media(f"sources/{sid}/中文原片.mp4").read_bytes() == raw
    config.video.window_seconds += 1
    duplicate = client.post("/api/admin/sources/upload", files={"file": ("重命名.mov", raw)})
    assert duplicate.json() == {"source_id": sid, "duplicate": True}
    assert len(Store(config.data_dir).sources()) == 1
    assert client.get(f"/api/admin/sources/{sid}").json()["original_name"] == "中文原片.mp4"
    for raw in (b"", b"broken video"):
        assert client.post("/api/admin/sources/upload", files={"file": ("失败.mp4", raw)}).status_code == 422
    config.source_upload_max_bytes = 10
    assert (
        client.post("/api/admin/sources/upload", files={"file": ("大文件.mp4", b"x" * 11)}).status_code == 413
    )
    assert not list((config.data_dir / "sources" / ".uploads").iterdir())
    assert not list(config.data_dir.rglob("*.partial"))
    # 中途读取失败也清理已经写入的临时文件。
    calls = 0

    async def interrupted(self, size):
        nonlocal calls
        calls += 1
        if calls == 1:
            return b"partial"
        raise RuntimeError("client disconnected")

    monkeypatch.setattr(UploadFile, "read", interrupted)
    from mindex.admin import admin_router

    store = Store(config.data_dir)
    router = next(
        r
        for r in admin_router(store, RuntimeConfig(config, store)).routes
        if getattr(r, "path", "") == "/api/admin/sources/upload"
    )
    with pytest.raises(RuntimeError, match="disconnected"):
        asyncio.run(router.endpoint(UploadFile(filename="中断.mp4", file=io.BytesIO(b""))))
    assert not list((config.data_dir / "sources" / ".uploads").iterdir())


def test_pipeline_tasks_frozen_layout_prerequisites_resume_and_publish_only(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.register(video_file)["source_id"]
    runtime = RuntimeConfig(config, pipeline.store)
    manager = SourceTasks(runtime, pipeline.store, FixtureEmbedder(config))
    with pytest.raises(Exception) as error:
        manager.start(sid, "analysis", WorkOptions())
    assert error.value.status_code == 409
    original_layout = config.layout_key
    config.video.window_seconds = 6
    task = finished(manager, manager.start(sid, "shots", WorkOptions(publish=False)))
    assert task["status"] == "complete", task
    assert (
        pipeline.store.one("SELECT layout_key FROM sources WHERE id=?", (sid,))["layout_key"]
        == original_layout
    )
    ids = [s["id"] for s in pipeline.store.segments(sid)]
    clip = pipeline.store.root / pipeline.store.segments(sid)[0]["path"]
    clip.unlink()
    assert finished(manager, manager.start(sid, "shots", WorkOptions(publish=False)))["status"] == "complete"
    assert clip.is_file() and [s["id"] for s in pipeline.store.segments(sid)] == ids
    fail = [True]
    real_publish = __import__("mindex.source_tasks", fromlist=["publish_work"]).publish_work

    def publish(*args, **kwargs):
        if fail[0]:
            raise RuntimeError("embedding unavailable")
        return real_publish(*args, **kwargs)

    monkeypatch.setattr("mindex.source_tasks.publish_work", publish)
    task = finished(manager, manager.start(sid, "shots", WorkOptions()))
    assert task["status"] == "complete" and task["publication"]["status"] == "failed"
    assert task["result"]["processing_status"] == "complete"
    fail[0] = False
    monkeypatch.setattr(Pipeline, "split", lambda *a, **kw: pytest.fail("仅重试发布不重跑切片"))
    retried = finished(manager, manager.retry(task["id"]))
    assert retried["status"] == "complete" and retried["publication"]["status"] == "complete"
    assert not check_index(pipeline.store, pipeline.store.active_index()["id"])
    # 发布阶段中断时结果已经落库，恢复仅发布。
    pipeline.store.execute("UPDATE source_tasks SET status='running' WHERE id=?", (task["id"],))
    restarted = SourceTasks(runtime, pipeline.store, FixtureEmbedder(config))
    assert restarted.get(task["id"])["status"] == "interrupted"
    assert finished(restarted, restarted.retry(task["id"]))["status"] == "complete"


@pytest.mark.parametrize("work", ["shots", "publish"])
def test_embedding_progress_persisted_in_api_and_resumed_checkpoints(config, video_file, tmp_path, work):
    from queue import Queue

    from mindex.adapters import ModelError
    from mindex.media import cut

    pipeline = Pipeline(config)
    other = tmp_path / "other.mp4"
    cut(video_file, 0, 1, other)
    other_id = pipeline.prepare(other)
    old = IndexBuilder(config, FixtureEmbedder(config)).build(source_id=other_id, activate=True)
    sid = pipeline.register(video_file)["source_id"]
    if work == "publish":
        pipeline.detect_source(sid)
        pipeline.split(sid)
    calls = Queue()
    release = threading.Semaphore(0)

    class ControlledEmbedder(FixtureEmbedder):
        attempts = 0

        def video(self, path):
            self.attempts += 1
            calls.put(self.attempts)
            assert release.acquire(timeout=10)
            if self.attempts == 3:
                raise ModelError("embedding failed")
            return super().video(path)

    client = TestClient(create_app(config, ControlledEmbedder(config)))
    options = {"work": "shots"} if work == "publish" else {}
    try:
        response = client.post(f"/api/admin/sources/{sid}/{work}", json=options)
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        task_url = f"/api/admin/source-tasks/{task_id}"
        assert calls.get(timeout=5) == 1
        total = len(pipeline.store.segments(sid, "moment"))
        assert total > 2

        def assert_progress(completed):
            task = client.get(task_url).json()
            assert task["status"] == "running"
            assert task["progress"]["stage"] == "embedding"
            assert task["progress"]["completed"] == completed
            assert task["progress"]["total"] == total  # excludes the retained other source
            assert task["progress"]["unit"] == "segments"
            assert task["progress"]["phase"] == "publish"
            assert task["publication"]["progress"]["completed"] == completed
            if work == "shots":
                processing = task["progress"]["processing_progress"]
                assert processing["phase"] == "process" and processing["stage"] == "split"
                assert processing["completed"] == processing["total"]
            source = client.get(f"/api/admin/sources/{sid}").json()
            key = "publish_task" if work == "publish" else "task"
            assert source["work"]["shots"][key]["progress"] == task["progress"]
            assert source["work"]["shots"]["search"]["indexed_segments"] == 0
            assert pipeline.store.active_index()["id"] == old

        assert_progress(0)
        release.release()
        assert calls.get(timeout=5) == 2
        assert_progress(1)
        release.release()
        assert calls.get(timeout=5) == 3
        assert_progress(2)
        release.release()
        for _ in range(500):
            task = client.get(task_url).json()
            if task["publication"]["status"] == "failed" and task["status"] != "running":
                break
            time.sleep(0.01)
        assert task["progress"]["completed"] == 2
        assert task["publication"]["status"] == "failed"
        assert task["status"] == ("failed" if work == "publish" else "complete")
        retry = client.post(task_url + "/retry")
        assert retry.status_code == 202, retry.text
        assert calls.get(timeout=5) == 4
        assert_progress(2)  # resumes at the durable count, without resetting to zero
        for _ in range(total):
            release.release()
        for _ in range(500):
            task = client.get(task_url).json()
            if task["status"] == "complete" and task["publication"]["status"] == "complete":
                break
            time.sleep(0.01)
        assert task["status"] == "complete", task
        assert task["publication"]["status"] == "complete"
        assert task["progress"]["stage"] == "published"
        assert task["progress"]["completed"] == task["progress"]["total"] == total
        if work == "shots":
            assert task["progress"]["processing_progress"]["stage"] == "split"
    finally:
        for _ in range(50):
            release.release()


def test_busy_frozen_analysis_config_failure_and_retry(config, video_file, monkeypatch):
    config.enforce_gpu_lock = True
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    entered, release = threading.Event(), threading.Event()
    seen = []

    def analyze(self, source_id, progress):
        seen.append(self.config.analyzer.model)
        progress({"stage": "analyze", "completed": 1, "total": 2})
        entered.set()
        assert release.wait(5)
        if len(seen) == 1:
            raise RuntimeError("upstream failed")

    monkeypatch.setattr(Pipeline, "analyze", analyze)
    frozen = config.analyzer.model
    task = manager.start(sid, "analysis", WorkOptions(publish=False))
    assert entered.wait(2)
    assert manager.get(task["id"])["progress"]["completed"] == 1
    with pytest.raises(Exception) as error:
        manager.start(sid, "shots", WorkOptions())
    assert error.value.status_code == 409
    config.analyzer.model = "changed"
    release.set()
    assert finished(manager, task)["status"] == "failed"
    assert finished(manager, manager.retry(task["id"]))["status"] == "complete"
    assert seen == [frozen, frozen]
    with pipeline.lock():
        pass


def test_analysis_task_recovers_invalid_response_without_losing_checkpoints(config, video_file, monkeypatch):
    import json
    from types import SimpleNamespace

    from mindex import adapters

    config.analyzer.model = "MiniMax-test"
    config.analyzer.llama_template = False
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    seen, delays = [], []

    def wire(method, url, **kwargs):
        body = kwargs["json"]
        seen.append(body)
        if "将连续影视片段" in body["messages"][0]["content"]:
            units = json.loads(body["messages"][1]["content"][0]["text"])
            text = json.dumps({"groups": [list(range(len(units)))]})
        elif len(seen) in {2, 3}:
            text = '{"description":'
        else:
            text = '{"description":"机场大厅"}'
        return {"choices": [{"message": {"content": text}}]}

    monkeypatch.setattr(adapters, "request", wire)
    monkeypatch.setattr(adapters, "frame_data", lambda path, count: [(0, "data:image/jpeg;base64," + path.stem)])
    monkeypatch.setattr(adapters, "time", SimpleNamespace(sleep=delays.append))
    checkpoint = pipeline.store.segments(sid, "moment")[0]
    pipeline._annotate(checkpoint)
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    task = finished(manager, manager.start(sid, "analysis", WorkOptions(publish=False)))
    assert task["status"] == "complete", task
    assert task["publication"]["status"] == "not_requested"
    assert delays == [1, 2]
    assert seen[1]["messages"] == seen[2]["messages"] == seen[3]["messages"]
    assert [body["max_tokens"] for body in seen[1:4]] == [12768, 25536, 51072]
    assert all(body != seen[0] for body in seen[1:])  # completed checkpoint was not requested again
    assert pipeline.store.one("SELECT COUNT(*) n FROM annotations")["n"] == len(pipeline.store.segments(sid))


def test_scene_grouping_failure_is_reported_after_segments_complete_and_retry_skips_them(
    config, video_file, monkeypatch
):
    import json
    from types import SimpleNamespace

    from mindex import adapters

    config.analyzer.response_retries = 1
    config.analyzer.model = "GLM-test"
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    for s in pipeline.store.segments(sid):
        pipeline.store.put_json("annotations", "segment_id", s["id"], config.analysis_key,
                                {"annotation": {"description": "机场大厅"}})
    base_count = len(pipeline.store.segments(sid))
    calls = []
    recovered = [False]

    def wire(method, url, **kwargs):
        body = kwargs["json"]
        calls.append(body)
        if "将连续影视片段" in body["messages"][0]["content"]:
            if not recovered[0]:
                return {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "推理"}}]}
            units = json.loads(body["messages"][1]["content"][0]["text"])
            text = json.dumps({"groups": [list(range(len(units)))]})
        else:
            text = '{"description":"机场场景"}'
        return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}

    monkeypatch.setattr(adapters, "request", wire)
    monkeypatch.setattr(adapters, "frame_data", lambda *a: [(0, "data:image/jpeg;base64,TEST")])
    monkeypatch.setattr(adapters, "time", SimpleNamespace(sleep=lambda _: None))
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    task = finished(manager, manager.start(sid, "analysis", WorkOptions(publish=False)))
    assert task["status"] == "failed"
    assert "finish_reason=length" in task["error"]
    assert task["progress"]["stage"] == "scene_grouping"
    assert task["progress"]["completed"] == 0
    assert task["progress"]["total"] == len(pipeline.store.segments(sid, "moment"))
    assert pipeline.store.one("SELECT COUNT(*) n FROM annotations")["n"] == base_count
    assert [c["max_tokens"] for c in calls] == [12768, 25536]
    recovered[0] = True
    result = finished(manager, manager.retry(task["id"]))
    assert result["status"] == "complete", result
    assert pipeline.store.one("SELECT data FROM scene_plans WHERE source_id=?", (sid,))
    assert len(calls) == 4  # two failures, grouping success, one new scene analysis


@pytest.mark.parametrize("frozen_cap,expected_cap", [(None, 131072), (16384, 16384)])
def test_legacy_analysis_snapshot_uses_new_defaults_and_preserves_frozen_cap(config, frozen_cap, expected_cap):
    import json

    from mindex.source_tasks import freeze_config, restore_config

    store = Store(config.data_dir)
    runtime = RuntimeConfig(config, store)
    snapshot = json.loads(freeze_config(config, store))
    snapshot["analyzer"].pop("initial_output_tokens")
    snapshot["analyzer"].pop("max_output_tokens")
    if frozen_cap is not None:
        snapshot["analyzer"]["max_output_tokens"] = frozen_cap
    restored = restore_config(json.dumps(snapshot), runtime, store)
    assert restored.analyzer.initial_output_tokens == 12768
    assert restored.analyzer.max_output_tokens == expected_cap
    assert restored.analysis_key == config.analysis_key


def test_scene_grouping_retry_reuses_successful_batch_and_limits_dense_input(config, video_file):
    from mindex.types import Annotation

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    template = pipeline.store.segments(sid, "moment")[0]
    pipeline.store.execute("DELETE FROM segments WHERE source_id=? AND kind='moment'", (sid,))
    for number in range(40):
        pipeline.store.execute(
            "INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,parent_id,reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"dense-{number}", sid, "moment", number / 10, (number + 1) / 10,
             template["path"], template["thumbnail"], template["generation"], template["parent_id"], "fixture"),
        )
    for segment in pipeline.store.segments(sid):
        pipeline.store.put_json("annotations", "segment_id", segment["id"], config.analysis_key,
                                {"annotation": {"description": "机场花园", "scene": ["花园"]}})

    class Analyzer:
        def __init__(self):
            self.calls = []
            self.fail = True

        def group_scenes(self, units):
            self.calls.append(len(units))
            if len(units) == 8 and self.fail:
                raise RuntimeError("second batch failed")
            return [list(range(len(units)))]

        def analyze(self, path, dialogue):
            return Annotation(description="机场场景")

    analyzer = Analyzer()
    pipeline._analyzer = analyzer
    with pytest.raises(RuntimeError, match="second batch failed"):
        pipeline.analyze(sid)
    assert analyzer.calls == [32, 8]
    analyzer.fail = False
    pipeline.analyze(sid)
    assert analyzer.calls == [32, 8, 8]  # successful first batch not sent again
    assert pipeline.store.json_value("scene_plans", "source_id", sid, config.analysis_key)


def test_full_source_flow_incremental_publish_retrieval_and_details(
    config, video_file, tmp_path, monkeypatch
):
    from test_persons import FakeClient, Tracker, photo

    from mindex.media import command
    from mindex.person_processing import PersonOptions, PersonProcessor, PersonTasks
    from mindex.persons import Entity, Persons

    embedder = FixtureEmbedder(config)
    monkeypatch.setattr("mindex.pipeline.make_analyzer", lambda _: FixtureAnalyzer(embedder))
    client = TestClient(create_app(config, embedder))
    pipeline = Pipeline(config)
    runtime = RuntimeConfig(config, pipeline.store)
    manager = SourceTasks(runtime, pipeline.store, embedder)
    # 先发布另一素材及 shot/moment 粒度，后续必须完整保留。
    other = tmp_path / "other.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=2:d=1", str(other)])
    other_id = pipeline.prepare(other)
    original = IndexBuilder(config, embedder).build(kinds=["shot", "moment"], activate=True)
    old_members = {s["id"] for s in pipeline.store.snapshots(original)}
    sid = client.post(
        "/api/admin/sources/upload", files={"file": ("隔离素材.mp4", video_file.read_bytes())}
    ).json()["source_id"]
    assert finished(manager, manager.start(sid, "shots", WorkOptions()))["status"] == "complete"
    dm = DialogueTasks(runtime, pipeline.store, embedder)
    assert finished(dm, dm.start(sid, DialogueOptions(method="asr")))["status"] == "complete"
    assert finished(manager, manager.start(sid, "analysis", WorkOptions()))["status"] == "complete"
    setup = client.get(f"/api/admin/sources/{sid}/person-setup").json()
    assert not setup["ready"] and setup["missing"]
    catalogue = Persons(pipeline.store)
    work = catalogue.save(Entity(kind="work", name="工程作品"))
    actor = catalogue.save(Entity(kind="actor", name="工程演员"))
    catalogue.save(Entity(kind="role", name="工程角色", actor_ids=[actor["id"]], work_ids=[work["id"]]))
    catalogue.add_reference(actor["id"], photo())
    assert client.get(f"/api/admin/sources/{sid}/person-setup?work_id={work['id']}").json()["ready"]
    monkeypatch.setattr(
        "mindex.person_processing.PersonProcessor", lambda cfg: PersonProcessor(cfg, FakeClient(), Tracker)
    )
    pm = PersonTasks(runtime, pipeline.store)
    assert (
        finished(pm, pm.start(sid, PersonOptions(work_id=work["id"], publish=False)))["status"] == "complete"
    )
    result = client.post(f"/api/admin/sources/{sid}/publish", json={"work": "persons"})
    assert result.status_code == 202, result.text
    for _ in range(1000):
        task = client.get(f"/api/admin/source-tasks/{result.json()['id']}").json()
        if task["status"] in {"complete", "failed"}:
            break
        time.sleep(0.01)
    assert task["status"] == "complete", task
    source = client.get(f"/api/admin/sources/{sid}").json()
    assert source["work"]["shots"]["status"] == "complete"
    assert source["work"]["dialogue"]["status"] == "no_audio"
    assert source["work"]["analysis"]["status"] == "complete"
    assert source["work"]["persons"]["status"] == "complete"
    assert source["work"]["persons"]["search"]["current"]
    active = pipeline.store.active_index()
    assert old_members <= {s["id"] for s in pipeline.store.snapshots(active["id"])}
    # 新素材默认 moment，已有其他素材粒度保留。
    assert {s["kind"] for s in pipeline.store.snapshots(active["id"]) if s["source_id"] == other_id} == {
        "shot",
        "moment",
    }
    response = client.post("/api/search/text", json={"text": "绿色画面", "source_id": sid})
    assert response.status_code == 200 and response.json()["results"]
    shot = pipeline.store.segments(sid, "shot")[0]
    details = client.get(f"/api/admin/segments/{shot['id']}/details").json()
    assert details["llm"]["annotation"] and details["person"]["status"] == "processed"
    assert not check_index(pipeline.store, active["id"])


def test_dialogue_manual_methods_and_prior_success_survives_failure(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    processor = DialogueProcessor(config)
    track = {"index": 1, "text": True, "codec": "subrip", "language": "zho", "forced": False, "default": True}
    monkeypatch.setattr("mindex.dialogue.subtitle_tracks", lambda _: [track])
    monkeypatch.setattr("mindex.dialogue.extract_subtitles", lambda *_: [])
    result = processor.process(sid, method="subtitle", subtitle_stream=1)["sources"][0]
    assert result["status"] == "empty" and result["source"] == "subtitle" and not result["fallback_reason"]
    with pytest.raises(ValueError, match="必须选择"):
        processor.process(sid, method="subtitle")
    with pytest.raises(ValueError, match="不存在"):
        processor.process(sid, method="subtitle", subtitle_stream=9)
    monkeypatch.setattr("mindex.dialogue.subtitle_tracks", lambda _: pytest.fail("手动 ASR 跳过字幕"))
    result = processor.process(sid, method="asr")["sources"][0]
    assert result["status"] == "no_audio" and result["source"] == "asr" and result["fallback_reason"] is None
    prior = {
        "status": "complete",
        "source": "asr",
        "utterances": [{"start": 0, "end": 1, "text": "已有对白"}],
    }
    processor.save(sid, "old", prior)
    processor.save(sid, "new", {"status": "failed", "utterances": [], "error": "upstream"})
    assert current_dialogue(pipeline.store, sid)["utterances"] == prior["utterances"]


def test_dialogue_publication_failure_retry_without_processing(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    manager = DialogueTasks(RuntimeConfig(config, pipeline.store), pipeline.store, FixtureEmbedder(config))
    monkeypatch.setattr(
        "mindex.source_tasks.publish_work", lambda *_, **kw: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    task = finished(manager, manager.start(sid, DialogueOptions()))
    assert task["status"] == "complete" and task["publication"]["status"] == "failed"
    monkeypatch.setattr(
        "mindex.dialogue_tasks.execute_dialogue", lambda *a, **kw: pytest.fail("不重复对白处理")
    )
    monkeypatch.setattr("mindex.source_tasks.publish_work", lambda *_, **kw: {"index_id": "retry-only-publish"})
    result = finished(manager, manager.retry(task["id"]))
    assert result["status"] == "complete" and result["result"]["index_id"] == "retry-only-publish"


def test_person_first_publish_and_publication_failure_retry(config, video_file, monkeypatch):
    from test_persons import FakeClient, Tracker, photo

    from mindex.person_processing import PersonOptions, PersonProcessor, PersonTasks
    from mindex.persons import Entity, Persons
    from mindex.source_tasks import publish_work

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    embedder = FixtureEmbedder(config)
    catalogue = Persons(pipeline.store)
    work_id = catalogue.work_for(sid)
    actor = catalogue.save(Entity(kind="actor", name="工程人物"))
    catalogue.save(Entity(kind="role", name="工程角色", work_ids=[work_id], actor_ids=[actor["id"]]))
    catalogue.add_reference(actor["id"], photo())
    monkeypatch.setattr(
        "mindex.person_processing.PersonProcessor", lambda cfg: PersonProcessor(cfg, FakeClient(), Tracker)
    )
    manager = PersonTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    real_publish = publish_work
    fail = [True]

    def publish(config, source_id, work, *_, **kwargs):
        if fail[0]:
            raise RuntimeError("publication unavailable")
        return real_publish(config, source_id, work, embedder, **kwargs)

    monkeypatch.setattr("mindex.source_tasks.publish_work", publish)
    task = finished(manager, manager.start(sid, PersonOptions(work_id=work_id, publish=True)))
    assert task["status"] == "complete" and task["publication"]["status"] == "failed"
    assert pipeline.store.active_index() is None
    monkeypatch.setattr(
        "mindex.person_processing.PersonProcessor", lambda *_: pytest.fail("仅发布，不重复人物识别")
    )
    # 已保存识别结果的发布重试不再依赖参考图，也不重复资料预检。
    pipeline.store.execute("DELETE FROM person_references")
    fail[0] = False
    retried = finished(manager, manager.retry(task["id"]))
    assert retried["status"] == "complete" and retried["publication"]["status"] == "complete"
    assert pipeline.store.active_index() and retried["result"]["release"]


def test_strict_subtitle_failure_never_asr_and_empty_auto_fallback(config, video_file, monkeypatch):
    from mindex.media import MediaError

    sid = Pipeline(config).prepare(video_file)
    processor = DialogueProcessor(config)
    track = {"index": 1, "text": True, "codec": "subrip", "language": "zho", "forced": False, "default": True}
    monkeypatch.setattr("mindex.dialogue.subtitle_tracks", lambda _: [track])
    monkeypatch.setattr(
        "mindex.dialogue.extract_subtitles", lambda *_: (_ for _ in ()).throw(MediaError("failed"))
    )
    with pytest.raises(MediaError):
        processor.process(sid, method="subtitle", subtitle_stream=1)
    assert not processor.store.one("SELECT 1 FROM channel_data")
    monkeypatch.setattr("mindex.dialogue.extract_subtitles", lambda *_: [])
    assert processor.process(sid, method="subtitle", subtitle_stream=1)["sources"][0]["status"] == "empty"
    # 空字幕之前手动提取成功，自动方式仍应回退，而不是复用为空的字幕。
    result = processor.process(sid, method="auto")["sources"][0]
    assert result["source"] == "asr" and result["status"] == "no_audio" and result["fallback_reason"]


def test_result_and_recent_failure_are_independent(config, video_file):
    from mindex.curation import Library

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    pipeline.store.execute(
        "INSERT INTO source_tasks(id,source_id,work,status,config,options,error) VALUES(?,?,?,?,?,?,?)",
        ("failed-latest", sid, "shots", "failed", "{}", "{}", "latest repair failed"),
    )
    source = Library(pipeline.store).source(sid)
    assert source["work"]["shots"]["status"] == "complete"
    assert source["work"]["shots"]["task"]["status"] == "failed"
    assert source["work"]["shots"]["task"]["error"] == "latest repair failed"
