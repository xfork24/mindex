from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .curation import CurationPatch, Library, Visibility, segment_public
from .storage import Store


def admin_router(store: Store) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["素材管理"])
    library = Library(store)

    @router.get("/sources")
    def sources(
        q: str = Query(default="", max_length=200),
        visibility: Visibility = "visible",
        favorite_only: bool = False,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=48, ge=1, le=100),
    ):
        return library.list_sources(q, visibility, favorite_only, page, page_size)

    @router.get("/sources/{source_id}")
    def source(source_id: str):
        return library.source(source_id)

    @router.api_route("/sources/{source_id}/media", methods=["GET", "HEAD"])
    def source_media(source_id: str):
        row = library.source_row(source_id)
        try:
            return FileResponse(store.resolve_media(row['path']))
        except FileNotFoundError as exc:
            raise HTTPException(404, "原片文件缺失") from exc

    @router.patch("/sources/{source_id}")
    def patch_source(source_id: str, value: CurationPatch):
        return library.patch("source", source_id, value)

    @router.delete("/sources/{source_id}")
    def delete_source(source_id: str):
        from filelock import FileLock, Timeout

        from .purge import PurgeRefused, purge_source

        lock = FileLock(str(store.root / "pipeline.lock"), timeout=0)
        try:
            lock.acquire(blocking=False)
        except Timeout as exc:
            raise HTTPException(409, "离线管线正在运行，请等待完成后再删除素材") from exc
        try:
            try:
                return purge_source(store, source_id)
            except KeyError as exc:
                raise HTTPException(404, "素材不存在") from exc
            except PurgeRefused as exc:
                raise HTTPException(409, str(exc)) from exc
        finally:
            lock.release()

    @router.get("/sources/{source_id}/segments")
    def segments(
        source_id: str,
        kind: Literal["shot", "moment"] = "shot",
        parent_id: str | None = None,
        visibility: Visibility = "visible",
        favorite_only: bool = False,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=48, ge=1, le=100),
    ):
        return library.list_segments(source_id, kind, parent_id, visibility, favorite_only, page, page_size)

    @router.get("/segments/{segment_id}")
    def segment(segment_id: str):
        return segment_public(library.segment_row(segment_id))

    @router.get("/segments/{segment_id}/details")
    def details(segment_id: str):
        from .segment_details import segment_details
        return segment_details(store, segment_id)

    @router.patch("/segments/{segment_id}")
    def patch_segment(segment_id: str, value: CurationPatch):
        return library.patch("segment", segment_id, value)

    def response(segment_id: str, field: str, download: bool = False):
        row = library.segment_row(segment_id)
        try:
            path = store.resolve_media(row[field])
        except FileNotFoundError as exc:
            raise HTTPException(404, "素材文件缺失，请通过 split 命令修复") from exc
        filename = None
        if download:
            name = row["custom_name"] or row["custom_source_name"] or row["original_source_name"]
            name = name.replace("/", "_").replace("\\", "_")
            filename = f"{name}_{row['kind']}_{row['start']:.3f}-{row['end']:.3f}.mp4"
        return FileResponse(
            path, media_type="video/mp4" if field == "path" else "image/jpeg", filename=filename
        )

    @router.api_route("/segments/{segment_id}/media", methods=["GET", "HEAD"])
    def media(segment_id: str, download: bool = False):
        return response(segment_id, "path", download)

    @router.api_route("/segments/{segment_id}/thumbnail", methods=["GET", "HEAD"])
    def thumbnail(segment_id: str):
        return response(segment_id, "thumbnail")

    return router
