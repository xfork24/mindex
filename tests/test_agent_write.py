"""智能助手确认策略与 Studio 写操作：预览、允许/拒绝、会话授权、失败关闭、乐观锁冲突、合并规则。"""

from __future__ import annotations

import pytest
from agent_helpers import new_session, result_text, results, send, wait_idle, wait_pending
from conftest import legacy_job
from fastapi.testclient import TestClient

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.tools.studio_write import merge_into
from mindex.api import create_app
from mindex.studio import studio
from mindex.studio import studio_workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-77f0c3e1cc"]
RECALL = {"mode": "text", "person_ids": [], "person_mode": "auto", "person_relation": "all",
          "filters": {"source_id": None, "source_ids": [], "min_duration": 2.0, "max_duration": None,
                      "scene": None, "emotion": None, "shot_size": None},
          "media_id": None, "media_kind": None}


@pytest.fixture
def job(config):
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id=SHOTS[0], text="红色开场。", query="红色", estimated_seconds=1.5),
        studio.Shot(id=SHOTS[1], text="蓝色中段。", query="蓝色", estimated_seconds=2.0, recall=RECALL),
        studio.Shot(id=SHOTS[2], text="绿色结尾。", query="绿色", estimated_seconds=1.0),
    ])
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id


@pytest.fixture
def agent(config, job):
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm, job


def workspace(client, job):
    return client.get(f"/api/studio/jobs/{job}/workspace").json()


def order(data):
    p = data["plans"][data["active_plan"]]
    return data["versions"][p["board"]]["data"]["order"]


def content(data, sid):
    p = data["plans"][data["active_plan"]]
    return data["versions"][p["shots"][sid]["content"]]["data"]


MERGE = {"operations": [{"op": "merge", "shots": ["925281d5", "a1daad59"]}]}


def test_merge_matches_frontend_rules():
    previous = {"text": "甲。", "query": "甲", "estimated_seconds": 1, "recall": None}
    merge_into(previous, {"text": "乙。", "query": "乙", "estimated_seconds": 2.5, "recall": RECALL})
    assert previous == {"text": "甲。乙。", "query": "甲；乙", "estimated_seconds": 3.5, "recall": RECALL}
    kept = {"text": "甲", "query": "甲", "estimated_seconds": 1, "recall": RECALL}
    merge_into(kept, {"text": "乙", "query": "乙", "estimated_seconds": 1, "recall": {**RECALL, "person_ids": ["x"]}})
    assert kept["recall"] == RECALL


def test_merge_with_approval(agent):
    client, llm, job = agent
    before = workspace(client, job)
    untouched = before["plans"][before["active_plan"]]["shots"][SHOTS[2]]["content"]
    llm.extend([call("edit_shots", call_id="m1", job_id=job, **MERGE)], [say("已合并。")])
    sid = new_session(client)
    send(client, sid, "合并 a1daad59 和 925281d5")
    pending = wait_pending(client, sid, "approval")
    assert pending["name"] == "edit_shots" and pending["risk"] == "write" and pending["allowGrant"]
    preview = pending["preview"]
    assert "3 → 2" in preview["summary"]
    assert preview["changes"][0]["after"] == "红色开场。蓝色中段。"
    # 未确认前不得写入。
    assert order(workspace(client, job)) == SHOTS
    assert client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}",
                       json={"outcome": "allowed-once"}).status_code == 200
    events = wait_idle(client, sid)
    after = workspace(client, job)
    assert order(after) == [SHOTS[0], SHOTS[2]]
    merged = content(after, SHOTS[0])
    assert merged["text"] == "红色开场。蓝色中段。" and merged["query"] == "红色；蓝色"
    assert merged["estimated_seconds"] == 3.5 and merged["recall"] == RECALL
    assert after["plans"][after["active_plan"]]["shots"][SHOTS[2]]["content"] == untouched
    types = [e["type"] for e in events]
    assert types.index("plugin:mindex/approval-asked") < types.index("plugin:mindex/approval-decided") \
        < types.index("tool/result")
    mutation = next(e for e in events if e["type"] == "plugin:mindex/mutation")
    assert mutation["data"] == {"domain": "studio", "job_id": job, "plan_id": after["active_plan"]}
    assert mutation["ignorable"] is True
    assert "已修改分镜：3 → 2" in result_text(results(events)[0])


def test_reject_and_cancelled_do_not_mutate(agent):
    client, llm, job = agent
    llm.extend([call("edit_shots", job_id=job, **MERGE)], [say("好的，不合并。")])
    sid = new_session(client)
    send(client, sid, "合并")
    pending = wait_pending(client, sid, "approval")
    client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "rejected"})
    events = wait_idle(client, sid)
    assert order(workspace(client, job)) == SHOTS
    result = results(events)[0]
    assert result["error"]["code"] == "rejected" and result["message"]["isError"]
    assert not any(e["type"] == "plugin:mindex/mutation" for e in events)
    assert client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}",
                       json={"outcome": "allowed-once"}).status_code == 404
    assert client.post(f"/api/agent/sessions/{sid}/approvals/x", json={"outcome": "maybe"}).status_code == 422

    llm.extend([call("edit_shots", job_id=job, **MERGE)])
    send(client, sid, "再试")
    wait_pending(client, sid, "approval")
    client.post(f"/api/agent/sessions/{sid}/cancel")
    events = wait_idle(client, sid)
    assert order(workspace(client, job)) == SHOTS
    assert any(e["type"] == "plugin:mindex/approval-decided" and e["data"]["outcome"] == "cancelled" for e in events)


