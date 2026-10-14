"""创作 API：任务创建、状态轮询、单步/全自动执行、产物下载。

执行模型：endpoint 同步抢全局 studio.lock（跨进程）+ 进程内单执行器，
步骤体在后台线程运行；前端轮询 GET /jobs/{id} 观察进度。
"""

from __future__ import annotations

import json
import mimetypes
import threading
import zipfile
from contextlib import contextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from filelock import FileLock, Timeout
from pydantic import Field, model_validator

from . import studio, studio_steps
from .adapters import MultimodalEmbedder, Speaker, Storyboarder
from .config import Config, StrictModel, fingerprint
from .curation import EntityNotFound
from .runtime_settings import RuntimeConfig
from .storage import Store
from .studio_artifacts import safe_path


class CreateJobBody(StrictModel):
    text: str
    title: str | None = Field(default=None, max_length=60)
    options: dict | None = None


class ShotBody(StrictModel):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
    text: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=400)
    estimated_seconds: float = Field(gt=0, le=60)


class StoryboardBody(StrictModel):
    title: str | None = Field(default=None, max_length=60)
    shots: list[ShotBody] = Field(min_length=1)


class ManualVideoBody(StrictModel):
    segment_id: str | None = None
    source_id: str | None = None
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def one_choice(self):
        explicit = any(value is not None for value in (self.source_id, self.start, self.end))
        if self.segment_id and explicit:
            raise ValueError("候选片段和人工区间不能同时提供")
        if not self.segment_id and not all(
            value is not None for value in (self.source_id, self.start, self.end)
        ):
            raise ValueError("请选择候选片段，或提供完整人工区间")
        return self


class RunStepBody(StrictModel):
    mode: Literal["resume", "force"] = "resume"
    shot_ids: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def unique_shots(self):
        if self.shot_ids is not None and (
            self.mode != "force" or len(self.shot_ids) != len(set(self.shot_ids))
        ):
            raise ValueError("指定分镜必须使用 force 模式，且分镜不能重复")
        return self


class EventBody(StrictModel):
    shot_id: str
    video: ManualVideoBody


class TimelineBody(StrictModel):
    events: list[EventBody] = Field(min_length=1)
    audio_mode: Literal["tts_only", "ducking"] | None = None
    burn: bool | None = None


