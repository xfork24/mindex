import json

import pytest

from mindex.shared.config import Config
from mindex.shared.processing_lock import PublicationLock
from mindex.shared.storage import Store
from mindex.shared.task_cancel import CancelRegistry, TaskCancelled, registry_for
from mindex.shared.task_progress import reset_task_attempt, save_task_progress


def test_request_check_clear(tmp_path):
    registry = CancelRegistry(tmp_path)
    assert not registry.requested("a1")
    registry.check("a1")
    registry.request("a1")
    assert registry.requested("a1")
    assert (tmp_path / "locks" / "cancel" / "a1").is_file()
    with pytest.raises(TaskCancelled, match="已取消"):
        registry.check("a1")
    assert not registry.requested("b2")
    registry.clear("a1")
    assert not registry.requested("a1")
    assert not (tmp_path / "locks" / "cancel" / "a1").exists()
    registry.clear("a1")  # 幂等


def test_sentinel_crosses_instances(tmp_path):
    """新实例模拟另一个进程：只能经哨兵文件看到取消。"""
    CancelRegistry(tmp_path).request("task")
    other = CancelRegistry(tmp_path)
    assert other.requested("task")
    other.clear("task")
    assert not CancelRegistry(tmp_path).requested("task")


def test_in_process_event_survives_missing_sentinel(tmp_path):
    registry = CancelRegistry(tmp_path)
    registry.request("task")
    registry.sentinel("task").unlink()
    assert registry.requested("task")


def test_key_is_sanitized(tmp_path):
    registry = CancelRegistry(tmp_path)
    registry.request("../evil/key")
    assert registry.sentinel("../evil/key").parent == tmp_path / "locks" / "cancel"
    assert registry.requested("../evil/key")
    with pytest.raises(ValueError):
        registry.sentinel("..")


def test_registry_for_is_singleton_per_data_dir(tmp_path):
    assert registry_for(tmp_path) is registry_for(tmp_path / "." )
    assert registry_for(tmp_path) is not registry_for(tmp_path / "other")
    assert not issubclass(TaskCancelled, ValueError)


@pytest.mark.parametrize("table", ["source_tasks", "dialogue_tasks", "person_tasks"])
def test_save_task_progress_raises_after_persisting(tmp_path, table):
    store = Store(tmp_path)
    if table == "source_tasks":
        store.execute("INSERT INTO source_tasks(id,source_id,work,status,config,options) "
                      "VALUES('t','s','analysis','running','{}','{}')")
    else:
        store.execute(f"INSERT INTO {table}(id,source_id,status,config,options) VALUES('t','s','running','{{}}','{{}}')")
    save_task_progress(store, table, "t", {"stage": "analyze", "completed": 1, "total": 3})
    registry = registry_for(store.root)
    registry.request("t")
    try:
        with pytest.raises(TaskCancelled):
            save_task_progress(store, table, "t", {"stage": "analyze", "completed": 2, "total": 3})
        progress = json.loads(store.one(f"SELECT progress FROM {table} WHERE id='t'")["progress"])
        assert progress["completed"] == 2
        # 激活之后的收尾进度不再中断。
        save_task_progress(store, table, "t", {"stage": "published", "phase": "publish"})
    finally:
        registry.clear("t")


def test_publication_admit_checks_while_waiting(tmp_path):
    config = Config(data_dir=tmp_path, enforce_gpu_lock=True)
    holder = PublicationLock(config, source_id="s", work="publish.shots.moment")
    holder.pipeline.acquire()
    waiter = PublicationLock(config, source_id="s2", work="publish.shots.moment")
    calls = []

    def check():
        calls.append(1)
        if len(calls) >= 3:
            raise TaskCancelled()

    try:
        with pytest.raises(TaskCancelled):
            waiter.admit(check)
        assert len(calls) == 3
        assert not waiter.pipeline.is_locked
    finally:
        holder.release()
    waiter.admit(lambda: None)
    assert waiter.pipeline.is_locked
    waiter.release()


