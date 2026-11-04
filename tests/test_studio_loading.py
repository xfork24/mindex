"""Workspace reads avoid loading person evidence and reuse only request-local state."""

import copy
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import legacy_job
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.persons.persons import Persons
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.workspace import signatures


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
    job_id = legacy_job(config, "旁白")['id']
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
    original_settings, original_specification = signatures._settings, signatures.specification

    def tracked_store(root):
        stores.append(root)
        return store

    def tracked_settings(cfg, selected_plan, stage, **kw):
        settings_calls[stage] += 1
        return original_settings(cfg, selected_plan, stage, **kw)

    def tracked_specification(cfg, jid, workspace, selected_plan, stage, sid=None, **kw):
        spec_calls[(stage, sid)] += 1
        return original_specification(cfg, jid, workspace, selected_plan, stage, sid, **kw)

    monkeypatch.setattr(signatures, "Store", SimpleNamespace(open=tracked_store))
    monkeypatch.setattr(signatures, "_settings", tracked_settings)
    monkeypatch.setattr(signatures, "specification", tracked_specification)
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


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for UI controller checks")
def test_plan_settings_voice_catalogue_caches_and_dedups():
    result = subprocess.run(
        ["node", "tests/studio_voice_catalogue_unit.cjs"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_compact_workspace_keeps_editor_fields_and_lazy_full_version(config):
    jid = studio.create_job(config, 'A short story.')['id']
    w = ws.load(config, jid)
    candidate = {'id': 'candidate', 'source_id': 'source', 'start': 1, 'end': 2,
                 'rank': 1, 'media_url': '/media', 'thumbnail_url': '/thumb',
                 'annotation': {'description': 'x' * 50000}, 'channel_metadata': {'large': 'x' * 50000},
                 'matches': [{'large': 'x' * 50000}]}
    rid = ws.add_version(config, jid, w, 'recall', 'shot', data={'results': [candidate]})
    sid = ws.add_version(config, jid, w, 'select', 'shot', data={'anchor': candidate})
    cid = ws.add_version(config, jid, w, 'compose', data={'graph': 'x' * 500000, 'plan': {'id': 'restorable'}})
    ws.persist(config, jid, w)
    original = copy.deepcopy(w)
    small = ws.detail(config, jid, w, compact=True)
    assert w == original
    assert len(json.dumps(small)) < len(json.dumps(w)) / 10
    assert small['versions'][rid]['data']['results'][0]['media_url'] == '/media'
    assert small['versions'][sid]['data']['anchor']['start'] == 1
    assert small['versions'][cid]['data']['plan'] == {'id': 'restorable'}
    assert 'graph' not in small['versions'][cid]['data']
    with TestClient(create_app(config)) as client:
        result = client.get(f'/api/studio/jobs/{jid}/workspace?compact=true')
        assert result.status_code == 200
        assert result.json()['versions'][rid]['compact']
        full = client.get(f'/api/studio/jobs/{jid}/versions/{rid}').json()
        assert full['data']['results'][0]['annotation'] == candidate['annotation']
        full_workspace = client.get(f'/api/studio/jobs/{jid}/workspace').json()
        assert full_workspace['versions'][cid]['data']['graph'] == 'x' * 500000
