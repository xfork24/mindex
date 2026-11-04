from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from mindex.admin.curation import Library
from mindex.search.search import SearchEngine, SimilarUnavailable, public_segment, status
from mindex.shared.shot_media import file_response as FileResponse
from mindex.shared.types import Filters, Granularity, SimilarSearch, TextSearch
from mindex.shared.uploads import save_upload


def search_router(config, runtime, store, embedder=None, *, index_id=None, backends=None):
    """检索与已发布媒体路由。``backends`` 提供媒体后端（S3 时出口走预签名/代理）。"""
    router = APIRouter()
    pins = None
    if backends is not None and backends.media.backend == "s3":
        from mindex.shared.media_delivery import DownloadPins

        pins = DownloadPins(store, backends.library_id)

    @router.get("/api/status")
    def get_status(health: bool = False, summary: bool = False):
        return status(runtime.current(), health=health, summary=summary, embedder=embedder, index_id=index_id)

    @router.post("/api/search/text")
    def text_search(query: TextSearch):
        return SearchEngine(runtime.current(), embedder, index_id=index_id).text(query)

    @router.post("/api/search/similar")
    def similar_search(body: SimilarSearch):
        # 种子不存在 → EntityNotFound（全局 404）；种子无画面渠道 → ValueError（全局 422）；
        # 画面向量缺失属于库内不一致，不是 ValueError，在此映射为 409。
        try:
            return SearchEngine(runtime.current(), embedder, index_id=index_id).similar(
                body.segment_id, body.granularity, body.limit, body.filters
            )
        except SimilarUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/search/image")
    async def image_search(
        file: Annotated[UploadFile, File()],
        granularity: Annotated[Granularity, Form()] = "moment",
        limit: Annotated[int, Form(ge=1, le=50)] = 20,
        filters: Annotated[str, Form()] = "{}",
    ):
        try:
            parsed = Filters.model_validate_json(filters)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "filters 必须是有效的筛选 JSON") from exc
        with tempfile.TemporaryDirectory(prefix="query-image-", dir=config.data_dir) as directory:
            path = Path(directory) / "upload.media"
            await save_upload(file, path, config.search.max_image_upload_mb * 1024 * 1024)
            return await run_in_threadpool(
                SearchEngine(runtime.current(), embedder, index_id=index_id).image,
                path,
                granularity,
                limit,
                parsed,
            )

    @router.post("/api/search/video")
    async def video_search(
        file: Annotated[UploadFile, File()],
        granularity: Annotated[Granularity, Form()] = "moment",
        limit: Annotated[int, Form(ge=1, le=50)] = 20,
        filters: Annotated[str, Form()] = "{}",
    ):
        try:
            parsed = Filters.model_validate_json(filters)
        except (ValidationError, json.JSONDecodeError) as exc:
            raise HTTPException(422, "filters 必须是有效的筛选 JSON") from exc
        with tempfile.TemporaryDirectory(prefix="query-", dir=config.data_dir) as directory:
            path = Path(directory) / "upload.media"
            await save_upload(file, path, config.search.max_upload_mb * 1024 * 1024)
            return await run_in_threadpool(
                SearchEngine(runtime.current(), embedder, index_id=index_id).video,
                path,
                granularity,
                limit,
                parsed,
            )

    def get_snapshot(segment_id: str):
        active = store.index_version(index_id) if index_id else store.active_index()
        if active is None:
            raise HTTPException(404, "没有已发布索引")
        row = store.one(
            "SELECT snapshot FROM index_members WHERE index_id=? AND segment_id=? AND complete=1",
            (active["id"], segment_id),
        )
        if row is None:
            raise HTTPException(404, "找不到片段")
        return json.loads(row["snapshot"])

    def media_response(relative: str, media_type: str, request: Request | None = None):
        if pins is not None:  # S3 形态：先鉴权/索引校验已由路由完成，再签发或代理
            from mindex.shared.media_delivery import deliver_media

            return deliver_media(backends.media, relative, media_type, request,
                                 config=config, pins=pins)
        try:
            return FileResponse(store.resolve_media(relative), media_type=media_type)
        except FileNotFoundError as exc:
            raise HTTPException(404, "素材文件缺失") from exc

    @router.get("/api/segments/{segment_id}")
    def get_segment(segment_id: str):
        library = Library(store)
        return public_segment(library.decorate(get_snapshot(segment_id), *library.overlays()))

    @router.api_route("/api/media/{segment_id}", methods=["GET", "HEAD"])
    def get_media(segment_id: str, request: Request):
        return media_response(get_snapshot(segment_id)["path"], "video/mp4", request)

    @router.api_route("/api/thumbnails/{segment_id}", methods=["GET", "HEAD"])
    def get_thumbnail(segment_id: str, request: Request):
        return media_response(get_snapshot(segment_id)["thumbnail"], "image/jpeg", request)

    @router.api_route("/api/context/{segment_id}", methods=["GET", "HEAD"])
    def get_context(segment_id: str):
        segment = get_snapshot(segment_id)
        from mindex.shared.shot_media import ShotMedia
        path = ShotMedia(store).materialize(segment["source_id"], max(0, segment["start"] - 3),
                                           min(segment["source_duration"], segment["end"] + 3))
        return FileResponse(path, media_type="video/mp4")

    @router.api_route("/api/sources/{source_id}/media", methods=["GET", "HEAD"])
    def source_media(source_id: str):
        active = store.index_version(index_id) if index_id else store.active_index()
        snapshots = store.snapshots(active["id"]) if active else []
        selected = next((s for s in snapshots if s["source_id"] == source_id), None)
        if selected is None:
            raise HTTPException(404, "找不到已发布的素材")
        from mindex.shared.shot_media import ShotMedia
        return FileResponse(ShotMedia(store).materialize(source_id), media_type="video/mp4")

    @router.get("/api/sources/{source_id}/playlist")
    def source_playlist(source_id: str):
        from mindex.shared.shot_media import ShotMedia
        active = store.index_version(index_id) if index_id else store.active_index()
        if not active or not any(s['source_id'] == source_id for s in store.snapshots(active['id'])):
            raise HTTPException(404, "找不到已发布的素材")
        return ShotMedia(store).playlist(source_id)

    @router.get("/api/playlists/{segment_id}")
    def segment_playlist(segment_id: str):
        from mindex.shared.shot_media import ShotMedia
        segment = get_snapshot(segment_id)
        return ShotMedia(store).playlist(segment["source_id"], segment["start"], segment["end"])

    @router.api_route("/api/shots/{shot_id}/media", methods=["GET", "HEAD"])
    def shot_media(shot_id: str, request: Request):
        shot = store.one("SELECT * FROM segments WHERE id=? AND kind='shot'", (shot_id,))
        active = store.index_version(index_id) if index_id else store.active_index()
        if not shot or not active or not any(s['source_id'] == shot['source_id'] for s in store.snapshots(active['id'])):
            raise HTTPException(404, "找不到已发布镜头")
        return media_response(shot['path'], "video/mp4", request)

    return router
