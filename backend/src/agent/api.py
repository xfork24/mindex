"""``/api/agent`` 路由：会话、消息、SSE 事件流、确认、提问、取消与能力缺口。

SSE 事件名：``session.event``（持久事件，信封同 dsh）、``assistant.stream``（临时流式块）、
``session.status``（``idle|running|waiting`` 与待答复请求）、``ping``（保活）。
断线后用 ``?after=<最后收到的 seq>`` 续传；临时流式块不补发，持久事件不丢。

附件（``/attachments``）不经网页鉴权中间件，由 ``attachment_caller`` 自行校验网页凭证或 MCP 令牌，
见该函数说明；消息体的 ``attachments`` 只能引用归属本会话的附件。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUpload

from mindex.agent.attachments import AttachmentError, AttachmentStore
from mindex.agent.loopback import MindexClient
from mindex.agent.runtime import BuiltinRuntime, RequestNotFound, RuntimeBusy, frame_sse
from mindex.agent.session_store import SessionNotFound, SessionStore
from mindex.shared.config import StrictModel
from mindex.shared.uploads import save_upload
from mindex.shared.web_auth import authorized, configured_token

MAX_ATTACHMENTS = 12
# 附件内容只对这些类型按原类型内联返回（缩略图、预览）；其余一律以下载方式返回，避免同源执行上传的 HTML/SVG。
INLINE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "video/", "audio/")


class SessionBody(StrictModel):
    title: str = Field(default="", max_length=60)


class MessageBody(StrictModel):
    content: str = Field(min_length=1, max_length=8000)
    context: dict | None = None
    attachments: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class ApprovalBody(StrictModel):
    outcome: str
    grant: bool = False


class ChangeBody(StrictModel):
    kind: Literal["mutation", "focus"]
    data: dict


class GapBody(StrictModel):
    request: str = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=1000)
    suggestion: str = Field(default="", max_length=1000)


class AnswerItem(StrictModel):
    id: str
    selected: list[str] = Field(default_factory=list)
    custom: str | None = Field(default=None, max_length=4000)
    values: dict[str, str | float | int | bool | None] | None = None


class AnswersBody(StrictModel):
    answers: list[AnswerItem]


def build_runtime(app, runtime_config, store, *, agent_llm=None, registry=None) -> BuiltinRuntime:
    from mindex.agent.llm.factory import make_agent_llm
    from mindex.agent.tools import build_registry
    from mindex.settings.runtime_settings import get_setting

    holder: dict = {}

    def client_factory(resolve_secret):
        if "client" not in holder:
            holder["client"] = MindexClient(app)
        return holder["client"].with_secrets(resolve_secret)

    def agent_profile():
        value = get_setting(store, "active.profile.agent")
        return int(value) if value else None

    runtime = BuiltinRuntime(
        SessionStore(store.root),
        registry=registry or build_registry(),
        llm_factory=(lambda: agent_llm) if agent_llm is not None else (lambda: make_agent_llm(runtime_config.current())),
        client_factory=client_factory,
        settings=lambda: runtime_config.current().agent,
        agent_profile=agent_profile,
    )
    from mindex.agent.watcher import TaskWatcher

    runtime.watcher = TaskWatcher(runtime, client_factory)
    from mindex.agent.changes import ChangeFeed

    runtime.feed = ChangeFeed(store.root)
    runtime.attachments = AttachmentStore(store.root)
    return runtime


def attachment_caller(request: Request, mcp_token_env: str) -> dict:
    """附件端点的调用方凭证（``/api/agent/attachments`` 不经网页中间件，在这里自行鉴权）。

    - ``web``：网页凭证（Cookie、Bearer ``MINDEX_WEB_TOKEN``、进程内部凭证）；未启用网页令牌时视为具备。
    - ``mcp``：Bearer ``MINDEX_MCP_TOKEN``（常量时间比较）；网页与 MCP 令牌都未设置时视为具备（本机默认）。

    两者都不具备时返回 401。归属规则：上传带 ``session_id`` 的需要 ``web``，附件归属该会话；不带的需要
    ``mcp``，附件归属 mcp。读取、下载与删除需要任一凭证；只有 MCP 凭证的调用方只能访问 mcp 附件。
    """
    state_token = getattr(request.app.state, "web_auth_token", None)
    web_secret = configured_token() if state_token is None else state_token
    mcp_secret = os.environ.get(mcp_token_env, "").strip()
    web = authorized(request, web_secret) if web_secret else True
    header = request.headers.get("authorization", "")
    bearer = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if mcp_secret:
        mcp = bool(bearer) and hmac.compare_digest(bearer.encode(), mcp_secret.encode())
    else:
        mcp = not web_secret
    if not (web or mcp):
        raise HTTPException(401, "需要访问令牌：使用网页登录凭证，或发送 Authorization: Bearer <MCP 令牌>",
                            headers={"WWW-Authenticate": "Bearer"})
    return {"web": web, "mcp": mcp}


def agent_router(runtime: BuiltinRuntime, runtime_config, *, injected: bool = False) -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["智能助手"])
    store = runtime.store
    attachments: AttachmentStore = runtime.attachments

    def caller(request: Request) -> dict:
        return attachment_caller(request, runtime_config.current().agent.mcp.token_env)

    def enabled():
        if not runtime_config.current().agent.enabled:
            raise HTTPException(503, "智能助手未启用（agent.enabled=false）")

    def session_or_404(sid):
        try:
            return store.get(sid)
        except SessionNotFound as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/status")
    async def status():
        cfg = runtime_config.current().agent
        key = cfg.llm.api_key or os.environ.get(cfg.llm.api_key_env, "")
        return {
            "enabled": cfg.enabled,
            "backend": cfg.llm.backend,
            "model": "scripted-test" if injected else cfg.llm.model,
            "configured": injected or bool(key) or not cfg.llm.base_url.startswith("https://"),
            "max_steps": cfg.max_steps,
        }

    @router.get("/sessions")
    async def list_sessions():
        await runtime.start()
        return {"items": store.list()}

    @router.post("/sessions", status_code=201)
    async def create_session(body: SessionBody | None = None):
        enabled()
        return await runtime.create_or_resume(None, (body or SessionBody()).title)

    @router.get("/sessions/{sid}")
    async def get_session(sid: str):
        await runtime.start()
        session = session_or_404(sid)
        return {**session, "status": runtime.status(sid), "pending": runtime.pending(sid)}

    @router.delete("/sessions/{sid}", status_code=204)
    async def delete_session(sid: str):
        session_or_404(sid)
        await runtime.cancel(sid)
        runtime.watcher.forget(sid)
        store.delete(sid)
        attachments.delete_session(sid)

    @router.post("/sessions/{sid}/messages", status_code=202)
    async def post_message(sid: str, body: MessageBody):
        enabled()
        session_or_404(sid)
        items = []
        for ref in dict.fromkeys(body.attachments):  # 去重并保持顺序
            try:
                attachments.resolve(ref, session_id=sid, owner="session")
                meta = attachments.get(ref)
            except AttachmentError as exc:
                raise HTTPException(422, f"{exc}（{ref}）") from exc
            items.append({"ref": meta["id"], "filename": meta["filename"], "content_type": meta["content_type"],
                          "bytes": meta["bytes"]})
        try:
            return await runtime.prompt(sid, [{"type": "text", "text": body.content}], context=body.context,
                                        attachments=items)
        except RuntimeBusy as exc:
            raise HTTPException(409, str(exc)) from exc

    # ------------------------------------------------------------ 附件
    def attachment_or_404(tok: str, who: dict) -> tuple[dict, Path]:
        try:
            meta, path = attachments.file(tok)
        except AttachmentError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not who["web"] and meta["owner"] != "mcp":
            raise HTTPException(404, "找不到附件，可能已被删除或过期回收，请重新上传")
        return meta, path

    @router.post("/attachments", status_code=201)
    async def upload_attachment(request: Request):
        """上传单个附件（multipart：``file``，可选 ``session_id``）。

        带 session_id 归属该面板会话（需网页凭证）；不带则归属 mcp（需 MCP 令牌）。先鉴权再解析请求体，
        未授权的请求不会落盘任何内容。
        """
        who = caller(request)
        cfg = runtime_config.current().agent
        limit = cfg.attachment_max_mb * 1024 * 1024
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit + 1024 * 1024:  # 预留 multipart 头部开销
            raise HTTPException(413, "上传文件过大")
        async with request.form(max_files=1, max_fields=4) as form:
            file = form.get("file")
            raw_session = form.get("session_id")
            if not isinstance(file, StarletteUpload):
                raise HTTPException(422, "缺少上传文件 file")
            session_id = raw_session.strip() if isinstance(raw_session, str) and raw_session.strip() else None
            return await store_attachment(file, session_id, who, cfg, limit)

    async def store_attachment(file, session_id: str | None, who: dict, cfg, limit: int) -> dict:
        if session_id:
            if not who["web"]:
                raise HTTPException(401, "会话附件需要网页登录凭证")
            session_or_404(session_id)
            owner = "session"
        elif who["mcp"]:
            owner = "mcp"
        else:
            raise HTTPException(422, "网页上传附件必须指定会话 session_id")
        staging = attachments.root / ".uploads"
        staging.mkdir(parents=True, exist_ok=True)
        temp = staging / secrets.token_hex(16)
        try:
            await save_upload(file, temp, limit)
            # 整文件 sha256 与移动是阻塞 IO，放进线程池以免冻结事件循环。
            return await run_in_threadpool(
                attachments.put, temp, filename=file.filename, content_type=file.content_type, owner=owner,
                session_id=session_id if owner == "session" else None, ttl_hours=cfg.attachment_ttl_hours,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            temp.unlink(missing_ok=True)  # 超限或失败时删除已写入的部分；成功时文件已被移走

    @router.get("/attachments/{tok}")
    async def attachment_meta(tok: str, request: Request):
        return attachment_or_404(tok, caller(request))[0]

    @router.get("/attachments/{tok}/content")
    async def attachment_content(tok: str, request: Request):
        meta, path = attachment_or_404(tok, caller(request))
        inline = meta["content_type"].startswith(INLINE_TYPES)
        return FileResponse(path, media_type=meta["content_type"] if inline else "application/octet-stream",
                            filename=meta["filename"], content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @router.delete("/attachments/{tok}", status_code=204)
    async def delete_attachment(tok: str, request: Request):
        """撤销暂存（面板移除已上传的附件）；不对应任何工具。"""
        attachment_or_404(tok, caller(request))
        try:
            attachments.delete(tok)
        except AttachmentError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(status_code=204)

    @router.get("/sessions/{sid}/log")
    async def get_log(sid: str, after: int = Query(default=-1, ge=-1)):
        await runtime.start()
        session_or_404(sid)
        return {"items": store.events(sid, after), "status": runtime.status(sid), "pending": runtime.pending(sid)}

    @router.get("/sessions/{sid}/events")
    async def stream_events(sid: str, after: int = Query(default=-1, ge=-1), follow: bool = True):
        await runtime.start()
        session_or_404(sid)

        async def body():
            async for frame in runtime.events(sid, after, follow=follow):
                yield frame_sse(frame)

        return StreamingResponse(body(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/sessions/{sid}/approvals/{rid}")
    async def answer_approval(sid: str, rid: str, body: ApprovalBody):
        session_or_404(sid)
        try:
            await runtime.answer_approval(sid, rid, body.outcome, grant=body.grant)
        except RequestNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @router.post("/sessions/{sid}/questions/{rid}")
    async def answer_question(sid: str, rid: str, body: AnswersBody):
        session_or_404(sid)
        pending = next((p for p in runtime.pending(sid) if p["requestId"] == rid and p["kind"] == "question"), None)
        if pending is None:
            raise HTTPException(404, "找不到待回答的提问，可能已经处理或已取消")
        ttl = runtime_config.current().agent.secret_ttl_seconds
        questions = {q["id"]: q for q in pending["questions"]}
        answers = []
        for item in body.answers:
            question = questions.get(item.id)
            if question is None:
                raise HTTPException(422, f"未知问题 {item.id}")
            value = item.model_dump(exclude_none=True)
            fields = {f["id"]: f for f in question.get("fields") or []}
            if item.values:
                values = {}
                for key, raw in item.values.items():
                    field = fields.get(key)
                    if field is None:
                        raise HTTPException(422, f"问题 {item.id} 没有字段 {key}")
                    if field.get("type") == "secret" and raw not in (None, ""):
                        values[key] = store.put_secret(sid, str(raw), ttl)  # 明文只进保险库
                    else:
                        values[key] = raw
                value["values"] = values
            answers.append(value)
        try:
            await runtime.answer_question(sid, rid, answers)
        except RequestNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @router.post("/sessions/{sid}/cancel")
    async def cancel(sid: str):
        session_or_404(sid)
        await runtime.cancel(sid)
        return {"ok": True, "status": runtime.status(sid)}

    @router.get("/changes/log")
    async def changes_log(after: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)):
        return {"items": runtime.feed.after(after, limit), "latest": runtime.feed.latest()}

    @router.post("/changes", status_code=201)
    async def report_change(body: ChangeBody):
        """独立进程的 MCP 服务（mindex mcp --base-url）经此报告改动与定位；来源固定为 mcp。"""
        return runtime.feed.publish(body.kind, body.data, source="mcp")

    @router.get("/changes")
    async def stream_changes(after: int = Query(default=-1, ge=-1), follow: bool = True):
        """全局改动与定位通道。after=-1 从当前最新开始（不回放历史）；断线后用最后收到的 seq 续传。"""
        feed = runtime.feed

        async def body():
            last = feed.latest() if after < 0 else after
            yield f"event: ready\ndata: {json.dumps({'seq': last})}\n\n"
            idle = 0.0
            while True:
                items = feed.after(last)
                for item in items:
                    last = item["seq"]
                    yield f"event: change\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if not follow:
                    return
                if items:
                    idle = 0.0
                    continue
                await asyncio.sleep(0.5)
                idle += 0.5
                if idle >= 15:
                    idle = 0.0
                    yield "event: ping\ndata: {}\n\n"

        return StreamingResponse(body(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/gaps")
    async def gaps(limit: int = Query(default=200, ge=1, le=1000)):
        return {"items": store.gaps(limit)}

    @router.post("/gaps", status_code=201)
    async def report_gap(body: GapBody):
        """独立进程的 MCP 服务（mindex mcp --base-url）经此记录能力缺口；不关联会话。"""
        return store.add_gap(None, body.request, body.reason, body.suggestion)

    return router
