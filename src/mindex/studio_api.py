"""创作 API：任务创建、状态轮询、单步/全自动执行、产物下载。

执行模型：endpoint 同步抢全局 studio.lock（跨进程）+ 进程内单执行器，
步骤体在后台线程运行；前端轮询 GET /jobs/{id} 观察进度。
"""

from __future__ import annotations

import json
import mimetypes
import threading
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field

from . import studio, studio_steps
from .adapters import MultimodalEmbedder, Speaker, Storyboarder
from .config import Config
from .curation import EntityNotFound
from .storage import Store


class CreateJobBody(BaseModel):
    text: str
    title: str | None = Field(default=None, max_length=60)
    options: dict | None = None


class ShotBody(BaseModel):
    id: str | None = Field(default=None, max_length=40)
    text: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=400)
    estimated_seconds: float = Field(gt=0, le=60)


class StoryboardBody(BaseModel):
    title: str | None = Field(default=None, max_length=60)
    shots: list[ShotBody] = Field(min_length=1)


class ManualVideoBody(BaseModel):
    segment_id: str | None = None
    source_id: str | None = None
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)


class EventBody(BaseModel):
    shot_id: str
    video: ManualVideoBody


class TimelineBody(BaseModel):
    events: list[EventBody] = Field(min_length=1)
    audio_mode: Literal["tts_only", "ducking"] | None = None
    burn: bool | None = None


