import subprocess
import sys
import threading

import pytest
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient
from filelock import FileLock, Timeout

from mindex.api import create_app
from mindex.dialogue_tasks import DialogueOptions, DialogueTasks
from mindex.pipeline import Pipeline
from mindex.processing_lock import ProcessingLock
from mindex.runtime_settings import RuntimeConfig, apply_runtime_overrides, put_setting
from mindex.source_tasks import SourceTasks, WorkOptions


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
    with ProcessingLock(config.model_copy(update={"enforce_gpu_lock": True})), pytest.raises(Timeout), ProcessingLock(config, source_id="b", work="dialogue"):
        pass
    with FileLock(str(config.data_dir / "pipeline.lock"), timeout=0), pytest.raises(Timeout), ProcessingLock(config):
        pass


def test_shared_lock_is_visible_to_cli_process(config):
    from mindex.storage import Store

    Store(config.data_dir)
    command = [sys.executable, "-c", """
import sys
from pathlib import Path
from filelock import Timeout
from mindex.config import Config
from mindex.processing_lock import ProcessingLock
try:
    with ProcessingLock(Config(data_dir=Path(sys.argv[1]), enforce_gpu_lock=True)):
        sys.exit(2)
except Timeout:
    sys.exit(0)
""", str(config.data_dir)]
    with ProcessingLock(config, source_id="a", work="analysis"):
        assert subprocess.run(command, check=False, timeout=10).returncode == 0


@pytest.mark.parametrize("enforced", [False, True])
def test_running_analysis_allows_other_work_and_other_source_deletion(config, video_file, monkeypatch, enforced):
    from test_source_work import finished

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    other = "other-source"
    pipeline.store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)",
                           (other, "其他素材", "other.mp4", '{"duration":1}', config.layout_key))
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
    monkeypatch.setattr("mindex.dialogue_tasks.execute_dialogue", lambda *a, **kw: {"index_id": None})
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
