"""Workspace reads avoid loading person evidence and reuse only request-local state."""

import copy
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from mindex import studio
from mindex import studio_workspace as ws
from mindex.persons import Persons
from mindex.storage import Store


def indexed_store(config):
    store = Store(config.data_dir)
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('index','{}','ready')")
    store.execute("INSERT INTO settings(key,value) VALUES('active','index')")
    # Invalid snapshot is deliberate: reading a release identity must not load evidence.
    store.execute("INSERT INTO person_versions(id,data) VALUES('persons','not JSON')")
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES('release','index','persons')")
    store.execute("INSERT INTO settings(key,value) VALUES('active.release','release')")
    return store


def test_recall_settings_use_release_identity_without_loading_snapshot(config, monkeypatch):
    store = indexed_store(config)
    monkeypatch.setattr(Persons, "published", lambda *a, **kw: pytest.fail("must not load person snapshots"))
    p = ws.new_plan("plan", {})
    before = ws.settings(config, p, "recall")
    assert before["person_release"] == "release" and before["index_id"] == "index"
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES('new','index',NULL)")
    store.execute("UPDATE settings SET value='new' WHERE key='active.release'")
    after = ws.settings(config, p, "recall")
    assert after["person_release"] == "new" and after != before
    store.execute("UPDATE settings SET value='missing' WHERE key='active.release'")
    assert ws.settings(config, p, "recall")["person_release"] is None


def test_detail_reuses_database_settings_and_dependency_checks_per_request(config, monkeypatch):
    store = indexed_store(config)
    job_id = studio.create_job(config, "旁白")['id']
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id=str(i), text="旁白", query="夜景", estimated_seconds=1) for i in range(3)
    ])
    for sid in ws.ordered_shots(w, p):
        for stage in ws.STAGES:
            spec = ws.specification(config, job_id, w, p, stage, sid)
            p["shots"][sid][stage] = ws.add_version(config, job_id, w, stage, sid, **spec)
    original = copy.deepcopy(w)
    settings_calls, spec_calls, stores = Counter(), Counter(), []
    original_settings, original_specification = ws._settings, ws.specification

    def tracked_store(root):
        stores.append(root)
        return store

    def tracked_settings(cfg, selected_plan, stage, **kw):
        settings_calls[stage] += 1
        return original_settings(cfg, selected_plan, stage, **kw)

    def tracked_specification(cfg, jid, workspace, selected_plan, stage, sid=None, **kw):
        spec_calls[(stage, sid)] += 1
        return original_specification(cfg, jid, workspace, selected_plan, stage, sid, **kw)

    monkeypatch.setattr(ws, "Store", tracked_store)
    monkeypatch.setattr(ws, "_settings", tracked_settings)
    monkeypatch.setattr(ws, "specification", tracked_specification)
    detail = ws.detail(config, job_id, w)
    assert len(stores) == 1
    assert settings_calls == Counter({stage: 1 for stage in ws.STAGES})
    assert len(spec_calls) == 12 and set(spec_calls.values()) == {1}
    assert all(not messages for messages in detail["conflicts"].values())
    assert w == original  # Response enrichment leaves persisted versions untouched.

    settings_calls.clear()
    spec_calls.clear()
    config.studio.tts.voice = "changed"
    fresh = ws.detail(config, job_id, w, store=store)
    assert len(stores) == 1  # The API's existing Store requires no initialization.
    assert settings_calls == Counter({stage: 1 for stage in ws.STAGES})
    assert all(fresh["conflicts"][f"tts:{sid}"] for sid in ws.ordered_shots(w, p))
    assert all(fresh["conflicts"][f"render:{sid}"] for sid in ws.ordered_shots(w, p))
    assert fresh["tts_settings"]["voice"] == "changed"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for UI controller checks")
def test_studio_loads_direct_links_without_waiting_for_job_list():
    result = subprocess.run(
        ["node", "tests/studio_loading_ui.cjs"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
