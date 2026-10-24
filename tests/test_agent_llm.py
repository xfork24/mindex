"""智能助手模型协议层：请求构造、流式工具调用拼装、结束原因与用量映射、错误码、角色档案。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mindex.agent.llm.anthropic import AnthropicClient
from mindex.agent.llm.anthropic import convert_messages as anthropic_messages
from mindex.agent.llm.openai_compat import OpenAICompatClient
from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.llm.types import LlmError, LlmRequest, collect, tool_message, user_message
from mindex.api import create_app
from mindex.settings import settings_api
from mindex.settings.runtime_settings import PROFILE_FIELDS, PROVIDER_PRESETS, merge_profile
from mindex.shared.config import AgentLlmConfig

TOOLS = [{"name": "list_jobs", "description": "列出任务", "parameters": {"type": "object", "properties": {}}}]


def sse(events: list[dict], done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def history():
    assistant = {
        "id": "a1", "role": "assistant", "source": {"kind": "model", "provider": "x", "model": "y"},
        "content": [{"type": "text", "text": "查一下"},
                    {"type": "tool-call", "id": "c1", "name": "list_jobs", "arguments": "{}"}],
    }
    return [user_message("列出任务"), assistant, tool_message("c1", '{"items": []}')]


def run(client, request):
    return asyncio.run(collect(client, request))


def test_openai_request_and_streamed_tool_call():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=sse([
            {"choices": [{"delta": {"reasoning_content": "想一想"}}]},
            {"choices": [{"delta": {"content": "好的，"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "function": {
                "name": "list_jobs", "arguments": "{\"li"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "mit\": 3}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 9,
                                      "prompt_tokens_details": {"cached_tokens": 100}}},
        ]))

    profile = AgentLlmConfig(base_url="https://llm.test/v1", model="m", api_key="sk-test-123456789")
    client = OpenAICompatClient(profile, transport=httpx.MockTransport(handler))
    result = run(client, LlmRequest(system="系统", messages=history(), tools=TOOLS))
    body = seen["body"]
    assert seen["url"] == "https://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test-123456789"
    assert body["stream"] is True and body["tools"][0]["function"]["name"] == "list_jobs"
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    assert body["messages"][2]["tool_calls"][0]["id"] == "c1"
    assert body["messages"][3]["tool_call_id"] == "c1"
    assert [b["type"] for b in result.content] == ["reasoning", "text", "tool-call"]
    assert result.content[2] == {"type": "tool-call", "id": "call_9", "name": "list_jobs",
                                 "arguments": "{\"limit\": 3}"}
    assert result.finish == {"kind": "tool-calls"}
    assert result.usage == {"inputTokens": 20, "outputTokens": 9, "cacheReadTokens": 100}


def test_openai_length_and_http_errors():
    def handler(_):
        return httpx.Response(200, content=sse([{"choices": [{"delta": {"content": "半"}, "finish_reason": "length"}]}]))

    profile = AgentLlmConfig(base_url="https://llm.test/v1", model="m")
    result = run(OpenAICompatClient(profile, transport=httpx.MockTransport(handler)),
                 LlmRequest(system="", messages=[user_message("x")]))
    assert result.finish == {"kind": "max-tokens"}

    calls = []

    def failing(_):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LlmError) as info:
        run(OpenAICompatClient(profile, transport=httpx.MockTransport(failing)),
            LlmRequest(system="", messages=[user_message("x")]))
    assert info.value.code == "http_401" and info.value.status == 401 and len(calls) == 1

    attempts = []

    def flaky(_):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]))

    result = run(OpenAICompatClient(profile, transport=httpx.MockTransport(flaky)),
                 LlmRequest(system="", messages=[user_message("x")]))
    assert result.content == [{"type": "text", "text": "ok"}] and len(attempts) == 2


def test_anthropic_request_and_stream():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        seen["url"] = str(request.url)
        return httpx.Response(200, content=sse([
            {"type": "message_start", "message": {"usage": {"input_tokens": 50, "output_tokens": 1,
                                                            "cache_read_input_tokens": 7}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1,
             "content_block": {"type": "tool_use", "id": "toolu_1", "name": "list_jobs", "input": {}}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"a\""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ":1}"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
            {"type": "message_stop"},
        ], done=False))

    profile = AgentLlmConfig(backend="anthropic", base_url="https://api.anthropic.test", model="claude",
                             api_key="sk-ant-123456789")
    result = run(AnthropicClient(profile, transport=httpx.MockTransport(handler)),
                 LlmRequest(system="系统", messages=history(), tools=TOOLS))
    body = seen["body"]
    assert seen["url"] == "https://api.anthropic.test/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-123456789"
    assert seen["headers"]["anthropic-version"]
    assert body["system"] == "系统" and body["tools"][0]["input_schema"] == TOOLS[0]["parameters"]
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "list_jobs", "input": {}}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert result.content[1] == {"type": "tool-call", "id": "toolu_1", "name": "list_jobs", "arguments": "{\"a\":1}"}
    assert result.finish == {"kind": "tool-calls"}
    assert result.usage == {"inputTokens": 50, "outputTokens": 30, "cacheReadTokens": 7}


def test_anthropic_merges_consecutive_roles():
    messages = [user_message("a"), user_message("b"), tool_message("x", "r")]
    converted = anthropic_messages(messages)
    assert len(converted) == 1 and len(converted[0]["content"]) == 3


def test_scripted_llm_roundtrip():
    llm = ScriptedLlm([[say("你好"), call("list_jobs", call_id="c1", limit=2)], [say("完成")]])
    first = run(llm, LlmRequest(system="", messages=[user_message("x")]))
    assert first.finish["kind"] == "tool-calls"
    assert json.loads(first.content[1]["arguments"]) == {"limit": 2}
    assert run(llm, LlmRequest(system="", messages=[])).content == [say("完成")]
    assert len(llm.requests) == 2


def test_agent_role_profiles(config):
    assert PROFILE_FIELDS["agent"] >= {"backend", "base_url", "model", "max_tokens"}
    assert PROVIDER_PRESETS["anthropic"]["agent"]["config"]["backend"] == "anthropic"
    for provider in ("deepseek", "aliyun", "zhipu"):
        assert PROVIDER_PRESETS[provider]["agent"]["config"]["backend"] == "openai"
    merged = merge_profile(config, "agent", {"config": {"backend": "anthropic", "model": "c", "base_url": "u"},
                                             "api_key": "k"})
    assert merged.agent.llm.backend == "anthropic" and merged.agent.llm.api_key == "k"
    assert config.agent.llm.backend == "openai"  # 原对象不变

    client = TestClient(create_app(config))
    created = client.post("/api/settings/profiles", json={
        "role": "agent", "name": "DeepSeek 助手", "provider": "deepseek",
        "config": {"backend": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
        "api_key": "sk-secret-abcdef123",
    })
    assert created.status_code == 201, created.text
    assert "sk-secret" not in created.text
    roles = {r["role"]: r for r in client.get("/api/settings?probe_embedding=false").json()["roles"]}
    assert roles["agent"]["label"] == "智能助手"
    assert roles["agent"]["profiles"][0]["name"] == "DeepSeek 助手"
    assert client.post(f"/api/settings/profiles/{created.json()['id']}/activate").status_code == 200
    effective = client.get("/api/settings?probe_embedding=false").json()["roles"]
    assert next(r for r in effective if r["role"] == "agent")["effective"]["model"] == "deepseek-chat"
    bad = client.post("/api/settings/profiles", json={
        "role": "agent", "name": "坏", "provider": "custom", "config": {"llama_template": True}})
    assert bad.status_code == 422


@pytest.mark.parametrize("calls_tool", [True, False])
def test_agent_probe_verifies_tool_calling(config, monkeypatch, calls_tool):
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "echo"
        delta = ({"tool_calls": [{"index": 0, "id": "e1", "function": {"name": "echo", "arguments": "{\"text\":\"ok\"}"}}]}
                 if calls_tool else {"content": "ok"})
        return httpx.Response(200, content=sse([{"choices": [{"delta": delta, "finish_reason": "stop"}]}]))

    original = settings_api._probe_agent
    monkeypatch.setattr(settings_api, "_probe_agent",
                        lambda cfg: original(cfg, transport=httpx.MockTransport(handler)))
    client = TestClient(create_app(config))
    profile = client.post("/api/settings/profiles", json={
        "role": "agent", "name": "测试", "provider": "custom",
        "config": {"backend": "openai", "base_url": "https://llm.test/v1", "model": "m"}}).json()
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is calls_tool
    assert result["tool_calling"] is calls_tool
