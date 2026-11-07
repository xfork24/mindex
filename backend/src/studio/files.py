"""创作产物路由：产物下载、分镜预览、打包与任务删除。"""

from __future__ import annotations

import mimetypes
import zipfile
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import FileResponse
from filelock import FileLock, Timeout

from admin.curation import EntityNotFound, Library
from shared import files
from shared.config import fingerprint
from shared.media_delivery import materialized_response
from shared.shot_media import ShotMedia
from studio import steps as studio_steps
from studio import studio
from studio.artifacts import safe_path

# 动画源码等 HTML 产物：只允许内联脚本与样式、data:/blob: 媒体，禁止任何网络加载；``sandbox allow-scripts``
# 让页面
# 即使被直接打开也运行在不透明源里（拿不到本站凭证与 DOM）。创作页以 sandbox="allow-scripts" 的 iframe 预览。
SANDBOX_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
    "media-src data: blob:; font-src data:; sandbox allow-scripts"
)
SANDBOX_HEADERS = {
    "Content-Security-Policy": SANDBOX_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
ACTIVE_TYPES = ("text/html", "application/xhtml+xml", "image/svg+xml")


def file_routes(router, base, store, mutation_lock):
    """注册产物相关路由；与 studio_router 共用同一个路由对象和任务锁。"""

    def artifact_response(job_id: str, relative: str, download: bool):
        directory = studio.job_dir(base, job_id)
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory.resolve()):
            raise HTTPException(404, "产物文件不存在")
        media_type = (
            "text/vtt"
            if target.suffix == ".vtt"
            else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        try:
            context = store.materialize_media(target.relative_to(store.root.resolve()).as_posix())
            return materialized_response(
                context,
                media_type=media_type,
                filename=f"{job_id}_{target.name}" if download else None,
                headers=SANDBOX_HEADERS if media_type in ACTIVE_TYPES else None,
            )
        except FileNotFoundError:
            raise HTTPException(404, "产物文件不存在") from None

    def artifact_items(job_id):
        root = studio.job_dir(base, job_id)
        state = studio.load_job(base, job_id)
        try:
            ids = {s.id for s in studio.load_storyboard(base, job_id).shots}
        except EntityNotFound:
            ids = set()
        items = []
        remote = base.storage.backend == "s3"
        indexed = {row["path"]: row for row in files.entries(store, f"studio/{job_id}/")} if remote else {}
        paths = [store.root / key for key in sorted(indexed)] if remote else sorted(root.rglob("*"))
        for path in paths:
            relative = path.relative_to(root).as_posix()
            if (
                (not remote and not path.is_file())
                or ".partial" in path.name
                or path.suffix == ".lock"
                or relative.startswith("exports/")
            ):
                continue
            safe_path(root, relative)
            parts = relative.split("/")
            attempt = parts[1].removesuffix(".json") if len(parts) > 1 and parts[0] == "attempts" else None
            active = attempt is None
            if parts[0] in {"audio", "candidates"}:
                step = "tts" if parts[0] == "audio" else "recall"
                active = path.stem in ids and state["steps"][step]["status"] == "complete"
            elif parts[0] == "render":
                active = state["steps"]["render"]["status"] == "complete" and bool(
                    state.get("published_render")
                )
            elif relative == "timeline.json":
                active = state["steps"]["select"]["status"] == "complete"
            items.append(
                {
                    "path": relative,
                    "bytes": indexed[path.relative_to(store.root).as_posix()]["size"]
                    if remote
                    else path.stat().st_size,
                    **(
                        {"sha256": indexed[path.relative_to(store.root).as_posix()]["sha256"]}
                        if remote
                        else {}
                    ),
                    "attempt": attempt,
                    "step": attempt.split("-")[0] if attempt else None,
                    "current": active,
                    "url": f"/api/studio/jobs/{job_id}/artifacts/{quote(relative, safe='/')}",
                }
            )
        return items

    @router.api_route("/jobs/{job_id}/preview/{shot_id}", methods=["GET", "HEAD"])
    def preview(job_id: str, shot_id: str):
        from shared.media import probe
        from studio.render import cut_shot

        state = studio.load_job(base, job_id)
        if state["steps"]["select"]["status"] != "complete":
            raise HTTPException(422, "请先完成选片对齐")
        timeline = studio.load_timeline(base, job_id)
        event = next((e for e in timeline.events if e.shot_id == shot_id), None)
        if event is None:
            raise HTTPException(404, "找不到分镜")
        if not studio_steps.interval_visible(
            store,
            event.video.source_id,
            event.video.start,
            event.video.end,
            overlays=Library(store).overlays(),
        ):
            raise HTTPException(422, "所选区间包含隐藏素材，请重新选片")
        row = store.one("SELECT path FROM sources WHERE id=?", (event.video.source_id,))
        if row is None:
            raise HTTPException(404, "原片不存在")
        with ShotMedia(store).materialized(
            event.video.source_id, event.video.start, event.video.end
        ) as source:
            local_event = event.model_copy(deep=True)
            local_event.video.end -= local_event.video.start
            local_event.video.start = 0
            key = fingerprint(
                {
                    "event": event.model_dump(),
                    "render": base.studio.render.model_dump(),
                    "source_mtime": source.stat().st_mtime_ns,
                }
            )
            path = safe_path(studio.job_dir(base, job_id), f"previews/{key}.mp4")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with FileLock(str(path) + ".lock", timeout=0):
                    try:
                        probe(path)
                    except ValueError:
                        cut_shot(source, local_event, base.studio.render, path)
            except Timeout:
                raise HTTPException(409, "该分镜预览正在生成，请稍候重试") from None
        return FileResponse(path, media_type="video/mp4")

    @router.get("/jobs/{job_id}/artifacts")
    def artifact_list(job_id: str):
        return {"items": artifact_items(job_id)}

    @router.get("/jobs/{job_id}/archive")
    def archive(job_id: str):
        with mutation_lock():
            root = studio.job_dir(base, job_id)
            items = artifact_items(job_id)
            version = fingerprint(
                [
                    item
                    if base.storage.backend == "s3"
                    else {**item, "mtime_ns": safe_path(root, item["path"]).stat().st_mtime_ns}
                    for item in items
                ]
            )
            path = safe_path(root, f"exports/{version}.zip")
            if path.is_file():
                return FileResponse(path, media_type="application/zip", filename=f"{job_id}.zip")
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".partial")
            try:
                with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as bundle:
                    for item in items:
                        relative = f"studio/{job_id}/{item['path']}"
                        with store.materialize_media(relative) as source:
                            bundle.write(source, item["path"])
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
        return FileResponse(path, media_type="application/zip", filename=f"{job_id}.zip")

    @router.get("/jobs/{job_id}/artifacts/{relative:path}")
    def artifact(job_id: str, relative: str, download: bool = False):
        return artifact_response(job_id, relative, download)

    @router.api_route("/jobs/{job_id}/audio/{shot_id}", methods=["GET", "HEAD"])
    def audio(job_id: str, shot_id: str):
        return artifact_response(job_id, f"audio/{shot_id}.wav", False)

    @router.delete("/jobs/{job_id}")
    def remove(job_id: str):
        with mutation_lock():
            studio.delete_job(base, job_id)
        return {"deleted": job_id}
