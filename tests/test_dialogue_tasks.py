import threading
import time

import pytest
from fastapi.testclient import TestClient

from admin.dialogue_tasks import DialogueOptions, DialogueTasks
from api import create_app
from conftest import FixtureEmbedder
from processing.pipeline import Pipeline
from settings.runtime_settings import RuntimeConfig, activate_profile, save_profile


def eventually(callback):
    for _ in range(300):
        value = callback()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("task did not finish")


def test_http_returns_task_id_immediately_polls_after_refresh_and_excludes_deletion(
    config, video_file, monkeypatch
):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    started = threading.Event()
    release = threading.Event()

    def run(frozen, source_id, **options):
        assert source_id == sid and not options["publish"]
        options["progress"]({"stage": "asr", "completed_chunks": 1, "total_chunks": 2})
        started.set()
        assert release.wait(5)
        return {"index_id": None}

    monkeypatch.setattr("admin.dialogue_tasks.execute_dialogue", run)
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    response = client.post(f"/api/admin/sources/{sid}/dialogue", json={"publish": False})
    assert response.status_code == 202
    task = response.json()
    assert task["id"] and "config" not in task
    assert started.wait(2)
    task_id = task["id"]
    assert client.get(f"/api/admin/dialogue-tasks/{task_id}").json()["progress"]["completed_chunks"] == 1
    source = client.get(f"/api/admin/sources/{sid}").json()
    assert source["dialogue_task"]["id"] == task_id
    assert source["channels"]["analysis"]["status"] == "not_processed"
    assert client.delete(f"/api/admin/sources/{sid}").status_code == 409
    assert client.post(f"/api/admin/sources/{sid}/dialogue", json={}).status_code == 409
    release.set()
    eventually(lambda: client.get(f"/api/admin/dialogue-tasks/{task_id}").json()["status"] == "complete")
    # Finished task must release the same lock used by CLI.
    eventually(lambda: not pipeline.lock().is_locked)  # ownership checked independently below
    with pipeline.lock():
        pass
    assert client.post(f"/api/admin/dialogue-tasks/{task_id}/retry").status_code == 409
    assert client.get("/api/admin/dialogue-tasks/unknown").status_code == 404


def test_frozen_runtime_config_retry_credentials_and_restart(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    store = pipeline.store
    profile = save_profile(
        store,
        role="asr",
        name="ASR",
        provider="minimax",
        config={"backend": "minimax", "model": "frozen", "base_url": "https://example.test/v1"},
        api_key="very-secret-key",
    )
    activate_profile(store, profile["id"])
    manager = DialogueTasks(RuntimeConfig(config, store), store)
    configs = []
    fail = [True]

    def run(frozen, source_id, **options):
        configs.append(frozen)
        if fail[0]:
            raise RuntimeError("interrupted upstream")
        return {"index_id": None}

    monkeypatch.setattr("admin.dialogue_tasks.execute_dialogue", run)
    task = manager.start(sid, DialogueOptions(publish=False))
    task_id = task["id"]
    eventually(lambda: manager.get(task_id)["status"] == "failed")
    eventually(lambda: task_id not in manager.threads)
    row = store.one("SELECT config FROM dialogue_tasks WHERE id=?", (task_id,))
    assert "very-secret-key" not in row["config"]
    assert configs[0].asr.api_key == "very-secret-key"
    store.execute("DELETE FROM settings WHERE key='active.profile.asr'")
    fail[0] = False
    manager.retry(task_id)
    eventually(lambda: manager.get(task_id)["status"] == "complete")
    eventually(lambda: task_id not in manager.threads)
    assert configs[-1].asr.model == "frozen"
    assert configs[-1].asr.api_key == "very-secret-key"
    # Simulate process exit: persisted running jobs are not automatically submitted again.
    store.execute("UPDATE dialogue_tasks SET status='running' WHERE id=?", (task_id,))
    DialogueTasks(RuntimeConfig(config, store), store)
    assert manager.get(task_id)["status"] == "interrupted"
    assert len(configs) == 2


def test_no_publish_cli_only_acquires_dialogue_data(config, video_file, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from cli import app

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    path = tmp_path / "config.yaml"
    path.write_text("data_dir: " + str(config.data_dir) + "\n")
    monkeypatch.setattr("processing.indexing.make_embedder", lambda *_: pytest.fail("no embedding"))
    response = CliRunner().invoke(app, ["-c", str(path), "enrich-dialogue", sid, "--no-publish"])
    assert response.exit_code == 0, response.output
    assert pipeline.store.active_index() is None
    assert pipeline.store.one("SELECT status FROM channel_data")["status"] == "no_audio"


def test_web_dialogue_job_skips_automatic_embedding_when_no_audio(config, video_file):
    from processing.indexing import IndexBuilder, check_index

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    embedder = FixtureEmbedder(config)
    old = IndexBuilder(config, embedder).build(mode="visual", activate=True)
    calls = embedder.calls
    client = TestClient(create_app(config, embedder))
    response = client.post(f"/api/admin/sources/{sid}/dialogue", json={"publish": True})
    assert response.status_code == 202
    task_id = response.json()["id"]
    eventually(
        lambda: client.get(f"/api/admin/dialogue-tasks/{task_id}").json()["status"] in {"complete", "failed"}
    )
    task = client.get(f"/api/admin/dialogue-tasks/{task_id}").json()
    assert task["status"] == "complete", task.get("error")
    assert task["result"]["index_id"] is None
    assert task["publication"] == {"status": "complete", "task_ids": []}
    assert pipeline.store.active_index()["id"] == old
    assert not check_index(pipeline.store, old)
    assert embedder.calls == calls  # no audio and no changed media require zero new inference
