"""处理互斥、跨进程锁与素材保护回归。"""

import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout

from admin.dialogue_tasks import DialogueOptions, DialogueTasks
from admin.source_tasks import SourceTasks, WorkOptions
from api import create_app
from conftest import FixtureEmbedder
from processing.pipeline import Pipeline
from settings.runtime_settings import RuntimeConfig, apply_runtime_overrides, put_setting
from shared.processing_lock import ProcessingLock


def test_shared_processing_locks_and_source_protection(config):
    assert config.enforce_gpu_lock is False
    with ProcessingLock(config, source_id="a", work="analysis"):
        with ProcessingLock(config, source_id="a", work="dialogue"):
            pass
        with ProcessingLock(config, source_id="b", deleting=True):
            pass
        with pytest.raises(Timeout), ProcessingLock(config, source_id="a", work="analysis"):
            pass
        with pytest.raises(Timeout), ProcessingLock(config, source_id="a", deleting=True):
            pass
        with pytest.raises(Timeout), ProcessingLock(config.model_copy(update={"enforce_gpu_lock": True})):
            pass
    with (
        ProcessingLock(config.model_copy(update={"enforce_gpu_lock": True})),
        pytest.raises(Timeout),
        ProcessingLock(config, source_id="b", work="dialogue"),
    ):
        pass
    with (
        FileLock(str(config.data_dir / "pipeline.lock"), timeout=0),
        pytest.raises(Timeout),
        ProcessingLock(config),
    ):
        pass


def test_shared_lock_is_visible_to_cli_process(config):
    from shared.storage import Store

    Store(config.data_dir)
    command = [
        sys.executable,
        "-c",
        """
import sys
import mindex  # 子进程自举后再使用包内短导入
from pathlib import Path
from filelock import Timeout
from shared.config import Config
from shared.processing_lock import ProcessingLock
try:
    with ProcessingLock(Config(data_dir=Path(sys.argv[1]), enforce_gpu_lock=True)):
        sys.exit(2)
except Timeout:
    sys.exit(0)
""",
        str(config.data_dir),
    ]
    with ProcessingLock(config, source_id="a", work="analysis"):
        assert subprocess.run(command, check=False, timeout=10).returncode == 0


@pytest.mark.parametrize("enforced", [False, True])
def test_running_analysis_allows_other_work_and_other_source_deletion(
    config, video_file, monkeypatch, enforced
):
    from test_source_work import finished

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    other = "other-source"
    pipeline.store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)",
        (other, "其他素材", "other.mp4", '{"duration":1}', config.layout_key),
    )
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    assert client.put("/api/settings/business", json={"enforce_gpu_lock": enforced}).status_code == 200
    assert client.get("/api/settings").json()["business"]["enforce_gpu_lock"] is enforced
    assert apply_runtime_overrides(config).enforce_gpu_lock is enforced
    assert client.get(f"/api/admin/sources/{sid}").json()["enforce_gpu_lock"] is enforced
    runtime = RuntimeConfig(config, pipeline.store)
    analysis = SourceTasks(runtime, pipeline.store)
    dialogue = DialogueTasks(runtime, pipeline.store)
    entered, release = threading.Event(), threading.Event()

    def analyze(self, source_id, progress):
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(Pipeline, "analyze", analyze)
    monkeypatch.setattr("admin.dialogue_tasks.execute_dialogue", lambda *a, **kw: {"index_id": None})
    task = analysis.start(sid, "analysis", WorkOptions(publish=False))
    try:
        assert entered.wait(2)
        # A second application instance must not interrupt the still-live task.
        SourceTasks(runtime, pipeline.store)
        assert analysis.get(task["id"])["status"] == "running"
        with pytest.raises(Exception) as exc:
            analysis.start(sid, "analysis", WorkOptions(publish=False))
        assert exc.value.status_code == 409
        assert client.delete(f"/api/admin/sources/{sid}").status_code == 409
        deletion = client.delete(f"/api/admin/sources/{other}")
        assert deletion.status_code == (409 if enforced else 200), deletion.text
        if enforced:
            with pytest.raises(Exception) as exc:
                dialogue.start(sid, DialogueOptions(publish=False))
            assert exc.value.status_code == 409
        else:
            job = dialogue.start(sid, DialogueOptions(publish=False))
            assert finished(dialogue, job)["status"] == "complete"
            put_setting(pipeline.store, "override.enforce_gpu_lock", "true")
            with pytest.raises(Exception) as exc:
                dialogue.start(sid, DialogueOptions(publish=False))
            assert exc.value.status_code == 409
    finally:
        release.set()
        assert finished(analysis, task)["status"] == "complete"
    assert client.delete(f"/api/admin/sources/{sid}").status_code == 200