def test_stale_revision_is_recoverable(agent):
    client, llm, job = agent
    llm.extend([call("edit_shots", job_id=job, **MERGE)], [say("工作台变了，我重新读取。")])
    sid = new_session(client)
    send(client, sid, "合并")
    pending = wait_pending(client, sid, "approval")
    data = workspace(client, job)
    renamed = client.patch(f"/api/studio/jobs/{job}/plans/{data['active_plan']}",
                           json={"revision": data["revision"], "name": "用户在页面上改了名"})
    assert renamed.status_code == 200
    client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})
    events = wait_idle(client, sid)
    result = results(events)[0]
    assert result["message"]["isError"] and "方案已变化" in result_text(result)
    assert order(workspace(client, job)) == SHOTS
    assert events[-1]["data"]["reason"] == {"kind": "completed"}


def test_session_grant_skips_later_write_approvals(agent):
    client, llm, job = agent
    update = {"operations": [{"op": "update", "shot": "#3", "fields": {"query": "绿色 草地"}}]}
    llm.extend([call("edit_shots", job_id=job, **update)], [say("好")],
               [call("edit_shots", job_id=job, operations=[{"op": "delete", "shot": "#3"}])], [say("删了")])
    sid = new_session(client)
    send(client, sid, "改查询")
    pending = wait_pending(client, sid, "approval")
    client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}",
                json={"outcome": "allowed-once", "grant": True})
    wait_idle(client, sid)
    assert client.get(f"/api/agent/sessions/{sid}").json()["grants"] == ["edit_shots"]
    send(client, sid, "删掉第三镜")
    events = wait_idle(client, sid)
    assert order(workspace(client, job)) == SHOTS[:2]
    assert sum(1 for e in events if e["type"] == "plugin:mindex/approval-asked") == 1
    assert any(e["type"] == "plugin:mindex/grant" for e in events)


def test_no_approval_channel_fails_closed(config, job):
    llm = ScriptedLlm([[call("edit_shots", job_id=job, **MERGE)], [say("无法执行")]])
    app = create_app(config, agent_llm=llm)
    app.state.agent_runtime.approvals = False
    with TestClient(app) as client:
        sid = new_session(client)
        send(client, sid, "合并")
        events = wait_idle(client, sid)
    assert results(events)[0]["error"]["code"] == "unavailable"
    assert order(TestClient(app).get(f"/api/studio/jobs/{job}/workspace").json()) == SHOTS


def test_other_write_tools(agent):
    client, llm, job = agent
    data = workspace(client, job)
    pid = data["active_plan"]
    scripted = [
        [call("update_plan_options", job_id=job, changes={"recall_limit": 7, "voice": "Ethan"}, name="改过的方案")],
        [call("edit_script", job_id=job, text="新的文案。")],
        [call("create_plan", job_id=job, name="方案二", copy_from=pid)],
        [call("activate_plan", job_id=job, plan_id=pid)],
        [call("create_job", outline="做一个关于秋天的短片")],
        [say("都完成了")],
    ]
    llm.extend(*scripted)
    sid = new_session(client)
    send(client, sid, "一系列修改")
    for _ in range(5):
        pending = wait_pending(client, sid, "approval")
        assert pending["preview"]["summary"]
        client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})
        deadline_seen = pending["requestId"]
        for _ in range(500):
            now = client.get(f"/api/agent/sessions/{sid}").json()["pending"]
            if not now or now[0]["requestId"] != deadline_seen:
                break
    events = wait_idle(client, sid)
    assert all(not r["message"].get("isError") for r in results(events)), [result_text(r) for r in results(events)]
    after = workspace(client, job)
    plan = after["plans"][pid]
    assert plan["options"]["recall_limit"] == 7 and plan["options"]["voice"] == "Ethan"
    assert plan["options"]["voice_mode"] == "custom" and plan["name"] == "改过的方案"
    assert after["script_stale"] is True and after["active_plan"] == pid and len(after["plans"]) == 2
    assert len(client.get("/api/studio/jobs").json()["items"]) == 2
    created = next(e for e in events if e["type"] == "plugin:mindex/mutation" and e["data"].get("created"))
    assert created["data"]["job_id"] != job


def test_ambiguous_and_non_adjacent_merge_errors(agent):
    client, llm, job = agent
    llm.extend([call("edit_shots", job_id=job, operations=[{"op": "merge", "shots": ["#1", "#3"]}])],
               [call("edit_shots", job_id=job, operations=[{"op": "delete", "shot": "zzzz"}])], [say("需要确认")])
    sid = new_session(client)
    send(client, sid, "合并一和三")
    events = wait_idle(client, sid)
    codes = [r["error"]["code"] for r in results(events)]
    assert codes == ["not_adjacent", "missing_shot"]  # 预览阶段即失败，不会请求确认
    assert not any(e["type"] == "plugin:mindex/approval-asked" for e in events)