# ---- 任务取消接口与执行器（Task 7.2） ----

import threading  # noqa: E402
import time  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def settled(manager, task_id):
    for _ in range(1000):
        value = manager.get(task_id)
        if value["status"] in {"complete", "failed", "interrupted", "cancelled"} and task_id not in manager.threads:
            return value
        time.sleep(0.01)
    pytest.fail("后台任务未结束")


def insert_task(store, table, task_id, status):
    if table == "source_tasks":
        store.execute("INSERT INTO source_tasks(id,source_id,work,status,config,options) "
                      "VALUES(?,'s','analysis',?,'{}','{}')", (task_id, status))
    else:
        store.execute(f"INSERT INTO {table}(id,source_id,status,config,options) VALUES(?,'s',?,'{{}}','{{}}')",
                      (task_id, status))


def test_cancel_endpoints_queued_terminal_and_unknown(config):
    from mindex.admin.dialogue_tasks import dialogue_router
    from mindex.admin.source_tasks import source_tasks_router
    from mindex.persons.person_api import person_router
    from mindex.settings.runtime_settings import RuntimeConfig

    store = Store(config.data_dir)
    runtime = RuntimeConfig(config, store)
    app = FastAPI()
    for router in (source_tasks_router(runtime, store), dialogue_router(runtime, store),
                   person_router(runtime, store)):
        app.include_router(router)
    client = TestClient(app)
    routes = {"source_tasks": "/api/admin/source-tasks/{}/cancel",
              "dialogue_tasks": "/api/admin/dialogue-tasks/{}/cancel",
              "person_tasks": "/api/persons/tasks/{}/cancel"}
    registry = registry_for(store.root)
    for table, route in routes.items():
        insert_task(store, table, table + "-q", "queued")
        insert_task(store, table, table + "-done", "complete")
        response = client.post(route.format(table + "-q"))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"
        assert response.json()["error"] == "已取消"
        assert registry.requested(table + "-q")
        # 已取消与已完成都不是可取消状态。
        assert client.post(route.format(table + "-q")).status_code == 409
        assert client.post(route.format(table + "-done")).status_code == 409
        assert not registry.requested(table + "-done")
        assert client.post(route.format("missing")).status_code == 404
        registry.clear(table + "-q")


def test_cancelled_while_queued_never_starts_work(config):
    """排队时取消：线程启动后既不置 running 也不执行，锁全部释放。"""
    from mindex.admin.dialogue_tasks import DialogueTasks
    from mindex.persons.person_processing import PersonOptions, PersonTasks
    from mindex.settings.runtime_settings import RuntimeConfig

    store = Store(config.data_dir)
    runtime = RuntimeConfig(config, store)
    dialogue, persons = DialogueTasks(runtime, store), PersonTasks(runtime, store)
    insert_task(store, "dialogue_tasks", "d1", "queued")
    insert_task(store, "person_tasks", "p1", "queued")
    dialogue.cancel("d1")
    persons.cancel("p1")
    lock = dialogue.lock("s").acquire()
    dialogue._run("d1", config, lock)
    lock = persons.lock("s").acquire()
    persons.run("p1", config, "s", PersonOptions(), lock)
    for manager, identifier in ((dialogue, "d1"), (persons, "p1")):
        row = manager.get(identifier)
        assert row["status"] == "cancelled" and row["error"] == "已取消"
        assert row["result"] is None
        assert not registry_for(store.root).requested(identifier)
    # 锁已释放：可以立刻再次获取。
    with dialogue.lock("s"), persons.lock("s"):
        pass


