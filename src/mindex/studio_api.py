"""创作 API：任务创建、状态轮询、单步/全自动执行、产物下载。

执行模型：endpoint 同步抢全局 studio.lock（跨进程）+ 进程内单执行器，
步骤体在后台线程运行；前端轮询 GET /jobs/{id} 观察进度。
"""

from __future__ import annotations

import copy
import json
import mimetypes
import threading
import zipfile
from contextlib import contextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from filelock import FileLock, Timeout
from pydantic import Field, model_validator

from . import studio, studio_steps
from . import studio_workspace as workspace
from .adapters import MultimodalEmbedder, Speaker, Storyboarder
from .config import Config, StrictModel, fingerprint
from .curation import EntityNotFound, Library
from .runtime_settings import RuntimeConfig
from .storage import Store
from .studio_artifacts import safe_path
from .studio_logs import ExecutionLog, read_events


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


class TitleBody(StrictModel):
    title: str = Field(min_length=1, max_length=60)


class PlanBody(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    copy_from: str | None = None
    from_final: str | None = None


class PlanPatchBody(StrictModel):
    revision: str
    name: str | None = Field(default=None, min_length=1, max_length=60)
    options: workspace.PlanOptions | None = None
    shots: list[ShotBody] | None = Field(default=None, min_length=1)


class VersionChoiceBody(StrictModel):
    revision: str
    version_id: str
    stage: Literal["content", "recall", "tts", "select", "render", "compose"]
    shot_id: str | None = None


class BuildBody(StrictModel):
    token: str
    force: bool = False


class NodeBody(StrictModel):
    revision: str
    inputs: dict[str, str | None]
    stage: Literal["split", "recall", "tts", "select", "render"]
    shot_id: str | None = None
    force: bool = False


class ColumnBody(StrictModel):
    revision: str
    stage: Literal["split", "recall", "tts", "select", "render"]
    inputs_by_shot: dict[str, dict[str, str | None]]


class RangeBody(StrictModel):
    revision: str
    shot_id: str
    source_id: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    segment_ids: list[str] | None = Field(default=None, min_length=1)
    preview_token: str | None = None


class AnchorBody(StrictModel):
    revision: str
    shot_id: str
    mode: Literal["auto", "manual"]
    recall_version: str | None = None
    candidate_id: str | None = None


class WindowBody(StrictModel):
    revision: str
    shot_id: str
    context_token: str
    anchor_ref: dict[str, str]
    tts_version: str
    start: float = Field(ge=0, allow_inf_nan=False)
    preview_token: str | None = None
    render_preview: bool = True


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
                reject_legacy_mutation(job_id)
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

    # Versioned workspace endpoints share the same cross-process execution lock.
    def reject_legacy_mutation(job_id):
        if workspace.path(base, job_id).is_file():
            raise HTTPException(409, "任务已接入版本工作台，请通过方案 API 编辑或执行")

    def read_workspace(job_id):
        if not workspace.path(base, job_id).is_file():
            with mutation_lock():
                return workspace.load(current(), job_id)
        w = workspace.load(base, job_id)
        if any(b["status"] == "running" for b in w["builds"]):
            try:
                with mutation_lock():
                    w = workspace.load(base, job_id)
                    workspace.recover(base, job_id, w)
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
        return w

    @router.get("/jobs/{job_id}/workspace")
    def get_workspace(job_id: str):
        return workspace.detail(current(), job_id, read_workspace(job_id))

    @router.patch("/jobs/{job_id}/title")
    def patch_title(job_id: str, body: TitleBody):
        with mutation_lock():
            state = studio.load_job(base, job_id)
            if not body.title.strip():
                raise studio.StudioError("标题不能为空")
            state.update(title=body.title.strip(), updated_at=studio.now())
            studio.save(studio.job_path(base, job_id), state)
        return state

    @router.post("/jobs/{job_id}/plans", status_code=201)
    def create_plan(job_id: str, body: PlanBody):
        import copy
        from uuid import uuid4

        with mutation_lock():
            w = workspace.load(current(), job_id)
            if body.copy_from and body.from_final:
                raise studio.StudioError("复制方案与恢复成片不能同时指定")
            if body.from_final:
                p = workspace.restore(w, body.from_final, body.name)
            elif body.copy_from:
                p = copy.deepcopy(workspace.plan(w, body.copy_from))
                p.update(id=uuid4().hex[:12], name=body.name, created_at=studio.now())
            else:
                p = workspace.new_plan(
                    body.name,
                    {
                        **copy.deepcopy(workspace.plan(w, w["active_plan"])["options"]),
                        "voice_mode": "inherit",
                        "voice": None,
                    },
                )
            w["plans"][p["id"]] = p
            w["active_plan"] = p["id"]
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.post("/jobs/{job_id}/plans/{plan_id}/activate")
    def activate_plan(job_id: str, plan_id: str):
        with mutation_lock():
            w = workspace.load(current(), job_id)
            workspace.plan(w, plan_id)
            w["active_plan"] = plan_id
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.patch("/jobs/{job_id}/plans/{plan_id}")
    def patch_plan(job_id: str, plan_id: str, body: PlanPatchBody):
        from uuid import uuid4

        with mutation_lock():
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            if body.name is not None:
                p["name"] = body.name
            if body.options:
                p["options"] = body.options.model_dump()
            if body.shots is not None:
                shots = [
                    studio.Shot(**{**s.model_dump(), "id": s.id or f"shot-{uuid4().hex[:10]}"})
                    for s in body.shots
                ]
                workspace.set_board(current(), job_id, w, p, shots)
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.get("/jobs/{job_id}/versions")
    def get_versions(job_id: str, stage: str | None = None, shot_id: str | None = None):
        w = read_workspace(job_id)
        return {
            "items": [
                v
                for v in workspace.detail(current(), job_id, w)["versions"].values()
                if (stage is None or v["stage"] == stage) and (shot_id is None or v["shot_id"] == shot_id)
            ]
        }

    @router.get("/jobs/{job_id}/versions/{version_id}")
    def get_version(job_id: str, version_id: str):
        w = read_workspace(job_id)
        return workspace.version(workspace.detail(current(), job_id, w), version_id)

    @router.get("/jobs/{job_id}/versions/{version_id}/dependencies")
    def get_dependencies(job_id: str, version_id: str):
        w = read_workspace(job_id)
        result = {}

        def visit(vid):
            if vid in result:
                return
            item = workspace.version(w, vid)
            result[vid] = item
            for dep in item["inputs"].values():
                if dep:
                    visit(dep)

        visit(version_id)
        return {"root": version_id, "nodes": result}

    @router.post("/jobs/{job_id}/plans/{plan_id}/select-version")
    def select_version(job_id: str, plan_id: str, body: VersionChoiceBody):
        with mutation_lock():
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            if not workspace.valid_files(base, job_id, workspace.version(w, body.version_id)):
                raise studio.StudioError("版本文件缺失或损坏，不能选为构建输入")
            workspace.choose(w, p, body.stage, body.shot_id, body.version_id)
            if body.stage == "content":
                shots = [
                    studio.Shot.model_validate(workspace.version(w, p["shots"][sid]["content"])["data"])
                    for sid in workspace.ordered_shots(w, p)
                ]
                workspace.set_board(current(), job_id, w, p, shots)
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.get("/jobs/{job_id}/plans/{plan_id}/preflight")
    def build_preflight(job_id: str, plan_id: str, force: bool = False):
        w = read_workspace(job_id)
        return workspace.preflight(current(), job_id, w, workspace.plan(w, plan_id), force=force)

    def launch_build(job_id, plan_id, *, body=None, node=None, column=None):
        from uuid import uuid4

        with guard:
            file_lock = acquire_lock()
            cfg = current()
            try:
                w = workspace.load(cfg, job_id)
                workspace.recover(cfg, job_id, w)
                p = workspace.plan(w, plan_id)
                if column:
                    workspace.require_revision(p, column.revision)
                    nodes = workspace.column_nodes(cfg, job_id, w, p, column.stage, column.inputs_by_shot)
                    force = True
                elif node:
                    workspace.require_revision(p, node.revision)
                    spec = workspace.specification(cfg, job_id, w, p, node.stage, node.shot_id)
                    if node.inputs != spec["inputs"]:
                        raise studio.StudioError("输入版本已变化，请刷新后重试")
                    if node.stage == "split" and node.shot_id is not None:
                        raise studio.StudioError("拆分镜不能指定单镜")
                    nodes = [
                        {
                            "key": f"{node.stage}:{node.shot_id}" if node.shot_id else node.stage,
                            "stage": node.stage,
                            "shot_id": node.shot_id,
                            "action": "rebuild",
                        }
                    ]
                    force = node.force
                else:
                    checked = workspace.preflight(cfg, job_id, w, p, force=body.force)
                    if body.token != checked["token"]:
                        raise studio.StudioError("预检已变化，请重新查看构建清单")
                    if not p["board"]:
                        raise studio.StudioError("请先生成或编辑分镜")
                    nodes, force = checked["nodes"], body.force
                bid = uuid4().hex[:12]
                build = {
                    "id": bid,
                    "plan_id": plan_id,
                    "status": "running",
                    "force": force,
                    "created_at": studio.now(),
                    "plan_snapshot": copy.deepcopy(p),
                    "nodes": [{**n, "status": "pending"} for n in nodes],
                }
                w["builds"].append(build)
                workspace.persist(cfg, job_id, w)
                ExecutionLog(cfg, job_id, bid).emit(f"已提交执行，共 {len(nodes)} 项")
            except Exception:
                file_lock.release()
                raise

            def worker():
                try:
                    workspace.run_build(
                        cfg, job_id, bid, speaker=speaker, storyboarder=storyboarder, embedder=embedder
                    )
                except Exception as exc:  # noqa: BLE001 — all background errors need terminal state
                    fresh = workspace.load(cfg, job_id)
                    record = next(b for b in fresh["builds"] if b["id"] == bid)
                    record.update(status="failed", error=str(exc), finished_at=studio.now())
                    ExecutionLog(cfg, job_id, bid).emit(f"执行失败：{exc}", "error")
                    workspace.persist(cfg, job_id, fresh)
                finally:
                    file_lock.release()

            thread = threading.Thread(target=worker, daemon=True, name=f"studio-build-{job_id}")
            executor["thread"] = thread
            try:
                thread.start()
            except Exception as exc:
                build.update(status="failed", error=str(exc), finished_at=studio.now())
                ExecutionLog(cfg, job_id, bid).emit(f"无法启动执行：{exc}", "error")
                workspace.persist(cfg, job_id, w)
                file_lock.release()
                raise
            return build

    @router.post("/jobs/{job_id}/plans/{plan_id}/build", status_code=202)
    def submit_build(job_id: str, plan_id: str, body: BuildBody):
        return launch_build(job_id, plan_id, body=body)

    @router.post("/jobs/{job_id}/plans/{plan_id}/nodes", status_code=202)
    def submit_node(job_id: str, plan_id: str, body: NodeBody):
        return launch_build(job_id, plan_id, node=body)

    @router.post("/jobs/{job_id}/plans/{plan_id}/regenerate-column", status_code=202)
    def regenerate_column(job_id: str, plan_id: str, body: ColumnBody):
        return launch_build(job_id, plan_id, column=body)

    @router.get("/jobs/{job_id}/builds/{build_id}/logs")
    def build_logs(job_id: str, build_id: str, after: int = Query(default=0, ge=0)):
        w = read_workspace(job_id)
        build = next((b for b in w["builds"] if b["id"] == build_id), None)
        if build is None:
            raise EntityNotFound("找不到构建执行")
        return {"build_id": build_id, "status": build["status"], **read_events(base, job_id, build_id, after)}

    @router.get("/sources/{source_id}/shots")
    def source_shots(source_id: str):
        return {"items": workspace.source_shots(current(), source_id)}

    @router.post("/jobs/{job_id}/plans/{plan_id}/anchor")
    def select_anchor(job_id: str, plan_id: str, body: AnchorBody):
        from . import studio_window

        with mutation_lock():
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            if body.mode == "manual" and (not body.recall_version or not body.candidate_id):
                raise studio.StudioError("请选择召回版本及候选")
            choice = {"mode": body.mode}
            if body.mode == "manual":
                choice.update(recall_version=body.recall_version, candidate_id=body.candidate_id)
            studio_window.set_anchor(w, p, body.shot_id, choice)
            if body.mode == "manual":
                studio_window.candidates(current(), w, p, body.shot_id)
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.get("/jobs/{job_id}/plans/{plan_id}/shots/{shot_id}/selection-context")
    def selection_context(job_id: str, plan_id: str, shot_id: str):
        from . import studio_window

        # Serialize ffmpeg resource generation with builds, previews and mutations.
        with mutation_lock():
            w = workspace.load(current(), job_id)
            ctx = studio_window.context(current(), job_id, w, workspace.plan(w, plan_id), shot_id)
            ctx.update(studio_window.resources(current(), job_id, ctx))
        return ctx

    def preview_range(job_id, w, p, body):
        import copy

        draft = copy.deepcopy(p)
        if isinstance(body, WindowBody):
            from . import studio_window

            selected_range, ctx = studio_window.selected_range(
                current(),
                job_id,
                w,
                p,
                body.shot_id,
                body.context_token,
                body.start,
                ref=body.anchor_ref,
                tts_version=body.tts_version,
            )
        else:
            selected_range = body.model_dump(
                include={"source_id", "start", "end", "segment_ids"}, exclude_none=True
            )
        draft["ranges"][body.shot_id] = selected_range
        if isinstance(body, WindowBody):
            result = studio_window.alignment(
                current(), job_id, w, draft, body.shot_id, selected_range, _context=ctx
            )
        else:
            result = workspace.alignment(current(), job_id, w, draft, body.shot_id, selected_range)
        result["preview_token"] = fingerprint(
            {
                "revision": body.revision,
                "result": result,
                "selection": workspace.settings(current(), p, "select"),
                "render": workspace.settings(current(), p, "render"),
            }
        )
        return result

    @router.post("/jobs/{job_id}/plans/{plan_id}/range-preview")
    def range_preview(job_id: str, plan_id: str, body: WindowBody | RangeBody):
        with mutation_lock():
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            result = preview_range(job_id, w, p, body)
            if not isinstance(body, WindowBody) or body.render_preview:
                # Media cache identity depends on actual inputs, not the mutable
                # plan revision used by the separate confirmation token.
                key = fingerprint(
                    {
                        "event": result["event"],
                        "range": result["range"],
                        "render": workspace.settings(current(), p, "render"),
                    }
                )
                result["preview_url"] = create_event_preview(
                    job_id, result["event"], key, p["options"], window_range=result["range"]
                )
        return result

    @router.post("/jobs/{job_id}/plans/{plan_id}/range-confirm")
    def range_confirm(job_id: str, plan_id: str, body: WindowBody | RangeBody):
        with mutation_lock():
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            result = preview_range(job_id, w, p, body)
            if body.preview_token != result["preview_token"]:
                raise studio.StudioError("请先预览并确认当前对齐结果")
            p["ranges"][body.shot_id] = result["range"]
            workspace.execute(current(), job_id, w, p, "select", body.shot_id)
        return workspace.detail(current(), job_id, w)

    def create_event_preview(job_id, data, key, options=None, *, window_range=None):
        import math
        import wave
        from fractions import Fraction

        from .media import atomic_media, probe
        from .studio_render import cut_shot, extract_ambient, process_shot_audio

        cfg = current()
        scale = min(1, 640 / cfg.studio.render.width, 360 / cfg.studio.render.height)
        preview_render = cfg.studio.render.model_copy(
            update={
                "width": max(2, int(cfg.studio.render.width * scale) // 2 * 2),
                "height": max(2, int(cfg.studio.render.height * scale) // 2 * 2),
                "preset": "ultrafast",
                "crf": max(28, cfg.studio.render.crf),
            }
        )
        event = studio.TimelineEvent.model_validate(data)
        if not studio_steps.interval_visible(
            store,
            event.video.source_id,
            event.video.start,
            event.video.end,
            overlays=Library(store).overlays(),
        ):
            raise studio.StudioError("所选区间包含隐藏素材")
        directory = safe_path(studio.job_dir(base, job_id), f"previews/quick-v1-{key}")
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "preview.mp4"
        with FileLock(str(output) + ".lock", timeout=0):
            if not output.is_file():
                row = store.one("SELECT path FROM sources WHERE id=?", (event.video.source_id,))
                if not row:
                    raise studio.StudioError("素材不存在")
                source = store.resolve_media(row["path"])
                preview_source, preview_event = source, event.model_copy(deep=True)
                if window_range and window_range.get("resource_key"):
                    cached = safe_path(
                        studio.job_dir(base, job_id),
                        f"previews/context-{window_range['resource_key']}/context.mp4",
                    )
                    if cached.is_file():
                        preview_source = cached
                        offset = window_range["context_range"]["start"]
                        preview_event.video.start -= offset
                        preview_event.video.end -= offset
                voice = safe_path(studio.job_dir(base, job_id), event.audio.file)
                ambient = None
                audio_config = cfg.studio.audio
                if options:
                    audio_config = audio_config.model_copy(update={"duck_gain": options["duck_gain"]})
                    if options["audio_mode"] == "ducking" and probe(source)["audio"]:
                        ambient = directory / "ambient.wav"
                        extract_ambient(source, event, audio_config, ambient)
                process_shot_audio(voice, ambient, event, audio_config, directory / "audio.wav")
                with wave.open(str(directory / "audio.wav"), "rb") as handle:
                    samples = handle.getnframes()
                fps = Fraction(str(preview_render.fps))
                frames = math.ceil(Fraction(samples, 48000) * fps)
                while math.floor(Fraction(frames, 1) / fps * 48000) < samples:
                    frames += 1
                cut_shot(
                    preview_source, preview_event, preview_render, directory / "video.mp4", frames=frames
                )
                end_sample = round(Fraction(frames, 1) / fps * 48000)
                atomic_media(
                    [
                        "-i",
                        str(directory / "video.mp4"),
                        "-i",
                        str(directory / "audio.wav"),
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                        "-c:v",
                        "copy",
                        "-af",
                        f"apad=whole_len={end_sample},atrim=end_sample={end_sample}",
                        "-c:a",
                        "aac",
                    ],
                    output,
                )
        return f"/api/studio/jobs/{job_id}/artifacts/{output.relative_to(studio.job_dir(base, job_id)).as_posix()}"

    @router.get("/jobs/{job_id}/versions/{version_id}/preview")
    def version_preview(job_id: str, version_id: str):
        w = read_workspace(job_id)
        item = workspace.version(w, version_id)
        if item["stage"] == "render":
            if "preview" not in item["files"]:
                return {"url": f"/api/studio/jobs/{job_id}/artifacts/{item['files']['video']['path']}"}
            return {"url": f"/api/studio/jobs/{job_id}/artifacts/{item['files']['preview']['path']}"}
        if item["stage"] != "select" or item["incomplete"]:
            raise studio.StudioError("此版本不能生成有配音的预览；可从产物查看原文件")
        key = fingerprint(
            {
                "version": version_id,
                "render": current().studio.render.model_dump(),
                "audio": current().studio.audio.model_dump(),
            }
        )
        with mutation_lock():
            return {
                "url": create_event_preview(
                    job_id, item["data"]["event"], key, window_range=item["data"].get("range")
                )
            }

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
            reject_legacy_mutation(job_id)
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
            reject_legacy_mutation(job_id)
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
