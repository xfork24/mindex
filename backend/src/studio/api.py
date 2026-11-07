"""创作 API：任务创建、状态轮询、单步/全自动执行、产物下载。

执行模型：endpoint 同步抢全局 studio.lock（跨进程）+ 进程内单执行器，
步骤体在后台线程运行；前端轮询 GET /jobs/{id} 观察进度。
"""

from __future__ import annotations

import copy
import json
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from filelock import FileLock
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUpload

from admin.curation import EntityNotFound, Library
from models.adapters import (
    Animator,
    ModelError,
    MultimodalEmbedder,
    Speaker,
    Storyboarder,
    VideoGenerator,
    Writer,
    make_storyboarder,
)
from settings.runtime_settings import RuntimeConfig
from shared.config import Config, fingerprint
from shared.images import image_png
from shared.shot_media import ShotMedia
from shared.storage import Store
from shared.uploads import save_upload
from shared.work_budget import copy_file
from studio import brands, script_templates, studio
from studio import steps as studio_steps
from studio import workspace as workspace
from studio.artifacts import digest, safe_path
from studio.brands_api import brand_routes
from studio.diagnostics import diagnostics
from studio.executor import StudioExecutor
from studio.files import SANDBOX_HEADERS, file_routes
from studio.logs import ExecutionLog, read_events
from studio.material_digest import build_digest, merged_vocabulary, safe_digest
from studio.models import (
    AnchorBody,
    AnimationBody,
    BuildBody,
    ColumnBody,
    CreateJobBody,
    LyricsBody,
    MaterialScopeBody,
    MetadataBody,
    NodeBody,
    OverlaySourceBody,
    PlanBody,
    PlanPatchBody,
    QuerySuggestionBody,
    RangeBody,
    ReferenceBody,
    RunStepBody,
    StoryboardBody,
    TimelineBody,
    TitleBody,
    VersionChoiceBody,
    WindowBody,
    validate_recall_references,
    validate_source_references,
)
from studio.music_api import music_routes
from studio.tasks import publication, submit
from studio.templates_api import template_routes
from studio.tools_api import tool_routes

log = logging.getLogger(__name__)


def _if_match(request: Request | None) -> str | None:
    """条件发布：客户端把 ``GET /jobs/{id}`` 返回的 workspace_revision 经 If-Match 带回。"""
    if request is None or getattr(request.state, "directory_revision_checked", False):
        return None
    value = request.headers.get("if-match")
    return value.strip().strip('"') if value else None


