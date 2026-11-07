"""改动与定位通道（Task 11）与各工具的定位描述、navigate（Task 12）。"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from agent.changes import ChangeFeed
from agent.llm.scripted import ScriptedLlm, call, say
from agent_helpers import new_session, parse_sse, result_text, results, send, wait_idle, wait_pending
from api import create_app
from conftest import legacy_job
from studio import studio
from studio import workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-925281d6cc"]
TAB = "tab-test-1"


@pytest.fixture
def job(config):
    job_id = legacy_job(config, "红色开场。蓝色中段。蓝色延续。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.0)
            for sid, text in zip(SHOTS, ["红色开场。", "蓝色中段。", "蓝色延续。"])
        ],
    )
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id


@pytest.fixture
def agent(config, job):
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm, job


def changes(client, after=0):
    return client.get("/api/agent/changes/log", params={"after": after}).json()["items"]


def focuses(events):
    return [e["data"] for e in events if e["type"] == "plugin:mindex/focus"]


def test_feed_records_read_and_write(agent):
    client, llm, job = agent
    start = client.get("/api/agent/changes/log").json()["latest"]
    llm.extend(
        [call("resolve_shots", job_id=job, refs=["#2"])],
        [call("edit_shots", job_id=job, operations=[{"op": "merge", "shots": ["#2", "#3"]}])],
        [say("好")],
    )
    sid = new_session(client)
    send(client, sid, "合并第二三镜", context={"page": "studio", "job_id": job, "tab_id": TAB})
    pending = wait_pending(client, sid, "approval")
    # 确认前已经定位到将要修改的分镜。
    preview = [c for c in changes(client, start) if c["kind"] == "focus"]
    assert preview[-1]["data"]["shots"] == SHOTS[1:] and preview[-1]["tab"] == TAB
    assert not any(c["kind"] == "mutation" for c in changes(client, start))
    client.post(
        f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"}
    )
    events = wait_idle(client, sid)
    items = changes(client, start)
    kinds = [c["kind"] for c in items]
    assert kinds == ["focus", "focus", "mutation", "focus"]
    assert items[0]["data"] == {
        "page": "studio",
        "job_id": job,
        "plan_id": items[0]["data"]["plan_id"],
        "shots": [SHOTS[1]],
    }
    assert (
        items[2]["data"]["domain"] == "studio"
        and items[2]["source"] == "agent"
        and items[2]["session_id"] == sid
    )
    final = items[3]["data"]
    assert final["shots"] == [SHOTS[1]] and final["stage"] == "content"  # 合并后保留下来的那一镜
    assert all(c["tab"] == TAB for c in items)
    # 会话日志也有定位记录（ignorable），模型看不到 tab_id。
    assert [f["tab"] for f in focuses(events)] == [TAB] * 3
    contexts = [e for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "context"]
    assert "tab_id" not in contexts[0]["data"]["content"][0]["text"]


def test_tab_change_does_not_reinject_context(agent):
    client, llm, job = agent
    llm.extend([say("一")], [say("二")])
    sid = new_session(client)
    send(client, sid, "a", context={"page": "studio", "job_id": job, "tab_id": "t1"})
    wait_idle(client, sid)
    send(client, sid, "b", context={"page": "studio", "job_id": job, "tab_id": "t2"})
    events = wait_idle(client, sid)
    assert (
        sum(1 for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "context")
        == 1
    )
    assert client.get(f"/api/agent/sessions/{sid}").json()["context"]["tab_id"] == "t2"


def test_sse_resume_and_no_replay(agent):
    client, _, _ = agent
    feed = client.app.state.agent_runtime.feed
    first = feed.publish("mutation", {"domain": "studio"})
    second = feed.publish("focus", {"page": "settings"}, tab="x")
    frames = parse_sse(
        client.get("/api/agent/changes", params={"after": first["seq"], "follow": "false"}).text
    )
    assert frames[0] == ("ready", {"seq": first["seq"]})
    assert [f[1]["seq"] for f in frames if f[0] == "change"] == [second["seq"]]
    assert frames[1][1]["data"] == {"page": "settings"} and frames[1][1]["tab"] == "x"
    fresh = parse_sse(client.get("/api/agent/changes", params={"follow": "false"}).text)
    assert fresh == [("ready", {"seq": second["seq"]})]  # after=-1 从最新开始，不回放


def test_remote_report_and_validation(agent):
    client, _, _ = agent
    created = client.post("/api/agent/changes", json={"kind": "mutation", "data": {"domain": "settings"}})
    assert created.status_code == 201 and created.json()["source"] == "mcp"
    assert client.post("/api/agent/changes", json={"kind": "other", "data": {}}).status_code == 422


def test_mcp_publishes(config, job):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from agent.mcp_server import server_for_app

    app = create_app(config)
    server = server_for_app(app, app.state.runtime_config, app.state.store, policy="write")

    async def go():
        async with create_connected_server_and_client_session(server) as session:
            return await session.call_tool(
                "edit_shots", {"job_id": job, "operations": [{"op": "delete", "shot": "#3"}]}
            )

    assert not asyncio.run(go()).isError
    items = ChangeFeed(config.data_dir).after(0)
    assert [(c["kind"], c["source"]) for c in items] == [("mutation", "mcp"), ("focus", "mcp")]
    assert items[0]["data"]["job_id"] == job


def test_no_plaintext_secret_in_feed(agent):
    client, llm, _ = agent
    secret = "sk-feed-PLAINTEXT-123456789zz"
    ask = call(
        "ask_user_question",
        questions=[
            {"id": "k", "question": "密钥", "fields": [{"id": "api_key", "label": "Key", "type": "secret"}]}
        ],
    )

    def create(request):
        ref = json.loads(request.messages[-1]["content"][0]["text"])["answers"][0]["values"]["api_key"]
        return [
            call(
                "create_model_profile",
                role="writer",
                name="W",
                config={"model": "w", "base_url": "http://x/v1"},
                api_key=ref,
            )
        ]

    llm.extend([ask], create, [say("好")])
    sid = new_session(client)
    send(client, sid, "配置", context={"page": "settings", "tab_id": TAB})
    pending = wait_pending(client, sid, "question")
    client.post(
        f"/api/agent/sessions/{sid}/questions/{pending['requestId']}",
        json={"answers": [{"id": "k", "selected": [], "values": {"api_key": secret}}]},
    )
    approval = wait_pending(client, sid, "approval")
    client.post(
        f"/api/agent/sessions/{sid}/approvals/{approval['requestId']}", json={"outcome": "allowed-once"}
    )
    wait_idle(client, sid)
    items = changes(client)
    assert secret not in json.dumps(items, ensure_ascii=False)
    assert items[-1]["data"]["role"] == "writer" and items[-1]["data"]["profile_id"]


def test_tool_focus_catalogue(agent):
    client, llm, job = agent
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "writer",
            "name": "文案",
            "provider": "custom",
            "config": {"backend": "openai", "base_url": "http://x/v1", "model": "w"},
        },
    ).json()
    llm.extend(
        # 创作页只自动加载 studio；跨领域先 load_tools（同时最多保持 3 个领域，最久未用的被卸载）。
        [
            call("list_jobs"),
            call("get_workspace_summary", job_id=job),
            call("get_preflight", job_id=job),
            call("list_versions", job_id=job, stage="script"),
            call("load_tools", domains=["settings", "search", "library"]),
            call("get_settings", role="tts"),
            call("get_settings"),
            call("list_voices"),
            call("search_media", text="夜景"),
            call("list_sources"),
            call("load_tools", domains=["persons"]),
            call("list_persons"),
            call("navigate", page="settings", role="agent"),
            call("navigate", page="studio", job_id=job, shots=["a1daad59", "#3"], stage="tts"),
            call("navigate", page="studio", job_id=job, shots=["925281d"]),
            call("navigate", page="settings", section="jobs"),
        ],
        [call("activate_model_profile", profile_id=profile["id"])],
        [say("完成")],
    )
    sid = new_session(client)
    send(client, sid, "到处看看", context={"page": "studio", "job_id": job})
    approval = wait_pending(client, sid, "approval")
    client.post(
        f"/api/agent/sessions/{sid}/approvals/{approval['requestId']}", json={"outcome": "allowed-once"}
    )
    events = wait_idle(client, sid)
    got = focuses(events)
    for item in got:
        item.pop("tab")
    pid = got[1]["plan_id"]
    assert got[0] == {"page": "studio", "section": "jobs"}
    assert got[1] == {"page": "studio", "job_id": job, "plan_id": pid, "section": "storyboard"}
    assert got[2]["shots"] == SHOTS and got[2]["section"] == "storyboard"
    assert got[3] == {"page": "studio", "job_id": job, "plan_id": pid, "section": "script"}
    assert got[4] == {"page": "settings", "role": "tts"}
    assert got[5] == {"page": "settings", "section": "models"}
    assert got[6] == {"page": "settings", "section": "voices"}
    # search_media 在没有索引时失败，不产生定位（有索引时的定位见 test_agent_library）。
    assert got[7] == {"page": "admin"} and got[8] == {"page": "persons"}
    assert got[9] == {"page": "settings", "role": "agent"}
    assert got[10] == {
        "page": "studio",
        "job_id": job,
        "plan_id": pid,
        "shots": [SHOTS[0], SHOTS[2]],
        "stage": "tts",
    }
    # 预览与执行都定位到要激活的档案。
    assert got[11] == got[12] == {"page": "settings", "role": "writer", "profile_id": profile["id"]}
    assert len(got) == 13
    codes = [r["error"]["code"] for r in results(events) if r["message"].get("isError")]
    assert codes[-2:] == ["ambiguous_shot", "invalid_arguments"] and len(codes) == 3
    navigated = [r for r in results(events) if "已在用户页面上打开：创作" in result_text(r)]
    assert "#1 shot-a1daad59" in result_text(navigated[0])
    loaded = [e["data"]["loaded"] for e in events if e["type"] == "plugin:mindex/tools-loaded"]
    assert loaded == [["settings", "search", "library"], ["settings", "library", "persons"]]


def test_navigate_not_exported():
    from agent.tools import build_registry
    from test_agent_tools import module_spec_count

    registry = build_registry()
    assert len(registry) == len(registry.all()) == module_spec_count()
    assert not registry.get("navigate").mcp and registry.get("navigate").risk == "read"
