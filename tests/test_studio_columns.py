"""分镜工作台列与阶段映射回归。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api import create_app
from conftest import FixtureSpeaker, FixtureStoryboarder
from shared.trace import record
from studio import studio
from studio import workspace as ws
from studio.logs import ExecutionLog, log_path, read_events
from test_studio_workspace import setup_workspace, wait_build


def request_body(config, jid, w, p, stage):
    return {
        "revision": ws.revision(p),
        "stage": stage,
        "inputs_by_shot": {
            sid: ws.specification(config, jid, w, p, stage, sid)["inputs"] for sid in ws.ordered_shots(w, p)
        }
        if stage != "split"
        else {},
    }


def test_column_force_is_one_scoped_batch_with_incremental_readable_logs(config):
    class LoggedSpeaker(FixtureSpeaker):
        def speak(self, text, voice=None):
            record(
                "model-request",
                {"attempt": 0, "json": {"token": "private-marker", "prompt": "private-prompt"}},
            )
            record("tts-response", {"status": 200, "body": "private-response"})
            return super().speak(text, voice)

    jid, w, p = setup_workspace(config)
    speaker = LoggedSpeaker(config)
    client = TestClient(create_app(config, speaker=speaker))
    base = f"/api/studio/jobs/{jid}"
    url = f"{base}/plans/{p['id']}/regenerate-column"
    body = request_body(config, jid, w, p, "tts")
    response = client.post(url, json=body)
    assert response.status_code == 202, response.text
    build_id = response.json()["id"]
    done = wait_build(client, jid)
    assert speaker.calls == 2
    assert done["builds"][-1]["force"]
    assert [(n["stage"], n["shot_id"]) for n in done["builds"][-1]["nodes"]] == [("tts", "a"), ("tts", "b")]
    saved = done["plans"][p["id"]]
    old_audio = [saved["shots"][sid]["tts"] for sid in ("a", "b")]
    assert saved["final"] is None and all(set(s) == {"content", "tts"} for s in saved["shots"].values())
    events = client.get(f"{base}/builds/{build_id}/logs").json()
    assert events["status"] == "complete" and not events["legacy"]
    assert [e["seq"] for e in events["items"]] == list(range(1, len(events["items"]) + 1))
    assert events["items"][-1]["message"] == "执行结束：全部完成"
    assert {e["shot_id"] for e in events["items"] if e["stage"] == "tts"} == {"a", "b"}
    payload = json.dumps(events, ensure_ascii=False)
    assert "第 1 次尝试" in payload and "HTTP 200" in payload
    assert all(secret not in payload for secret in ("private-marker", "private-prompt", "private-response"))
    after = events["items"][2]["seq"]
    tail = client.get(f"{base}/builds/{build_id}/logs?after={after}").json()
    assert tail["items"] == events["items"][3:]
    assert client.get(f"{base}/builds/{build_id}/logs?after=-1").status_code == 422
    assert client.get(f"{base}/builds/unknown/logs").status_code == 404
    other = studio.create_job(config, "另一个任务。")["id"]
    assert client.get(f"/api/studio/jobs/{other}/builds/{build_id}/logs").status_code == 404
    assert client.post(url, json=body).status_code == 422
    w = ws.load(config, jid)
    p = ws.plan(w, p["id"])
    assert client.post(url, json=request_body(config, jid, w, p, "tts")).status_code == 202
    again = wait_build(client, jid)
    assert speaker.calls == 4
    assert all(
        again["plans"][p["id"]]["shots"][sid]["tts"] != old_audio[i] for i, sid in enumerate(("a", "b"))
    )
    assert all(vid in again["versions"] for vid in old_audio)


def test_column_failure_keeps_old_selection_and_continues(config):
    class Partial(FixtureSpeaker):
        broken = False

        def speak(self, text, voice=None):
            if self.broken and text.startswith("红"):
                raise RuntimeError("当前配音服务断开")
            return super().speak(text, voice)

    jid, w, p = setup_workspace(config)
    speaker = Partial(config)
    for sid in ("a", "b"):
        ws.execute(config, jid, w, p, "tts", sid, speaker=speaker)
    before = {sid: p["shots"][sid]["tts"] for sid in ("a", "b")}
    speaker.broken = True
    client = TestClient(create_app(config, speaker=speaker))
    base = f"/api/studio/jobs/{jid}"
    r = client.post(f"{base}/plans/{p['id']}/regenerate-column", json=request_body(config, jid, w, p, "tts"))
    assert r.status_code == 202
    done = wait_build(client, jid)
    b = done["builds"][-1]
    assert b["status"] == "partial"
    assert [n["status"] for n in b["nodes"]] == ["failed", "complete"]
    selected = done["plans"][p["id"]]["shots"]
    assert selected["a"]["tts"] == before["a"] and selected["b"]["tts"] != before["b"]
    logs = client.get(f"{base}/builds/{b['id']}/logs").json()["items"]
    assert any(e["shot_id"] == "a" and e["level"] == "error" for e in logs)
    assert any(e["shot_id"] == "b" and e["message"] == "配音：完成" for e in logs)


@pytest.mark.parametrize("stage", ["select", "render"])
def test_column_missing_dependencies_block_without_running_upstream(config, stage):
    jid, w, p = setup_workspace(config)
    speaker = FixtureSpeaker(config)
    client = TestClient(create_app(config, speaker=speaker))
    url = f"/api/studio/jobs/{jid}/plans/{p['id']}/regenerate-column"
    assert client.post(url, json=request_body(config, jid, w, p, stage)).status_code == 202
    done = wait_build(client, jid)
    assert all(n["status"] == "blocked" for n in done["builds"][-1]["nodes"])
    assert speaker.calls == 0 and done["plans"][p["id"]]["shots"] == p["shots"]
    assert done["plans"][p["id"]]["final"] is None


def test_column_binding_validation_and_split_plan_isolation(config):
    jid, w, p = setup_workspace(config)
    client = TestClient(create_app(config, storyboarder=FixtureStoryboarder(config)))
    base = f"/api/studio/jobs/{jid}"
    copied = client.post(f"{base}/plans", json={"name": "副本", "copy_from": p["id"]}).json()
    copy_id = copied["active_plan"]
    w = ws.load(config, jid)
    p = ws.plan(w, p["id"])
    url = f"{base}/plans/{p['id']}/regenerate-column"
    body = request_body(config, jid, w, p, "tts")
    del body["inputs_by_shot"]["b"]
    assert client.post(url, json=body).status_code == 422
    body = request_body(config, jid, w, p, "tts")
    body["inputs_by_shot"]["a"]["content"] = "unbound"
    assert client.post(url, json=body).status_code == 422
    split = request_body(config, jid, w, p, "split")
    assert client.post(url, json=split).status_code == 202
    done = wait_build(client, jid)
    assert done["builds"][-1]["status"] == "complete"
    assert ws.ordered_shots(done, done["plans"][p["id"]]) != ["a", "b"]
    assert ws.ordered_shots(done, done["plans"][copy_id]) == ["a", "b"]
    assert len(done["builds"][-1]["nodes"]) == 1 and done["builds"][-1]["nodes"][0]["stage"] == "split"


def test_log_partial_append_and_legacy_record(config):
    jid, _w, _p = setup_workspace(config)
    assert read_events(config, jid, "old")["legacy"]
    log = ExecutionLog(config, jid, "new")
    log.emit("开始")
    path = log_path(config, jid, "new")
    first = path.read_text()
    with path.open("a") as stream:
        stream.write('{"seq": 2')
    assert read_events(config, jid, "new")["next_seq"] == 1
    event = {"seq": 2, "at": studio.now(), "stage": "tts", "shot_id": "a", "level": "info", "message": "完成"}
    path.write_text(first + json.dumps(event) + "\n")
    items = read_events(config, jid, "new", after=1)["items"]
    assert len(items) == 1
    assert {key: items[0][key] for key in event} == event
    assert items[0]["message_key"] and items[0]["message_params"] == {}


def test_execute_column_reuses_successes_and_runs_missing_cells(config):
    jid, w, p = setup_workspace(config)
    speaker = FixtureSpeaker(config)
    old = ws.execute(config, jid, w, p, "tts", "a", speaker=speaker)
    client = TestClient(create_app(config, speaker=speaker))
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{p['id']}/execute-column",
        json=request_body(config, jid, w, p, "tts"),
    )
    assert response.status_code == 202, response.text
    done = wait_build(client, jid)
    build = done["builds"][-1]
    assert build["force"] is False
    assert [n["status"] for n in build["nodes"]] == ["reused", "complete"]
    assert done["plans"][p["id"]]["shots"]["a"]["tts"] == old
    assert speaker.calls == 2
