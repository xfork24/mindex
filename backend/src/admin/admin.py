from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile

from mindex.admin.curation import CurationPatch, Library, Visibility, segment_public
from mindex.shared.shot_media import file_response as FileResponse
from mindex.shared.storage import Store

# Process-wide cap on concurrent FFmpeg frame decodes for /sources/{id}/frame.
FRAME_SLOTS = threading.BoundedSemaphore(4)


def etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    values = {value.strip().removeprefix("W/") for value in header.split(",")}
    return "*" in values or etag in values


def admin_router(store: Store, runtime=None) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["素材管理"])
    library = Library(store)
    from mindex.admin.work_cache import WorkCache
    work_cache = WorkCache(library)

    @router.post("/sources/upload", status_code=201)
    async def upload_source(file: UploadFile):
        import tempfile
        from pathlib import Path

        from starlette.concurrency import run_in_threadpool

        from mindex.processing.pipeline import Pipeline
        from mindex.shared.media import MediaError
        from mindex.shared.uploads import save_upload

        config = runtime.current().model_copy(deep=True)
        name = (file.filename or "video.mp4").replace("\\", "/").rsplit("/", 1)[-1]
        if name in {"", ".", ".."}:
            await file.close()
            raise HTTPException(422, "无效的文件名")
        def reserve() -> Path:
            directory = store.root / "sources" / ".uploads"
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory, suffix=Path(name).suffix, delete=False) as handle:
                return Path(handle.name)

        path = await run_in_threadpool(reserve)
        try:
            await save_upload(file, path, config.source_upload_max_bytes)
            try:
                return await run_in_threadpool(Pipeline(config).register, path, original_name=name, move=True)
            except (MediaError, ValueError) as exc:
                raise HTTPException(422, str(exc)) from exc
        finally:
            # 取消路径上不能再 await（可能被再次取消而留下临时文件）；单次 unlink 开销可忽略。
            path.unlink(missing_ok=True)

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
    def source(source_id: str, detail: Literal["full", "summary", "work"] = "full"):
        config = runtime.current() if runtime else None
        value = work_cache.get(source_id) if detail == "work" else library.source(source_id, detail=detail)
        return {**value, "enforce_gpu_lock": config.enforce_gpu_lock if config else False}

    @router.post("/sources/{source_id}/work-cache/refresh")
    def refresh_work_cache(source_id: str):
        config = runtime.current() if runtime else None
        return {**work_cache.refresh(source_id), "work_refreshing": False,
                "enforce_gpu_lock": config.enforce_gpu_lock if config else False}

    @router.api_route("/sources/{source_id}/media", methods=["GET", "HEAD"])
    def source_media(source_id: str):
        library.source_row(source_id)
        try:
            from mindex.shared.shot_media import ShotMedia
            return FileResponse(ShotMedia(store).materialize(source_id), media_type="video/mp4")
        except FileNotFoundError as exc:
            raise HTTPException(404, "原片文件缺失") from exc

    @router.get("/sources/{source_id}/playlist")
    def source_playlist(source_id: str):
        from mindex.shared.shot_media import ShotMedia
        library.source_row(source_id)
        return ShotMedia(store).playlist(source_id, prefix="/api/admin/shots")

    @router.get("/sources/{source_id}/frame")
    def source_frame(
        request: Request,
        source_id: str,
        t: float = Query(..., description="素材时间点（秒）"),
        width: int = Query(default=768, ge=16, le=1024),
    ):
        import hashlib
        import json
        import math

        from mindex.shared.media import MediaError
        from mindex.shared.shot_media import ShotMedia

        row = store.one("SELECT media FROM sources WHERE id=?", (source_id,))
        if row is None:
            raise HTTPException(404, "找不到素材")
        duration = float(json.loads(row["media"])["duration"])
        if not math.isfinite(t) or not 0 <= t <= duration:
            raise HTTPException(422, f"时间点必须在 0 到 {duration:.3f} 秒之间")
        media = ShotMedia(store)
        try:
            revision = media.frame_revision(source_id, t)
            etag = '"' + hashlib.sha256(f"{revision}:{t!r}:{width}".encode()).hexdigest()[:20] + '"'
            headers = {"ETag": etag, "Cache-Control": "private, max-age=3600"}
            if etag_matches(request.headers.get("if-none-match"), etag):
                return Response(status_code=304, headers=headers)
            with FRAME_SLOTS:
                content, _ = media.frame_jpeg(source_id, t, width)
        except FileNotFoundError as exc:
            raise HTTPException(404, "镜头文件缺失，请通过 split 命令修复") from exc
        except MediaError as exc:
            raise HTTPException(422, f"无法抽取该时间点的画面：{exc}") from exc
        return Response(content, media_type="image/jpeg", headers=headers)

    @router.get("/sources/{source_id}/report")
    def source_report(source_id: str):
        # 同步端点：FastAPI 在线程池执行，避免逐文件检查阻塞事件循环。
        from mindex.diagnostics.film import source_report as build_report
        from mindex.shared.config import Config

        library.source_row(source_id)
        config = runtime.current() if runtime else Config(data_dir=store.root)
        try:
            return build_report(config, source_id)
        except ValueError as exc:
            raise HTTPException(404, "找不到素材") from exc

    @router.api_route("/shots/{shot_id}/media", methods=["GET", "HEAD"])
    def shot_media(shot_id: str):
        shot = store.one("SELECT * FROM segments WHERE id=? AND kind='shot'", (shot_id,))
        if not shot:
            raise HTTPException(404, "找不到镜头")
        return FileResponse(store.resolve_media(shot['path']), media_type="video/mp4")

    @router.patch("/sources/{source_id}")
    def patch_source(source_id: str, value: CurationPatch):
        return library.patch("source", source_id, value)

    @router.delete("/sources/{source_id}")
    def delete_source(source_id: str):
        from filelock import Timeout

        from mindex.maintenance.purge import PurgeRefused, purge_source
        from mindex.shared.config import Config
        from mindex.shared.processing_lock import ProcessingLock, index_write_lock

        config = runtime.current() if runtime else Config(data_dir=store.root)
        lock = ProcessingLock(config, source_id=source_id, deleting=True)
        try:
            lock.acquire(blocking=False)
        except Timeout as exc:
            raise HTTPException(409, "GPU 独占锁或该素材正在处理，请完成后再删除") from exc
        try:
            try:
                with index_write_lock(store.root):
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
        from mindex.admin.segment_details import segment_details
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

    @router.get("/segments/{segment_id}/playlist")
    def segment_playlist(segment_id: str):
        from mindex.shared.shot_media import ShotMedia
        row = library.segment_row(segment_id)
        return ShotMedia(store).playlist(row['source_id'], row['start'], row['end'], prefix="/api/admin/shots")

    @router.api_route("/segments/{segment_id}/media", methods=["GET", "HEAD"])
    def media(segment_id: str, download: bool = False):
        return response(segment_id, "path", download)

    @router.api_route("/segments/{segment_id}/thumbnail", methods=["GET", "HEAD"])
    def thumbnail(segment_id: str):
        return response(segment_id, "thumbnail")

    return router