def test_running_publication_cancels_at_next_progress_then_retries(config, video_file, monkeypatch):
    from mindex.admin.source_tasks import PublishOptions, SourceTasks
    from mindex.processing.pipeline import Pipeline
    from mindex.settings.runtime_settings import RuntimeConfig

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    entered, proceed = threading.Event(), threading.Event()
    calls = []

    def publish(config, source_id, owner, *args, scope="moment", progress=None, **kwargs):
        calls.append(scope)
        progress({"stage": "embedding", "completed": 0, "total": 2})
        if len(calls) == 1:
            entered.set()
            assert proceed.wait(10)
        progress({"stage": "embedding", "completed": 1, "total": 2})
        return {"index_id": "fixture"}

    monkeypatch.setattr("mindex.admin.source_tasks.publish_work", publish)
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    task = manager.start(sid, "publish", PublishOptions(work="shots"))
    try:
        assert entered.wait(5)
        running = manager.cancel(task["id"])
        assert running["status"] == "running"  # 运行中只设标记，等待下一检查点
    finally:
        proceed.set()
    value = settled(manager, task["id"])
    assert value["status"] == "cancelled" and value["error"] == "已取消"
    assert value["publication"]["status"] == "cancelled"
    assert value["progress"]["completed"] == 1
    assert not registry_for(pipeline.store.root).requested(task["id"])
    with pytest.raises(Exception) as conflict:
        manager.cancel(task["id"])
    assert conflict.value.status_code == 409
    # cancelled 可重试，且不会被上次的取消标记立即中断。
    registry_for(pipeline.store.root).request(task["id"])
    manager.retry(task["id"])
    value = settled(manager, task["id"])
    assert value["status"] == "complete", value
    assert value["publication"]["status"] == "complete"
    assert len(calls) == 2


def test_publication_waiting_for_gpu_can_be_cancelled(config, video_file, monkeypatch):
    from mindex.admin.source_tasks import PublishOptions, SourceTasks
    from mindex.processing.pipeline import Pipeline
    from mindex.settings.runtime_settings import RuntimeConfig
    from mindex.shared.processing_lock import ProcessingLock

    config.enforce_gpu_lock = True
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    monkeypatch.setattr("mindex.admin.source_tasks.publish_work",
                        lambda *a, **k: pytest.fail("取消的排队任务不应开始发布"))
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    with ProcessingLock(config):  # 占住 GPU，发布子任务停在准入等待
        task = manager.start(sid, "publish", PublishOptions(work="shots"))
        time.sleep(0.3)
        assert manager.get(task["id"])["status"] == "queued"
        assert manager.cancel(task["id"])["status"] == "cancelled"
        value = settled(manager, task["id"])
    assert value["status"] == "cancelled" and value["error"] == "已取消"
    with manager.lock(sid, "publish", PublishOptions(work="shots")):
        pass


# ---- 运行中取消与断点续跑（Task 7.4） ----


class CheckpointWorker:
    """测试桩：逐单元处理并先落检查点再上报进度，复刻真实管线“检查点→进度”的顺序。

    首次尝试在第一个单元落检查点后停在闸门上，等测试请求取消；随后的进度上报即
    取消检查点。重试只处理检查点里没有的单元，可据此断言续跑不重做已完成部分。
    """

    def __init__(self, units=("u1", "u2", "u3"), stage="analyze"):
        self.units, self.stage = list(units), stage
        self.done: list[str] = []
        self.processed: list[str] = []
        self.attempts = 0
        self.entered, self.proceed = threading.Event(), threading.Event()

    def __call__(self, progress):
        self.attempts += 1
        for unit in self.units:
            if unit in self.done:
                continue
            self.processed.append(unit)
            self.done.append(unit)
            if self.attempts == 1 and len(self.done) == 1:
                self.entered.set()
                assert self.proceed.wait(10)
            progress({"stage": self.stage, "completed": len(self.done), "total": len(self.units)})

    def cancel_while_running(self, manager, task_id):
        try:
            assert self.entered.wait(5)
            assert manager.cancel(task_id)["status"] == "running"  # 运行中只设标记
        finally:
            self.proceed.set()
        value = settled(manager, task_id)
        assert value["status"] == "cancelled" and value["error"] == "已取消", value
        # 取消发生在第二次进度上报：已落盘的进度保留第一单元之后的检查点。
        assert value["progress"]["completed"] == 1
        assert value["result"] is None
        assert self.done == self.units[:1]
        return value

    def assert_resumed(self, value):
        assert value["status"] == "complete", value
        assert self.attempts == 2
        assert self.processed == self.units  # 每个单元只处理一次
        assert value["progress"]["completed"] == len(self.units)


