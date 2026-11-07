"""智能助手运行时：dsh 形状事件、多步工具调用、日志派生回放、SSE 续传、取消、并发、重启恢复。"""

from __future__ import annotations

from threading import Event

import pytest
from fastapi.testclient import TestClient

from agent.llm.scripted import ScriptedLlm, call, say
from agent.llm.types import assistant_message, tool_calls, tool_message, user_message
from agent.session_store import OMITTED, SessionStore, derive_messages
from agent_helpers import final_text, new_session, parse_sse, result_text, results, send, wait_idle
from api import create_app
from shared.config import AgentConfig

CORE = {
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "user/message",
    "assistant/message",
    "tool/call",
    "tool/result",
}


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
    assert call_event["data"] == {
        "turn": 1,
        "step": 1,
        "callId": "c1",
        "name": "list_jobs",
        "arguments": "{}",
    }
    result = results(events)[0]
    assert result["message"]["toolCallId"] == "c1" and "共 0 个任务" in result_text(result)
    assert final_text(events) == "目前没有创作任务。"
    usage = next(e for e in events if e["type"] == "assistant/message")["data"]["usage"]
    assert set(usage) == {"inputTokens", "outputTokens"}
    # 发给模型的历史 = 由日志派生：第二次请求的 messages 与从日志重新派生的结果完全一致。
    replay = derive_messages(
        [
            e
            for e in events
            if e["seq"]
            < next(x["seq"] for x in events if x["type"] == "step/start" and x["data"]["step"] == 2)
        ]
    )
    assert llm.requests[1].messages == replay
    assert {t["name"] for t in llm.requests[0].tools} >= {"list_jobs", "get_workspace_summary"}
    assert client.get("/api/agent/sessions").json()["items"][0]["title"] == "列出我的创作任务"

    # 同一上下文不重复注入；上下文变化才追加。
    llm.extend([say("好")], [say("好")])
    send(client, sid, "再说一次", context={"page": "studio"})
    events = wait_idle(client, sid)
    assert (
        sum(1 for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "context")
        == 1
    )


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
        [
            {"type": "tool-call", "id": "x1", "name": "no_such_tool", "arguments": "{}"},
            {"type": "tool-call", "id": "x2", "name": "list_jobs", "arguments": "{bad"},
            {"type": "tool-call", "id": "x3", "name": "list_jobs", "arguments": '{"limit": 0}'},
        ],
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


def test_unlimited_steps_with_rolling_window(config):
    # 即使配置文件仍带旧的步数限制，也应跨过 16 步和 256 步后正常完成。
    config.agent = AgentConfig.model_validate({"max_steps": 2, "max_context_tokens": 1_000_000})
    llm = ScriptedLlm([[call("list_jobs", call_id=f"c{i}")] for i in range(260)] + [[say("全部完成")]])
    with TestClient(create_app(config, agent_llm=llm)) as client:
        sid = new_session(client)
        send(client, sid, "完成长任务", context={"page": "studio"})
        # 260 次真实工具调用与 SQLite 落盘在 CI 上可能超过普通会话的 30 秒等待预算。
        events = wait_idle(client, sid, timeout=120)
        status = client.get("/api/agent/status").json()
        assert status["max_steps"] is None and status["context_window_steps"] == 256
    assert events[-1]["data"]["reason"] == {"kind": "completed"}
    assert final_text(events) == "全部完成"
    assert [e["data"]["step"] for e in events if e["type"] == "step/start"] == list(range(1, 262))
    assert len(results(events)) == 260  # 日志不随模型窗口删除。
    for index, request in enumerate(llm.requests):
        calls = [c["id"] for m in request.messages if m["role"] == "assistant" for c in tool_calls(m)]
        answers = [m["toolCallId"] for m in request.messages if m["role"] == "tool"]
        assert calls == answers == [f"c{i}" for i in range(max(0, index - 256), index)]
        assert request.messages[0]["source"]["kind"] == "context"
        assert request.messages[1]["content"][0]["text"] == "完成长任务"


