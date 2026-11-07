"""设置领域扩展工具：删除档案保护、音色库、克隆音色只凭表单授权。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from agent.llm.scripted import ScriptedLlm, call, say
from agent.loopback import MindexClient
from agent.registry import ToolContext, ToolError
from agent.tools.settings_tools import CONSENT, DECLINE, CloneVoice, clone_voice, specs
from agent_helpers import new_session, result_text, results, send, wait_idle, wait_pending
from api import create_app
from shared.storage import Store

SETTINGS_PAGE = {"page": "settings"}


def start_agent(config, backend="openai"):
    config.studio.tts.backend = backend
    config.studio.tts.base_url = (
        "http://127.0.0.1:9/v1"  # 不可达：克隆在厂商调用处失败（502），不访问外部网络
    )
    llm = ScriptedLlm()
    return TestClient(create_app(config, agent_llm=llm)), llm


@pytest.fixture
def agent(config):
    client, llm = start_agent(config)
    with client:
        yield client, llm, config


@pytest.fixture
def dashscope_agent(config):
    client, llm = start_agent(config, "dashscope")
    with client:
        yield client, llm, config


def approve(client, sid):
    pending = wait_pending(client, sid, "approval")
    client.post(
        f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"}
    )
    return pending


def answer_consent(client, sid, choice, name):
    pending = wait_pending(client, sid, "question")
    question = pending["questions"][0]
    assert [o["label"] for o in question["options"]] == [CONSENT, DECLINE]
    response = client.post(
        f"/api/agent/sessions/{sid}/questions/{pending['requestId']}",
        json={
            "answers": [
                {"id": question["id"], "selected": [choice], "values": {"name": name, "ref_text": "你好"}}
            ]
        },
    )
    assert response.status_code == 200, response.text


def test_delete_profile_guard_and_reset_business(agent):
    client, llm, config = agent
    active = client.post(
        "/api/settings/profiles",
        json={
            "role": "writer",
            "name": "在用",
            "provider": "custom",
            "config": {"backend": "openai", "base_url": "http://x/v1", "model": "w"},
        },
    ).json()
    client.post(f"/api/settings/profiles/{active['id']}/activate")
    spare = client.post(
        "/api/settings/profiles",
        json={
            "role": "writer",
            "name": "备用",
            "provider": "custom",
            "config": {"backend": "openai", "base_url": "http://y/v1", "model": "v"},
        },
    ).json()
    client.put("/api/settings/business", json={"search": {"rrf_k": 42}})
    llm.extend(
        [call("delete_model_profile", profile_id=active["id"])],
        [call("delete_model_profile", profile_id=spare["id"])],
        [call("reset_business_settings")],
        [say("完成")],
    )
    sid = new_session(client)
    send(client, sid, "清理", context=SETTINGS_PAGE)
    assert approve(client, sid)["name"] == "delete_model_profile"
    assert approve(client, sid)["name"] == "reset_business_settings"
    events = wait_idle(client, sid)
    first, second, third = results(events)[:3]
    assert first["error"]["code"] == "active_profile"  # 预览阶段即拒绝，未发送删除请求
    assert "备用" in result_text(second) and third.get("error") is None
    names = {p["name"] for p in Store(config.data_dir).rows("SELECT name FROM model_profiles")}
    assert names == {"在用"}
    business = client.get("/api/settings?probe_embedding=false").json()["business"]
    assert business["search"]["rrf_k"] == config.search.rrf_k


def test_clone_voice_requires_form_consent(agent):
    client, llm, config = agent
    sid = new_session(client)
    upload = client.post(
        "/api/agent/attachments",
        data={"session_id": sid},
        files={"file": ("sample.wav", b"RIFF0000WAVEfmt ", "audio/wav")},
    )
    assert upload.status_code == 201, upload.text
    ref = upload.json()["id"]
    llm.extend(
        [call("clone_voice", name="旁白", attachment=ref)],
        [call("clone_voice", name="旁白", attachment=ref)],
        [say("完成")],
    )
    send(client, sid, "克隆我的声音", context=SETTINGS_PAGE)
    approve(client, sid)
    answer_consent(client, sid, DECLINE, "旁白")
    approve(client, sid)
    answer_consent(client, sid, CONSENT, "表单名")
    events = wait_idle(client, sid)
    declined, consented = results(events)[:2]
    assert declined["error"]["code"] == "consent_declined"
    # 授权后真正请求了克隆接口：厂商不可达返回 502，并留下使用表单名称的失败记录。
    assert consented["error"]["code"] == "http_502", consented
    rows = Store(config.data_dir).rows("SELECT name, status FROM voices")
    assert rows == [{"name": "表单名", "status": "failed"}]


def test_clone_voice_has_no_consent_argument_and_refuses_without_form():
    assert "consent" not in CloneVoice.model_json_schema()["properties"]
    with pytest.raises(ToolError) as caught:
        asyncio.run(clone_voice(CloneVoice(name="x", audio_url="https://a/b.wav"), ToolContext(client=None)))
    assert caught.value.code == "consent_unavailable"


# ---------------------------------------------------------------------------
# 删除模型档案：自身档案与激活中档案受保护（预览与执行都拒绝，且不发删除请求）


class FakeClient:
    """记录请求的假回环客户端：``GET /api/settings`` 返回给定数据，克隆上传返回成功。"""

    def __init__(self, settings: dict):
        self.settings = settings
        self.requests: list[tuple[str, str, dict]] = []

    async def get(self, path, **params):
        self.requests.append(("GET", path, params))
        return {"ok": True, "status": 200, "data": self.settings}

    async def delete(self, path, *args, **kwargs):
        self.requests.append(("DELETE", path, kwargs))
        return {"ok": True, "status": 204, "data": None}

    async def upload(self, path, *, files, data=None, method="POST"):
        self.requests.append((method, path, {"files": files, "data": data}))
        return {
            "ok": True,
            "status": 201,
            "data": {"id": 9, "name": data["name"], "voice_id": "v9", "status": "ready"},
        }

    def writes(self) -> list[tuple[str, str, dict]]:
        return [r for r in self.requests if r[0] != "GET"]


def profile_settings() -> dict:
    return {
        "business": {"tts_backend": "openai", "default_voice": None},
        "voices": [],
        "roles": [
            {
                "role": "agent",
                "label": "智能助手",
                "active_profile_id": 7,
                "profiles": [{"id": 7, "name": "助手", "provider": "custom", "config": {"model": "a"}}],
            },
            {
                "role": "writer",
                "label": "文案",
                "active_profile_id": 3,
                "profiles": [
                    {"id": 3, "name": "在用", "provider": "custom", "config": {"model": "w"}},
                    {"id": 4, "name": "备用", "provider": "custom", "config": {"model": "v"}},
                ],
            },
        ],
    }


@pytest.mark.parametrize(
    ("profile_id", "agent_profile_id", "code"),
    [
        (7, 7, "self_profile"),  # 智能助手自身正在使用的档案
        (3, 7, "active_profile"),  # 其他角色当前激活中的档案
        (7, None, "active_profile"),  # 无法确定自身档案时，仍按“激活中”保护
    ],
)
def test_delete_profile_guard_in_preview_and_execute(profile_id, agent_profile_id, code):
    spec = next(s for s in specs() if s.name == "delete_model_profile")
    assert spec.risk == "danger"
    client = FakeClient(profile_settings())
    ctx = ToolContext(client=client, agent_profile_id=agent_profile_id)
    args = spec.parse({"profile_id": profile_id})
    for step in (spec.preview, spec.execute):
        with pytest.raises(ToolError) as caught:
            asyncio.run(step(args, ctx))
        assert caught.value.code == code
    assert client.writes() == []  # 只读取设置，没有任何删除请求


def test_delete_spare_profile_previews_and_deletes():
    spec = next(s for s in specs() if s.name == "delete_model_profile")
    client = FakeClient(profile_settings())
    ctx = ToolContext(client=client, agent_profile_id=7)
    args = spec.parse({"profile_id": 4})
    preview = asyncio.run(spec.preview(args, ctx))
    assert "备用" in preview.summary and preview.changes[0]["after"] == "删除"
    output = asyncio.run(spec.execute(args, ctx))
    assert output.value == {"deleted": 4, "role": "writer"}
    assert [(m, p) for m, p, _ in client.writes()] == [("DELETE", "/api/settings/profiles/4")]


def test_delete_agent_own_profile_rejected_in_session(agent):
    client, llm, config = agent

    def create(name, model):
        response = client.post(
            "/api/settings/profiles",
            json={
                "role": "agent",
                "name": name,
                "provider": "custom",
                "config": {"backend": "openai", "base_url": "http://llm.test/v1", "model": model},
            },
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["id"]

    own = create("助手 A", "a")
    assert client.post(f"/api/settings/profiles/{own}/activate").status_code == 200
    other = create("助手 B", "b")
    llm.extend(
        [call("delete_model_profile", profile_id=own)],
        [call("delete_model_profile", profile_id=other)],
        [say("完成")],
    )
    sid = new_session(client)
    send(client, sid, "删掉助手档案", context=SETTINGS_PAGE)
    assert approve(client, sid)["name"] == "delete_model_profile"  # 只有备用档案走到确认
    events = wait_idle(client, sid)
    own_result, other_result = results(events)[:2]
    assert own_result["error"]["code"] == "self_profile"
    assert "不能删除智能助手当前正在使用的档案" in result_text(own_result)  # 原因转述给模型
    assert other_result.get("error") is None and "助手 B" in result_text(other_result)
    names = {p["name"] for p in Store(config.data_dir).rows("SELECT name FROM model_profiles")}
    assert names == {"助手 A"}


def test_mcp_remote_mode_protects_profiles_on_delete(config):
    """远程 MCP 模式：自身档案经远端设置解析，同样拒绝删除。"""
    pytest.importorskip("mcp")
    from agent.mcp_server import RemoteAgentProfile, build_server
    from agent.tools import build_registry

    app = create_app(config)
    http = TestClient(app)
    ids = {}
    for role, name in (("agent", "助手"), ("writer", "在用"), ("writer", "备用")):
        response = http.post(
            "/api/settings/profiles",
            json={
                "role": role,
                "name": name,
                "provider": "custom",
                "config": {"backend": "openai", "base_url": "http://llm.test/v1", "model": name},
            },
        )
        ids[name] = response.json()["id"]
    http.post(f"/api/settings/profiles/{ids['助手']}/activate")
    http.post(f"/api/settings/profiles/{ids['在用']}/activate")
    loop_client = MindexClient(app)
    server = build_server(
        build_registry(), lambda: loop_client, policy="all", agent_profile=RemoteAgentProfile(loop_client)
    )
    own, active, spare = mcp_calls(
        server, [("delete_model_profile", {"profile_id": ids[name]}) for name in ("助手", "在用", "备用")]
    )
    assert own.isError and "不能删除智能助手当前正在使用的档案" in mcp_text(own)
    assert active.isError and "当前正在使用的档案，无法删除" in mcp_text(active)
    assert not spare.isError, mcp_text(spare)
    names = {p["name"] for p in Store(config.data_dir).rows("SELECT name FROM model_profiles")}
    assert names == {"助手", "在用"}


# ---------------------------------------------------------------------------
# 克隆音色按语音合成后端分派：openai/minimax 上传附件样本，dashscope 提交公网 URL


class FormRuntime:
    def __init__(self, values: dict):
        self.values = values
        self.asked: list[dict] = []

    async def ask_question(self, session_id, call_id, questions):
        self.asked.extend(questions)
        return {"answers": [{"id": "clone_consent", "selected": [CONSENT], "values": self.values}]}


@pytest.mark.parametrize("backend", ["openai", "minimax", "dashscope"])
def test_clone_voice_dispatch_by_backend(backend):
    spec = next(s for s in specs() if s.name == "clone_voice")
    client = FakeClient({"business": {"tts_backend": backend}})
    runtime = FormRuntime(
        {"name": "旁白", "ref_text": "你好", "audio_url": "https://example.invalid/form.wav"}
    )
    ctx = ToolContext(client=client, session_id="s1", extra={"runtime": runtime, "call_id": "c1"})
    args = spec.parse(
        {"name": "旁白", "attachment": "attachment://tok", "audio_url": "https://example.invalid/arg.wav"}
    )

    preview = asyncio.run(spec.preview(args, ctx))
    sample = next(c["after"] for c in preview.changes if c["label"] == "样本")
    asyncio.run(spec.execute(args, ctx))
    ((method, path, payload),) = client.writes()
    assert (method, path) == ("POST", "/api/settings/voices/clone")
    assert payload["data"]["consent"] is True and payload["data"]["name"] == "旁白"
    fields = [f["id"] for f in runtime.asked[0]["fields"]]
    if backend == "dashscope":
        # URL 复刻：不上传文件，URL 取表单值；表单里有 URL 字段。
        assert sample == "https://example.invalid/arg.wav"
        assert payload["files"] == {} and payload["data"]["audio_url"] == "https://example.invalid/form.wav"
        assert "audio_url" in fields
    else:
        assert sample == "attachment://tok"
        assert payload["files"] == {"file": "attachment://tok"} and "audio_url" not in payload["data"]
        assert "audio_url" not in fields


@pytest.mark.parametrize("backend", ["openai", "minimax"])
def test_clone_voice_upload_backend_requires_attachment(backend):
    spec = next(s for s in specs() if s.name == "clone_voice")
    client = FakeClient({"business": {"tts_backend": backend}})
    runtime = FormRuntime({"name": "旁白"})
    ctx = ToolContext(client=client, session_id="s1", extra={"runtime": runtime, "call_id": "c1"})
    args = spec.parse({"name": "旁白", "audio_url": "https://example.invalid/a.wav"})
    for step in (spec.preview, spec.execute):
        with pytest.raises(ToolError) as caught:
            asyncio.run(step(args, ctx))
        assert caught.value.code == "invalid_arguments" and "附件" in str(caught.value)
    assert runtime.asked == [] and client.writes() == []


def test_clone_voice_dashscope_url_end_to_end(dashscope_agent):
    """dashscope 后端：表单含 URL 字段，提交后克隆接口按 URL 复刻（厂商不可达 → 502 失败记录）。"""
    client, llm, config = dashscope_agent
    llm.extend([call("clone_voice", name="旁白", audio_url="https://example.invalid/a.wav")], [say("完成")])
    sid = new_session(client)
    send(client, sid, "用这个链接克隆音色", context=SETTINGS_PAGE)
    pending = approve(client, sid)
    sample = next(c["after"] for c in pending["preview"]["changes"] if c["label"] == "样本")
    assert sample == "https://example.invalid/a.wav"
    question = wait_pending(client, sid, "question")
    form = question["questions"][0]
    assert "audio_url" in [f["id"] for f in form["fields"]]
    client.post(
        f"/api/agent/sessions/{sid}/questions/{question['requestId']}",
        json={
            "answers": [
                {
                    "id": form["id"],
                    "selected": [CONSENT],
                    "values": {"name": "云端", "audio_url": "https://example.invalid/a.wav"},
                }
            ]
        },
    )
    events = wait_idle(client, sid)
    assert results(events)[0]["error"]["code"] == "http_502"
    rows = Store(config.data_dir).rows("SELECT name, provider, status FROM voices")
    assert rows == [{"name": "云端", "provider": "dashscope", "status": "failed"}]


# ---------------------------------------------------------------------------
# 音色库：登记音色；删除当前默认音色时联动清除默认设置（预览中提示）


def test_register_and_delete_default_voice_clears_override(agent):
    client, llm, _ = agent
    llm.extend([call("register_voice", name="旁白甲", provider="self", voice_id="voice-a")], [say("已登记")])
    sid = new_session(client)
    send(client, sid, "登记音色", context=SETTINGS_PAGE)
    assert approve(client, sid)["preview"]["summary"] == "把音色「旁白甲」登记到音色库"
    registered = results(wait_idle(client, sid))[0]
    assert registered.get("error") is None, registered
    voices = {v["name"]: v for v in client.get("/api/settings?probe_embedding=false").json()["voices"]}
    assert voices["旁白甲"]["voice_id"] == "voice-a" and voices["旁白甲"]["provider"] == "self"
    other = client.post(
        "/api/settings/voices", json={"name": "旁白乙", "provider": "self", "voice_id": "voice-b"}
    )
    assert other.status_code == 201, other.text
    put = client.put("/api/settings/business", json={"default_voice_id": voices["旁白甲"]["id"]})
    assert put.status_code == 200 and put.json()["default_voice"] == "voice-a"

    llm.extend(
        [call("delete_voice", voice_id=other.json()["id"])],
        [call("delete_voice", voice_id=voices["旁白甲"]["id"])],
        [say("完成")],
    )
    send(client, sid, "删除两个音色", context=SETTINGS_PAGE)
    plain = approve(client, sid)
    assert not any("默认音色" in w for w in plain["preview"]["warnings"])
    default = approve(client, sid)
    assert any("当前默认音色" in w for w in default["preview"]["warnings"])
    events = wait_idle(client, sid)
    first, second = results(events)[-2:]
    assert "并清除了默认音色覆盖" not in result_text(first)
    assert "并清除了默认音色覆盖" in result_text(second)
    data = client.get("/api/settings?probe_embedding=false").json()
    assert data["voices"] == [] and data["business"]["default_voice"] is None


# ---------------------------------------------------------------------------
# MCP：没有提问通道，clone_voice 被拒且不调用克隆接口


def mcp_calls(server, calls):
    from mcp.shared.memory import create_connected_server_and_client_session

    async def go():
        async with create_connected_server_and_client_session(server) as session:
            return [await session.call_tool(name, args) for name, args in calls]

    return asyncio.run(go())


def mcp_text(result) -> str:
    return "".join(block.text for block in result.content)


def test_mcp_clone_voice_refused(config):
    pytest.importorskip("mcp")
    from agent.mcp_server import server_for_app

    config.studio.tts.backend = "dashscope"
    app = create_app(config)
    server = server_for_app(app, app.state.runtime_config, app.state.store, policy="all")
    (refused,) = mcp_calls(
        server, [("clone_voice", {"name": "旁白", "audio_url": "https://example.invalid/a.wav"})]
    )
    assert refused.isError and "需要在 Mindex 页面助手中确认声音授权" in mcp_text(refused)
    assert Store(config.data_dir).rows("SELECT id FROM voices") == []