def studio_router(
    config: Config | RuntimeConfig,
    store: Store,
    *,
    embedder: MultimodalEmbedder | None = None,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
    writer: Writer | None = None,
    video_generator: VideoGenerator | None = None,
    animator: Animator | None = None,
    animation_capture=None,
    image_generator=None,
    music_generator=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/studio", tags=["创作"])
    base = config if isinstance(config, Config) else config.base  # 路径与渲染参数走基础配置
    # 进程内单执行线程 + 跨进程 studio.lock；取消走进程内事件加哨兵文件，见 studio/executor.py。
    runner = StudioExecutor(base)
    mutation_lock = runner.mutation

    def current() -> Config:
        """传入 RuntimeConfig 时按最新覆盖层解析；覆盖层不触碰路径，路径用法保持稳定。"""
        return config.current() if isinstance(config, RuntimeConfig) else config

    def launch(job_id: str, requested_step: str | None, body: RunStepBody):
        with runner.reserve() as file_lock, publication(current(), job_id):
            cfg = current()  # 一次任务运行绑定同一份生效配置；运行中改设置下次生效
            reject_legacy_mutation(job_id)
            state = studio.reconcile(cfg, job_id)
            studio.legacy_text(state)  # 新任务没有外部文案，只能走版本工作台
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
            runner.clear("steps", job_id)  # 上次执行残留的取消请求作废
            runner.cancel_event("steps", job_id)
            studio.begin_step(cfg, job_id, steps[0])
            response = studio.set_execution(cfg, job_id, "running", steps[0], steps[1:])
            if cfg.queue.backend == "postgres":
                task_id = submit(
                    cfg, job_id, "steps", {"steps": steps, "mode": body.mode, "shot_ids": body.shot_ids}
                )
                return {
                    "task_id": task_id,
                    "steps": {n: i["status"] for n, i in response["steps"].items()},
                    "execution": response["execution"],
                }
            outcome = {"status": "complete"}

            def run_steps():
                for index, step in enumerate(steps):
                    if index:
                        # 只在步骤之间检查取消：已开始的步骤照常完成，未开始的步骤保持原状态。
                        if runner.cancelled("steps", job_id):
                            outcome["status"] = "cancelled"
                            return
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

            def record_failure(exc):  # 所有后台失败必须记录终态
                outcome["status"] = "failed"
                fresh = studio.load_job(cfg, job_id)
                fresh["execution"]["error"] = str(exc)
                studio.save(studio.job_path(cfg, job_id), fresh, config=cfg)

            def finish():
                try:
                    studio.set_execution(cfg, job_id, outcome["status"], None, [])
                finally:
                    runner.clear("steps", job_id)

            def worker():
                runner.run_guarded(
                    run_steps,
                    message=f"创作任务 {job_id} 的步骤执行失败",
                    on_error=record_failure,
                    finally_=finish,
                )

            def start_failed(exc):
                runner.clear("steps", job_id)
                studio.fail_step(cfg, job_id, steps[0], exc)
                studio.set_execution(cfg, job_id, "failed", None, [])

            runner.start(f"studio-{job_id}", worker, lock=file_lock, on_failure=start_failed)
            return {
                "steps": {name: info["status"] for name, info in response["steps"].items()},
                "execution": response["execution"],
            }

    def shot_summary(shot: dict) -> dict:
        """聚合单镜的分镜/候选/配音/选片信息，供轮询端点一次带全。"""
        summary = {**shot, "candidates": None, "audio": None, "selection": None}
        candidate_file = studio.candidates_dir(base, shot["job_id"]) / f"{shot['id']}.json"
        if candidate_file.is_file():
            document = json.loads(candidate_file.read_text(encoding="utf-8"))
            summary["candidates"] = {
                "status": document.get("status"),
                "count": len(document.get("results", [])),
                "top": document.get("results")[0] if document.get("results") else None,
                "pool": document.get("results", []),
            }
        manifest_file = studio.job_dir(base, shot["job_id"]) / "audio.json"
        if manifest_file.is_file():
            manifest = json.loads(manifest_file.read_text(encoding="utf-8")).get("shots", {})
            if shot["id"] in manifest:
                summary["audio"] = {
                    "duration": manifest[shot["id"]]["duration"],
                    "valid": studio.load_job(base, shot["job_id"])["steps"]["tts"]["status"] == "complete",
                    "url": f"/api/studio/jobs/{shot['job_id']}/audio/{shot['id']}"
                    f"?v={manifest[shot['id']].get('sha256', 'legacy')}",
                }
        timeline_file = studio.timeline_path(base, shot["job_id"])
        if timeline_file.is_file():
            events = json.loads(timeline_file.read_text(encoding="utf-8")).get("events", [])
            summary["selection"] = next(
                (
                    {
                        "source_name": event["video"].get("source_name", ""),
                        "start": event["video"]["start"],
                        "end": event["video"]["end"],
                        "alignment": event["video"].get("alignment"),
                        "atempo": event["audio"].get("atempo"),
                        "preview_url"
                        "": f"/api/studio/jobs/{shot['job_id']}/preview/{shot['id']}?v={fingerprint(event)}",
                    }
                    for event in events
                    if event.get("shot_id") == shot["id"]
                ),
                None,
            )
        return summary

    def job_detail(job_id: str) -> dict:
        state = studio.load_job(base, job_id)
        if base.storage.backend == "s3":
            from shared import files

            for entry in files.entries(store, f"studio/{job_id}/"):
                relative = entry["path"].removeprefix(f"studio/{job_id}/")
                if relative in {"audio.json", "timeline.json", "checkpoints.json"} or relative.startswith(
                    "candidates/"
                ):
                    files.hydrate(store, store.root / entry["path"])
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
        if (
            store.media_available(f"studio/{job_id}/render/final.mp4")
            if base.storage.backend == "s3"
            else final.is_file()
        ):
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
        if base.queue.backend == "postgres":
            return w
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
    def get_workspace(job_id: str, compact: bool = False):
        return workspace.detail(current(), job_id, read_workspace(job_id), store=store, compact=compact)

    @router.get("/jobs/{job_id}/material-digest")
    def get_material_digest(job_id: str, language: Literal["zh", "en"] = "zh"):
        """任务元信息中的可用素材摘要预览：只读、不加锁，不改动任务、方案、版本与签名。"""
        state = studio.load_job(base, job_id)  # 不存在 → EntityNotFound → 404"找不到创作任务"
        return build_digest(current(), store, state, language=language).public()

    @router.post("/jobs/{job_id}/music/lyrics")
    def generate_lyrics(job_id: str, body: LyricsBody):
        (
            """背景音乐歌词生成（MiniMax """
            """lyrics_generation）：只返回结果，不改动分镜；由页面或助手写入提示点后再保存。"""
        )
        from models.adapters import make_music_generator
        from studio.workspace.handlers.music import unavailable

        studio.load_job(base, job_id)  # 不存在 → 404
        cfg = current()
        generator = music_generator
        if generator is None:
            if reason := unavailable(cfg):
                raise HTTPException(422, reason)
            generator = make_music_generator(cfg)
        try:
            result = generator.lyrics(body.prompt or "", mode=body.mode, lyrics=body.lyrics, title=body.title)
        except ModelError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"title": result.title, "style_tags": result.style_tags, "lyrics": result.lyrics}

    @router.post("/jobs/{job_id}/plans/{plan_id}/shots/{shot_id}/query-suggestion")
    def query_suggestion(job_id: str, plan_id: str, shot_id: str, body: QuerySuggestionBody):
        """按画面意象重写召回词，只返回建议：不加锁、不 persist，不改动分镜、版本与签名。

        不使用 read_workspace()：它在任务未迁移或有运行中的构建时会加锁并写盘。
        """
        cfg = current()
        studio.load_job(base, job_id)  # 不存在 → EntityNotFound → 404"找不到创作任务"
        if not workspace.path(base, job_id).is_file():
            raise EntityNotFound("找不到构建方案")  # 未接入版本工作台的任务没有方案
        w = workspace.load(base, job_id)  # 已迁移任务只读加载，规范化只发生在内存中
        p = w["plans"].get(plan_id)
        if p is None:
            raise EntityNotFound("找不到构建方案")
        order = workspace.ordered_shots(w, p)
        if shot_id not in p["shots"] or shot_id not in order:
            raise EntityNotFound("找不到分镜")

        def content(sid):
            vid = p["shots"].get(sid, {}).get("content")
            return w["versions"].get(vid, {}).get("data", {}) if vid else {}

        current_shot = content(shot_id)
        raw = body.visual if body.visual is not None else current_shot.get("visual")
        visual = studio.normalize_visual(raw)
        if not visual:
            raise studio.StudioError("请先填写画面意象，再重写召回词")
        if len(visual) > studio.VISUAL_LIMIT:
            raise studio.StudioError(f"画面意象超过 {studio.VISUAL_LIMIT} 字上限（当前 {len(visual)} 字）")
        index = order.index(shot_id)
        previous = content(order[index - 1]).get("text", "") if index > 0 else ""
        following = content(order[index + 1]).get("text", "") if index + 1 < len(order) else ""
        recall = studio.ShotRecall.model_validate(current_shot.get("recall") or {})
        vocabulary = merged_vocabulary(safe_digest(cfg, job_id, store=store, filters=recall.filters))
        try:
            query = (storyboarder or make_storyboarder(cfg)).suggest_query(
                current_shot.get("text", ""),
                visual,
                (previous, following),
                vocabulary,
                **({"language": "en"} if w.get("job_language") == "en" else {}),
            )
        except ModelError as exc:
            raise ModelError(f"召回词重写失败：{exc}") from exc
        query = " ".join(str(query or "").split())
        if not query:
            raise ModelError("召回词重写失败：服务返回了空召回词")
        return {"query": query}

    @router.get("/jobs/{job_id}/material-scope")
    def get_material_scope(job_id: str):
        state = studio.load_job(base, job_id)
        return {**studio.scope_status(state, store), "source_group_ids": state.get("source_group_ids", [])}

    @router.put("/jobs/{job_id}/material-scope")
    def set_material_scope(job_id: str, body: MaterialScopeBody):
        with mutation_lock():
            state = studio.load_job(base, job_id)
            parsed = studio.JobOptions.model_validate(body.model_dump())
            if parsed.source_ids and set(parsed.source_ids) - store.source_ids():
                raise studio.StudioError("素材不存在，请重新选择")
            from shared.media_groups import Groups

            Groups(store).resolve("source", parsed.source_group_ids)
            state["source_ids"] = sorted(set(parsed.source_ids)) if parsed.source_ids else None
            state["source_group_ids"] = sorted(set(parsed.source_group_ids))
            state["updated_at"] = studio.now()
            studio.save(studio.job_path(base, job_id), state, config=base)
            return {**studio.scope_status(state, store), "source_group_ids": state["source_group_ids"]}

    @router.patch("/jobs/{job_id}/title")
    def patch_title(job_id: str, body: TitleBody):
        with mutation_lock():
            state = studio.load_job(base, job_id)
            if not body.title.strip():
                raise studio.StudioError("标题不能为空")
            # 用户手动命名即视为最终标题，关闭待生成态，后台线程不再覆盖。
            state.update(title=body.title.strip(), title_pending=False, updated_at=studio.now())
            studio.save(studio.job_path(base, job_id), state, config=base)
        return state

    @router.post("/jobs/{job_id}/plans", status_code=201)
    def create_plan(job_id: str, body: PlanBody, request: Request):
        import copy
        from uuid import uuid4

        with mutation_lock():
            w = workspace.load(current(), job_id)
            workspace.require_workspace_revision(w, _if_match(request))
            if body.copy_from and body.from_final:
                raise studio.StudioError("复制方案与恢复成片不能同时指定")
            if body.from_final:
                p = workspace.restore(w, body.from_final, body.name)
            elif body.copy_from:
                p = copy.deepcopy(workspace.plan(w, body.copy_from))
                p.update(id=uuid4().hex[:12], name=body.name, created_at=studio.now())
            else:
                active = workspace.plan(w, w["active_plan"])
                from studio.delivery_templates import apply, default_snapshot

                p = workspace.new_plan(
                    body.name,
                    {
                        **apply(copy.deepcopy(active["options"]), default_snapshot(store)),
                        "voice_mode": "inherit",
                        "voice": None,
                    },
                )
                # 新方案从当前文案出发重新拆分镜；文案不必重写。
                p["script"] = active.get("script")
            # 从选题创建的任务：新方案沿用任务的账号档案快照（文案写作约束）。
            current_profile = workspace.plan(w, w["active_plan"]).get("profile_hash")
            if current_profile and "profile_hash" not in p:
                p["profile_hash"] = current_profile
            w["plans"][p["id"]] = p
            w["active_plan"] = p["id"]
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.post("/jobs/{job_id}/plans/{plan_id}/activate")
    def activate_plan(job_id: str, plan_id: str, request: Request):
        with mutation_lock():
            w = workspace.load(current(), job_id)
            workspace.require_workspace_revision(w, _if_match(request))
            workspace.plan(w, plan_id)
            w["active_plan"] = plan_id
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.delete("/jobs/{job_id}/plans/{plan_id}")
    def delete_plan(job_id: str, plan_id: str, request: Request):
        """只删除方案记录：版本、构建记录与文件全部保留，未被引用的产物由 mindex clean 回收。"""
        with mutation_lock():
            w = workspace.load(current(), job_id)
            workspace.require_workspace_revision(w, _if_match(request))
            workspace.plan(w, plan_id)  # 不存在 → "找不到构建方案"
            if any(b["status"] == "running" for b in w["builds"]):
                raise HTTPException(409, "有构建正在执行，请等待完成或取消后再删除方案")
            workspace.remove_plan(w, plan_id)
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.patch("/jobs/{job_id}/plans/{plan_id}")
    def patch_plan(job_id: str, plan_id: str, body: PlanPatchBody):
        from uuid import uuid4

        with mutation_lock():
            # 先校验画面意象长度：超长时整份请求拒绝，不写入任何版本、不 persist。
            if body.shots is not None and (error := studio.visual_error(body.shots)):
                raise studio.StudioError(error)
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            if body.name is not None:
                p["name"] = body.name
            if body.options:
                if not script_templates.exists(store, body.options.template_id):
                    raise studio.StudioError(f"找不到文案模板：{body.options.template_id}")
                if body.options.brand_id and not brands.exists(store, body.options.brand_id):
                    raise studio.StudioError(f"找不到品牌模板：{body.options.brand_id}")
                overrides = body.options.brand_overrides
                for key in ("intro", "outro", "watermark"):
                    if overrides is not None and (clip := getattr(overrides, key)) is not None:
                        brands.require_asset(base, clip.asset)
                if body.options.delivery:
                    from studio.delivery_templates import apply, copy_cover

                    previous = p["options"].get("delivery")
                    from studio.delivery_templates import DeliverySnapshot, freeze_config

                    hydrated = DeliverySnapshot.model_validate(previous) if previous else None
                    if hydrated:
                        freeze_config(hydrated.config, legacy=True)
                    same_snapshot = body.options.delivery.model_dump() in (
                        previous,
                        hydrated.model_dump() if hydrated else None,
                    )
                    if same_snapshot and body.options.canvas == p["options"].get("canvas", "16:9"):
                        cfg = body.options.delivery.config
                        from studio.delivery_templates import DeliveryExport

                        own = next((x for x in cfg.exports if x.canvas == body.options.canvas), None)
                        cfg.exports = ([own] if own else []) + [
                            DeliveryExport(**x.model_dump()) for x in body.options.exports or []
                        ]
                        old_media = cfg.publish.cover.media_id if cfg.publish else None
                        cfg.publish = body.options.publish
                        if cfg.publish and cfg.publish.cover.media_id != old_media:
                            cfg.cover_asset = None
                    copy_cover(current(), job_id, body.options.delivery)
                    body.options = type(body.options).model_validate(
                        apply(body.options.model_dump(), body.options.delivery)
                    )
                from studio import platforms

                if body.options.publish:
                    body.options.publish = type(body.options.publish).model_validate(
                        platforms.snapshot_publish(
                            body.options.publish.model_dump(), store, previous=p["options"].get("publish")
                        )
                    )
                    if body.options.delivery:
                        body.options.delivery.config.publish = body.options.publish
                p["options"] = {
                    **body.options.model_dump(),
                    # 参考文稿登记只经 /references 增删：整份提交方案参数时保留现有登记。
                    "references": workspace.references.references(p),
                }
            if body.script is not None:
                workspace.add_script(current(), job_id, w, p, body.script, "manual")
            if body.shots is not None:
                shots = [
                    studio.Shot(**{**s.model_dump(), "id": s.id or f"shot-{uuid4().hex[:10]}"})
                    for s in body.shots
                ]
                validate_recall_references(base, store, job_id, shots)
                validate_source_references(base, job_id, shots)
                workspace.set_board(current(), job_id, w, p, shots)
            # 多画幅重新渲染：创建或同步关联子方案（没有 rerender 导出时不改动任何方案）。
            if body.options or body.shots is not None:
                workspace.variants.sync(w, p)
            workspace.persist(base, job_id, w)
        return workspace.detail(current(), job_id, w)

    @router.post("/jobs/{job_id}/plans/{plan_id}/query-media", status_code=201)
    async def upload_query_media(
        job_id: str,
        plan_id: str,
        file: Annotated[UploadFile, File()],
        kind: Annotated[str, Form()],
        purpose: Annotated[str, Form()] = "query",
    ):
        """分镜查询文件或画面来源文件：内容寻址去重落盘 inputs/。

        ``purpose=query``（缺省）的校验与检索页上传一致；``purpose=source`` 用于 clip/image 画面来源，
        视频按 ``studio.max_clip_mb`` / ``studio.max_clip_seconds`` 限制，图片与查询图片相同。"""
        from shared.media import probe

        if kind not in {"image", "video", "audio"}:
            raise HTTPException(422, "查询文件类型只支持 image、video 或 audio")
        if purpose not in {"query", "source"}:
            raise HTTPException(422, "上传用途只支持 query 或 source")
        if kind == "audio" and purpose != "source":
            raise HTTPException(422, "音频只能作为参考素材（purpose=source）上传")
        config = current()
        label = "画面" if purpose == "source" else "查询"
        # Streaming must not hold a blocking threading.Lock on the event loop.
        # Verify and commit together in a worker, under the normal mutation lock.
        with tempfile.TemporaryDirectory(prefix="studio-upload-") as directory:
            suffix = Path(file.filename or "").suffix.lower()
            allowed = (
                {".png", ".jpg", ".jpeg", ".webp"}
                if kind == "image"
                else {".mp3", ".wav", ".flac", ".m4a", ".aac"}
                if kind == "audio"
                else {".mp4", ".mov", ".mkv", ".webm", ".avi"}
            )
            if suffix not in allowed:
                suffix = {"image": ".png", "audio": ".mp3"}.get(kind, ".mp4")
            staged = Path(directory) / ("query" + suffix)
            if kind == "image":
                megabytes = config.search.max_image_upload_mb
            else:
                megabytes = config.studio.max_clip_mb if purpose == "source" else config.search.max_upload_mb
            limit = megabytes * 1024 * 1024
            await save_upload(file, staged, limit)

            def commit():
                with mutation_lock():
                    w = workspace.load(config, job_id)
                    workspace.plan(w, plan_id)
                    seconds = None
                    try:
                        if kind == "image":
                            image_png(staged, config.video.max_pixels)
                        elif kind == "audio":
                            # 参考音频：多模态参考生视频（2–15 秒）或翻唱（6 秒–6 分钟），
                            # 具体时长由生成服务校验。
                            from shared.media import audio_info

                            seconds = audio_info(staged)["duration"]
                            if seconds > 360 + 0.05:
                                raise ValueError("参考音频不能超过 6 分钟")
                        else:
                            info = probe(staged)
                            seconds = info["duration"]
                            maximum = (
                                config.studio.max_clip_seconds
                                if purpose == "source"
                                else config.search.max_query_seconds
                            )
                            if seconds > maximum + 0.05:
                                raise ValueError(f"{label}视频不能超过 {maximum:g} 秒")
                    except HTTPException:
                        raise
                    except Exception as exc:
                        raise HTTPException(422, f"{label}文件无法解码：{exc}") from exc
                    media_id = digest(staged)
                    inputs = studio.job_dir(config, job_id) / "inputs"
                    inputs.mkdir(parents=True, exist_ok=True)
                    existing = next(
                        (p for p in inputs.glob(media_id + ".*") if p.is_file() and p.suffix != ".partial"),
                        None,
                    )
                    target = existing or inputs / f"{media_id}{suffix}"
                    if not target.exists() or digest(target) != media_id:
                        copy_file(staged, target)
                    if base.storage.backend == "s3":
                        from shared import files

                        files.publish(store, target)
                    return {
                        "media_id": media_id,
                        "kind": kind,
                        "purpose": purpose,
                        "duration": seconds,
                        "bytes": target.stat().st_size,
                        "name": file.filename,
                        "preview_url": f"/api/studio/jobs/{job_id}/artifacts/inputs/{target.name}",
                    }

            return await run_in_threadpool(commit)

    @router.post("/jobs/{job_id}/plans/{plan_id}/references", status_code=201)
    async def add_reference(job_id: str, plan_id: str, request: Request):
        """登记参考文稿：multipart（``file`` + ``revision`` + 可选 ``name``）
        或 JSON ``{revision, name, text}``。

        正文规范化为 UTF-8 后按 sha256 存 ``inputs/<sha>.txt``，同一正文只登记一次；总字数超过
        ``studio.max_reference_chars`` 返回 422。"""
        config = current()
        limit = config.studio.max_reference_chars * 4 + 4096  # UTF-8 每字最多 4 字节
        if (request.headers.get("content-type") or "").startswith("multipart/form-data"):
            form = await request.form()
            upload, revision = form.get("file"), form.get("revision")
            if not isinstance(upload, StarletteUpload) or not isinstance(revision, str):
                raise HTTPException(422, "请上传参考文稿文件并提供方案 revision")
            suffix = Path(upload.filename or "").suffix.lower()
            ctype = (upload.content_type or "").split(";")[0].strip().lower()
            if suffix not in workspace.references.TEXT_SUFFIXES and not ctype.startswith("text/"):
                raise HTTPException(422, "参考文稿只支持 .txt 或 .md 文本文件")
            raw = await upload.read(limit + 1)
            await upload.close()
            if len(raw) > limit:
                raise HTTPException(413, "上传文件过大")
            name = form.get("name")
            name = name if isinstance(name, str) and name.strip() else Path(upload.filename or "").stem
        else:
            try:
                body = ReferenceBody.model_validate(await request.json())
            except ValueError as exc:
                raise HTTPException(422, "请求体必须是包含 revision 与 text 的 JSON") from exc
            revision, name, raw = body.revision, body.name or "", body.text

        def commit():
            with mutation_lock():
                w = workspace.load(config, job_id)
                p = workspace.plan(w, plan_id)
                workspace.require_revision(p, revision)
                item = workspace.references.register(config, job_id, p, name, raw)
                workspace.persist(base, job_id, w)
            return {"reference": item, **workspace.detail(config, job_id, w)}

        return await run_in_threadpool(commit)

    @router.delete("/jobs/{job_id}/plans/{plan_id}/references/{reference_id}")
    def remove_reference(job_id: str, plan_id: str, reference_id: str, revision: str):
        """只移除登记；inputs/ 里的文件保留（可能被其他方案或历史文案引用）。"""
        with mutation_lock():
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, revision)
            workspace.references.unregister(p, reference_id)
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
            if body.stage == "script":
                # 切换文案版本：同时把分镜工作台还原为依赖此版本的最后一次构建结果。
                chosen = workspace.version(w, body.version_id)
                if (
                    chosen["status"] != "complete"
                    or chosen["stage"] != "script"
                    or chosen["shot_id"] is not None
                ):
                    raise studio.StudioError("只能选用属于此阶段和分镜的成功版本")
                workspace.restore_script_board(w, p, body.version_id)
                workspace.persist(base, job_id, w)
                return workspace.detail(current(), job_id, w)
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
    def build_preflight(job_id: str, plan_id: str, force: bool = False, all_canvases: bool = False):
        w = read_workspace(job_id)
        p = workspace.plan(w, plan_id)
        if all_canvases:
            return workspace.variants.preflight_all(current(), job_id, w, p, force=force)
        return workspace.preflight(current(), job_id, w, p, force=force)

    @router.get("/jobs/{job_id}/plans/{plan_id}/diagnostics")
    def plan_diagnostics(job_id: str, plan_id: str):
        """方案诊断：分镜检查、字幕环境、已选节点状态与变化原因、各阶段当前输入。

        只读：不加锁、不 persist，构建执行期间也可调用。不使用 read_workspace()——它在任务
        未迁移或有运行中的构建时会加锁并写盘。
        """
        studio.load_job(base, job_id)  # 不存在 → EntityNotFound → 404"找不到创作任务"
        if not workspace.path(base, job_id).is_file():
            raise EntityNotFound("找不到构建方案")  # 未接入版本工作台的任务没有方案
        w = workspace.load(base, job_id)
        p = w["plans"].get(plan_id)
        if p is None:
            raise EntityNotFound("找不到构建方案")
        return diagnostics(current(), job_id, w, p, store=store)

    def launch_build(job_id, plan_id, *, body=None, node=None, column=None):
        from uuid import uuid4

        with runner.reserve() as file_lock, publication(current(), job_id):
            cfg = current()
            w = workspace.load(cfg, job_id)
            workspace.recover(cfg, job_id, w)
            p = workspace.plan(w, plan_id)
            if column:
                workspace.require_revision(p, column.revision)
                nodes = workspace.column_nodes(cfg, job_id, w, p, column.stage, column.inputs_by_shot)
                force = column.force
            elif node:
                workspace.require_revision(p, node.revision)
                if (
                    node.shot_id in p["shots"]
                    and node.stage in workspace.stages.names(scope="shot")
                    and node.stage not in workspace.stages.plan_shot_stages(w, p, node.shot_id)
                ):
                    raise studio.StudioError("此画面来源没有该阶段")
                spec = workspace.specification(cfg, job_id, w, p, node.stage, node.shot_id)
                if node.inputs != spec["inputs"]:
                    raise studio.StudioError("输入版本已变化，请刷新后重试")
                if node.stage in ("script", "split") and node.shot_id is not None:
                    raise studio.StudioError("生成文案和拆分镜不能指定单镜")
                if node.stage == "split" and not p.get("script"):
                    raise studio.StudioError("请先生成文案，再拆分镜")
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
                checked = (
                    workspace.variants.preflight_all(cfg, job_id, w, p, force=body.force)
                    if body.all_canvases
                    else workspace.preflight(cfg, job_id, w, p, force=body.force)
                )
                if body.token != checked["token"]:
                    raise studio.StudioError("预检已变化，请重新查看构建清单")
                nodes, force = checked["nodes"], body.force
            bid = uuid4().hex[:12]
            build = {
                "id": bid,
                "plan_id": plan_id,
                "status": "running",
                "force": force,
                "created_at": studio.now(),
                # 整体构建以文案或拆分镜开头时，拆分完成后按新分镜展开后续节点。
                "auto_split": not node
                and not column
                and any(n["stage"] in ("script", "split") for n in nodes),
                "plan_snapshot": copy.deepcopy(p),
                "nodes": [{**n, "status": "pending"} for n in nodes],
            }
            w["builds"].append(build)
            workspace.persist(cfg, job_id, w)
            ExecutionLog(cfg, job_id, bid).emit(f"已提交执行，共 {len(nodes)} 项")
            if cfg.queue.backend == "postgres":
                task_id = submit(cfg, job_id, "build", {}, build_id=bid)
                return {**build, "task_id": task_id}
            cancel_event = runner.cancel_event("build", job_id, bid)

            def run():
                workspace.run_build(
                    cfg,
                    job_id,
                    bid,
                    speaker=speaker,
                    storyboarder=storyboarder,
                    embedder=embedder,
                    writer=writer,
                    video_generator=video_generator,
                    animator=animator,
                    animation_capture=animation_capture,
                    cancel_event=cancel_event,
                    image_generator=image_generator,
                    music_generator=music_generator,
                )

            def record_failure(exc):  # 所有后台失败必须记录终态
                fresh = workspace.load(cfg, job_id)
                record = next((b for b in fresh["builds"] if b["id"] == bid), None)
                if record is None:  # 构建记录已不存在：原始异常已记入日志，这里只说明无法写终态
                    log.error("创作任务 %s 的构建 %s 记录已不存在，无法写入失败状态", job_id, bid)
                    return
                record.update(status="failed", error=str(exc), finished_at=studio.now())
                ExecutionLog(cfg, job_id, bid).emit(f"执行失败：{exc}", "error")
                workspace.persist(cfg, job_id, fresh)

            def worker():
                runner.run_guarded(
                    run,
                    message=f"创作任务 {job_id} 的构建 {bid} 失败",
                    on_error=record_failure,
                    finally_=lambda: runner.clear("build", job_id, bid),
                )

            def start_failed(exc):
                runner.clear("build", job_id, bid)
                build.update(status="failed", error=str(exc), finished_at=studio.now())
                ExecutionLog(cfg, job_id, bid).emit(f"无法启动执行：{exc}", "error")
                workspace.persist(cfg, job_id, w)

            runner.start(f"studio-build-{job_id}", worker, lock=file_lock, on_failure=start_failed)
            return build

    @router.get("/jobs/{job_id}/tasks/{task_id}")
    def execution_task(job_id: str, task_id: str):
        from shared.background_tasks import public_task

        row = store.one("SELECT * FROM studio_tasks WHERE id=? AND job_id=?", (task_id, job_id))
        if row is None:
            raise EntityNotFound("找不到创作执行账本")
        return public_task(row)

    @router.post("/jobs/{job_id}/tasks/{task_id}/retry", status_code=202)
    def retry_execution(job_id: str, task_id: str):
        from studio.tasks import retry

        return retry(current(), job_id, task_id)

    @router.get("/jobs/{job_id}/external-calls")
    def external_calls(job_id: str):
        studio.load_job(base, job_id)
        return {
            "items": store.rows(
                "SELECT e.id,e.operation,e.state,e.request_id,e.error FROM external_calls e "
                "JOIN studio_tasks t ON e.task_id=t.id WHERE t.job_id=? ORDER BY e.created_at",
                (job_id,),
            )
        }

    @router.post("/jobs/{job_id}/external-calls/{call_id}/resolve")
    def resolve_call(job_id: str, call_id: str, body: dict):
        from shared.external_calls import resolve

        if not store.one(
            "SELECT e.id FROM external_calls e JOIN studio_tasks t ON t.id=e.task_id "
            "WHERE e.id=? AND t.job_id=?",
            (call_id, job_id),
        ):
            raise EntityNotFound("找不到此外部调用")
        resolve(
            store,
            call_id,
            expected_state=body.get("expected_state"),
            outcome=body.get("outcome"),
            evidence=str(body.get("evidence", "")),
            request_id=body.get("request_id"),
        )
        return {"status": "resolved"}

    @router.post("/jobs/{job_id}/plans/{plan_id}/build", status_code=202)
    def submit_build(job_id: str, plan_id: str, body: BuildBody):
        return launch_build(job_id, plan_id, body=body)

    @router.post("/jobs/{job_id}/plans/{plan_id}/nodes", status_code=202)
    def submit_node(job_id: str, plan_id: str, body: NodeBody):
        return launch_build(job_id, plan_id, node=body)

    @router.post("/jobs/{job_id}/plans/{plan_id}/execute-column", status_code=202)
    def execute_column(job_id: str, plan_id: str, body: ColumnBody):
        return launch_build(job_id, plan_id, column=body.model_copy(update={"force": False}))

    @router.post("/jobs/{job_id}/plans/{plan_id}/regenerate-column", status_code=202)
    def regenerate_column(job_id: str, plan_id: str, body: ColumnBody):
        return launch_build(job_id, plan_id, column=body.model_copy(update={"force": True}))

    @router.get("/jobs/{job_id}/builds/{build_id}/logs")
    def build_logs(job_id: str, build_id: str, after: int = Query(default=0, ge=0)):
        w = read_workspace(job_id)
        build = next((b for b in w["builds"] if b["id"] == build_id), None)
        if build is None:
            raise EntityNotFound("找不到构建执行")
        return {"build_id": build_id, "status": build["status"], **read_events(base, job_id, build_id, after)}

    @router.post("/jobs/{job_id}/builds/{build_id}/cancel")
    def cancel_build(job_id: str, build_id: str):
        """请求取消运行中的构建：执行器在下一个节点开始前停止，已开始的节点照常完成。

        不加锁读取工作台：构建执行期间创作锁由 worker 持有。
        """
        studio.load_job(base, job_id)  # 不存在 → EntityNotFound → 404"找不到创作任务"
        build = None
        if workspace.path(base, job_id).is_file():
            w = workspace.load(base, job_id)
            build = next((b for b in w["builds"] if b["id"] == build_id), None)
        if build is None:
            raise EntityNotFound("找不到构建执行")
        if build["status"] != "running":
            raise HTTPException(409, "构建不在执行中，无法取消")
        runner.request_cancel("build", job_id, build_id)
        ExecutionLog(base, job_id, build_id).emit("已请求取消，当前节点完成后停止", "warning")
        return {"build_id": build_id, "status": "cancelling"}

    @router.get("/sources/{source_id}/shots")
    def source_shots(source_id: str):
        return {"items": workspace.source_shots(current(), source_id, store=store)}

    def require_recall_shot(w, p, shot_id):
        """候选、锚点与人工区间只属于素材召回来源的分镜。"""
        if shot_id in p["shots"] and "recall" not in workspace.stages.plan_shot_stages(w, p, shot_id):
            raise studio.StudioError("该分镜的画面来源不是素材召回，不能选择候选或人工选片")

    @router.post("/jobs/{job_id}/plans/{plan_id}/anchor")
    def select_anchor(job_id: str, plan_id: str, body: AnchorBody):
        from studio import window as studio_window

        with mutation_lock():
            w = workspace.load(current(), job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            require_recall_shot(w, p, body.shot_id)
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

    @router.get("/jobs/{job_id}/plans/{plan_id}/shots/{shot_id}/selection-anchor")
    def selection_anchor(job_id: str, plan_id: str, shot_id: str):
        from studio import window as studio_window

        cfg = current()
        w = read_workspace(job_id)
        p = workspace.plan(w, plan_id)
        if shot_id not in p["shots"]:
            raise studio.StudioError("分镜不存在于当前方案")
        require_recall_shot(w, p, shot_id)
        speech = w["versions"].get(p["shots"][shot_id].get("tts"), {})
        target = speech.get("data", {}).get("duration") if speech.get("status") == "complete" else None
        anchor = studio_window.resolve_anchor(cfg, w, p, shot_id, target)
        if anchor is None:
            anchor = studio_window.candidates(cfg, w, p, shot_id)[0]
        return {"candidate_id": studio_window.candidate_id(anchor)}

    @router.get("/jobs/{job_id}/plans/{plan_id}/shots/{shot_id}/selection-context")
    def selection_context(job_id: str, plan_id: str, shot_id: str):
        from studio import window as studio_window

        # Serialize ffmpeg resource generation with builds, previews and mutations.
        with mutation_lock():
            w = workspace.load(current(), job_id)
            require_recall_shot(w, workspace.plan(w, plan_id), shot_id)
            ctx = studio_window.context(current(), job_id, w, workspace.plan(w, plan_id), shot_id)
            ctx.update(studio_window.resources(current(), job_id, ctx))
        return ctx

    def preview_range(job_id, w, p, body):
        import copy

        require_recall_shot(w, p, body.shot_id)
        draft = copy.deepcopy(p)
        if isinstance(body, WindowBody):
            from studio import window as studio_window

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

    def create_event_preview(job_id, data, key, options=None, *, window_range=None, w=None):
        import math
        import wave
        from fractions import Fraction

        from shared.media import atomic_media, probe
        from studio.render import cut_shot, extract_ambient, process_shot_audio

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
        if event.video.kind == "file":
            return file_event_preview(job_id, event, key, preview_render, cfg, options, w)
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
                source = ShotMedia(store).materialize(
                    event.video.source_id, event.video.start, event.video.end
                )
                local_event = event.model_copy(deep=True)
                local_event.video.end -= local_event.video.start
                local_event.video.start = 0
                preview_source, preview_event = source, local_event.model_copy(deep=True)
                if window_range and window_range.get("resource_key"):
                    cached = safe_path(
                        studio.job_dir(base, job_id),
                        f"previews/context-{window_range['resource_key']}/context.mp4",
                    )
                    if cached.is_file():
                        preview_source = cached
                        preview_event = event.model_copy(deep=True)
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
                        raw_ambient = directory / "shot-audio.wav"
                        ShotMedia(store).audio(
                            event.video.source_id,
                            raw_ambient,
                            event.video.start,
                            event.video.end - event.video.start,
                        )
                        extract_ambient(raw_ambient, local_event, audio_config, ambient)
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
        return (
            f"/api/studio/jobs/{job_id}/artifacts/"
            f"{output.relative_to(studio.job_dir(base, job_id)).as_posix()}"
        )

    def file_event_preview(job_id, event, key, preview_render, cfg, options, w):
        """文件区间（上传片段）的带声预览：画面与原声直接取自复验摘要后的片段文件。"""
        import math
        import wave
        from fractions import Fraction

        from shared.media import atomic_media, probe
        from studio.render import cut_shot, extract_ambient, process_shot_audio

        source = workspace.sources.video_file(cfg, job_id, w, event.video)
        directory = safe_path(studio.job_dir(base, job_id), f"previews/quick-v1-{key}")
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "preview.mp4"
        with FileLock(str(output) + ".lock", timeout=0):
            if not output.is_file():
                voice = safe_path(studio.job_dir(base, job_id), event.audio.file)
                audio_config = cfg.studio.audio
                ambient = None
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
                cut_shot(source, event, preview_render, directory / "video.mp4", frames=frames)
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
        return (
            f"/api/studio/jobs/{job_id}/artifacts/"
            f"{output.relative_to(studio.job_dir(base, job_id)).as_posix()}"
        )

    @router.get("/jobs/{job_id}/versions/{version_id}/source-playlist")
    def version_source_playlist(job_id: str, version_id: str):
        """选片版本对应原片区间的镜头播放列表（分镜卡片静音预览用）。

        只读：不调用 ffmpeg、不持有变更锁、不写任务数据。镜头地址用 /api/admin/shots，
        不要求素材仍在当前生效索引中（工作台依赖的检索组合可能与当前生效的不同）。"""
        w = read_workspace(job_id)
        item = workspace.version(w, version_id)
        if item["stage"] != "select" or item["status"] != "complete" or item.get("incomplete"):
            raise studio.StudioError("只有已完成的选片版本可以播放原片区间")
        video = item["data"]["event"]["video"]
        if video.get("kind", "library") != "library":
            raise studio.StudioError("此选片来自上传片段或生成画面，请直接播放对应文件")
        if not studio_steps.interval_visible(
            store,
            video["source_id"],
            video["start"],
            video["end"],
            overlays=Library(store).overlays(),
        ):
            raise studio.StudioError("所选区间包含隐藏素材")
        return ShotMedia(store).playlist(
            video["source_id"], video["start"], video["end"], prefix="/api/admin/shots"
        )

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
                    job_id, item["data"]["event"], key, window_range=item["data"].get("range"), w=w
                )
            }

    @router.patch("/jobs/{job_id}/plans/{plan_id}/shots/{shot_id}/animation")
    def save_animation(job_id: str, plan_id: str, shot_id: str, body: AnimationBody):
        """人工保存 AI 动画源码：静态校验通过后成为该分镜选用的 origin=manual 动画版本。

        只有该分镜的渲染与成片随之需要重建；之后的构建保留人工源码，不会按签名重新生成覆盖。
        ``dry_run`` 只做静态校验并返回 ``{valid, errors, bytes, max_kb}``，不加锁、不写版本。"""
        if body.dry_run:
            from models.animator import animation_errors

            cfg = current()
            w = workspace.load(base, job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            limit = cfg.studio.animator.max_html_kb
            errors = animation_errors(body.html, limit)
            return {
                "valid": not errors,
                "errors": errors,
                "bytes": len(body.html.encode("utf-8")),
                "max_kb": limit,
            }
        with mutation_lock():
            cfg = current()
            w = workspace.load(cfg, job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            if shot_id not in p["shots"] or shot_id not in workspace.ordered_shots(w, p):
                raise EntityNotFound("找不到分镜")
            workspace.save_animation(cfg, job_id, w, p, shot_id, body.html)
            workspace.persist(base, job_id, w)
        return workspace.detail(cfg, job_id, w)

    @router.patch("/jobs/{job_id}/plans/{plan_id}/shots/{shot_id}/overlay")
    def save_overlay(job_id: str, plan_id: str, shot_id: str, body: OverlaySourceBody):
        """人工保存 HTML 文字层的叠加层源码（``index`` 为该层在分镜 overlays 中的序号）。

        只有该分镜的渲染与成片需要重建；之后的构建保留人工源码。``dry_run`` 只做静态校验。"""
        from models.animator import animation_errors

        if body.dry_run:
            cfg = current()
            w = workspace.load(base, job_id)
            workspace.require_revision(workspace.plan(w, plan_id), body.revision)
            limit = cfg.studio.animator.max_html_kb
            errors = animation_errors(body.html, limit)
            return {
                "valid": not errors,
                "errors": errors,
                "bytes": len(body.html.encode("utf-8")),
                "max_kb": limit,
            }
        with mutation_lock():
            cfg = current()
            w = workspace.load(cfg, job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            if shot_id not in p["shots"] or shot_id not in workspace.ordered_shots(w, p):
                raise EntityNotFound("找不到分镜")
            workspace.save_overlay(cfg, job_id, w, p, shot_id, body.index, body.html)
            workspace.persist(base, job_id, w)
        return workspace.detail(cfg, job_id, w)

    @router.get("/jobs/{job_id}/versions/{version_id}/overlay-{index}.html")
    def overlay_source(job_id: str, version_id: str, index: int):
        """HTML 文字层的叠加层源码（text/html，严格 CSP），供沙箱预览与源码编辑读取。"""
        w = read_workspace(job_id)
        item = workspace.version(w, version_id)
        if item["stage"] != "overlay" or f"overlay-{index}" not in item["files"]:
            raise studio.StudioError("此版本没有这个叠加层源码")
        content = workspace.file_path(base, job_id, item, f"overlay-{index}").read_bytes()
        return Response(
            content,
            media_type="text/html; charset=utf-8",
            headers={**SANDBOX_HEADERS, "Cache-Control": "no-store"},
        )

    @router.get("/jobs/{job_id}/plans/{plan_id}/cover-candidates")
    def cover_candidates(
        job_id: str, plan_id: str, platform: str, limit: int = Query(default=6, ge=1, le=20)
    ):
        (
            """封面候选帧：在该平台对应成片的每个镜头中点抽帧，按清晰度排序（缓存在任务 previews/ """
            """下，不改动版本）。"""
        )
        from studio.platforms import for_plan

        w = read_workspace(job_id)
        p = workspace.plan(w, plan_id)
        for_plan(p, platform)
        _, vid = workspace.publish.film_version(w, p, platform)
        item = w["versions"].get(vid or "")
        if not item or item["status"] != "complete":
            raise studio.StudioError("该平台对应画幅的成片还没有构建")
        frame = "clean" if "clean" in item["files"] else "mp4"
        source = workspace.file_path(base, job_id, item, frame)
        directory = safe_path(studio.job_dir(base, job_id), f"previews/cover-{item['id']}")
        directory.mkdir(parents=True, exist_ok=True)
        with FileLock(str(directory) + ".lock"):
            found = workspace.publish.frame_candidates(
                source, item["data"].get("boundaries") or [], directory, limit
            )
        relative = studio.job_dir(base, job_id)
        return {
            "film": item["id"],
            "items": [
                {
                    **{k: v for k, v in entry.items() if k != "path"},
                    "url": f"/api/studio/jobs/{job_id}"
                    f"/artifacts/{entry['path'].relative_to(relative).as_posix()}",
                }
                for entry in found
            ],
        }

    @router.patch("/jobs/{job_id}/plans/{plan_id}/publish-metadata/{platform}")
    def edit_publish_metadata(job_id: str, plan_id: str, platform: str, body: MetadataBody):
        """人工修改某个平台的发布元数据（按平台预设校验修正）；新版本被钉住，发布包随之重建。"""
        with mutation_lock():
            cfg = current()
            w = workspace.load(cfg, job_id)
            p = workspace.plan(w, plan_id)
            workspace.require_revision(p, body.revision)
            workspace.save_metadata(
                cfg, job_id, w, p, platform, body.model_dump(exclude_unset=True, exclude={"revision"})
            )
            workspace.persist(base, job_id, w)
        return workspace.detail(cfg, job_id, w)

    @router.get("/jobs/{job_id}/versions/{version_id}/animation.html")
    def animation_source(job_id: str, version_id: str):
        """动画源码（text/html）：带严格 CSP 与 nosniff，供沙箱 iframe 预览与源码编辑读取。"""
        w = read_workspace(job_id)
        item = workspace.version(w, version_id)
        if item["stage"] != "animate" or "animation" not in item["files"]:
            raise studio.StudioError("此版本没有动画源码")
        content = workspace.file_path(base, job_id, item, "animation").read_bytes()
        # 严格 CSP（见 studio_files.SANDBOX_CSP）：
        # 页面以 sandbox="allow-scripts"（不含 allow-same-origin）的 iframe
        # 加载，直接打开时 CSP 的 sandbox 指令同样把它隔离在不透明源里。
        return Response(
            content,
            media_type="text/html; charset=utf-8",
            headers={**SANDBOX_HEADERS, "Cache-Control": "no-store"},
        )

    def generate_title_async(job_id: str, text: str):
        """后台补全标题：失败不影响任务，占位标题继续可用。

        模型调用不持锁；合并标题时使用与步骤相同的写锁，执行期间延后读取状态，
        避免把旧的 running 状态写回。等待在 guard 外进行，防止阻塞执行线程释放锁。
        """

        def worker():
            cfg = current()
            try:
                title = (storyboarder or make_storyboarder(cfg)).title(text)  # text 为视频大纲
            except Exception:
                # 标题生成失败只保留占位标题，但要留下诊断（常见于模型配置错误）。
                log.warning("创作任务 %s 的标题生成失败，保留占位标题", job_id, exc_info=True)
                title = None
            try:
                while True:
                    try:
                        with runner.mutation():
                            state = studio.load_job(base, job_id)
                            if not state.get("title_pending"):
                                return  # 用户已手动改名，尊重用户输入
                            if title and title.strip():
                                state["title"] = title.strip()[:60]
                            state["title_pending"] = False
                            state["updated_at"] = studio.now()
                            studio.save(studio.job_path(base, job_id), state, config=base)
                        return
                    except HTTPException as exc:
                        if exc.status_code != 409:
                            raise
                        time.sleep(0.05)
            except EntityNotFound:
                return  # 任务已被删除，无需更新标题
            except Exception:
                log.exception("创作任务 %s 写入生成标题失败", job_id)

        threading.Thread(
            target=lambda: runner.run_guarded(worker, message=f"创作任务 {job_id} 的标题生成线程异常"),
            daemon=True,
            name=f"studio-title-{job_id}",
        ).start()

    @router.post("/jobs", status_code=201)
    def create_job(body: CreateJobBody):
        try:
            cfg = current()
            # 标题生成改为后台执行：创建立即返回占位标题，避免阻塞 LLM 往返。
            state = studio.create_job(
                cfg,
                body.text,
                body.title,
                body.options,
                store=store,
                defer_title=True,
                original_input=body.original_input,
                language=body.language,
            )
            if state.get("title_pending"):
                generate_title_async(state["id"], state["outline"])
            return state
        except studio.StudioError:
            raise
        except ValueError as exc:
            raise HTTPException(422, f"任务选项无效：{exc}") from exc

    @router.get("/jobs")
    def jobs():
        # 恢复与列举合并为一次目录扫描，避免 recover_stale + list_jobs 两次全量读 job.json。
        return {"items": studio.list_jobs(base, recover=True)}

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

    @router.post("/jobs/{job_id}/cancel")
    def cancel_steps(job_id: str):
        """请求取消旧任务的步骤执行：当前步骤完成后停止，未开始的步骤保持原状态。

        不加锁：执行期间创作锁由 worker 持有；哨兵文件对其他进程同样可见。
        """
        state = studio.load_job(base, job_id)
        if state.get("execution", {}).get("status") != "running":
            raise HTTPException(409, "任务没有正在执行的步骤，无法取消")
        runner.request_cancel("steps", job_id)
        return {"job_id": job_id, "status": "cancelling"}

    @router.patch("/jobs/{job_id}/storyboard")
    def patch_storyboard(job_id: str, body: StoryboardBody):
        with mutation_lock():
            reject_legacy_mutation(job_id)
            if error := studio.visual_error(body.shots):
                raise HTTPException(422, error)
            shots, seen = [], set()
            for index, item in enumerate(body.shots, 1):
                if item.source is not None and item.source.kind != "recall":
                    raise HTTPException(422, "老任务不支持设置画面来源，请先接入版本工作台")
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
                        visual=item.visual,
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
                studio.save(studio.job_path(base, job_id), state, config=base)
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

    file_routes(router, base, store, mutation_lock)
    template_routes(router, base, store, mutation_lock, current=current, writer=writer)
    brand_routes(router, base, store, mutation_lock, current=current)
    from studio.delivery_templates import delivery_routes

    delivery_routes(router, store, mutation_lock, current=current)
    music_routes(router, base, store, mutation_lock, current=current, writer=writer)
    tool_routes(router, base, store, mutation_lock)

    from studio.platforms import platform_routes

    platform_routes(router, store, mutation_lock)

    return router
