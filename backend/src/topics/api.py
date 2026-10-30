"""选题挖掘 REST（``/api/topics``）：服务层的薄封装。

错误映射：``EntityNotFound`` → 404、``ValueError`` → 422、``ModelError`` → 503 由 ``create_app`` 全局处理；
``TopicConflict`` 在这里转为 409。
"""

from __future__ import annotations

import json
from functools import wraps
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from mindex.shared.config import StrictModel
from mindex.topics import cards as topic_cards
from mindex.topics import create_job as topic_jobs
from mindex.topics import datasources, profiles
from mindex.topics import gaps as topic_gaps
from mindex.topics import items as topic_items
from mindex.topics import runner as topic_runner
from mindex.topics.common import TopicConflict
from mindex.topics.llm import make_llm
from mindex.topics.models import ProfileBody, ProfileChanges
from mindex.topics.sources import base as source_base


class CreateJobBody(StrictModel):
    language: Literal["zh", "en"] = "zh"
    force: bool = False


def conflicts(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except TopicConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    return wrapped


def topics_router(runtime, store, embedder=None, *, topic_llm=None, scheduler: bool = False) -> APIRouter:
    router = APIRouter(prefix="/api/topics", tags=["选题挖掘"])
    runs = topic_runner.TopicRuns(runtime, store, embedder, llm=topic_llm)
    router.topic_runs = runs  # 调度器复用同一个 runner（同一进程内的线程表与健康状态）

    def models():
        config = runtime.current()
        if embedder is not None:
            return config, make_llm(config, topic_llm), embedder
        from mindex.models.adapters import make_embedder

        return config, make_llm(config, topic_llm), make_embedder(config)

    # ---------------------------------------------------------------- 领域档案
    @router.get("/profiles")
    def list_profiles():
        return {"items": profiles.list_profiles(store)}

    @router.post("/profiles", status_code=201)
    def create_profile(body: ProfileBody):
        return profiles.create_profile(store, body)

    @router.get("/profiles/{profile_id}")
    def get_profile(profile_id: str):
        return profiles.get_profile(store, profile_id)

    @router.patch("/profiles/{profile_id}")
    def update_profile(profile_id: str, changes: ProfileChanges):
        return profiles.update_profile(store, profile_id, changes)

    @router.delete("/profiles/{profile_id}")
    @conflicts
    def delete_profile(profile_id: str):
        return profiles.delete_profile(store, profile_id)

    # ---------------------------------------------------------------- 数据源与抓取
    @router.get("/profiles/{profile_id}/sources")
    def list_sources(profile_id: str):
        return {"items": datasources.list_sources(store, profile_id), "kinds": source_base.kinds()}

    @router.post("/profiles/{profile_id}/sources", status_code=201)
    def create_source(profile_id: str, body: datasources.SourceBody):
        return datasources.create_source(store, profile_id, body)

    @router.get("/sources/{source_id}")
    def get_source(source_id: str):
        return datasources.get_source(store, source_id)

    @router.patch("/sources/{source_id}")
    def update_source(source_id: str, changes: datasources.SourceChanges):
        return datasources.update_source(store, source_id, changes)

    @router.delete("/sources/{source_id}")
    def delete_source(source_id: str):
        return datasources.delete_source(store, source_id)

    @router.post("/sources/{source_id}/test")
    def try_source(source_id: str):
        return datasources.try_source(runtime.current(), store, source_id)

    @router.post("/sources/{source_id}/calendar")
    async def import_calendar(source_id: str, request: Request, mode: str = "replace"):
        """导入日历：multipart（``file``）或 JSON ``{text, format?, mode?}``；超过 ``topics.fetch.max_bytes`` 返回 413。"""
        from starlette.concurrency import run_in_threadpool

        limit = runtime.current().topics.fetch.max_bytes
        fmt = None
        if (request.headers.get("content-type") or "").startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise HTTPException(422, "请选择日历文件（ICS 或 CSV）")
            raw = await upload.read(limit + 1)
            name = (getattr(upload, "filename", "") or "").lower()
            fmt = "ics" if name.endswith(".ics") else "csv" if name.endswith(".csv") else None
            mode = str(form.get("mode") or mode)
        else:
            raw = await request.body()
            if len(raw) > limit * 2:
                raise HTTPException(413, f"日历内容超过 {limit // 1024} KB 上限")
            try:
                payload = json.loads(raw or b"{}")
            except ValueError as exc:
                raise HTTPException(422, "请求体不是有效的 JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                raise HTTPException(422, "请提供日历文本 text")
            fmt, mode = payload.get("format"), str(payload.get("mode") or mode)
            raw = payload["text"].encode("utf-8")
        if len(raw) > limit:
            raise HTTPException(413, f"日历内容超过 {limit // 1024} KB 上限")
        if mode not in ("replace", "append"):
            raise HTTPException(422, "mode 只能是 replace 或 append")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(422, "日历文件不是 UTF-8 文本") from exc
        return await run_in_threadpool(datasources.import_calendar, store, source_id, text, fmt=fmt,
                                       append=mode == "append")

    @router.get("/calendar-samples")
    def calendar_samples():
        from mindex.topics.sources import calendar

        return {"items": calendar.sample_list()}

    @router.post("/sources/{source_id}/calendar-sample/{name}")
    def import_calendar_sample(source_id: str, name: str, mode: str = "replace"):
        if mode not in ("replace", "append"):
            raise HTTPException(422, "mode 只能是 replace 或 append")
        return datasources.import_sample(store, source_id, name, append=mode == "append")

    @router.post("/profiles/{profile_id}/fetch")
    def fetch(profile_id: str, source_id: Annotated[list[str] | None, Query()] = None):
        return datasources.fetch_profile(runtime.current(), store, profile_id, source_ids=source_id)

    @router.get("/profiles/{profile_id}/items")
    def list_items(profile_id: str, limit: Annotated[int, Query(ge=1, le=500)] = 50, source_id: str | None = None):
        profiles.get_profile(store, profile_id)
        return {"items": topic_items.list_items(store, profile_id, limit=limit, source_id=source_id)}

    # ---------------------------------------------------------------- 挖掘任务
    @router.post("/profiles/{profile_id}/runs", status_code=202)
    def start_run(profile_id: str, options: topic_runner.RunOptions | None = None):
        task = runs.start(profile_id, options)
        return topic_runner.get_run(store, task["id"])

    @router.get("/profiles/{profile_id}/runs")
    def list_runs(profile_id: str, limit: Annotated[int, Query(ge=1, le=200)] = 20):
        return {"items": topic_runner.list_runs(store, profile_id, limit)}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str):
        return topic_runner.get_run(store, run_id)

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        topic_runner.get_run(store, run_id)
        runs.cancel(run_id)
        return topic_runner.get_run(store, run_id)

    @router.post("/runs/{run_id}/retry", status_code=202)
    def retry_run(run_id: str):
        topic_runner.get_run(store, run_id)
        runs.retry(run_id)
        return topic_runner.get_run(store, run_id)

    # ---------------------------------------------------------------- 选题卡片
    @router.get("/profiles/{profile_id}/cards")
    def list_cards(profile_id: str, status: str = "new", limit: Annotated[int, Query(ge=1, le=500)] = 100):
        return {"items": topic_cards.list_cards(store, profile_id, status=status, limit=limit),
                "counts": topic_cards.counts(store, profile_id)}

    @router.get("/cards/{card_id}")
    def get_card(card_id: str):
        return topic_cards.get_card(store, card_id)

    @router.patch("/cards/{card_id}")
    def update_card(card_id: str, changes: topic_cards.CardChanges):
        return topic_cards.update_card(store, card_id, changes)

    @router.post("/cards/{card_id}/score")
    def score_card(card_id: str):
        config, llm, vectors = models()
        return topic_cards.rescore(config, store, card_id, llm=llm, embedder=vectors)

    @router.get("/cards/{card_id}/coverage")
    def card_coverage(card_id: str):
        card = topic_cards.get_card(store, card_id)
        return {"card_id": card_id, "title": card["title"], "coverage": card["coverage"]}

    @router.post("/cards/{card_id}/outline")
    def card_outline(card_id: str):
        config, llm, _ = models()
        return topic_cards.write_outline(config, store, card_id, llm=llm)

    # ---------------------------------------------------------------- 建创作任务与任务档案
    @router.post("/cards/{card_id}/job", status_code=201)
    @conflicts
    def create_job(card_id: str, body: CreateJobBody | None = None):
        return topic_jobs.create_from_card(runtime.current(), store, card_id, force=bool(body and body.force),
                                           language=body.language if body else "zh")

    @router.get("/jobs/{job_id}/profile")
    def job_profile(job_id: str):
        return topic_jobs.job_profile(runtime.current(), store, job_id)

    @router.post("/jobs/{job_id}/profile/sync")
    @conflicts
    def sync_job_profile(job_id: str):
        return topic_jobs.sync_job_profile(runtime.current(), store, job_id)

    # ---------------------------------------------------------------- 素材采集清单
    @router.get("/profiles/{profile_id}/gaps")
    def list_gaps(profile_id: str, status: str = "open", limit: Annotated[int, Query(ge=1, le=1000)] = 200):
        return {"items": topic_gaps.list_gaps(store, profile_id, status=status, limit=limit)}

    @router.patch("/gaps/{gap_id}")
    def update_gap(gap_id: str, changes: topic_gaps.GapChanges):
        return topic_gaps.update_gap(store, gap_id, changes)

    @router.get("/profiles/{profile_id}/gaps.csv")
    def export_gaps(profile_id: str, status: str = "all"):
        from urllib.parse import quote

        name = profiles.get_profile(store, profile_id)["name"]
        data = topic_gaps.export_csv(store, profile_id, status=status)
        filename = quote(f"素材采集清单-{name}.csv")
        return Response(data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=gaps.csv; filename*=UTF-8''{filename}"})

    # ---------------------------------------------------------------- 定时
    @router.get("/schedule")
    def schedule_status():
        from mindex.topics import scheduler as topic_scheduler

        return topic_scheduler.status(store, enabled=scheduler)

    return router
