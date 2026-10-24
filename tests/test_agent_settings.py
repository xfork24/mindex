"""智能助手提问与密钥保险库、设置工具：明文不进日志/模型/SSE，引用过期与跨会话拒绝，自身档案保护。"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest
from agent_helpers import new_session, result_text, results, send, wait_idle, wait_pending
from fastapi.testclient import TestClient

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.api import create_app
from mindex.shared.storage import Store

SECRET = "sk-live-PLAINTEXT-0123456789abcdef"


def last_tool_json(request) -> dict:
    return json.loads(request.messages[-1]["content"][0]["text"])


ASK = call("ask_user_question", questions=[{
    "id": "endpoint", "question": "请提供拆分镜模型的连接信息", "header": "自建模型",
    "fields": [{"id": "base_url", "label": "Endpoint", "required": True},
               {"id": "model", "label": "模型名", "required": True},
               {"id": "api_key", "label": "API Key", "type": "secret"}]}])


def create_from_answer(request):
    values = last_tool_json(request)["answers"][0]["values"]
    assert values["api_key"].startswith("secret://")
    return [call("create_model_profile", role="generator", name="自建 Qwen", provider="self",
                 config={"backend": "openai", "base_url": values["base_url"], "model": values["model"],
                         "llama_template": True},
                 api_key=values["api_key"])]


@pytest.fixture
def agent(config):
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm, config


def answer(client, sid, pending, values):
    return client.post(f"/api/agent/sessions/{sid}/questions/{pending['requestId']}",
                       json={"answers": [{"id": "endpoint", "selected": [], "values": values}]})


def test_secret_form_never_leaks(agent):
    client, llm, config = agent
    llm.extend([ASK], create_from_answer, [say("档案已创建")])
    sid = new_session(client)
    send(client, sid, "我要用自建的 Qwen 做拆分镜")
    pending = wait_pending(client, sid, "question")
    assert pending["questions"][0]["fields"][2]["type"] == "secret"
    assert answer(client, sid, pending, {"base_url": "http://gpu:4040/v1", "model": "qwen3",
                                         "api_key": SECRET}).status_code == 200
    approval = wait_pending(client, sid, "approval")
    assert approval["name"] == "create_model_profile" and SECRET not in json.dumps(approval)
    client.post(f"/api/agent/sessions/{sid}/approvals/{approval['requestId']}", json={"outcome": "allowed-once"})
    events = wait_idle(client, sid)
    sse = client.get(f"/api/agent/sessions/{sid}/events", params={"follow": "false"}).text

    assert SECRET not in json.dumps(events, ensure_ascii=False)
    assert SECRET not in sse
    assert SECRET not in json.dumps([r.messages for r in llm.requests], ensure_ascii=False, default=str)
    with sqlite3.connect(config.data_dir / "agent.sqlite3") as db:
        dump = "\n".join(db.iterdump())
    assert SECRET not in dump.split("INSERT INTO \"agent_secrets\"")[0]  # 只有保险库表里有明文
    assert not any(SECRET in line for line in dump.splitlines() if "agent_events" in line)
    # 档案真正拿到了明文密钥，对外只出掩码。
    row = Store(config.data_dir).one("SELECT api_key FROM model_profiles WHERE name='自建 Qwen'")
    assert row["api_key"] == SECRET
    assert "已设置（…cdef）" in result_text(results(events)[-1])
    answered = next(e for e in events if e["type"] == "plugin:mindex/question-answered")
    assert re.fullmatch(r"secret://[0-9a-f]{24}", answered["data"]["answers"][0]["values"]["api_key"])


def test_plaintext_and_expired_refs_rejected(agent):
    client, llm, config = agent
    llm.extend([call("create_model_profile", role="generator", name="坏", config={"model": "x"}, api_key="sk-plain")],
               [ASK], create_from_answer, [say("引用过期")])
    sid = new_session(client)
    send(client, sid, "试试")
    pending = wait_pending(client, sid, "question")
    answer(client, sid, pending, {"base_url": "http://gpu/v1", "model": "q", "api_key": SECRET})
    approval = wait_pending(client, sid, "approval")
    with sqlite3.connect(config.data_dir / "agent.sqlite3") as db:
        db.execute("UPDATE agent_secrets SET expires_at=0")
    client.post(f"/api/agent/sessions/{sid}/approvals/{approval['requestId']}", json={"outcome": "allowed-once"})
    events = wait_idle(client, sid)
    first, last = results(events)[0], results(events)[-1]
    assert first["error"]["code"] == "plaintext_secret"
    assert last["message"]["isError"] and "过期" in result_text(last)
    assert Store(config.data_dir).one("SELECT * FROM model_profiles") is None

    other = new_session(client)
    ref = next(e for e in events if e["type"] == "plugin:mindex/question-answered")["data"]["answers"][0]["values"]["api_key"]
    with pytest.raises(ValueError, match="不属于本会话"):
        client.app.state.agent_runtime.store.resolve_secret(other, ref)


def test_settings_tools_and_self_profile_guard(agent):
    client, llm, _ = agent
    agent_profile = client.post("/api/settings/profiles", json={
        "role": "agent", "name": "助手 A", "provider": "deepseek",
        "config": {"backend": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}}).json()
    client.post(f"/api/settings/profiles/{agent_profile['id']}/activate")
    backup = client.post("/api/settings/profiles", json={
        "role": "agent", "name": "助手 B", "provider": "anthropic",
        "config": {"backend": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude"}}).json()
    writer = client.post("/api/settings/profiles", json={
        "role": "writer", "name": "文案", "provider": "custom",
        "config": {"backend": "openai", "base_url": "http://x/v1", "model": "w"}}).json()
    llm.extend(
        [call("get_settings"), call("list_provider_presets", role="agent"), call("list_voices")],
        [call("update_model_profile", profile_id=agent_profile["id"], config={"model": "other"}),
         call("activate_model_profile", profile_id=backup["id"])],
        [call("activate_model_profile", profile_id=writer["id"])],
        [call("update_business_settings", search={"rrf_k": 42})],
        [say("完成")],
    )
    sid = new_session(client)
    send(client, sid, "看看设置")
    for _ in range(2):
        pending = wait_pending(client, sid, "approval")
        client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})
        for _ in range(200):
            now = client.get(f"/api/agent/sessions/{sid}").json()["pending"]
            if not now or now[0]["requestId"] != pending["requestId"]:
                break
    events = wait_idle(client, sid)
    texts = [result_text(r) for r in results(events)]
    assert "智能助手(agent)：deepseek-chat" in texts[0] and "档案 #" in texts[0]
    assert "anthropic" in texts[1]
    assert "音色库" in texts[2]
    codes = [r.get("error", {}).get("code") for r in results(events)]
    assert codes[3:5] == ["self_profile", "self_profile"]  # 预览阶段即拒绝，不请求确认
    assert codes[5] is None and codes[6] is None
    settings = client.get("/api/settings?probe_embedding=false").json()
    assert next(r for r in settings["roles"] if r["role"] == "writer")["active_profile_id"] == writer["id"]
    assert settings["business"]["search"]["rrf_k"] == 42
    assert sum(1 for e in events if e["type"] == "plugin:mindex/mutation" and e["data"] == {"domain": "settings"}) == 2
