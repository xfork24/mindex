"""曲库 API：``/api/studio/music-library``（导入、列出、修改、删除、分析、推荐与音频文件）。

导入为 multipart（file + 授权字段），按内容去重；分析与推荐在线程池中同步完成（推荐与情绪标签调用文案模型，
模型不可用时退回确定性推断）。挂在创作路由上，删除与方案修改共用 studio 修改锁。Web、CLI、智能助手、MCP 共用。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import Field, ValidationError, model_validator

from shared.config import StrictModel
from shared.uploads import save_upload
from studio import music_library, studio
from studio.music_library import TrackChanges, TrackInUse, TrackMeta


class AnalyzeBody(StrictModel):
    ai: bool = Field(default=True, description="用文案模型打情绪标签（不可用时按节奏特征推断）")
    retag: bool = Field(default=True, description="重新打情绪标签；关闭时保留现有（含人工修改的）标签")


class RecommendBody(StrictModel):
    music_group_ids: list[str] = Field(default_factory=list, max_length=200)
    text: str | None = Field(
        default=None, max_length=8000, description="段落文案；与 job_id/plan_id/shot_id 二选一"
    )
    job_id: str | None = None
    plan_id: str | None = None
    shot_id: str | None = None
    seconds: float | None = Field(default=None, gt=0, le=3600)
    limit: int = Field(default=5, ge=1, le=50)
    ai: bool = True

    @model_validator(mode="after")
    def _source(self):
        located = self.job_id and self.plan_id and self.shot_id
        if not (self.text and self.text.strip()) and not located:
            raise ValueError("需要段落文案，或 job_id、plan_id、shot_id 指定的提示点分镜")
        return self


def music_routes(router, base, store, mutation_lock, *, current, writer=None):
    """current() 返回最新配置；writer 为测试注入的文案模型。"""

    def text_model(enabled: bool):
        if not enabled:
            return None
        if writer is not None:
            return writer
        from models import adapters

        return adapters.make_writer(current())

    @router.get("/music-library")
    def list_tracks(
        mood: str | None = None,
        q: str | None = None,
        music_group_ids: Annotated[list[str] | None, Query()] = None,
    ):
        return {
            "items": music_library.list_tracks(store, mood=mood, query=q, music_group_ids=music_group_ids),
            "moods": [{"key": key, "label": label} for key, (label, _, _) in music_library.MOODS.items()],
        }

    @router.post("/music-library", status_code=201)
    async def import_track(
        file: Annotated[UploadFile, File()],
        title: Annotated[str, Form()] = "",
        source: Annotated[str, Form()] = "",
        license: Annotated[str, Form()] = "",
        attribution: Annotated[str, Form()] = "",
        artist: Annotated[str, Form()] = "",
        expires_at: Annotated[str, Form()] = "",
        tags: Annotated[str, Form()] = "",
        analyze: Annotated[bool, Form()] = True,
        ai: Annotated[bool, Form()] = True,
    ):
        """导入曲目：授权来源、许可、署名必填；重复内容返回已有曲目（``duplicate: true``，原记录不变）。"""
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in music_library.SUFFIXES:
            raise HTTPException(422, "曲库只支持 MP3/WAV/FLAC/OGG/M4A/AAC 音频")
        try:
            meta = TrackMeta(
                title=title or Path(file.filename or "").stem,
                source=source,
                license=license,
                attribution=attribution,
                artist=artist,
                expires_at=expires_at or None,
                tags=tags,
            )
        except ValidationError as exc:
            raise HTTPException(
                422, "；".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            ) from exc
        with tempfile.TemporaryDirectory(prefix="studio-music-") as directory:
            staged = Path(directory) / f"track{suffix}"
            await save_upload(file, staged, music_library.MAX_TRACK_MB * 1024 * 1024)
            item, duplicate = await run_in_threadpool(
                music_library.import_track, current(), store, staged, suffix, meta, now=studio.now()
            )
        if analyze and not duplicate:
            try:
                item = await run_in_threadpool(
                    music_library.analyze_track,
                    current(),
                    store,
                    item["id"],
                    writer=text_model(ai),
                    now=studio.now(),
                )
            except studio.StudioError:
                item = music_library.get_track(store, item["id"])  # 分析失败记录在曲目上，导入照常成功
        return {**item, "duplicate": duplicate}

    @router.post("/music-library/recommend")
    def recommend(body: RecommendBody):
        text, seconds = body.text or "", body.seconds
        if not text.strip():
            text, estimated = music_library.segment_text(current(), body.job_id, body.plan_id, body.shot_id)
            seconds = seconds or estimated or None
        return music_library.recommend(
            store,
            text,
            seconds=seconds,
            limit=body.limit,
            writer=text_model(body.ai),
            music_group_ids=body.music_group_ids,
        )

    @router.get("/music-library/{track_id}")
    def get_track(track_id: str):
        return music_library.get_track(store, track_id)

    @router.put("/music-library/{track_id}")
    def update_track(track_id: str, body: TrackChanges):
        try:
            return music_library.update_track(store, track_id, body, now=studio.now())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.delete("/music-library/{track_id}")
    def delete_track(track_id: str):
        with mutation_lock():
            try:
                music_library.delete_track(base, store, track_id)
            except TrackInUse as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"deleted": track_id}

    @router.post("/music-library/{track_id}/analyze")
    def analyze_track(track_id: str, body: AnalyzeBody | None = None):
        body = body or AnalyzeBody()
        return music_library.analyze_track(
            current(), store, track_id, writer=text_model(body.ai), retag=body.retag, now=studio.now()
        )

    @router.get("/music-library/{track_id}/file")
    def track_file(track_id: str, download: bool = False):
        item = music_library.get_track(store, track_id)
        path = music_library.require_file(base, store, track_id)
        return FileResponse(
            path,
            filename=f"{item['title']}{item['suffix']}" if download else None,
            content_disposition_type="attachment" if download else "inline",
        )
