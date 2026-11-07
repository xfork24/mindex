"""媒体 HTTP 出口：本地直读 / S3 预签名 + 代理回退（plans §8 阶段 5）。

- **先鉴权与库访问检查，再签发短期读 URL**：调用方（路由）已完成 web_auth 与
  活动索引/素材校验；本模块只负责交付形态。
- S3 形态：GET 且无 Range → 307 重定向到预签名 URL（浏览器直连对象存储）；
  带 Range/HEAD → 代理物化文件（Starlette FileResponse 原生支持 Range/ETag/
  断点请求），不牺牲 seek 能力。
- 签发/代理都登记**下载保留 pin**（覆盖 URL 生命周期 + 下载宽限），避免预签名后
  内容立刻被 purge/GC 删除；pin 存 PG（多副本共享）或进程内（本地形态用不到，
  保留 API 一致性）。
- URL 是可转交能力：过期前无法靠登出撤销（docs/scaling.md 已注明）；CORS/独立
  可达 endpoint 由部署负责。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from starlette.responses import Response

from shared import files
from shared.object_coordination import ObjectCoordinator

if TYPE_CHECKING:
    from shared.config import Config

log = logging.getLogger(__name__)

__all__ = ["DownloadPins", "deliver_media"]

# 预签名 URL 失效后仍保留的下载宽限（慢速大文件断点续传）。
_PIN_GRACE_SECONDS = 300


class DownloadPins:
    """下载保留记录：key → 截止时间（epoch 秒）。S3 形态写 PG，多副本一致。"""

    def __init__(self, store, library_id: str):
        self.store = store
        self.library_id = library_id
        self._memory: dict[str, float] = {}

    def pin(self, key: str, expires: float) -> None:

        ObjectCoordinator(self.store).download(key, ttl=expires + _PIN_GRACE_SECONDS)
        deadline = time.time() + expires + _PIN_GRACE_SECONDS
        if self.store.pg:
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO download_pins(library_id,key,expires_at) VALUES(?,?,to_timestamp(?))"
                    " ON CONFLICT (library_id,key) DO UPDATE SET"
                    " expires_at=GREATEST(download_pins.expires_at,excluded.expires_at)",
                    (self.library_id, key, deadline),
                )
        else:
            self._memory[key] = deadline

    def pinned(self, key: str) -> bool:
        if self.store.pg:
            row = self.store.one(
                "SELECT 1 AS hit FROM download_pins WHERE library_id=? AND key=?"
                " AND expires_at > clock_timestamp()",
                (self.library_id, key),
            )
            return row is not None
        return self._memory.get(key, 0) > time.time()

    def prune(self) -> int:

        ObjectCoordinator(self.store).prune()
        if self.store.pg:
            with self.store.connect() as db:
                return db.execute(
                    "DELETE FROM download_pins WHERE library_id=? AND expires_at < clock_timestamp()",
                    (self.library_id,),
                ).rowcount
        now = time.time()
        stale = [key for key, deadline in self._memory.items() if deadline <= now]
        for key in stale:
            del self._memory[key]
        return len(stale)


def deliver_media(media, key: str, media_type: str, request, *, config: Config, pins: DownloadPins):
    """按存储后端交付媒体：本地直读；S3 预签名或代理物化。"""
    if getattr(media, "backend", "local") != "s3":
        raise HTTPException(500, "deliver_media 只用于对象存储后端；本地形态走 FileResponse")
    expires = config.storage.s3.presign_expire_seconds

    entry = files.pointer(pins.store, key)
    if entry is not None:
        if entry["size"] < 0:
            raise HTTPException(404, "素材文件缺失")
        key = entry["object_key"]
    try:
        obj = media.stat(key)
    except FileNotFoundError as exc:
        raise HTTPException(404, "素材文件缺失") from exc
    etag = '"' + (obj.etag or obj.sha256 or "") + '"'
    conditional = conditional_response(request, etag)
    if conditional is not None:
        return conditional
    range_header = request.headers.get("range") if request is not None else None
    wants_head = request is not None and request.method == "HEAD"
    pins.pin(key, expires)
    if not wants_head and not range_header:
        url = media.presign_get(key, expires=expires)
        if url:
            # 307 保留方法与 Range 语义；浏览器直连对象存储
            return RedirectResponse(url, status_code=307)
    try:
        return materialized_response(media.materialize(key), media_type=media_type, headers={"ETag": etag})
    except FileNotFoundError as exc:
        raise HTTPException(404, "素材文件缺失") from exc


def materialized_response(context, **kwargs):
    """物化上下文由 ASGI 响应最终释放，包括断流和读取异常。"""
    path = context.__enter__()

    class PinnedResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                context.__exit__(None, None, None)

    try:
        return PinnedResponse(Path(path), **kwargs)
    except BaseException:
        context.__exit__(None, None, None)
        raise


def conditional_response(request, etag, *, headers=None):
    """GET/HEAD 实体条件；Range/If-Range 继续交给 FileResponse。"""

    if request is None:
        return None
    values = {"ETag": etag, **(headers or {})}
    match = request.headers.get("if-match")
    if match and match != "*" and etag not in {x.strip() for x in match.split(",")}:
        return Response(status_code=412, headers=values)
    none = request.headers.get("if-none-match")
    if none and (none == "*" or etag in {x.strip().removeprefix("W/") for x in none.split(",")}):
        return Response(status_code=304, headers=values)
    return None