def test_running_analysis_cancels_between_progress_and_resumes(config, video_file, monkeypatch):
    from mindex.admin.source_tasks import SourceTasks, WorkOptions
    from mindex.processing.pipeline import Pipeline
    from mindex.settings.runtime_settings import RuntimeConfig

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    worker = CheckpointWorker()
    monkeypatch.setattr(Pipeline, "analyze", lambda self, source_id=None, progress=None: worker(progress))
    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    task = manager.start(sid, "analysis", WorkOptions())
    worker.cancel_while_running(manager, task["id"])
    assert not registry_for(pipeline.store.root).requested(task["id"])
    # 锁已释放，且 cancelled 任务可重试。
    value = settled(manager, manager.retry(task["id"])["id"])
    worker.assert_resumed(value)
    assert value["result"] == {"processing_status": "complete"}
    assert value["publication"] == {"status": "not_requested"}


def test_running_dialogue_cancels_between_progress_and_resumes(config, video_file, monkeypatch):
    from mindex.admin.dialogue_tasks import DialogueOptions, DialogueTasks
    from mindex.processing.pipeline import Pipeline
    from mindex.settings.runtime_settings import RuntimeConfig

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    worker = CheckpointWorker(stage="transcribing")

    def enrich(self, source_id=None, progress=None, **options):
        worker(progress)
        return {"sources": [], "elapsed_seconds": 0}

    monkeypatch.setattr(Pipeline, "enrich_dialogue", enrich)
    manager = DialogueTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    task = manager.start(sid, DialogueOptions())
    worker.cancel_while_running(manager, task["id"])
    value = settled(manager, manager.retry(task["id"])["id"])
    worker.assert_resumed(value)
    assert value["result"]["sources"] == []
    assert value["publication"] == {"status": "not_requested"}


def test_running_person_task_cancels_between_progress_and_resumes(config, video_file, monkeypatch):
    from mindex.persons.person_processing import PersonOptions, PersonProcessor, PersonTasks
    from mindex.processing.pipeline import Pipeline
    from mindex.settings.runtime_settings import RuntimeConfig

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    worker = CheckpointWorker(stage="extracting")

    def run(self, source_id, options, progress=None):
        worker(progress)
        return {"source_id": source_id, "shots": len(worker.units)}

    monkeypatch.setattr(PersonProcessor, "run", run)
    manager = PersonTasks(RuntimeConfig(config, pipeline.store), pipeline.store)
    task = manager.start(sid, PersonOptions())
    worker.cancel_while_running(manager, task["id"])
    value = settled(manager, manager.retry(task["id"])["id"])
    worker.assert_resumed(value)
    assert value["result"] == {"source_id": sid, "shots": 3}


