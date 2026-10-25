"""Persistent, explicitly retried dialogue jobs shared by Web and CLI."""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, HTTPException, UploadFile
from filelock import Timeout
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from mindex.processing.dialogue import atomic_json
from mindex.processing.pipeline import Pipeline
from mindex.shared.background_tasks import BackgroundTaskRunner
from mindex.shared.config import Config, StrictModel, absolutize_config_paths, relativize_config_paths
from mindex.shared.processing_lock import ProcessingLock
from mindex.shared.storage import dumps


class DialogueOptions(StrictModel):
    method: Literal["auto", "subtitle", "asr"] = "auto"
    subtitle_stream: int | None = Field(default=None, ge=0)
    subtitle_file: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    publish: bool = False
    workers: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def valid_subtitle_source(self):
        if self.subtitle_file and (self.method != "subtitle" or self.subtitle_stream is not None):
            raise ValueError("上传字幕必须使用字幕方式，且不能同时选择内嵌字幕轨道")
        return self


def execute_dialogue(
    config, source_id=None, *, method="auto", subtitle_stream=None, subtitle_file=None, publish=True, workers=2,
    progress=None, embedder=None
):
    started = perf_counter()
    pipeline = Pipeline(config)
    result = pipeline.enrich_dialogue(
        source_id, method=method, subtitle_stream=subtitle_stream, subtitle_file=subtitle_file,
        workers=workers, progress=progress
    )
    summaries = [
        {
            key: s.get(key)
            for key in (
                "source_id",
                "status",
                "source",
                "content_version",
                "completed_chunks",
                "total_chunks",
                "fallback_reason",
            )
        }
        | {"words": len(s.get("words", [])), "utterances": len(s.get("utterances", []))}
        for s in result["sources"]
    ]
    result = {"sources": summaries, "processing_seconds": result["elapsed_seconds"], "index_id": None}
    if publish:
        if progress:
            progress({"stage": "embedding", "message": "对白处理完成，正在嵌入并检查新索引"})
        from mindex.admin.source_tasks import publication_attempt
        result["publication"] = publication_attempt(
            config, source_id, "dialogue", embedder=embedder, progress=progress
        )
        result["index_id"] = result["publication"].get("index_id")
    result["elapsed_seconds"] = round(perf_counter() - started, 3)
    atomic_json(config.data_dir / "verification" / "recall" / "dialogue-latest.json", result)
    return result


class DialogueTasks(BackgroundTaskRunner):
    table = "dialogue_tasks"
    label = "对白任务"
    busy = "GPU 独占锁或该素材对白任务正在运行"
    not_retryable = "只有失败或中断的任务可以重试"
    thread_prefix = "dialogue"

    def lock(self, source_id):
        return ProcessingLock(self.runtime.current(), source_id=source_id, work="dialogue")

    def start(self, source_id, options: DialogueOptions):
        def create():
            if not self.store.one("SELECT 1 FROM sources WHERE id=?", (source_id,)):
                raise HTTPException(404, "找不到素材")
            if options.subtitle_file:
                from mindex.processing.subtitle_uploads import resolve_subtitle

                try:
                    resolve_subtitle(self.store, source_id, options.subtitle_file)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
            config = self.runtime.current().model_copy(deep=True)
            task_id = uuid.uuid4().hex
            self.store.execute(
                "INSERT INTO dialogue_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
                (task_id, source_id, "queued", self.freeze(config), options.model_dump_json()),
            )
            return task_id, config

        return self.submit(self.lock(source_id), create)

    def freeze(self, config):
        snapshot = config.model_dump()
        snapshot["data_dir"] = str(config.data_dir)
        # 数据目录内部的路径相对 data_dir.parent 存储（移动/改名后仍可解析）；
        # 外部路径保持绝对，由 retry 回读时按当前 data_dir 还原。
        snapshot = relativize_config_paths(config.data_dir, snapshot)
        # Freeze service settings and retain a reference to credentials, never serialize keys.
        snapshot["_credential_profiles"] = {
            role: self.store.one("SELECT value FROM settings WHERE key=?", ("active.profile." + role,))
            for role in ("asr", "embedding")
        }
        return dumps(snapshot)

    def restore(self, row):
        snapshot = json.loads(row["config"])
        profiles = snapshot.pop("_credential_profiles", {})
        # 可移植路径按当前 data_dir 还原为绝对路径（镜像 load_config），
        # 使数据目录移动/改名后的历史任务仍能解析 data_dir / 部署清单 / 字幕字体。
        snapshot = absolutize_config_paths(self.runtime.base.data_dir, snapshot)
        config = Config.model_validate(snapshot)
        from mindex.admin.source_tasks import preserve_numbers
        preserve_numbers(config, snapshot)
        for role, reference in profiles.items():
            if reference:
                profile = self.store.one(
                    "SELECT api_key FROM model_profiles WHERE id=?", (reference["value"],)
                )
                if profile:
                    getattr(config, role).api_key = profile["api_key"] or ""
        return config

    def execute(self, task_id, row, config, save):
        options = json.loads(row["options"])
        publish = options.pop("publish", True)
        result = json.loads(row["result"]) if row["result"] and publish else execute_dialogue(
            config, row["source_id"], **options, publish=False,
            progress=lambda v: save("progress", v), embedder=self.embedder,
        )
        save("result", result)
        if publish:
            from mindex.admin.source_tasks import enqueue_publications

            save("publication", enqueue_publications(
                self.runtime, self.store, row['source_id'], 'dialogue', self.embedder,
            ))
        else:
            save("publication", {"status": "not_requested"})


def dialogue_router(runtime, store, embedder=None):
    router = APIRouter(prefix="/api/admin", tags=["对白任务"])
    manager = DialogueTasks(runtime, store, embedder)

    @router.post("/sources/{source_id}/subtitles", status_code=201)
    async def upload_subtitle(source_id: str, file: UploadFile):
        from mindex.processing.subtitle_uploads import (
            SUBTITLE_EXTENSIONS,
            SUBTITLE_MAX_BYTES,
            register_subtitle,
        )
        from mindex.shared.uploads import save_upload

        name = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        try:
            if not store.one("SELECT 1 FROM sources WHERE id=?", (source_id,)):
                raise HTTPException(404, "找不到素材")
            if Path(name).suffix.lower() not in SUBTITLE_EXTENSIONS:
                raise HTTPException(422, "不支持的字幕文件格式，支持 SRT、ASS、SSA、VTT、SUB、SMI、SAMI")
            lock = manager.lock(source_id)
            try:
                lock.acquire()
            except Timeout as exc:
                raise HTTPException(409, "该素材正在处理对白，请稍后上传字幕") from exc
            try:
                directory = store.root / "subtitles" / ".uploads"
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=directory) as temporary:
                    path = Path(temporary) / ("upload" + Path(name).suffix.lower())
                    await save_upload(file, path, SUBTITLE_MAX_BYTES)
                    try:
                        return await run_in_threadpool(register_subtitle, store, source_id, path, name)
                    except ValueError as exc:
                        raise HTTPException(422, str(exc)) from exc
            finally:
                lock.release()
        finally:
            await file.close()

    @router.post("/sources/{source_id}/dialogue", status_code=202)
    def start(source_id: str, options: DialogueOptions | None = None):
        return manager.start(source_id, options or DialogueOptions())

    @router.get("/dialogue-tasks/{task_id}")
    def get(task_id: str):
        return manager.get(task_id)

    @router.post("/dialogue-tasks/{task_id}/retry", status_code=202)
    def retry(task_id: str):
        return manager.retry(task_id)

    @router.post("/dialogue-tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return manager.cancel(task_id)

    return router
