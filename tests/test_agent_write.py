"""智能助手确认策略与 Studio 写操作：预览、允许/拒绝、会话授权、失败关闭、乐观锁冲突、合并规则。"""

from __future__ import annotations

import pytest
from agent_helpers import new_session, result_text, results, send, wait_idle, wait_pending
from conftest import legacy_job
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.tools.common import PlanView, board_text
from mindex.agent.tools.studio_write import ShotFields, ShotOperation, merge_into, plan_shots
from mindex.api import create_app
from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.studio import VISUAL_LIMIT

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


def page(job):
    """创作页上下文：自动加载 studio 领域（编辑类工具都在该领域）。"""
    return {"page": "studio", "job_id": job}


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


VISUAL_TEXT = st.text(alphabet=st.sampled_from(list("画面意象夜市ab 　\t\n；😀👨\u200d")), max_size=80)
VISUAL_VALUE = st.one_of(st.none(), st.text(alphabet=" \t\n　", max_size=4), VISUAL_TEXT)


# Feature: studio-material-consistency, Property 20: 合并 visual 的拼接与截断
@settings(max_examples=100, deadline=None)
@given(visuals=st.lists(VISUAL_VALUE, min_size=2, max_size=5))
def test_merge_visual_join_and_clip(visuals):
    """**Validates: Requirements 8.6, 8.9**"""
    def shot(visual):
        data = {"text": "甲", "query": "甲", "estimated_seconds": 1, "recall": None}
        if visual is not None:
            data["visual"] = visual
        return data

    merged = shot(visuals[0])
    for visual in visuals[1:]:
        merge_into(merged, shot(visual))
    expected = "；".join(v.strip() for v in visuals if v and v.strip())[:VISUAL_LIMIT]
    if expected:
        assert merged["visual"] == expected
    else:
        assert "visual" not in merged


def test_merge_with_approval(agent):
    client, llm, job = agent
    before = workspace(client, job)
    untouched = before["plans"][before["active_plan"]]["shots"][SHOTS[2]]["content"]
    llm.extend([call("edit_shots", call_id="m1", job_id=job, **MERGE)], [say("已合并。")])
    sid = new_session(client)
    send(client, sid, "合并 a1daad59 和 925281d5", context=page(job))
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
    send(client, sid, "合并", context=page(job))
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
    send(client, sid, "再试", context=page(job))
    wait_pending(client, sid, "approval")
    client.post(f"/api/agent/sessions/{sid}/cancel")
    events = wait_idle(client, sid)
    assert order(workspace(client, job)) == SHOTS
    assert any(e["type"] == "plugin:mindex/approval-decided" and e["data"]["outcome"] == "cancelled" for e in events)


def test_stale_revision_is_recoverable(agent):
    client, llm, job = agent
    llm.extend([call("edit_shots", job_id=job, **MERGE)], [say("工作台变了，我重新读取。")])
    sid = new_session(client)
    send(client, sid, "合并", context=page(job))
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
    send(client, sid, "改查询", context=page(job))
    pending = wait_pending(client, sid, "approval")
    client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}",
                json={"outcome": "allowed-once", "grant": True})
    wait_idle(client, sid)
    assert client.get(f"/api/agent/sessions/{sid}").json()["grants"] == ["edit_shots"]
    send(client, sid, "删掉第三镜", context=page(job))
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
        send(client, sid, "合并", context=page(job))
        events = wait_idle(client, sid)
        assert results(events)[0]["error"]["code"] == "unavailable"
        # API lifespan 退出即关闭后端上下文；终态核对须在同一应用生命周期内。
        assert order(client.get(f"/api/studio/jobs/{job}/workspace").json()) == SHOTS


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
    send(client, sid, "一系列修改", context=page(job))
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
    send(client, sid, "合并一和三", context=page(job))
    events = wait_idle(client, sid)
    codes = [r["error"]["code"] for r in results(events)]
    assert codes == ["not_adjacent", "missing_shot"]  # 预览阶段即失败，不会请求确认
    assert not any(e["type"] == "plugin:mindex/approval-asked" for e in events)