def test_index_build_cancelled_keeps_active_index_and_resumes(config, video_file, tmp_path):
    """发布中途取消：活动索引不变、候选版本保持 building 可续建，重试复用已写向量。"""
    from conftest import FixtureEmbedder

    from mindex.admin.source_tasks import PublishOptions, SourceTasks
    from mindex.processing.indexing import IndexBuilder, check_index
    from mindex.processing.pipeline import Pipeline
    from mindex.settings.runtime_settings import RuntimeConfig
    from mindex.shared.media import cut

    pipeline = Pipeline(config)
    other = tmp_path / "other.mp4"
    cut(video_file, 0, 1, other)
    old = IndexBuilder(config, FixtureEmbedder(config)).build(source_id=pipeline.prepare(other), activate=True)
    sid = pipeline.prepare(video_file)
    total = len(pipeline.store.segments(sid, "moment"))
    assert total > 1
    entered, proceed = threading.Event(), threading.Event()

    class GatedEmbedder(FixtureEmbedder):
        videos = 0

        def video(self, path):
            type(self).videos += 1
            if type(self).videos == 1:
                entered.set()
                assert proceed.wait(10)
            return super().video(path)

    manager = SourceTasks(RuntimeConfig(config, pipeline.store), pipeline.store, GatedEmbedder(config))
    task = manager.start(sid, "publish", PublishOptions(work="shots"))
    try:
        assert entered.wait(10)
        assert manager.cancel(task["id"])["status"] == "running"
    finally:
        proceed.set()
    value = settled(manager, task["id"])
    assert value["status"] == "cancelled" and value["error"] == "已取消", value
    assert value["publication"]["status"] == "cancelled"
    assert value["progress"]["stage"] == "embedding"
    assert 0 < value["progress"]["completed"] < total
    assert pipeline.store.active_index()["id"] == old
    candidate = value["progress"]["index_id"]
    version = pipeline.store.one("SELECT status FROM index_versions WHERE id=?", (candidate,))
    assert version["status"] == "building"
    durable = pipeline.store.one(
        "SELECT count(*) AS n FROM index_members WHERE index_id=? AND complete=1 "
        "AND json_extract(snapshot,'$.source_id')=?", (candidate, sid),
    )["n"]
    assert durable == value["progress"]["completed"]
    # 索引写锁已释放，重试从检查点续建同一候选版本并激活。
    value = settled(manager, manager.retry(task["id"])["id"])
    assert value["status"] == "complete", value
    assert value["publication"]["status"] == "complete"
    assert value["publication"]["index_id"] == candidate
    assert pipeline.store.active_index()["id"] == candidate
    assert not check_index(pipeline.store, candidate)
    report = json.loads(pipeline.store.one(
        "SELECT value FROM settings WHERE key=?", ("index_report." + candidate,))["value"])
    assert report["resumed_vectors"] >= 1


def test_restart_recovery_interrupts_live_rows_and_keeps_cancelled(config):
    """重启恢复只把 queued/running 置为 interrupted；cancelled 终态保持原样。"""
    from mindex.admin.dialogue_tasks import DialogueTasks
    from mindex.admin.source_tasks import SourceTasks
    from mindex.persons.person_processing import PersonTasks
    from mindex.settings.runtime_settings import RuntimeConfig

    store = Store(config.data_dir)
    runtime = RuntimeConfig(config, store)
    tables = ("source_tasks", "dialogue_tasks", "person_tasks")
    for table in tables:
        for status in ("queued", "running", "cancelled"):
            insert_task(store, table, f"{table}-{status}", status)
        store.execute(f"UPDATE {table} SET error='已取消' WHERE id=?", (f"{table}-cancelled",))
    # 运行中被请求取消、但进程在检查点前退出：恢复仍按中断处理，重试会清掉遗留标记。
    registry_for(store.root).request("source_tasks-running")
    try:
        managers = dict(zip(tables, (SourceTasks(runtime, store), DialogueTasks(runtime, store),
                                     PersonTasks(runtime, store))))
        for table, manager in managers.items():
            for status in ("queued", "running"):
                row = manager.get(f"{table}-{status}")
                assert row["status"] == "interrupted", (table, status)
                assert row["error"] == "进程已中断；请重试继续检查点"
            row = manager.get(f"{table}-cancelled")
            assert row["status"] == "cancelled" and row["error"] == "已取消"
            # 中断与已取消都不是可取消状态。
            for identifier in (f"{table}-running", f"{table}-cancelled"):
                with pytest.raises(Exception) as conflict:
                    manager.cancel(identifier)
                assert conflict.value.status_code == 409
        # 重试前重置尝试会清掉遗留的取消标记，新执行不会被立即中断。
        reset_task_attempt(store, "source_tasks", "source_tasks-running")
        assert not registry_for(store.root).requested("source_tasks-running")
        assert store.one("SELECT status FROM source_tasks WHERE id='source_tasks-running'")["status"] == "queued"
    finally:
        registry_for(store.root).clear("source_tasks-running")
