from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from api import create_app
from conftest import FixtureSpeaker
from studio import workspace as ws
from test_studio_workspace import setup_workspace, wait_build


def test_tts_conflicts_explain_service_change_without_claiming_text_changed(config):
    jid, w, p = setup_workspace(config)
    ws.execute(config, jid, w, p, "tts", "a", speaker=FixtureSpeaker(config))
    selected = p["shots"]["a"]["tts"]
    config.studio.tts.backend = "minimax"
    config.studio.tts.model = "speech-2.8-hd"
    messages = ws.conflicts(config, jid, w, p, "tts", "a", selected)
    assert any("配音服务已变化" in m and "minimax" in m for m in messages)
    assert any("配音模型已变化" in m and "speech-2.8-hd" in m for m in messages)
    assert not any("旁白" in m or "阶段参数与当前方案不一致" in m for m in messages)
    p["options"].update(voice_mode="custom", voice="new-voice")
    assert any("方案音色已变化" in m for m in ws.conflicts(config, jid, w, p, "tts", "a", selected))


def test_invalid_voice_failure_keeps_success_and_exposes_attempted_parameters(config):
    class InvalidVoice:
        def speak(self, text, voice=None):
            raise RuntimeError("MiniMax TTS 合成失败：2054 voice id not exist")

    jid, w, p = setup_workspace(config)
    p["options"].update(voice_mode="custom", voice="uncle_fu")
    ws.execute(config, jid, w, p, "tts", "a", speaker=FixtureSpeaker(config))
    before = copy.deepcopy(p["shots"]["a"])
    config.studio.tts.backend = "minimax"
    config.studio.tts.model = "speech-2.8-hd"
    spec = ws.specification(config, jid, w, p, "tts", "a")
    client = TestClient(create_app(config, speaker=InvalidVoice()))
    base = f"/api/studio/jobs/{jid}"
    response = client.post(
        f"{base}/plans/{p['id']}/nodes",
        json={
            "revision": ws.revision(p),
            "stage": "tts",
            "shot_id": "a",
            "inputs": spec["inputs"],
            "force": True,
        },
    )
    assert response.status_code == 202, response.text
    done = wait_build(client, jid)
    node = done["builds"][-1]["nodes"][0]
    assert node["status"] == "failed" and "voice id not exist" in node["error"]
    assert done["plans"][p["id"]]["shots"]["a"] == before
    attempt = done["versions"][node["version"]]
    assert attempt["parameters"]["tts"]["voice"] == "uncle_fu"
    assert attempt["parameters"]["tts"]["model"] == "speech-2.8-hd"
    assert "voice id not exist" in attempt["error"]