# ------------------------------------------------------------ 画面意象 visual（需求 8.9–8.11）
def test_update_sets_and_clears_visual(agent):
    client, llm, job = agent
    set_op = {"operations": [{"op": "update", "shot": "#2", "fields": {"visual": "  湖边安静的村落  "}}]}
    clear_op = {"operations": [{"op": "update", "shot": "#2", "fields": {"visual": ""}}]}
    llm.extend([call("edit_shots", job_id=job, **set_op)], [say("已设置")],
               [call("edit_shots", job_id=job, **clear_op)], [say("已清空")])
    sid = new_session(client)
    send(client, sid, "设置第二镜画面意象", context=page(job))
    pending = wait_pending(client, sid, "approval")
    change = pending["preview"]["changes"][0]
    assert change["before"] is None and change["after"] == "湖边安静的村落"
    client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}",
                json={"outcome": "allowed-once", "grant": True})
    events = wait_idle(client, sid)
    assert not results(events)[0]["message"].get("isError"), result_text(results(events)[0])
    after = workspace(client, job)
    assert content(after, SHOTS[1])["visual"] == "湖边安静的村落"
    assert "visual" not in content(after, SHOTS[0])

    send(client, sid, "清空第二镜画面意象", context=page(job))
    events = wait_idle(client, sid)
    assert not results(events)[0]["message"].get("isError"), result_text(results(events)[0])
    cleared = workspace(client, job)
    assert "visual" not in content(cleared, SHOTS[1])
    assert content(cleared, SHOTS[1])["query"] == "蓝色" and order(cleared) == SHOTS


def test_split_inherits_visual(agent):
    client, _llm, job = agent
    ws_data = workspace(client, job)
    shots = PlanView(ws_data).shots()
    shots[0]["visual"] = "红色幕布拉开"
    pid = ws_data["active_plan"]
    saved = client.patch(f"/api/studio/jobs/{job}/plans/{pid}",
                         json={"revision": ws_data["revision"], "shots": shots})
    assert saved.status_code == 200, saved.text
    view = PlanView(workspace(client, job))
    split = ShotOperation(op="split", shot="#1", parts=[
        ShotFields(text="红色"), ShotFields(text="开场。", visual="舞台中央的红色灯光")])
    planned, _, _ = plan_shots(view, [split])
    assert planned[0]["visual"] == "红色幕布拉开"            # 未提供 visual 的段继承原镜
    assert planned[1]["visual"] == "舞台中央的红色灯光"
    assert planned[0]["id"] == SHOTS[0] and "id" not in planned[1]
    cleared = ShotOperation(op="split", shot="#1", parts=[ShotFields(text="红色", visual=" "), ShotFields(text="开场。")])
    planned, _, _ = plan_shots(view, [cleared])
    assert "visual" not in planned[0] and planned[1]["visual"] == "红色幕布拉开"


def test_overlong_visual_is_rejected_without_patch(agent):
    client, llm, job = agent
    before = workspace(client, job)
    too_long = "夜" * (VISUAL_LIMIT + 1)
    op = {"operations": [{"op": "update", "shot": "#1", "fields": {"visual": too_long}}]}
    llm.extend([call("edit_shots", job_id=job, **op)], [say("画面意象太长")])
    sid = new_session(client)
    send(client, sid, "设置很长的画面意象", context=page(job))
    events = wait_idle(client, sid)
    result = results(events)[0]
    assert result["error"]["code"] == "invalid_shot" and result["message"]["isError"]
    assert "120" in result_text(result) and "第 1 个分镜" in result_text(result)
    assert not any(e["type"] == "plugin:mindex/approval-asked" for e in events)
    assert not any(e["type"] == "plugin:mindex/mutation" for e in events)
    after = workspace(client, job)
    assert after["revision"] == before["revision"]
    assert after["plans"][after["active_plan"]]["shots"] == before["plans"][before["active_plan"]]["shots"]
    # 恰好 120 字可通过预览。
    view = PlanView(after)
    exact = ShotOperation(op="update", shot="#1", fields=ShotFields(visual="夜" * VISUAL_LIMIT))
    assert plan_shots(view, [exact])[0][0]["visual"] == "夜" * VISUAL_LIMIT