def test_rolling_window_across_turns_and_reopen(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create()["id"]
    store.append(sid, "user/message", user_message("旧任务"))
    store.append(sid, "assistant/message", {"message": assistant_message([say("旧回答")], "p", "m")})
    store.append(sid, "user/message", user_message("页面 A", "context"))
    store.append(sid, "user/message", user_message("当前任务"))
    for step in range(3):
        calls = [call("list_jobs", call_id=f"c{step}-{i}") for i in range(2)]
        store.append(sid, "assistant/message", {"message": assistant_message(calls, "p", "m")})
        for item in calls:
            store.append(sid, "tool/result", {"message": tool_message(item["id"], "结果")})
    store.append(sid, "user/message", user_message("页面 B", "context"))
    store.append(sid, "user/message", user_message("继续", "followup"))
    # 重新打开会话后，窗口仍按完整步骤裁剪，多工具结果不被拆开。
    reopened = SessionStore(tmp_path)
    history = derive_messages(reopened.model_events(sid, window_steps=2))
    assert [m["content"][0]["text"] for m in history if m["role"] == "user"] == [
        "页面 A",
        "当前任务",
        "页面 B",
        "继续",
    ]
    assert [c["id"] for m in history if m["role"] == "assistant" for c in tool_calls(m)] == [
        "c1-0",
        "c1-1",
        "c2-0",
        "c2-1",
    ]
    assert [m["toolCallId"] for m in history if m["role"] == "tool"] == ["c1-0", "c1-1", "c2-0", "c2-1"]
    # 新轮次的回复把上轮最后一步留在窗口内，之前的任务指令仍可解释该步。
    store.append(sid, "assistant/message", {"message": assistant_message([say("完成")], "p", "m")})
    history = derive_messages(store.model_events(sid, window_steps=2))
    assert [m["content"][0]["text"] for m in history if m["role"] == "user"] == [
        "页面 A",
        "当前任务",
        "页面 B",
        "继续",
    ]
    history = derive_messages(store.model_events(sid, window_steps=1))
    assert [m["content"][0]["text"] for m in history if m["role"] == "user"] == ["页面 B", "继续"]
    assert history[-1]["content"][0]["text"] == "完成"


def test_cancel_after_window_rollover(config):
    config.agent.context_window_steps = 2
    reached = Event()

    def pause(_request):
        reached.set()
        return {"blocks": [say("继续处理中")], "delay": 30}

    llm = ScriptedLlm([[call("list_jobs", call_id=f"c{i}")] for i in range(20)] + [pause])
    with TestClient(create_app(config, agent_llm=llm)) as client:
        sid = new_session(client)
        send(client, sid, "处理长任务", context={"page": "studio"})
        assert reached.wait(timeout=10)
        assert [m["toolCallId"] for m in llm.requests[-1].messages if m["role"] == "tool"] == ["c18", "c19"]
        response = client.post(f"/api/agent/sessions/{sid}/cancel")
        assert response.json()["status"] == "idle"
        events = wait_idle(client, sid)
        assert events[-1]["data"]["reason"]["kind"] == "aborted"
        llm.extend([say("已恢复")])
        send(client, sid, "继续")
        assert final_text(wait_idle(client, sid)) == "已恢复"


@pytest.mark.parametrize("size", [0, 257])
def test_context_window_size_bounds(size, tmp_path):
    with pytest.raises(ValueError):
        AgentConfig(context_window_steps=size)
    store = SessionStore(tmp_path)
    with pytest.raises(ValueError, match="1–256"):
        store.model_events(store.create()["id"], window_steps=size)


def test_restart_closes_open_turn(config):
    store = SessionStore(config.data_dir)
    session = store.create("中断的会话")
    sid = session["id"]
    store.append(sid, "turn/start", {"turn": 1})
    store.append(sid, "user/message", user_message("做点事"))
    store.append(
        sid,
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "id": "m",
                "role": "assistant",
                "source": {"kind": "model", "provider": "p", "model": "m"},
                "content": [{"type": "tool-call", "id": "c9", "name": "list_jobs", "arguments": "{}"}],
            },
        },
    )
    store.append(
        sid, "tool/call", {"turn": 1, "step": 1, "callId": "c9", "name": "list_jobs", "arguments": "{}"}
    )
    store.update(sid, status="running")
    with TestClient(create_app(config, agent_llm=ScriptedLlm())) as client:
        data = client.get(f"/api/agent/sessions/{sid}").json()
        assert data["status"] == "idle"
        events = client.get(f"/api/agent/sessions/{sid}/log").json()["items"]
    assert events[-1] == {
        **events[-1],
        "type": "turn/end",
        "data": {"turn": 1, "reason": {"kind": "interrupted"}},
    }
    closed = results(events)[0]
    assert closed["message"]["toolCallId"] == "c9" and closed["error"]["code"] == "interrupted"


def test_derive_messages_budget_and_missing_results():
    assistant = {
        "id": "a",
        "role": "assistant",
        "source": {"kind": "model"},
        "content": [{"type": "tool-call", "id": "c1", "name": "t", "arguments": "{}"}],
    }
    events = [
        {"type": "user/message", "data": user_message("q")},
        {"type": "assistant/message", "data": {"message": assistant}},
    ]
    derived = derive_messages(events)
    assert derived[-1]["role"] == "tool" and derived[-1]["isError"]
    big = [{"type": "user/message", "data": user_message("q")}]
    for index in range(10):
        call_id = f"c{index}"
        big.append(
            {
                "type": "assistant/message",
                "data": {
                    "message": {
                        **assistant,
                        "content": [{"type": "tool-call", "id": call_id, "name": "t", "arguments": "{}"}],
                    }
                },
            }
        )
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