def test_gpu_lock_business_setting_roundtrip_reset_and_validation(config):
    client = TestClient(create_app(config))
    assert client.get("/api/settings").json()["business"]["enforce_gpu_lock"] is False
    for value in (True, False):
        assert client.put("/api/settings/business", json={"enforce_gpu_lock": value}).status_code == 200
        assert client.get("/api/settings").json()["business"]["enforce_gpu_lock"] is value
    for value in (1, "false", {}):
        assert client.put("/api/settings/business", json={"enforce_gpu_lock": value}).status_code == 422
    client.put("/api/settings/business", json={"enforce_gpu_lock": True})
    assert client.delete("/api/settings/business").status_code == 204
    assert client.get("/api/settings").json()["business"]["enforce_gpu_lock"] is False


@pytest.mark.parametrize("enforced", [False, True])
def test_all_four_processing_groups_across_sources_follow_gpu_setting(config, monkeypatch, enforced):
    """Hold real task workers concurrently, rather than just probing lock files."""

    from persons.person_processing import PersonOptions, PersonTasks
    from shared.storage import Store
    from test_source_work import finished

    config.enforce_gpu_lock = enforced
    store = Store(config.data_dir)
    for sid in ("source-a", "source-b"):
        store.execute(
            "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
            (sid, sid, sid + ".mp4", json.dumps({"duration": 10}), config.layout_key),
        )
    runtime = RuntimeConfig(config, store)
    sources = SourceTasks(runtime, store)
    dialogue = DialogueTasks(runtime, store)
    persons = PersonTasks(runtime, store)
    release, all_entered = threading.Event(), threading.Event()
    entered, guard = set(), threading.Lock()
    expected = 1 if enforced else 8

    def wait(sid, work):
        with guard:
            entered.add((sid, work))
            if len(entered) == expected:
                all_entered.set()
        assert release.wait(10)

    monkeypatch.setattr(Pipeline, "detect_source", lambda self, sid, progress: wait(sid, "shots"))
    monkeypatch.setattr(Pipeline, "split", lambda *args: None)
    monkeypatch.setattr(Pipeline, "analyze", lambda self, sid, progress: wait(sid, "analysis"))
    monkeypatch.setattr(
        "admin.dialogue_tasks.execute_dialogue",
        lambda cfg, sid, **kw: wait(sid, "dialogue") or {"index_id": None},
    )
    monkeypatch.setattr(
        "persons.person_processing.PersonProcessor",
        lambda cfg: SimpleNamespace(run=lambda sid, opts, progress: wait(sid, "persons") or {}),
    )
    jobs = []

    def start(sid, work):
        if work in ("shots", "analysis"):
            return sources, sources.start(sid, work, WorkOptions(publish=False))
        if work == "dialogue":
            return dialogue, dialogue.start(sid, DialogueOptions(publish=False))
        return persons, persons.start(sid, PersonOptions(publish=False))

    try:
        jobs.append(start("source-a", "shots"))
        for sid in ("source-a", "source-b"):
            for work in ("shots", "analysis", "dialogue", "persons"):
                if (sid, work) == ("source-a", "shots"):
                    continue
                if enforced:
                    with pytest.raises(Exception) as exc:
                        start(sid, work)
                    assert exc.value.status_code == 409
                else:
                    jobs.append(start(sid, work))
        assert all_entered.wait(3), entered
        assert len(entered) == expected
        assert all(manager.get(task["id"])["status"] == "running" for manager, task in jobs)
        # Duplicate execution of the same source and work stays guarded.
        with pytest.raises(Exception) as exc:
            start("source-a", "shots")
        assert exc.value.status_code == 409
        SourceTasks(runtime, store)
        DialogueTasks(runtime, store)
        PersonTasks(runtime, store)
        assert all(manager.get(task["id"])["status"] == "running" for manager, task in jobs)
    finally:
        release.set()
        for manager, task in jobs:
            assert finished(manager, task)["status"] == "complete"


def test_index_wait_can_be_cancelled_without_waiting_for_writer(config):
    from shared.processing_lock import index_write_lock
    from shared.task_cancel import TaskCancelled

    cancelled = threading.Event()
    finished = threading.Event()
    errors = []

    def check():
        if cancelled.is_set():
            raise TaskCancelled()

    def wait():
        try:
            with index_write_lock(config.data_dir, check=check):
                errors.append("等待者不应进入占用中的写锁")
        except TaskCancelled:
            finished.set()

    with index_write_lock(config.data_dir):
        worker = threading.Thread(target=wait)
        worker.start()
        cancelled.set()
        assert finished.wait(2)
        worker.join(2)
    assert not errors and not worker.is_alive()
