"""助手附件：端点（上传/元数据/内容/删除、大小限制、令牌与归属、三种令牌配置、消息引用校验、会话删除级联），
回环客户端按归属解析附件提交 multipart（面板会话、进程内 MCP、远程模式），真实备份导出的排除，
以及大文件上传的内存行为。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUpload

from agent.llm.scripted import ScriptedLlm, call, say
from agent.loopback import LoopbackError, MindexClient
from agent.registry import ToolOutput, ToolRegistry, ToolSpec
from agent.tools import meta
from agent_helpers import new_session, result_text, results, send, wait_idle
from api import create_app
from shared.config import StrictModel

WEB, MCP = "web-secret", "mcp-secret"


def upload(client, data: bytes, name="a.png", content_type="image/png", session_id=None, headers=None):
    form = {"session_id": session_id} if session_id else {}
    return client.post(
        "/api/agent/attachments", files={"file": (name, data, content_type)}, data=form, headers=headers or {}
    )


def tok(ref: str) -> str:
    return ref.removeprefix("attachment://")


@pytest.fixture
def local(config, monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.delenv("MINDEX_MCP_TOKEN", raising=False)
    config.agent.attachment_max_mb = 1
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm, config


def test_upload_metadata_content_delete(local):
    client, _llm, config = local
    sid = new_session(client)
    response = upload(client, b"\x89PNG-data", session_id=sid)
    assert response.status_code == 201, response.text
    meta = response.json()
    assert meta["id"].startswith("attachment://") and meta["owner"] == "session" and meta["session_id"] == sid
    assert meta["filename"] == "a.png" and meta["bytes"] == 9 and len(meta["sha256"]) == 64
    assert meta["expires_at"] - meta["created_at"] == config.agent.attachment_ttl_hours * 3600 * 1000
    t = tok(meta["id"])
    assert client.get(f"/api/agent/attachments/{t}").json() == meta
    content = client.get(f"/api/agent/attachments/{t}/content")
    assert content.content == b"\x89PNG-data" and content.headers["content-type"] == "image/png"
    assert content.headers["x-content-type-options"] == "nosniff"
    # 不带会话：本机默认（未设置任何令牌）归属 mcp。
    assert upload(client, b"x").json()["owner"] == "mcp"
    assert client.delete(f"/api/agent/attachments/{t}").status_code == 204
    assert client.get(f"/api/agent/attachments/{t}").status_code == 404
    assert client.delete(f"/api/agent/attachments/{t}").status_code == 404
    assert client.get("/api/agent/attachments/not-a-token").status_code == 404


def test_untrusted_types_are_downloaded_not_inlined(local):
    client, _llm, _config = local
    ref = upload(client, b"<script>alert(1)</script>", name="x.html", content_type="text/html").json()["id"]
    content = client.get(f"/api/agent/attachments/{tok(ref)}/content")
    assert content.headers["content-type"] == "application/octet-stream"
    assert content.headers["content-disposition"].startswith("attachment")


def test_size_limits_and_partial_cleanup(local):
    client, _llm, config = local
    sid = new_session(client)
    too_big = upload(client, b"0" * (1024 * 1024 + 1), session_id=sid)
    assert too_big.status_code == 413
    assert upload(client, b"", session_id=sid).status_code == 422
    staging = config.data_dir / "agent-attachments" / ".uploads"
    assert not any(staging.iterdir())
    assert upload(client, b"x", session_id="missing").status_code == 404
    assert client.post("/api/agent/attachments", data={"session_id": sid}).status_code == 422


def test_message_attachments_validated_and_recorded(local):
    client, llm, config = local
    sid, other = new_session(client), new_session(client)
    mine = upload(client, b"img", session_id=sid).json()
    theirs = upload(client, b"img", session_id=other).json()
    mcp = upload(client, b"img").json()
    for ref in (theirs["id"], mcp["id"], "attachment://" + "0" * 32, "bogus"):
        response = client.post(
            f"/api/agent/sessions/{sid}/messages", json={"content": "看图", "attachments": [ref]}
        )
        assert response.status_code == 422, ref
    too_many = client.post(
        f"/api/agent/sessions/{sid}/messages", json={"content": "看图", "attachments": [mine["id"]] * 13}
    )
    assert too_many.status_code == 422

    llm.extend([say("收到")])
    response = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"content": "看图", "attachments": [mine["id"], mine["id"]]},
    )
    assert response.status_code == 202, response.text
    events = wait_idle(client, sid)
    message = next(
        e["data"] for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "user"
    )
    assert message["attachments"] == [
        {"ref": mine["id"], "filename": "a.png", "content_type": "image/png", "bytes": 3}
    ]
    text = message["content"][0]["text"]
    assert text.startswith("看图") and f"附件：{mine['id']} a.png（image/png，3 B）" in text
    assert client.get(f"/api/agent/sessions/{sid}").json()["title"] == "看图"
    # 发给模型的就是该文本；附件元数据不影响协议转换。
    assert any(
        f"附件：{mine['id']}" in m["content"][0]["text"]
        for m in llm.requests[0].messages
        if m["role"] == "user"
    )

    folder = config.data_dir / "agent-attachments" / tok(mine["id"])
    assert folder.is_dir()
    assert client.delete(f"/api/agent/sessions/{sid}").status_code == 204
    assert not folder.exists()
    assert client.get(f"/api/agent/attachments/{tok(mine['id'])}").status_code == 404
    assert client.get(f"/api/agent/attachments/{tok(theirs['id'])}").status_code == 200


def test_tokens_and_owner_rules(config, monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.setenv("MINDEX_MCP_TOKEN", MCP)
    web = {"Authorization": f"Bearer {WEB}"}
    mcp = {"Authorization": f"Bearer {MCP}"}
    with TestClient(create_app(config, agent_llm=ScriptedLlm(), web_token=WEB)) as client:
        assert upload(client, b"x").status_code == 401
        assert upload(client, b"x", headers={"Authorization": "Bearer wrong"}).status_code == 401
        # 其余助手接口仍受网页令牌保护。
        assert client.get("/api/agent/sessions").status_code == 401
        sid = client.post("/api/agent/sessions", json={}, headers=web).json()["id"]

        session_ref = upload(client, b"x", session_id=sid, headers=web)
        assert session_ref.status_code == 201 and session_ref.json()["owner"] == "session"
        assert upload(client, b"x", headers=web).status_code == 422  # 网页上传必须绑定会话
        assert upload(client, b"x", session_id=sid, headers=mcp).status_code == 401
        mcp_ref = upload(client, b"x", headers=mcp)
        assert (
            mcp_ref.status_code == 201
            and mcp_ref.json()["owner"] == "mcp"
            and mcp_ref.json()["session_id"] is None
        )

        session_tok, mcp_tok = tok(session_ref.json()["id"]), tok(mcp_ref.json()["id"])
        assert client.get(f"/api/agent/attachments/{mcp_tok}").status_code == 401
        assert client.get(f"/api/agent/attachments/{mcp_tok}/content", headers=mcp).content == b"x"
        assert client.get(f"/api/agent/attachments/{mcp_tok}", headers=web).status_code == 200
        # 只持 MCP 令牌的调用方看不到会话附件。
        assert client.get(f"/api/agent/attachments/{session_tok}", headers=mcp).status_code == 404
        assert client.delete(f"/api/agent/attachments/{session_tok}", headers=mcp).status_code == 404
        assert client.delete(f"/api/agent/attachments/{session_tok}", headers=web).status_code == 204


@pytest.mark.parametrize("situation", ["none", "web_only", "mcp_only"])
def test_three_token_situations(config, monkeypatch, situation):
    """``attachment_caller``：未启用网页令牌即视为具备 web；MCP 令牌与网页令牌都未设置时才视为具备 mcp。"""
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.delenv("MINDEX_MCP_TOKEN", raising=False)
    if situation == "mcp_only":
        monkeypatch.setenv("MINDEX_MCP_TOKEN", MCP)
    web_token = WEB if situation == "web_only" else None
    web = {"Authorization": f"Bearer {WEB}"} if situation == "web_only" else {}
    mcp = {"Authorization": f"Bearer {MCP}"}
    with TestClient(create_app(config, agent_llm=ScriptedLlm(), web_token=web_token)) as client:
        sid = client.post("/api/agent/sessions", json={}, headers=web).json()["id"]
        session_up = upload(client, b"x", session_id=sid, headers=web)
        assert session_up.status_code == 201 and session_up.json()["owner"] == "session"
        bare = upload(client, b"x")
        if situation == "none":
            # 本机默认：不带会话即归属 mcp。
            assert bare.status_code == 201 and bare.json()["owner"] == "mcp"
        elif situation == "web_only":
            # 只有网页令牌：没有 mcp 归属路径；不带凭证 401，带网页凭证但无会话 422。
            assert bare.status_code == 401
            assert upload(client, b"x", session_id=sid).status_code == 401
            assert upload(client, b"x", headers=web).status_code == 422
            assert upload(client, b"x", headers=mcp).status_code == 401
        else:
            # 只有 MCP 令牌：网页侧未启用令牌，会话上传无需凭证；mcp 归属必须持 MCP 令牌。
            assert bare.status_code == 422
            assert upload(client, b"x", headers={"Authorization": "Bearer wrong"}).status_code == 422
            mcp_up = upload(client, b"x", headers=mcp)
            assert mcp_up.status_code == 201 and mcp_up.json()["owner"] == "mcp"
            # 带 MCP 令牌但同时给会话：网页侧仍视为具备，归属会话。
            assert upload(client, b"x", session_id=sid, headers=mcp).json()["owner"] == "session"


def test_backup_export_excludes_agent_state(tmp_path, video_file):
    """真实导出：agent.sqlite3 与 agent-attachments/ 都列入排除清单，不出现在备份文件里。"""
    import yaml

    from agent.attachments import AttachmentStore
    from maintenance.backup import export_backup
    from test_backup import seed

    config, _sources, _index = seed(tmp_path / "origin", video_file)
    staged = tmp_path / "photo.png"
    staged.write_bytes(b"\x89PNG-backup")
    ref = AttachmentStore(config.data_dir).put(
        staged, filename="photo.png", content_type="image/png", owner="mcp", session_id=None, ttl_hours=1
    )["id"]
    partial = config.data_dir / "agent-attachments" / ".uploads" / ("f" * 32)
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"partial")
    config_path = tmp_path / "original.yaml"
    config_path.write_text(yaml.safe_dump({"data_dir": str(config.data_dir), "embedding": {"dimensions": 8}}))

    bundle = tmp_path / "backup"
    export_backup(config, bundle, all_sources=True, config_path=config_path)
    manifest = json.loads((bundle / "manifest.json").read_text())

    attachment = f"agent-attachments/{tok(ref)}/photo.png"
    reasons = {item["path"]: item["reason"] for item in manifest["excluded"]}
    assert {"agent.sqlite3", attachment, f"agent-attachments/.uploads/{'f' * 32}"} <= reasons.keys()
    assert "不进入备份" in reasons[attachment] and "不进入备份" in reasons["agent.sqlite3"]
    files = [item["path"] for item in manifest["files"]]
    assert files and not [p for p in files if "agent-attachments" in p or p.endswith("agent.sqlite3")]
    assert not list(bundle.rglob("agent-attachments")) and not list(bundle.rglob("agent.sqlite3"))


# ---------------------------------------------------------------- 大文件上传的内存行为

MIB = 1024 * 1024


def add_sink(app, received: list) -> None:
    """按块读取上传文件、只记录字节数与是否已落盘（不把文件读进内存）。"""

    async def sink(request: Request):
        async with request.form() as form:
            file, size = form["file"], 0
            while chunk := await file.read(MIB):
                size += len(chunk)
            received.append({"bytes": size, "spooled_to_disk": bool(getattr(file.file, "_rolled", False))})
        return {"bytes": size}

    app.router.routes.insert(0, APIRoute("/api/test/sink", sink, methods=["POST"]))


def big_file(path, size: int):
    with path.open("wb") as fh:
        for _ in range(size // MIB):
            fh.write(b"\0" * MIB)
    return path


async def peak_mib(make):
    """``make()`` 执行期间 Python 堆的峰值（MiB）。"""
    import tracemalloc

    tracemalloc.start()
    try:
        result = await make()
        return result, tracemalloc.get_traced_memory()[1] / MIB
    finally:
        tracemalloc.stop()


def test_large_upload_memory_behaviour(local, tmp_path):
    """进程内回环与上传端点都流式处理：峰值与文件大小无关（实测 64 MiB 文件约 2 MiB）。
    远程模式先把附件整个取回内存再提交，峰值至少为文件大小——这是 docs/agent.md「附件」一节记录的已知限制。"""
    client, _llm, config = local
    config.agent.attachment_max_mb = 64
    app, received = client.app, []
    add_sink(app, received)
    store = app.state.agent_runtime.attachments
    sid = new_session(client)
    size = 24 * MIB
    session_ref = store.put(
        big_file(tmp_path / "s.bin", size),
        filename="s.mp4",
        content_type="video/mp4",
        owner="session",
        session_id=sid,
        ttl_hours=1,
    )["id"]
    mcp_ref = store.put(
        big_file(tmp_path / "m.bin", size),
        filename="m.mp4",
        content_type="video/mp4",
        owner="mcp",
        session_id=None,
        ttl_hours=1,
    )["id"]
    source = store.resolve(session_ref, session_id=sid, owner="session")

    async def go():
        base = MindexClient(app)
        loop = base.with_resolvers(attachment=store.resolver(session_id=sid, owner="session"))
        remote = MindexClient(base_url="http://remote.test")
        await remote.http.aclose()
        remote.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://remote.test")

        async def panel_upload():  # 与浏览器 XHR 一样流式提交到附件端点本身
            with source.open("rb") as fh:
                return await base.http.post(
                    "/api/agent/attachments",
                    data={"session_id": sid},
                    files={"file": ("p.mp4", fh, "video/mp4")},
                )

        try:
            forwarded = await peak_mib(lambda: loop.upload("/api/test/sink", files={"file": session_ref}))
            stored = await peak_mib(panel_upload)
            via_remote = await peak_mib(lambda: remote.upload("/api/test/sink", files={"file": mcp_ref}))
            return forwarded, stored, via_remote
        finally:
            await remote.aclose()
            await base.aclose()

    (forwarded, forwarded_peak), (stored, stored_peak), (via_remote, remote_peak) = asyncio.run(go())
    assert forwarded["ok"] and forwarded["data"]["bytes"] == size
    assert stored.status_code == 201 and stored.json()["bytes"] == size
    assert via_remote["ok"] and via_remote["data"]["bytes"] == size
    assert received == [{"bytes": size, "spooled_to_disk": True}] * 2
    assert forwarded_peak < 8 and stored_peak < 8, (forwarded_peak, stored_peak)
    assert remote_peak >= size / MIB, remote_peak


# ---------------------------------------------------------------- 回环 multipart：按归属解析附件


class ProbeArgs(StrictModel):
    ref: str


def add_echo(app, hits: list) -> None:
    """测试专用的 multipart 回显路由（插到最前，避免被静态资源挂载遮住）。"""

    async def echo(request: Request):
        hits.append(1)
        form = await request.form()
        files, fields = [], {}
        for key, value in form.multi_items():
            if isinstance(value, StarletteUpload):
                files.append(
                    {
                        "field": key,
                        "filename": value.filename,
                        "content_type": value.content_type,
                        "data": (await value.read()).decode(),
                    }
                )
            else:
                fields[key] = value
        return {"files": files, "fields": fields}

    app.router.routes.insert(0, APIRoute("/api/test/echo", echo, methods=["POST"]))


def probe_registry() -> ToolRegistry:
    async def execute(args, ctx) -> ToolOutput:
        result = await ctx.client.upload("/api/test/echo", files={"file": args.ref})
        return ToolOutput(result, text=json.dumps(result, ensure_ascii=False))

    return ToolRegistry(
        [*meta.specs(), ToolSpec("upload_probe", "meta", "回显上传", ProbeArgs, "read", execute)]
    )


def test_loopback_upload_resolves_session_attachments(local):
    client, _llm, _config = local
    app, hits = client.app, []
    add_echo(app, hits)
    store = app.state.agent_runtime.attachments
    sid, other = new_session(client), new_session(client)
    mine = upload(
        client,
        b"1\n00:00:00,000 --> 00:00:01,000\nhi\n",
        name="a.srt",
        content_type="application/x-subrip",
        session_id=sid,
    ).json()["id"]
    theirs = upload(client, b"x", session_id=other).json()["id"]
    mcp_ref = upload(client, b"x").json()["id"]

    async def go():
        base = MindexClient(app)
        loop = base.with_resolvers(attachment=store.resolver(session_id=sid, owner="session"))
        try:
            ok = await loop.upload(
                "/api/test/echo",
                files={"file": mine, "extra": [mine, mine]},
                data={"kind": "image", "filters": {"a": 1}, "flag": True, "skip": None},
            )
            bad = [
                await loop.upload("/api/test/echo", files={"file": ref})
                for ref in (theirs, mcp_ref, "attachment://" + "0" * 32, "secret://x", "/etc/passwd")
            ]
            mixed = await loop.upload("/api/test/echo", files={"file": [mine, theirs]})
            unresolved = await base.upload("/api/test/echo", files={"file": mine})
            return ok, bad, mixed, unresolved
        finally:
            await base.aclose()

    before = len(hits)
    ok, bad, mixed, unresolved = asyncio.run(go())
    assert ok["ok"], ok
    assert [f["field"] for f in ok["data"]["files"]] == ["file", "extra", "extra"]
    assert ok["data"]["files"][0] == {
        "field": "file",
        "filename": "a.srt",
        "content_type": "application/x-subrip",
        "data": "1\n00:00:00,000 --> 00:00:01,000\nhi\n",
    }
    assert ok["data"]["fields"] == {"kind": "image", "filters": '{"a": 1}', "flag": "true"}
    # 越权、不存在、格式不对、只要有一个无法解析：都不向目标接口发送请求。
    for result in (*bad, mixed):
        assert result["ok"] is False and result["status"] == 400 and result["code"] == "attachment_invalid", (
            result
        )
    assert "不属于当前会话" in bad[0]["error"] and "不属于当前会话" in bad[1]["error"]
    assert "当前环境不能使用附件引用" in unresolved["error"]
    assert len(hits) == before + 1


def test_runtime_tool_calls_resolve_only_own_session(local):
    client, llm, _config = local
    app, hits = client.app, []
    add_echo(app, hits)
    app.state.agent_runtime.registry = probe_registry()
    sid, other = new_session(client), new_session(client)
    mine = upload(client, b"mine", session_id=sid).json()["id"]
    theirs = upload(client, b"theirs", session_id=other).json()["id"]
    llm.extend([call("upload_probe", ref=mine), call("upload_probe", ref=theirs), say("好了")])
    send(client, sid, "上传")
    outputs = [json.loads(result_text(r)) for r in results(wait_idle(client, sid))]
    assert outputs[0]["ok"] and outputs[0]["data"]["files"][0]["data"] == "mine"
    assert outputs[1]["ok"] is False and outputs[1]["code"] == "attachment_invalid"
    assert len(hits) == 1


def test_remote_client_fetches_mcp_attachment_before_upload(local):
    client, _llm, _config = local
    app, hits = client.app, []
    add_echo(app, hits)
    sid = new_session(client)
    mcp_ref = upload(client, b"<b>remote</b>", name="r.html", content_type="text/html").json()["id"]
    session_ref = upload(client, b"x", session_id=sid).json()["id"]

    async def go():
        remote = MindexClient(base_url="http://remote.test")
        await remote.http.aclose()
        remote.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://remote.test")
        try:
            ok = await remote.upload("/api/test/echo", files={"file": mcp_ref})
            denied = await remote.upload("/api/test/echo", files={"file": session_ref})
            missing = await remote.upload("/api/test/echo", files={"file": "attachment://" + "0" * 32})
            content = await remote.fetch_bytes(f"/api/agent/attachments/{tok(mcp_ref)}/content")
            with pytest.raises(LoopbackError) as gone:
                await remote.fetch_bytes(f"/api/agent/attachments/{'0' * 32}/content")
            return ok, denied, missing, content, gone.value.status
        finally:
            await remote.aclose()

    ok, denied, missing, content, gone = asyncio.run(go())
    # 内容端点对 html 以下载方式返回，但上传时用元数据里的原始文件名与类型。
    assert ok["ok"] and ok["data"]["files"] == [
        {"field": "file", "filename": "r.html", "content_type": "text/html", "data": "<b>remote</b>"}
    ]
    assert denied["status"] == 400 and denied["code"] == "attachment_invalid"
    assert missing["status"] == 400 and missing["code"] == "attachment_invalid"
    assert content == (b"<b>remote</b>", "application/octet-stream") and gone == 404
    assert len(hits) == 1


def test_inprocess_mcp_resolves_only_mcp_attachments(local, monkeypatch):
    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    import agent.tools as tools
    from agent.mcp_server import server_for_app

    client, _llm, _config = local
    app, hits = client.app, []
    add_echo(app, hits)
    sid = new_session(client)
    mcp_ref = upload(client, b"via-mcp").json()["id"]
    session_ref = upload(client, b"x", session_id=sid).json()["id"]
    monkeypatch.setattr(tools, "build_registry", probe_registry)
    server = server_for_app(app, app.state.runtime_config, app.state.store)

    async def go():
        async with create_connected_server_and_client_session(server) as session:
            return [await session.call_tool("upload_probe", {"ref": ref}) for ref in (mcp_ref, session_ref)]

    ok, denied = asyncio.run(go())
    assert json.loads(ok.content[0].text)["data"]["files"][0]["data"] == "via-mcp"
    assert json.loads(denied.content[0].text)["code"] == "attachment_invalid"
    assert len(hits) == 1
