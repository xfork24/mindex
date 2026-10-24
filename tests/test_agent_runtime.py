"""智能助手运行时：dsh 形状事件、多步工具调用、日志派生回放、SSE 续传、取消、并发、重启恢复。"""

from __future__ import annotations

import pytest
from agent_helpers import final_text, new_session, parse_sse, result_text, results, send, wait_idle
from fastapi.testclient import TestClient

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.llm.types import tool_message, user_message
from mindex.agent.session_store import OMITTED, SessionStore, derive_messages
from mindex.api import create_app

CORE = {"turn/start", "turn/end", "step/start", "step/end", "user/message", "assistant/message", "tool/call",
        "tool/result"}


@pytest.fixture
def agent(config):
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm


def test_multi_step_tool_call_and_replay(agent):
    client, llm = agent
    llm.extend([say("我来查一下。"), call("list_jobs", call_id="c1")], [say("目前没有创作任务。")])
    sid = new_session(client)
    send(client, sid, "列出我的创作任务", context={"page": "studio"})
    events = wait_idle(client, sid)
    types = [e["type"] for e in events]
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert all(e["session_id"] == sid and isinstance(e["time"], int) for e in events)
    assert set(t for t in types if not t.startswith("plugin:")) <= CORE
    assert types[0] == "turn/start" and types[-1] == "turn/end"
    assert events[-1]["data"]["reason"] == {"kind": "completed"}
    sources = [e["data"]["source"]["kind"] for e in events if e["type"] == "user/message"]
    assert sources == ["context", "user"]
    call_event = next(e for e in events if e["type"] == "tool/call")
    assert call_event["data"] == {"turn": 1, "step": 1, "callId": "c1", "name": "list_jobs", "arguments": "{}"}
    result = results(events)[0]
    assert result["message"]["toolCallId"] == "c1" and "共 0 个任务" in result_text(result)
    assert final_text(events) == "目前没有创作任务。"
    usage = next(e for e in events if e["type"] == "assistant/message")["data"]["usage"]
    assert set(usage) == {"inputTokens", "outputTokens"}
    # 发给模型的历史 = 由日志派生：第二次请求的 messages 与从日志重新派生的结果完全一致。
    replay = derive_messages([e for e in events if e["seq"] < next(
        x["seq"] for x in events if x["type"] == "step/start" and x["data"]["step"] == 2)])
    assert llm.requests[1].messages == replay
    assert {t["name"] for t in llm.requests[0].tools} >= {"list_jobs", "get_workspace_summary"}
    assert client.get("/api/agent/sessions").json()["items"][0]["title"] == "列出我的创作任务"

    # 同一上下文不重复注入；上下文变化才追加。
    llm.extend([say("好")], [say("好")])
    send(client, sid, "再说一次", context={"page": "studio"})
    events = wait_idle(client, sid)
    assert sum(1 for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "context") == 1


def test_sse_resume_and_status(agent):
    client, llm = agent
    llm.extend([say("你好")])
    sid = new_session(client)
    send(client, sid, "hi")
    events = wait_idle(client, sid)
    raw = client.get(f"/api/agent/sessions/{sid}/events", params={"after": 2, "follow": "false"}).text
    frames = parse_sse(raw)
    seqs = [data["seq"] for name, data in frames if name == "session.event"]
    assert seqs == list(range(3, len(events)))
    assert frames[-1][0] == "session.status" and frames[-1][1]["status"] == "idle"


def test_cancel_and_busy(agent):
    client, llm = agent
    llm.extend({"blocks": [say("很慢的回答")], "delay": 5})
    sid = new_session(client)
    send(client, sid, "慢一点")
    busy = client.post(f"/api/agent/sessions/{sid}/messages", json={"content": "插话"})
    assert busy.status_code == 409
    assert client.post(f"/api/agent/sessions/{sid}/cancel").json()["status"] == "idle"
    events = wait_idle(client, sid)
    assert events[-1]["type"] == "turn/end"
    assert events[-1]["data"]["reason"] == {"kind": "aborted", "reason": {"kind": "user"}}
    llm.extend([say("恢复正常")])
    send(client, sid, "继续")
    assert final_text(wait_idle(client, sid)) == "恢复正常"


