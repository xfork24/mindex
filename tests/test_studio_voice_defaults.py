from __future__ import annotations

import copy
import json

import pytest
from conftest import FixtureSpeaker
from fastapi.testclient import TestClient
from test_studio_workspace import setup_workspace, wait_build

from mindex.api import create_app
from mindex.settings.runtime_settings import activate_profile, save_profile
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio import workspace as ws


def test_new_and_legacy_plans_inherit_without_changing_history(config):
    jid, w, p = setup_workspace(config)
    assert p["options"]["voice"] is None and p["options"]["voice_mode"] == "inherit"
    ws.execute(config, jid, w, p, "tts", "a", speaker=FixtureSpeaker(config))
    old = copy.deepcopy(w["versions"])
    p["options"].pop("voice_mode")
    p["options"]["voice"] = "uncle_fu"
    ws.persist(config, jid, w)
    original = ws.path(config, jid).read_bytes()
    loaded = ws.load(config, jid)
    migrated = ws.plan(loaded, p["id"])
    assert migrated["options"]["voice"] is None
    assert migrated["options"]["voice_mode"] == "inherit"
    assert migrated["legacy_voice"] == "uncle_fu"
    assert loaded["versions"] == old
    assert ws.path(config, jid).read_bytes() == original  # Read does not write outside the lock.
    assert ws.effective_voice(config, migrated) == config.studio.tts.voice


def test_live_profile_switch_changes_batch_voice_and_keeps_version_snapshots(config):
    class CapturingSpeaker(FixtureSpeaker):
        voices = None

        def speak(self, text, voice=None):
            self.voices = [*(self.voices or []), voice]
            return super().speak(text, voice)

    jid, _w, p = setup_workspace(config)
    speaker = CapturingSpeaker(config)
    client = TestClient(create_app(config, speaker=speaker))
    base = f"/api/studio/jobs/{jid}"

    def run_column():
        w = client.get(f"{base}/workspace").json()
        selected = w["plans"][p["id"]]
        inputs = {sid: {"content": shot["content"]} for sid, shot in selected["shots"].items()}
        response = client.post(
            f"{base}/plans/{p['id']}/regenerate-column",
            json={"revision": w["revision"], "stage": "tts", "inputs_by_shot": inputs},
        )
        assert response.status_code == 202, response.text
        return wait_build(client, jid)

    first = run_column()
    old_id = first["plans"][p["id"]]["shots"]["a"]["tts"]
    old = copy.deepcopy(first["versions"][old_id])
    store = Store(config.data_dir)
    profile = save_profile(
        store,
        role="tts",
        name="MiniMax",
        provider="minimax",
        config={"backend": "minimax", "model": "speech-2.8-hd", "voice": "Chinese (Mandarin)_News_Anchor"},
    )
    activate_profile(store, profile["id"])
    changed = client.get(f"{base}/workspace").json()
    assert changed["tts_settings"]["voice"] == "Chinese (Mandarin)_News_Anchor"
    assert changed["plans"][p["id"]]["options"]["voice"] is None
    second = run_column()
    assert speaker.voices == [config.studio.tts.voice] * 2 + ["Chinese (Mandarin)_News_Anchor"] * 2
    new = second["versions"][second["plans"][p["id"]]["shots"]["a"]["tts"]]
    assert new["parameters"]["voice"] == "Chinese (Mandarin)_News_Anchor"
    assert new["parameters"]["tts"]["model"] == "speech-2.8-hd"
    assert second["versions"][old_id] == old
    assert old["parameters"]["voice"] == config.studio.tts.voice
    assert new["signature"] != old["signature"]


def test_explicit_override_and_reset_to_inherit(config):
    jid, _w, p = setup_workspace(config)
    client = TestClient(create_app(config))
    base = f"/api/studio/jobs/{jid}"
    initial = client.get(f"{base}/workspace").json()
    options = {**p["options"], "voice_mode": "custom", "voice": "my-voice"}
    fixed = client.patch(
        f"{base}/plans/{p['id']}", json={"revision": initial["revision"], "options": options}
    ).json()
    config.studio.tts.voice = "new-default"
    current = client.get(f"{base}/workspace").json()
    assert current["tts_settings"]["voice"] == "my-voice"
    assert current["tts_settings"]["default_voice"] == "new-default"
    options.update(voice_mode="inherit")
    inherited = client.patch(
        f"{base}/plans/{p['id']}", json={"revision": fixed["revision"], "options": options}
    ).json()
    assert inherited["plans"][p["id"]]["options"]["voice"] is None
    assert inherited["tts_settings"]["voice"] == "new-default"
    options.update(voice_mode="custom", voice=None)
    assert (
        client.patch(
            f"{base}/plans/{p['id']}", json={"revision": inherited["revision"], "options": options}
        ).status_code
        == 422
    )


def test_explicit_create_voice_is_preserved(config):
    jid = studio.create_job(config, "指定音色。", options={"voice": "my-voice"})["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    assert p["options"]["voice_mode"] == "custom"
    assert ws.effective_voice(config, p) == "my-voice"


def test_new_plan_inherits_while_copy_preserves_explicit_choice(config):
    jid = studio.create_job(config, "指定音色。", options={"voice": "my-voice"})["id"]
    client = TestClient(create_app(config))
    base = f"/api/studio/jobs/{jid}"
    initial = client.get(f"{base}/workspace").json()
    original = initial["active_plan"]
    new = client.post(f"{base}/plans", json={"name": "新方案"}).json()
    assert new["plans"][new["active_plan"]]["options"]["voice_mode"] == "inherit"
    copied = client.post(f"{base}/plans", json={"name": "副本", "copy_from": original}).json()
    assert copied["plans"][copied["active_plan"]]["options"]["voice_mode"] == "custom"
    assert copied["tts_settings"]["voice"] == "my-voice"


def test_restore_legacy_snapshot_inherits_without_rewriting_final(config):
    jid, w, p = setup_workspace(config)
    snapshot = copy.deepcopy(p)
    snapshot["options"].pop("voice_mode")
    snapshot["options"]["voice"] = "uncle_fu"
    final = ws.add_version(config, jid, w, "compose", data={"plan": snapshot})
    original = json.dumps(w["versions"][final], sort_keys=True)
    restored = ws.restore(w, final, "恢复")
    assert restored["options"]["voice_mode"] == "inherit"
    assert restored["options"]["voice"] is None
    assert json.dumps(w["versions"][final], sort_keys=True) == original


def test_default_mode_does_not_pin_voice(config):
    assert ws.PlanOptions().model_dump()["voice"] is None
    assert ws.PlanOptions(voice="explicit").voice_mode == "custom"
    assert ws.PlanOptions(voice_mode="inherit", voice="old-default").voice is None
    with pytest.raises(ValueError, match="指定音色"):
        ws.PlanOptions(voice_mode="custom")