def studio_router(
    config: Config,
    store: Store,
    *,
    embedder: MultimodalEmbedder | None = None,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/studio", tags=["创作"])
    guard = threading.Lock()  # 进程内单执行器；跨进程由 studio.lock 兜底
    executor: dict[str, threading.Thread | None] = {"thread": None}

    def acquire_lock() -> FileLock:
        try:
            lock = FileLock(str(studio.lock_path(config)), timeout=0)
            lock.acquire()
            return lock
        except Timeout:
            raise HTTPException(409, "已有创作步骤在执行，请稍候再试") from None

    def launch(job_id: str, steps: list[str]):
        with guard:
            if executor["thread"] is not None and executor["thread"].is_alive():
                raise HTTPException(409, "已有创作步骤在执行，请稍候再试")
            file_lock = acquire_lock()

            def worker():
                try:
                    for step in steps:
                        try:
                            studio_steps.run_step(
                                config,
                                job_id,
                                step,
                                storyboarder=storyboarder,
                                speaker=speaker,
                                embedder=embedder,
                            )
                        except Exception:  # noqa: BLE001 — 任意失败都已记录到步骤状态
                            break  # 全自动连跑遇失败即停
                finally:
                    file_lock.release()

            thread = threading.Thread(target=worker, daemon=True, name=f"studio-{job_id}")
            executor["thread"] = thread
            thread.start()

    def shot_summary(shot: dict) -> dict:
        """聚合单镜的分镜/候选/配音/选片信息，供轮询端点一次带全。"""
        summary = {**shot, "candidates": None, "audio": None, "selection": None}
        candidate_file = studio.candidates_dir(config, shot["job_id"]) / f"{shot['id']}.json"
        if candidate_file.is_file():
            document = json.loads(candidate_file.read_text())
            summary["candidates"] = {
                "status": document.get("status"),
                "count": len(document.get("results", [])),
                "top": document.get("results")[0] if document.get("results") else None,
            }
        manifest_file = studio.job_dir(config, shot["job_id"]) / "audio.json"
        if manifest_file.is_file():
            manifest = json.loads(manifest_file.read_text()).get("shots", {})
            if shot["id"] in manifest:
                summary["audio"] = {
                    "duration": manifest[shot["id"]]["duration"],
                    "url": f"/api/studio/jobs/{shot['job_id']}/audio/{shot['id']}",
                }
        timeline_file = studio.timeline_path(config, shot["job_id"])
        if timeline_file.is_file():
            events = json.loads(timeline_file.read_text()).get("events", [])
            summary["selection"] = next(
                (
                    {
                        "source_name": event["video"].get("source_name", ""),
                        "start": event["video"]["start"],
                        "end": event["video"]["end"],
                        "alignment": event["video"].get("alignment"),
                        "atempo": event["audio"].get("atempo"),
                    }
                    for event in events
                    if event.get("shot_id") == shot["id"]
                ),
                None,
            )
        return summary

    def job_detail(job_id: str) -> dict:
        state = studio.load_job(config, job_id)
        detail = {"job": state, "storyboard": None, "shots": [], "timeline": None, "render": None}
        try:
            storyboard = studio.load_storyboard(config, job_id)
        except EntityNotFound:
            return detail
        detail["storyboard"] = {
            "coverage_warning": storyboard.coverage_warning,
            "shots": [shot.model_dump() for shot in storyboard.shots],
        }
        detail["shots"] = [shot_summary({**shot.model_dump(), "job_id": job_id}) for shot in storyboard.shots]
        try:
            timeline = studio.load_timeline(config, job_id)
            detail["timeline"] = {
                "mode": timeline.mode,
                "audio_mode": timeline.audio_mode,
                "burn": timeline.burn,
                "total_seconds": timeline.total_seconds,
                "events": [event.model_dump() for event in timeline.events],
            }
        except EntityNotFound:
            pass
        final = studio.render_dir(config, job_id) / "final.mp4"
        if final.is_file():
            detail["render"] = {
                "ready": state["steps"]["render"]["status"] == "complete",
                "final_url": f"/api/studio/jobs/{job_id}/artifacts/render/final.mp4",
                "srt_url": f"/api/studio/jobs/{job_id}/artifacts/render/final.srt",
            }
        return detail

    @router.post("/jobs", status_code=201)
    def create_job(body: CreateJobBody):
        try:
            return studio.create_job(config, body.text, body.title, body.options)
        except studio.StudioError:
            raise
        except ValueError as exc:
            raise HTTPException(422, f"任务选项无效：{exc}") from exc

    @router.get("/jobs")
    def jobs():
        studio.recover_stale(config)
        return {"items": studio.list_jobs(config)}

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        studio.recover_stale(config)
        return job_detail(job_id)

    @router.post("/jobs/{job_id}/steps/{step}", status_code=202)
    def run_step(job_id: str, step: str):
        state = studio.load_job(config, job_id)  # 404
        studio.check_step(state, step)  # 422：未知步骤或依赖未完成
        launch(job_id, [step])
        return {"steps": {name: info["status"] for name, info in state["steps"].items()}}

    @router.post("/jobs/{job_id}/run", status_code=202)
    def run_all(job_id: str):
        state = studio.load_job(config, job_id)
        remaining = [step for step in studio.STEPS if state["steps"][step]["status"] != "complete"]
        if not remaining:
            raise HTTPException(422, "所有步骤均已完成，可重跑单步或编辑产物后继续")
        launch(job_id, remaining)
        return {"steps": {name: info["status"] for name, info in state["steps"].items()}}

    @router.patch("/jobs/{job_id}/storyboard")
    def patch_storyboard(job_id: str, body: StoryboardBody):
        lock = acquire_lock()  # 执行中不可编辑
        try:
            with guard:
                shots, seen = [], set()
                for index, item in enumerate(body.shots, 1):
                    shot_id = (item.id or f"shot-{index:02d}").strip()
                    if shot_id in seen:
                        raise HTTPException(422, f"分镜 id 重复：{shot_id}")
                    seen.add(shot_id)
                    shots.append(
                        studio.Shot(
                            id=shot_id,
                            text=item.text.strip(),
                            query=item.query.strip(),
                            estimated_seconds=item.estimated_seconds,
                        )
                    )
                if len(shots) > config.studio.max_shots:
                    raise HTTPException(422, f"分镜数量 {len(shots)} 超过上限 {config.studio.max_shots}")
                try:
                    studio.save_storyboard(config, job_id, studio.Storyboard(job_id=job_id, shots=shots))
                except studio.StudioError as exc:
                    raise HTTPException(422, str(exc)) from exc
                if body.title is not None:
                    state = studio.load_job(config, job_id)
                    state["title"] = body.title.strip()[:60] or state["title"]
                    studio.save(studio.job_path(config, job_id), state)
        finally:
            lock.release()
        return job_detail(job_id)

    @router.patch("/jobs/{job_id}/timeline")
    def patch_timeline(job_id: str, body: TimelineBody):
        lock = acquire_lock()  # 执行中不可编辑
        try:
            with guard:
                patches = [
                    {"shot_id": event.shot_id, "video": event.video.model_dump(exclude_none=True)}
                    for event in body.events
                ]
                try:
                    studio_steps.apply_timeline_patch(
                        config, job_id, patches, audio_mode=body.audio_mode, burn=body.burn
                    )
                except studio.StudioError as exc:
                    raise HTTPException(422, str(exc)) from exc
        finally:
            lock.release()
        return job_detail(job_id)

    def artifact_response(job_id: str, relative: str, download: bool):
        directory = studio.job_dir(config, job_id)
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory.resolve()) or not target.is_file():
            raise HTTPException(404, "产物文件不存在")
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(
            target, media_type=media_type, filename=f"{job_id}_{target.name}" if download else None
        )

    @router.get("/jobs/{job_id}/artifacts/{relative:path}")
    def artifact(job_id: str, relative: str, download: bool = False):
        return artifact_response(job_id, relative, download)

    @router.api_route("/jobs/{job_id}/audio/{shot_id}", methods=["GET", "HEAD"])
    def audio(job_id: str, shot_id: str):
        return artifact_response(job_id, f"audio/{shot_id}.wav", False)

    @router.delete("/jobs/{job_id}")
    def remove(job_id: str):
        lock = acquire_lock()
        try:
            with guard:
                studio.delete_job(config, job_id)
        finally:
            lock.release()
        return {"deleted": job_id}

    return router