def test_merged_visual_cut_on_whitespace_is_stripped():
    """120 字截断恰好落在空白上时，存储值去掉尾部空白（与 Shot 的 visual 规范化一致）。"""
    first = {"text": "甲", "query": "甲", "estimated_seconds": 1, "recall": None, "visual": "甲" * 119}
    merge_into(first, {"text": "乙", "query": "乙", "estimated_seconds": 1, "recall": None, "visual": "乙乙"})
    # "甲"*119 + "；" 正好 120 字，"；" 不是空白，保留。
    assert first["visual"] == "甲" * 119 + "；"
    spaced = {"text": "甲", "query": "甲", "estimated_seconds": 1, "recall": None, "visual": "甲" * 118 + " 丙"}
    merge_into(spaced, {"text": "乙", "query": "乙", "estimated_seconds": 1, "recall": None, "visual": "乙"})
    # 中间的空白不受影响：拼接为 "甲"*118 + " 丙；乙"，截到 120 字是 "甲"*118 + " 丙"。
    assert spaced["visual"] == "甲" * 118 + " 丙"
    edge = {"text": "甲", "query": "甲", "estimated_seconds": 1, "recall": None, "visual": "甲" * 119 + " 丁"}
    merge_into(edge, {"text": "乙", "query": "乙", "estimated_seconds": 1, "recall": None, "visual": "乙"})
    # 截到 120 字为 "甲"*119 + " "，尾部空白被去掉。
    assert edge["visual"] == "甲" * 119
    assert edge["visual"] == studio.Shot(id="shot-x", text="甲", query="甲", estimated_seconds=1,
                                         visual="甲" * 119 + " ").visual


def test_shot_rows_include_visual(agent):
    client, _llm, job = agent
    ws_data = workspace(client, job)
    shots = PlanView(ws_data).shots()
    shots[1]["visual"] = "蓝色海面"
    saved = client.patch(f"/api/studio/jobs/{job}/plans/{ws_data['active_plan']}",
                         json={"revision": ws_data["revision"], "shots": shots})
    assert saved.status_code == 200, saved.text
    view = PlanView(workspace(client, job))
    rows = view.shot_rows()
    assert [row["visual"] for row in rows] == [None, "蓝色海面", None]
    text = board_text(view, rows)
    assert text.count("画面意象：") == 1 and "  画面意象：蓝色海面" in text


def test_create_job_records_user_words_as_original_input(agent):
    """create_job 的任务大纲是助手整理的文字；任务的 original_input 记录用户原话：
    上一次 create_job 之后用户发的全部消息（按顺序空行连接），不含页面上下文与跟进消息。"""
    client, llm, job = agent
    llm.extend([say("好的，请问时长多少？")],
               [call("create_job", outline="A 60-second autumn short film about falling leaves")],
               [say("已创建")],
               [call("create_job", outline="A winter short film")],
               [say("已创建")])
    sid = new_session(client)

    def approve():
        pending = wait_pending(client, sid, "approval")
        client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})

    send(client, sid, "做一个关于秋天落叶的短片", context=page(job))
    wait_idle(client, sid)
    send(client, sid, "一分钟左右", context=page(job))
    approve()
    wait_idle(client, sid)
    send(client, sid, "再做一个冬天的", context=page(job))
    approve()
    wait_idle(client, sid)
    detail = {}
    for item in client.get("/api/studio/jobs").json()["items"]:
        job = client.get(f"/api/studio/jobs/{item['id']}/workspace").json()["job"]
        detail[job["outline"]] = job
    assert detail["A 60-second autumn short film about falling leaves"]["original_input"] == "做一个关于秋天落叶的短片\n\n一分钟左右"
    assert detail["A winter short film"]["original_input"] == "再做一个冬天的"