def test_llm_error_unknown_tool_and_bad_arguments(agent):
    client, llm = agent
    llm.extend(
        [{"type": "tool-call", "id": "x1", "name": "no_such_tool", "arguments": "{}"},
         {"type": "tool-call", "id": "x2", "name": "list_jobs", "arguments": "{bad"},
         {"type": "tool-call", "id": "x3", "name": "list_jobs", "arguments": "{\"limit\": 0}"}],
        {"blocks": [], "error": "上游 500"},
    )
    sid = new_session(client)
    send(client, sid, "试试", context={"page": "studio"})
    events = wait_idle(client, sid)
    errors = [r["error"]["code"] for r in results(events)]
    assert errors == ["UNKNOWN_TOOL", "invalid_arguments", "invalid_arguments"]
    assert all(r["message"]["isError"] for r in results(events))
    assert events[-1]["data"]["reason"]["kind"] == "error"
    assert events[-1]["data"]["reason"]["error"]["message"] == "上游 500"


def test_max_steps(config):
    config.agent.max_steps = 2
    llm = ScriptedLlm([[call("list_jobs")]] * 5)
    with TestClient(create_app(config, agent_llm=llm)) as client:
        sid = new_session(client)
        send(client, sid, "无限循环", context={"page": "studio"})
        events = wait_idle(client, sid)
    assert events[-1]["data"]["reason"] == {"kind": "blocked"}
    assert any(e["type"] == "plugin:mindex/notice" and e.get("ignorable") for e in events)


def test_restart_closes_open_turn(config):
    store = SessionStore(config.data_dir)
    session = store.create("中断的会话")
    sid = session["id"]
    store.append(sid, "turn/start", {"turn": 1})
    store.append(sid, "user/message", user_message("做点事"))
    store.append(sid, "assistant/message", {"turn": 1, "step": 1, "message": {
        "id": "m", "role": "assistant", "source": {"kind": "model", "provider": "p", "model": "m"},
        "content": [{"type": "tool-call", "id": "c9", "name": "list_jobs", "arguments": "{}"}]}})
    store.append(sid, "tool/call", {"turn": 1, "step": 1, "callId": "c9", "name": "list_jobs", "arguments": "{}"})
    store.update(sid, status="running")
    with TestClient(create_app(config, agent_llm=ScriptedLlm())) as client:
        data = client.get(f"/api/agent/sessions/{sid}").json()
        assert data["status"] == "idle"
        events = client.get(f"/api/agent/sessions/{sid}/log").json()["items"]
    assert events[-1] == {**events[-1], "type": "turn/end", "data": {"turn": 1, "reason": {"kind": "interrupted"}}}
    closed = results(events)[0]
    assert closed["message"]["toolCallId"] == "c9" and closed["error"]["code"] == "interrupted"


def test_derive_messages_budget_and_missing_results():
    assistant = {"id": "a", "role": "assistant", "source": {"kind": "model"},
                 "content": [{"type": "tool-call", "id": "c1", "name": "t", "arguments": "{}"}]}
    events = [{"type": "user/message", "data": user_message("q")},
              {"type": "assistant/message", "data": {"message": assistant}}]
    derived = derive_messages(events)
    assert derived[-1]["role"] == "tool" and derived[-1]["isError"]
    big = [{"type": "user/message", "data": user_message("q")}]
    for index in range(10):
        call_id = f"c{index}"
        big.append({"type": "assistant/message", "data": {"message": {**assistant, "content": [
            {"type": "tool-call", "id": call_id, "name": "t", "arguments": "{}"}]}}})
        big.append({"type": "tool/result", "data": {"message": tool_message(call_id, "x" * 3000)}})
    trimmed = derive_messages(big, budget=4000, keep_recent_results=2)
    tools = [m for m in trimmed if m["role"] == "tool"]
    assert tools[0]["content"][0]["text"] == OMITTED and tools[-1]["content"][0]["text"] == "x" * 3000


def test_session_crud(agent):
    client, _ = agent
    sid = new_session(client)
    assert client.get(f"/api/agent/sessions/{sid}").json()["status"] == "idle"
    assert client.delete(f"/api/agent/sessions/{sid}").status_code == 204
    assert client.get(f"/api/agent/sessions/{sid}").status_code == 404
    assert client.get("/api/agent/status").json()["model"] == "scripted-test"
