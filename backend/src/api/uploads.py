"""可跨 API 续传的上传协议；每个会话使用独立的能力凭据。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import tempfile
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from shared.media_delivery import conditional_response, materialized_response
from shared.upload_sessions import UploadError, UploadSessions


class UploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["api", "s3"] = "api"
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0)
    sha256: str = Field(pattern="^[a-f0-9]{64}$")


class PartReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    etag: str = Field(min_length=1, max_length=256)


def upload_router(store, runtime):
    router = APIRouter(prefix="/api/uploads", tags=["上传会话"])

    def sessions():
        return UploadSessions(store, runtime.current())

    def call(fn, *args):
        try:
            return fn(*args)
        except UploadError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @router.post("", status_code=201)
    def create(body: UploadBody):
        return call(sessions().create, body.filename, body.size, body.sha256, body.transport)

    @router.get("/capabilities")
    def capabilities():
        cfg = runtime.current()
        transports = ["api", "s3"] if getattr(store.media, "backend", "local") == "s3" else ["api"]
        return {
            "transports": transports,
            "default_transport": cfg.uploads.default_transport,
            "library_id": store.library_id,
        }

    @router.post("/{session_id}/parts/{number}/sign")
    def sign_part(session_id: str, number: int, x_upload_token: str = Header(default="")):
        return call(sessions().sign_part, session_id, x_upload_token, number)

    @router.post("/{session_id}/parts/{number}/receipt")
    def receipt(session_id: str, number: int, body: PartReceipt, x_upload_token: str = Header(default="")):
        return call(sessions().register_part, session_id, x_upload_token, number, body.etag)

    @router.get("/{session_id}")
    def status(session_id: str, x_upload_token: str = Header(default="")):
        return call(sessions().get, session_id, x_upload_token)

    @router.put("/{session_id}/parts/{number}")
    async def part(session_id: str, number: int, request: Request, x_upload_token: str = Header(default="")):
        service = sessions()
        info = await run_in_threadpool(call, service.get, session_id, x_upload_token)
        if info["state"] != "pending":
            raise HTTPException(410, "上传会话已结束")
        # 分片有上限，始终流式写临时文件；落盘和后端上传均在线程池完成。
        staging = getattr(store.media, "staging", None)
        if staging is not None:
            await run_in_threadpool(staging.reserve, info["part_size"], purpose="上传分片输入")
        try:
            return await receive_part(request, service, info, session_id, x_upload_token, number, staging)
        finally:
            if staging is not None:
                staging.release(info["part_size"])

    async def receive_part(request, service, info, session_id, token, number, staging):
        digest, size = hashlib.sha256(), 0
        with tempfile.TemporaryFile(dir=staging.root if staging else None) as target:
            async for chunk in request.stream():
                size += len(chunk)
                if size > info["part_size"]:
                    raise HTTPException(413, "上传分片过大")
                digest.update(chunk)
                await run_in_threadpool(target.write, chunk)
            target.seek(0)
            return await run_in_threadpool(
                call, service.part, session_id, token, number, target, size, digest.hexdigest()
            )

    @router.post("/{session_id}/complete")
    def complete(session_id: str, x_upload_token: str = Header(default="")):
        return call(sessions().complete, session_id, x_upload_token)

    @router.delete("/{session_id}")
    def abort(session_id: str, x_upload_token: str = Header(default="")):
        return call(sessions().abort, session_id, x_upload_token)

    @router.api_route("/{session_id}/content", methods=["GET", "HEAD"])
    def content(session_id: str, request: Request):
        row = store.one("SELECT * FROM uploaded_assets WHERE id=?", (session_id,))
        if row is None:
            raise HTTPException(404, "找不到已完成的上传素材")
        etag = '"' + row["sha256"] + '"'
        conditional = conditional_response(request, etag)
        if conditional is not None:
            return conditional
        return materialized_response(
            store.media.materialize(row["object_key"]), filename=row["filename"], headers={"ETag": etag}
        )

    return router


def install_reaper(app, store, runtime):

    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application):
        stop = asyncio.Event()

        async def reap():
            while not stop.is_set():
                try:
                    await run_in_threadpool(UploadSessions(store, runtime.current()).reap)
                except Exception:  # 后端故障保留会话，下个周期继续；不能静默停止回收
                    logging.getLogger(__name__).exception("上传会话回收失败，下个周期重试")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=60)

        async with previous(application) as state:
            task = asyncio.create_task(reap())
            try:
                yield state
            finally:
                stop.set()
                await task

    app.router.lifespan_context = lifespan
