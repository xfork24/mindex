from __future__ import annotations

import json
import mimetypes
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .adapters import ModelError, MultimodalEmbedder, Speaker, Storyboarder
from .admin import admin_router
from .config import Config, load_config
from .curation import EntityNotFound, Library
from .media import cut
from .search import SearchEngine, public_segment, status
from .storage import Store
from .studio_api import studio_router
from .types import Filters, Granularity, TextSearch


async def save_upload(upload: UploadFile, path: Path, max_bytes: int):
    try:
        size = 0
        with path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, "上传文件过大")
                handle.write(chunk)
        if not size:
            raise HTTPException(422, "上传文件为空")
    finally:
        await upload.close()


def create_app(
    config: Config | None = None,
    embedder: MultimodalEmbedder | None = None,
    *,
    index_id: str | None = None,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
) -> FastAPI:
    config = config or load_config()
    store = Store(config.data_dir)
    app = FastAPI(title="Mindex 视频素材检索", version="0.1.0")
    if index_id and store.index_version(index_id)["status"] != "ready":
        raise ValueError("固定预览只允许就绪索引")
    app.include_router(admin_router(store))
    app.include_router(studio_router(config, store, embedder=embedder, storyboarder=storyboarder, speaker=speaker))

    @app.exception_handler(EntityNotFound)
    async def entity_not_found(_, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_page():
        return FileResponse(Path(__file__).parent / "static" / "admin.html", media_type="text/html")

    @app.get("/studio", include_in_schema=False)
    @app.get("/studio/", include_in_schema=False)
    def studio_page():
        return FileResponse(Path(__file__).parent / "static" / "studio.html", media_type="text/html")

    @app.exception_handler(ModelError)
    async def model_error(_, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error(_, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/api/status")
    def get_status(health: bool = False):
        return status(config, health=health, embedder=embedder, index_id=index_id)

    @app.post("/api/search/text")
    def text_search(query: TextSearch):
        return SearchEngine(config, embedder, index_id=index_id).text(query)

    @app.post("/api/search/image")
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
                SearchEngine(config, embedder, index_id=index_id).image, path, granularity, limit, parsed
            )

    @app.post("/api/search/video")
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
                SearchEngine(config, embedder, index_id=index_id).video, path, granularity, limit, parsed
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

    def media_response(relative: str, media_type: str):
        try:
            return FileResponse(store.resolve_media(relative), media_type=media_type)
        except FileNotFoundError as exc:
            raise HTTPException(404, "素材文件缺失") from exc

    @app.get("/api/segments/{segment_id}")
    def get_segment(segment_id: str):
        library = Library(store)
        return public_segment(library.decorate(get_snapshot(segment_id), *library.overlays()))

    @app.api_route("/api/media/{segment_id}", methods=["GET", "HEAD"])
    def get_media(segment_id: str):
        return media_response(get_snapshot(segment_id)["path"], "video/mp4")

    @app.api_route("/api/thumbnails/{segment_id}", methods=["GET", "HEAD"])
    def get_thumbnail(segment_id: str):
        return media_response(get_snapshot(segment_id)["thumbnail"], "image/jpeg")

    @app.api_route("/api/context/{segment_id}", methods=["GET", "HEAD"])
    def get_context(segment_id: str):
        segment = get_snapshot(segment_id)
        relative = f"context/{segment_id}.mp4"
        destination = store.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(destination) + ".lock", timeout=120):
            cut(
                store.resolve_media(segment["source_path"]),
                max(0, segment["start"] - 3),
                min(segment["source_duration"], segment["end"] + 3),
                destination,
            )
        return media_response(relative, "video/mp4")

    @app.api_route("/api/sources/{source_id}/media", methods=["GET", "HEAD"])
    def source_media(source_id: str):
        active = store.index_version(index_id) if index_id else store.active_index()
        snapshots = store.snapshots(active["id"]) if active else []
        selected = next((s for s in snapshots if s["source_id"] == source_id), None)
        if selected is None:
            raise HTTPException(404, "找不到已发布的素材")
        media_type = mimetypes.guess_type(selected["source_path"])[0] or "application/octet-stream"
        return media_response(selected["source_path"], media_type)

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="web")
    return app
