"""Real pinned SDK/runtime against a deterministic local OpenAI streaming endpoint."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import BaseModel

from mindex.agent.dsh_runtime import DshRuntime, patch, readiness
from mindex.agent.registry import ToolOutput, ToolRegistry, ToolSpec
from mindex.agent.session_store import SessionStore
from mindex.agent.tools import meta
from mindex.shared.config import AgentConfig


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("failure", ["missing", "version", "files"])
def test_dependency_repair_matches_installation(monkeypatch, frozen, failure):
    import importlib.metadata

    import mindex.agent.dsh_runtime as module

    monkeypatch.setattr(module.sys, "frozen", frozen, raising=False)

    def version(package):
        if package == "deepseek-harness-sdk":
            if failure == "missing":
                raise importlib.metadata.PackageNotFoundError(package)
            if failure == "version":
                return "0.0.0"
        return module.DSH_VERSION if package != "mcp" else "1.30.0"

    monkeypatch.setattr(importlib.metadata, "version", version)
    if failure == "files":
        import types

        def resolve(mode=None):
            assert mode == ("exe" if frozen else None)
            raise FileNotFoundError("missing runtime")

        monkeypatch.setitem(module.sys.modules, "deepseek_harness_runtime",
                            types.SimpleNamespace(resolve_bundled_launch_args=resolve))
    result = readiness(AgentConfig(llm={"model": "test", "base_url": "http://localhost/v1"}))
    assert not result["ready"]
    message = "；".join([*result["errors"], result["install"]])
    if frozen:
        assert "桌面版" in message and "重新安装" in message
        assert "uv sync" not in message
    else:
        assert "uv sync --extra dsh" in message


def test_frozen_uses_bundled_executable(monkeypatch):
    import types

    import mindex.agent.dsh_runtime as module

    monkeypatch.setattr(module.sys, "frozen", True, raising=False)
    modes = []
    monkeypatch.setitem(module.sys.modules, "deepseek_harness_runtime", types.SimpleNamespace(
        resolve_bundled_launch_args=lambda mode: (modes.append(mode) or "/bundled/dsh",)))
    assert DshRuntime._bundled_executable() == "/bundled/dsh"
    assert modes == ["exe"]
    monkeypatch.setattr(module.sys, "frozen", False)
    assert DshRuntime._bundled_executable() is None


class Empty(BaseModel):
    pass


@pytest.mark.parametrize("capacity", [None, 1048576])
def test_model_context_capacity_is_separate_from_output_budget(capacity):
    settings = AgentConfig(max_context_tokens=60000, llm={"context_window": capacity, "max_tokens": 4096})
    model = patch(settings, "http://localhost/mcp")[-1]["insert"][0]["config"]["providers"]["mindex"]["models"][0]
    assert model["contextWindow"] == (capacity or 60000)
    assert model["maxTokens"] == 4096


@pytest.fixture
def model_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({**body, "_path": self.path})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if self.path.split("?")[0].endswith("/messages"):
                used = any(
                    isinstance(m.get("content"), list)
                    and any(b.get("type") == "tool_result" for b in m["content"])
                    for m in body["messages"]
                )
                events = [
                    (
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": "msg-fixture",
                                "type": "message",
                                "role": "assistant",
                                "model": "fixture",
                                "content": [],
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {"input_tokens": 10, "output_tokens": 0},
                            },
                        },
                    )
                ]
                block = (
                    {"type": "text", "text": ""}
                    if used
                    else {"type": "tool_use", "id": "call-fixture", "name": "mcp__mindex__probe", "input": {}}
                )
                events += [
                    (
                        "content_block_start",
                        {"type": "content_block_start", "index": 0, "content_block": block},
                    ),
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "fixture completed"}
                            if used
                            else {"type": "input_json_delta", "partial_json": "{}"},
                        },
                    ),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": "end_turn" if used else "tool_use",
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": 10},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ]
                for name, event in events:
                    self.wfile.write(("event: " + name + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                return
            used = any(m["role"] == "tool" for m in body["messages"])
            tool_name, arguments = "mcp__mindex__probe", "{}"
            if body["model"] == "dynamic":
                prior = [c for m in body["messages"] for c in m.get("tool_calls", [])]
                used = len(prior) >= 3
                if len(prior) == 1:
                    tool_name, arguments = "mcp__mindex__load_tools", '{"domains":["library"]}'
            delta = (
                {"content": "fixture completed"}
                if used
                else {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": tool_name + str(len(body["messages"])),
                            "type": "function",
                            "function": {"name": tool_name, "arguments": arguments},
                        }
                    ]
                }
            )
            for chunk in [
                {
                    "id": "completion",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                },
                {"id": "completion", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {
                    "id": "completion",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop" if used else "tool_calls"}],
                    **({"usage": {"prompt_tokens": 66000, "completion_tokens": 10, "total_tokens": 66010}}
                       if body["model"] == "million-context" else {}),
                },
            ]:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", requests
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("backend", ["openai", "anthropic"])
def test_real_dsh_tool_and_resume(tmp_path, model_server, backend):
    url, requests = model_server
    settings = AgentConfig(
        runtime="dsh", llm={"base_url": url, "model": "fixture", "api_key": "fixture-key", "backend": backend}
    )
    if not readiness(settings)["ready"]:
        pytest.skip("uv sync --extra dsh")
    called = []

    async def execute(args, ctx):
        called.append(ctx.session_id)
        return ToolOutput({"ok": True}, text="probe result")

    registry = ToolRegistry([*meta.specs(), ToolSpec("probe", "meta", "probe", Empty, "read", execute)])
    store = SessionStore(tmp_path)
    sid = store.create(runtime="dsh")["id"]

    async def run():
        for _ in range(2):
            runtime = DshRuntime(
                store,
                registry=registry,
                llm_factory=lambda: None,
                client_factory=lambda _: None,
                settings=lambda: settings,
            )
            await runtime.prompt(sid, [{"type": "text", "text": "run probe"}])
            await runtime.wait_idle(sid)
            events = store.events(sid)
            ends = [e for e in events if e["type"] == "turn/end"]
            assert ends[-1]["data"]["reason"]["kind"] == "completed", events
            assert not runtime.clients
        assert called == [sid]
        assert len(requests) == 3
        assert requests[0]["_path"].split("?")[0] == (
            "/v1/messages" if backend == "anthropic" else "/v1/chat/completions"
        )
        names = [t.get("function", t)["name"] for t in requests[0]["tools"]]
        assert names and all(n.startswith("mcp__mindex__") for n in names), names
        assert "fixture completed" in json.dumps(store.events(sid))
        user_events = [e for e in store.events(sid) if e["type"] == "user/message"]
        assert len([e for e in user_events if e["data"]["source"]["kind"] == "user"]) == 2
        assert any(e["data"]["source"]["kind"] == "runtime-context" for e in user_events)
        assert "fixture-key" not in json.dumps(store.events(sid))
        assert "fixture-key" not in (tmp_path / "dsh" / sid / "managed.patch.yml").read_text()
        seq = [e["seq"] for e in store.events(sid)]
        assert seq == list(range(len(seq)))

    asyncio.run(run())


def test_real_dsh_million_context(tmp_path, model_server):
    url, requests = model_server
    settings = AgentConfig(llm={"base_url": url, "model": "million-context", "api_key": "fixture",
                                "context_window": 1048576, "max_tokens": 4096})
    if not readiness(settings)["ready"]:
        pytest.skip("uv sync --extra dsh")
    store = SessionStore(tmp_path)
    sid = store.create(runtime="dsh")["id"]

    async def execute(args, ctx):
        return ToolOutput({"ok": True})

    runtime = DshRuntime(store, registry=ToolRegistry([ToolSpec("probe", "meta", "probe", Empty, "read", execute)]),
                         llm_factory=lambda: None, client_factory=lambda _: None, settings=lambda: settings)

    async def run():
        await runtime.prompt(sid, [{"type": "text", "text": "run probe"}])
        await runtime.wait_idle(sid)
        events = store.events(sid)
        assert events[-1]["type"] == "turn/end"
        assert events[-1]["data"]["reason"]["kind"] == "completed", events
        assert len(requests) == 2
        assert requests[-1].get("max_tokens", requests[-1].get("max_completion_tokens")) == 4096

    asyncio.run(run())


def test_runtime_setting_keeps_existing_owner(config):
    from fastapi.testclient import TestClient

    from mindex.agent.llm.scripted import ScriptedLlm, say
    from mindex.api import create_app

    app = create_app(config, agent_llm=ScriptedLlm([[say("builtin")]]))
    with TestClient(app) as http:
        old = http.post("/api/agent/sessions", json={}).json()
        assert old["runtime"] == "builtin"
        check = http.get("/api/settings/agent-runtime").json()["dsh"]
        if not check["ready"]:
            pytest.skip("uv sync --extra dsh")
        assert http.put("/api/settings/agent-runtime", json={"runtime": "dsh"}).status_code == 200
        new = http.post("/api/agent/sessions", json={}).json()
        assert new["runtime"] == "dsh"
        assert http.get(f"/api/agent/sessions/{old['id']}").json()["runtime"] == "builtin"
        assert app.state.agent_runtime.owner(old["id"]) is app.state.agent_runtime.runtimes["builtin"]
        assert app.state.agent_runtime.owner(new["id"]) is app.state.agent_runtime.runtimes["dsh"]
    with TestClient(create_app(config)) as http:
        assert http.get("/api/settings/agent-runtime").json()["runtime"] == "dsh"
        assert http.get(f"/api/agent/sessions/{old['id']}").json()["runtime"] == "builtin"


def test_old_schema_and_atomic_upstream_dedup(tmp_path):
    from mindex.agent.schema import _v1, _v2
    from mindex.agent.session_store import Database

    db = Database(tmp_path / "agent.sqlite3")
    with db.connect() as connection:
        _v1(connection)
        _v2(connection)
        connection.execute("INSERT INTO agent_sessions(id,created_at,updated_at) VALUES('old',0,0)")
        connection.execute("PRAGMA user_version=2")
    store = SessionStore(tmp_path)
    assert store.get("old")["runtime"] == "builtin"
    assert store.append("old", "step/start", {"step": 1}, upstream_seq=42)["seq"] == 0
    assert store.append("old", "step/start", {"step": 1}, upstream_seq=42) is None
    assert store.append("old", "plugin:mindex/notice", {"text": "local"})["seq"] == 1
    assert store.events("old")[0]["upstream_seq"] == 42


@pytest.mark.parametrize("decision", ["allowed-once", "rejected", "cancel", "crash"])
def test_real_dsh_approval_and_exit(tmp_path, model_server, decision):
    url, _ = model_server
    settings = AgentConfig(runtime="dsh", llm={"base_url": url, "model": "fixture", "api_key": "fixture"})
    if not readiness(settings)["ready"]:
        pytest.skip("uv sync --extra dsh")
    calls = []

    async def execute(args, ctx):
        calls.append(True)
        return ToolOutput({"ok": True})

    registry = ToolRegistry([ToolSpec("probe", "meta", "probe", Empty, "write", execute)])
    store = SessionStore(tmp_path)
    sid = store.create(runtime="dsh")["id"]
    runtime = DshRuntime(
        store,
        registry=registry,
        llm_factory=lambda: None,
        client_factory=lambda _: None,
        settings=lambda: settings,
    )

    async def run():
        await runtime.prompt(sid, [{"type": "text", "text": "run probe"}])
        async with asyncio.timeout(20):
            while not runtime.pending(sid):  # noqa: ASYNC110 — observe UI pending state
                await asyncio.sleep(0.02)
            pending = runtime.pending(sid)[0]
            if decision == "cancel":
                await runtime.cancel(sid)
            elif decision == "crash":
                runtime.clients[sid]._proc.kill()
            else:
                await runtime.answer_approval(sid, pending["requestId"], decision)
            await runtime.wait_idle(sid)
        assert calls == ([True] if decision == "allowed-once" else [])
        assert not runtime.pending(sid)
        assert not runtime.clients
        events = store.events(sid)
        if decision == "cancel":
            assert [e for e in events if e["type"] == "turn/end"][-1]["data"]["reason"]["kind"] == "aborted"
        if decision in ("allowed-once", "rejected"):
            assert [e for e in events if e["type"] == "turn/end"][-1]["data"]["reason"]["kind"] == "completed"
            assert len([e for e in events if e["type"] == "tool/call"]) == 1
            assert len([e for e in events if e["type"] == "tool/result"]) == 1
        frames = [f async for f in runtime.events(sid, 0, follow=False)]
        assert [f["data"]["seq"] for f in frames if f["event"] == "session.event"] == list(
            range(1, len(events))
        )

    asyncio.run(run())


def test_settings_runtime_browser(config):
    import os
    import subprocess

    from test_agent_panel_ui import ROOT, playwright_available, serve

    from mindex.api import create_app

    if not playwright_available() or not readiness(config.agent)["ready"]:
        pytest.skip("Requires Playwright and dsh extra")
    server, thread, url = serve(create_app(config))
    try:
        result = subprocess.run(
            ["node", "tests/settings_runtime_ui.cjs"],
            cwd=ROOT,
            env={**os.environ, "MINDEX_RUNTIME_URL": url},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_missing_dsh_keeps_preference(config, monkeypatch):
    from fastapi.testclient import TestClient

    import mindex.agent.dsh_runtime as module
    from mindex.api import create_app
    from mindex.settings.runtime_settings import put_setting
    from mindex.shared.storage import Store

    store = Store(config.data_dir)
    put_setting(store, "agent.runtime", "dsh")
    monkeypatch.setattr(module, "readiness", lambda _: {"ready": False, "errors": ["uv sync --extra dsh"]})
    with TestClient(create_app(config)) as http:
        assert http.get("/api/settings/agent-runtime").json()["runtime"] == "dsh"
        assert http.put("/api/settings/agent-runtime", json={"runtime": "dsh"}).status_code == 422
        row = http.post("/api/agent/sessions", json={}).json()
        assert row["runtime"] == "dsh"
        http.post(f"/api/agent/sessions/{row['id']}/messages", json={"content": "hello"})
        from agent_helpers import wait_idle

        events = wait_idle(http, row["id"])
        assert events[-1]["data"]["reason"]["error"]["code"] == "dsh_unavailable"


@pytest.mark.parametrize("vision", [False, True])
def test_real_dsh_image_boundary(tmp_path, model_server, vision):
    import io

    from PIL import Image

    url, requests = model_server
    settings = AgentConfig(
        runtime="dsh", llm={"base_url": url, "model": "fixture", "api_key": "fixture", "vision": vision}
    )
    if not readiness(settings)["ready"]:
        pytest.skip("uv sync --extra dsh")
    store = SessionStore(tmp_path)
    sid = store.create(runtime="dsh")["id"]
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(image, format="PNG")

    async def execute(args, ctx):
        return ToolOutput({"ok": True})

    registry = ToolRegistry([ToolSpec("probe", "meta", "probe", Empty, "read", execute)])
    runtime = DshRuntime(
        store,
        registry=registry,
        llm_factory=lambda: None,
        client_factory=lambda _: None,
        settings=lambda: settings,
    )
    from mindex.agent.attachments import AttachmentError, AttachmentStore

    attachments = AttachmentStore(tmp_path)
    source = tmp_path / "image.png"
    source.write_bytes(image.getvalue())
    item = attachments.put(
        source, filename="image.png", content_type="image/png", owner="session", session_id=sid, ttl_hours=1
    )
    runtime.attachments = attachments
    other = store.create(runtime="dsh")["id"]
    with pytest.raises(AttachmentError):
        asyncio.run(runtime._image_loader(other)(item["id"]))
    from mindex.agent.llm.types import image_block, user_message

    message = user_message("look at image", "user")
    message["content"].append(image_block(item["id"], "image/png"))

    async def run():
        await runtime._run_turn(sid, message, None)

    asyncio.run(run())
    has_image = any(
        isinstance(m.get("content"), list) and any(b.get("type") == "image_url" for b in m["content"])
        for m in requests[0]["messages"]
    )
    assert has_image == vision
    assert "base64" not in json.dumps(store.events(sid))


def test_failed_sdk_start_releases_workers(tmp_path, monkeypatch):
    deepseek_harness = pytest.importorskip("deepseek_harness")

    class Broken:
        def __init__(self, config):
            pass

        def start(self):
            raise OSError("secret diagnostics")

        def close(self):
            pass

        def next_request(self):
            raise AssertionError("request reader must not start")

    monkeypatch.setattr(deepseek_harness, "HarnessClient", Broken)
    settings = AgentConfig(runtime="dsh")
    store = SessionStore(tmp_path)
    sid = store.create(runtime="dsh")["id"]
    runtime = DshRuntime(
        store,
        registry=ToolRegistry([]),
        llm_factory=lambda: None,
        client_factory=lambda _: None,
        settings=lambda: settings,
    )

    async def run():
        await runtime.prompt(sid, [{"type": "text", "text": "hello"}])
        async with asyncio.timeout(5):
            await runtime.wait_idle(sid)
        assert not runtime.clients
        assert store.events(sid)[-1]["data"]["reason"]["error"]["code"] == "dsh_failed"
        assert "secret diagnostics" not in json.dumps(store.events(sid))

    asyncio.run(run())


@pytest.mark.parametrize("method", ["approval/request", "user-question/ask"])
def test_native_requests_map_to_existing_cards(tmp_path, method):
    from types import SimpleNamespace

    store = SessionStore(tmp_path)
    sid = store.create(runtime="dsh")["id"]
    runtime = DshRuntime(
        store,
        registry=ToolRegistry([]),
        llm_factory=lambda: None,
        client_factory=lambda _: None,
        settings=AgentConfig,
    )
    replies = []
    client = SimpleNamespace(respond=lambda rid, body: replies.append((rid, body)))
    request = SimpleNamespace(
        id="native-1",
        method=method,
        payload={"toolName": "native-tool", "questions": [{"id": "q", "question": "continue?"}]},
    )

    async def run():
        task = asyncio.create_task(runtime._native_request(sid, client, request))
        await asyncio.sleep(0)
        pending = runtime.pending(sid)[0]
        assert runtime.status(sid) == "waiting"
        if method == "approval/request":
            await runtime.answer_approval(sid, pending["requestId"], "allowed-once")
        else:
            await runtime.answer_question(
                sid, pending["requestId"], [{"id": "q", "selected": [], "custom": "yes"}]
            )
        await task
        assert not runtime.pending(sid)
        assert replies[0][0] == "native-1"

    asyncio.run(run())


def test_real_dsh_dynamic_loading(tmp_path, model_server):
    url, requests = model_server
    settings = AgentConfig(runtime="dsh", llm={"base_url": url, "model": "dynamic", "api_key": "fixture"})
    if not readiness(settings)["ready"]:
        pytest.skip("uv sync --extra dsh")
    calls = []

    async def execute(args, ctx):
        calls.append(True)
        return ToolOutput({"ok": True})

    registry = ToolRegistry([*meta.specs(), ToolSpec("probe", "library", "probe", Empty, "read", execute)])
    store = SessionStore(tmp_path)
    sid = store.create(runtime="dsh")["id"]
    runtime = DshRuntime(
        store,
        registry=registry,
        llm_factory=lambda: None,
        client_factory=lambda _: None,
        settings=lambda: settings,
    )

    async def run():
        await runtime.prompt(sid, [{"type": "text", "text": "load and probe"}])
        await runtime.wait_idle(sid)

    asyncio.run(run())
    assert calls == [True]
    assert len(requests) == 4
    assert store.get(sid)["loaded_domains"] == ["library"]
    assert any(e["data"].get("error", {}).get("code") == "TOOL_NOT_LOADED" for e in store.events(sid))
    assert any(t["function"]["name"] == "mcp__mindex__probe" for t in requests[0]["tools"])
