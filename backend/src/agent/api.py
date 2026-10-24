"""``/api/agent`` 路由：会话、消息、SSE 事件流、确认、提问、取消与能力缺口。

SSE 事件名：``session.event``（持久事件，信封同 dsh）、``assistant.stream``（临时流式块）、
``session.status``（``idle|running|waiting`` 与待答复请求）、``ping``（保活）。
断线后用 ``?after=<最后收到的 seq>`` 续传；临时流式块不补发，持久事件不丢。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import Field

from mindex.agent.loopback import MindexClient
from mindex.agent.runtime import BuiltinRuntime, RequestNotFound, RuntimeBusy, frame_sse
from mindex.agent.session_store import SessionNotFound, SessionStore
from mindex.shared.config import StrictModel


class SessionBody(StrictModel):
    title: str = Field(default="", max_length=60)


class MessageBody(StrictModel):
    content: str = Field(min_length=1, max_length=8000)
    context: dict | None = None


class ApprovalBody(StrictModel):
    outcome: str
    grant: bool = False


class ChangeBody(StrictModel):
    kind: Literal["mutation", "focus"]
    data: dict


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
    from mindex.agent.watcher import BuildWatcher

    runtime.watcher = BuildWatcher(runtime, client_factory)
    from mindex.agent.changes import ChangeFeed

    runtime.feed = ChangeFeed(store.root)
    return runtime


def agent_router(runtime: BuiltinRuntime, runtime_config, *, injected: bool = False) -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["智能助手"])
    store = runtime.store

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

    @router.post("/sessions/{sid}/messages", status_code=202)
    async def post_message(sid: str, body: MessageBody):
        enabled()
        session_or_404(sid)
        try:
            return await runtime.prompt(sid, [{"type": "text", "text": body.content}], context=body.context)
        except RuntimeBusy as exc:
            raise HTTPException(409, str(exc)) from exc

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

    return router
