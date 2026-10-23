"""Persistent, explicitly retried dialogue jobs shared by Web and CLI."""

from __future__ import annotations

import json
import logging
import tempfile
import threading
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
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.shared.config import Config, StrictModel, absolutize_config_paths, relativize_config_paths
from mindex.shared.processing_lock import ProcessingLock, recover_tasks
from mindex.shared.storage import Store, dumps
from mindex.shared.task_progress import reset_task_attempt, save_task_progress, task_logs

log = logging.getLogger(__name__)


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


def task_public(row):
    if row is None:
        raise ValueError("找不到对白任务")
    return {
        key: json.loads(row[key])
        if row.get(key) is not None and key in {"options", "progress", "result", "publication"}
        else row.get(key)
        for key in (
            "id",
            "source_id",
            "status",
            "options",
            "progress",
            "error",
            "result",
            "publication",
            "created_at",
            "updated_at",
        )
    }


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


class DialogueTasks:
    def __init__(self, runtime: RuntimeConfig, store: Store, embedder=None):
        self.runtime, self.store = runtime, store
        self.threads = {}
        self.embedder = embedder
        recover_tasks(store, "dialogue_tasks")

    def lock(self, source_id):
        return ProcessingLock(self.runtime.current(), source_id=source_id, work="dialogue")

    def get(self, task_id):
        row = self.store.one("SELECT * FROM dialogue_tasks WHERE id=?", (task_id,))
        if row is None:
            raise HTTPException(404, "找不到对白任务")
        return task_public(row)

    def start(self, source_id, options: DialogueOptions):
        lock = self.lock(source_id)
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "GPU 独占锁或该素材对白任务正在运行，请稍后启动") from exc
        try:
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
            self.store.execute(
                "INSERT INTO dialogue_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
                (task_id, source_id, "queued", dumps(snapshot), options.model_dump_json()),
            )
            self._launch(task_id, config, lock)
            return self.get(task_id)
        except BaseException:
            lock.release()
            raise

    def retry(self, task_id):
        row = self.store.one("SELECT * FROM dialogue_tasks WHERE id=?", (task_id,))
        if row is None:
            raise HTTPException(404, "找不到对白任务")
        lock = self.lock(row["source_id"])
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "GPU 独占锁或该素材对白任务正在运行，请稍后重试") from exc
        try:
            row = self.store.one("SELECT * FROM dialogue_tasks WHERE id=?", (task_id,))
            if row is None:
                raise HTTPException(404, "找不到对白任务")
            publication = json.loads(row["publication"] or "{}")
            if row["status"] not in {"failed", "interrupted"} and publication.get("status") != "failed":
                raise HTTPException(409, "只有失败或中断的任务可以重试")
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
            reset_task_attempt(self.store, "dialogue_tasks", task_id)
            self._launch(task_id, config, lock)
            return self.get(task_id)
        except BaseException:
            lock.release()
            raise

    def _launch(self, task_id, config, lock):
        thread = threading.Thread(
            target=self._run, args=(task_id, config, lock), daemon=True, name="dialogue-" + task_id[:8]
        )
        self.threads[task_id] = thread
        thread.start()

    @task_logs("dialogue_tasks")
    def _run(self, task_id, config, lock):
        try:
            row = self.store.one("SELECT * FROM dialogue_tasks WHERE id=?", (task_id,))
            self.store.execute(
                "UPDATE dialogue_tasks SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )

            def progress(value):
                save_task_progress(self.store, "dialogue_tasks", task_id, value)

            options = json.loads(row["options"])
            publish = options.pop("publish", True)
            result = json.loads(row["result"]) if row["result"] and publish else execute_dialogue(
                config, row["source_id"], **options, publish=False, progress=progress, embedder=self.embedder,
            )
            self.store.execute("UPDATE dialogue_tasks SET result=? WHERE id=?", (dumps(result), task_id))
            if publish:
                from mindex.admin.source_tasks import enqueue_publications

                publication = enqueue_publications(self.runtime, self.store, row['source_id'], 'dialogue', self.embedder)
                self.store.execute('UPDATE dialogue_tasks SET publication=? WHERE id=?', (dumps(publication), task_id))
            else:
                self.store.execute("UPDATE dialogue_tasks SET publication=? WHERE id=?",
                                   (dumps({"status": "not_requested"}), task_id))
            self.store.execute(
                "UPDATE dialogue_tasks SET status='complete',result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (dumps(result), task_id),
            )
        except BaseException as exc:
            log.exception("Dialogue task %s failed", task_id)
            self.store.execute(
                "UPDATE dialogue_tasks SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc)[:2000], task_id),
            )
        finally:
            lock.release()
            self.threads.pop(task_id, None)


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

    return router