def studio_router(
    config: Config | RuntimeConfig,
    store: Store,
    *,
    embedder: MultimodalEmbedder | None = None,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/studio", tags=["创作"])
    guard = threading.Lock()  # 进程内单执行器；跨进程由 studio.lock 兜底
    executor: dict[str, threading.Thread | None] = {"thread": None}

    base = config if isinstance(config, Config) else config.base  # 路径与渲染参数走基础配置

    def current() -> Config:
        """传入 RuntimeConfig 时按最新覆盖层解析；覆盖层不触碰路径，路径用法保持稳定。"""
        return config.current() if isinstance(config, RuntimeConfig) else config

    def acquire_lock() -> FileLock:
        try:
            lock = FileLock(str(studio.lock_path(base)), timeout=0, thread_local=False)
            lock.acquire()
            return lock
        except Timeout:
            raise HTTPException(409, "已有创作步骤在执行，请稍候再试") from None

    @contextmanager
    def mutation_lock():
        with guard:
            lock = acquire_lock()
            try:
                yield
            finally:
                lock.release()

    def launch(job_id: str, requested_step: str | None, body: RunStepBody):
        with guard:
            if executor["thread"] is not None and executor["thread"].is_alive():
                raise HTTPException(409, "已有创作步骤在执行，请稍候再试")
            file_lock = acquire_lock()
            cfg = current()  # 一次任务运行绑定同一份生效配置；运行中改设置下次生效
            try:
                state = studio.reconcile(cfg, job_id)
                if requested_step:
                    studio.check_step(state, requested_step)
                    steps = [requested_step]
                else:
                    steps = [step for step in studio.STEPS if state["steps"][step]["status"] != "complete"]
                    if not steps:
                        raise HTTPException(422, "所有步骤均已完成，可重新生成单步或编辑产物后继续")
                if body.shot_ids is not None:
                    if requested_step not in {"recall", "tts", "render"}:
                        raise HTTPException(422, "只有召回、配音、渲染支持指定分镜")
                    ids = {s.id for s in studio.load_storyboard(cfg, job_id).shots}
                    if not set(body.shot_ids) <= ids:
                        raise HTTPException(422, "指定分镜不存在于当前任务")
                studio.begin_step(cfg, job_id, steps[0])
                response = studio.set_execution(cfg, job_id, "running", steps[0], steps[1:])
            except Exception:
                file_lock.release()
                raise

            def worker():
                status = "complete"
                try:
                    for index, step in enumerate(steps):
                        if index:
                            studio.begin_step(cfg, job_id, step)
                            studio.set_execution(cfg, job_id, "running", step, steps[index + 1 :])
                        studio_steps.run_step(
                            cfg,
                            job_id,
                            step,
                            storyboarder=storyboarder,
                            speaker=speaker,
                            embedder=embedder,
                            mode=body.mode,
                            shot_ids=body.shot_ids,
                            started=True,
                        )
                except Exception as exc:  # noqa: BLE001 — 所有后台失败必须记录终态
                    status = "failed"
                    fresh = studio.load_job(cfg, job_id)
                    fresh["execution"]["error"] = str(exc)
                    studio.save(studio.job_path(cfg, job_id), fresh)
                finally:
                    try:
                        studio.set_execution(cfg, job_id, status, None, [])
                    finally:
                        file_lock.release()

            thread = threading.Thread(target=worker, daemon=True, name=f"studio-{job_id}")
            executor["thread"] = thread
            try:
                thread.start()
            except Exception as exc:
                studio.fail_step(cfg, job_id, steps[0], exc)
                studio.set_execution(cfg, job_id, "failed", None, [])
                file_lock.release()
                raise
            return {
                "steps": {name: info["status"] for name, info in response["steps"].items()},
                "execution": response["execution"],
            }

    def shot_summary(shot: dict) -> dict:
        """聚合单镜的分镜/候选/配音/选片信息，供轮询端点一次带全。"""
        summary = {**shot, "candidates": None, "audio": None, "selection": None}
        candidate_file = studio.candidates_dir(base, shot["job_id"]) / f"{shot['id']}.json"
        if candidate_file.is_file():
            document = json.loads(candidate_file.read_text())
            summary["candidates"] = {
                "status": document.get("status"),
                "count": len(document.get("results", [])),
                "top": document.get("results")[0] if document.get("results") else None,
                "pool": document.get("results", []),
            }
        manifest_file = studio.job_dir(base, shot["job_id"]) / "audio.json"
        if manifest_file.is_file():
            manifest = json.loads(manifest_file.read_text()).get("shots", {})
            if shot["id"] in manifest:
                summary["audio"] = {
                    "duration": manifest[shot["id"]]["duration"],
                    "valid": studio.load_job(base, shot["job_id"])["steps"]["tts"]["status"] == "complete",
                    "url": f"/api/studio/jobs/{shot['job_id']}/audio/{shot['id']}?v={manifest[shot['id']].get('sha256', 'legacy')}",
                }
        timeline_file = studio.timeline_path(base, shot["job_id"])
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
                        "preview_url": f"/api/studio/jobs/{shot['job_id']}/preview/{shot['id']}?v={fingerprint(event)}",
                    }
                    for event in events
                    if event.get("shot_id") == shot["id"]
                ),
                None,
            )
        return summary

    def job_detail(job_id: str) -> dict:
        state = studio.load_job(base, job_id)
        detail = {"job": state, "storyboard": None, "shots": [], "timeline": None, "render": None}
        try:
            storyboard = studio.load_storyboard(base, job_id)
        except EntityNotFound:
            return detail
        detail["storyboard"] = {
            "coverage_warning": storyboard.coverage_warning,
            "shots": [shot.model_dump() for shot in storyboard.shots],
        }
        detail["shots"] = [shot_summary({**shot.model_dump(), "job_id": job_id}) for shot in storyboard.shots]
        try:
            timeline = studio.load_timeline(base, job_id)
            detail["timeline"] = {
                "mode": timeline.mode,
                "audio_mode": timeline.audio_mode,
                "burn": timeline.burn,
                "total_seconds": timeline.total_seconds,
                "events": [event.model_dump() for event in timeline.events],
            }
        except EntityNotFound:
            pass
        final = studio.render_dir(base, job_id) / "final.mp4"
        if final.is_file():
            published = state.get("published_render", {})
            version = published.get("version", "legacy")
            files = published.get("files", {})

            def output_url(logical):
                relative = quote(files.get(logical, logical), safe="/")
                return f"/api/studio/jobs/{job_id}/artifacts/{relative}?v={version}"

            detail["render"] = {
                "ready": state["steps"]["render"]["status"] == "complete" and bool(published),
                "legacy": not bool(published),
                "version": version,
                "final_url": output_url("render/final.mp4"),
                "srt_url": output_url("render/final.srt"),
                "vtt_url": output_url("render/final.vtt"),
                "burn": detail["timeline"]["burn"] if detail["timeline"] else True,
            }
        return detail

    @router.post("/jobs", status_code=201)
    def create_job(body: CreateJobBody):
        try:
            return studio.create_job(current(), body.text, body.title, body.options)
        except studio.StudioError:
            raise
        except ValueError as exc:
            raise HTTPException(422, f"任务选项无效：{exc}") from exc

    @router.get("/jobs")
    def jobs():
        studio.recover_stale(base)
        return {"items": studio.list_jobs(base)}

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        studio.recover_stale(base)
        return job_detail(job_id)

    @router.post("/jobs/{job_id}/steps/{step}", status_code=202)
    def run_step(job_id: str, step: str, body: RunStepBody | None = None):
        return launch(job_id, step, body or RunStepBody())

    @router.post("/jobs/{job_id}/run", status_code=202)
    def run_all(job_id: str):
        return launch(job_id, None, RunStepBody())

    @router.patch("/jobs/{job_id}/storyboard")
    def patch_storyboard(job_id: str, body: StoryboardBody):
        with mutation_lock():
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
            if len(shots) > current().studio.max_shots:
                raise HTTPException(422, f"分镜数量 {len(shots)} 超过上限 {current().studio.max_shots}")
            try:
                studio.save_storyboard(base, job_id, studio.Storyboard(job_id=job_id, shots=shots))
            except studio.StudioError as exc:
                raise HTTPException(422, str(exc)) from exc
            if body.title is not None:
                state = studio.load_job(base, job_id)
                state["title"] = body.title.strip()[:60] or state["title"]
                studio.save(studio.job_path(base, job_id), state)
        return job_detail(job_id)

    @router.patch("/jobs/{job_id}/timeline")
    def patch_timeline(job_id: str, body: TimelineBody):
        with mutation_lock():
            patches = [
                {"shot_id": event.shot_id, "video": event.video.model_dump(exclude_none=True)}
                for event in body.events
            ]
            try:
                studio_steps.apply_timeline_patch(
                    current(), job_id, patches, audio_mode=body.audio_mode, burn=body.burn
                )
            except studio.StudioError as exc:
                raise HTTPException(422, str(exc)) from exc
        return job_detail(job_id)

    def artifact_response(job_id: str, relative: str, download: bool):
        directory = studio.job_dir(base, job_id)
        target = (directory / relative).resolve()
        if not target.is_relative_to(directory.resolve()) or not target.is_file():
            raise HTTPException(404, "产物文件不存在")
        media_type = (
            "text/vtt"
            if target.suffix == ".vtt"
            else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        )
        return FileResponse(
            target, media_type=media_type, filename=f"{job_id}_{target.name}" if download else None
        )

    def artifact_items(job_id):
        root = studio.job_dir(base, job_id)
        state = studio.load_job(base, job_id)
        try:
            ids = {s.id for s in studio.load_storyboard(base, job_id).shots}
        except EntityNotFound:
            ids = set()
        items = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if (
                not path.is_file()
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
                    "bytes": path.stat().st_size,
                    "attempt": attempt,
                    "step": attempt.split("-")[0] if attempt else None,
                    "current": active,
                    "url": f"/api/studio/jobs/{job_id}/artifacts/{quote(relative, safe='/')}",
                }
            )
        return items

    @router.api_route("/jobs/{job_id}/preview/{shot_id}", methods=["GET", "HEAD"])
    def preview(job_id: str, shot_id: str):
        from .media import probe
        from .studio_render import cut_shot

        state = studio.load_job(base, job_id)
        if state["steps"]["select"]["status"] != "complete":
            raise HTTPException(422, "请先完成选片对齐")
        timeline = studio.load_timeline(base, job_id)
        event = next((e for e in timeline.events if e.shot_id == shot_id), None)
        if event is None:
            raise HTTPException(404, "找不到分镜")
        if not studio_steps.interval_visible(
            store, event.video.source_id, event.video.start, event.video.end
        ):
            raise HTTPException(422, "所选区间包含隐藏素材，请重新选片")
        row = store.one("SELECT path FROM sources WHERE id=?", (event.video.source_id,))
        if row is None:
            raise HTTPException(404, "原片不存在")
        source = store.resolve_media(row["path"])
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
                    cut_shot(source, event, base.studio.render, path)
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
                [{**item, "mtime_ns": safe_path(root, item["path"]).stat().st_mtime_ns} for item in items]
            )
            path = safe_path(root, f"exports/{version}.zip")
            if path.is_file():
                return FileResponse(path, media_type="application/zip", filename=f"{job_id}.zip")
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".partial")
            try:
                with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as bundle:
                    for item in items:
                        bundle.write(safe_path(root, item["path"]), item["path"])
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

    return router
