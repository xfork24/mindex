"""Persistent, explicitly retried dialogue jobs shared by Web and CLI."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from time import perf_counter

from fastapi import APIRouter, HTTPException
from filelock import FileLock, Timeout
from pydantic import Field

from .config import Config, StrictModel
from .dialogue import atomic_json
from .pipeline import Pipeline
from .runtime_settings import RuntimeConfig
from .storage import Store, dumps

log = logging.getLogger(__name__)


class DialogueOptions(StrictModel):
    subtitle_stream: int | None = Field(default=None, ge=0)
    publish: bool = True
    workers: int = Field(default=2, ge=1, le=8)


def task_public(row):
    if row is None:
        raise ValueError("找不到对白任务")
    return {
        key: json.loads(row[key])
        if row[key] is not None and key in {"options", "progress", "result"}
        else row[key]
        for key in (
            "id",
            "source_id",
            "status",
            "options",
            "progress",
            "error",
            "result",
            "created_at",
            "updated_at",
        )
    }


def execute_dialogue(
    config, source_id=None, *, subtitle_stream=None, publish=True, workers=2, progress=None, embedder=None
):
    started = perf_counter()
    pipeline = Pipeline(config)
    result = pipeline.enrich_dialogue(
        source_id, subtitle_stream=subtitle_stream, workers=workers, progress=progress
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
        from .indexing import IndexBuilder

        # Auto first-time builds also include visual; incremental jobs only update dialogue.
        channels = ["dialogue"] if pipeline.store.active_index() else None
        result["index_id"] = IndexBuilder(config, embedder).build(
            activate=True, source_id=source_id, channels=channels
        )
    result["elapsed_seconds"] = round(perf_counter() - started, 3)
    atomic_json(config.data_dir / "verification" / "recall" / "dialogue-latest.json", result)
    return result


class DialogueTasks:
    def __init__(self, runtime: RuntimeConfig, store: Store, embedder=None):
        self.runtime, self.store = runtime, store
        self.threads = {}
        self.embedder = embedder
        # Do not resubmit remote ASR after a restart. A live process owns this same lock.
        lock = self.lock()
        try:
            with lock:
                store.execute(
                    "UPDATE dialogue_tasks SET status='interrupted',error='进程已中断；请重试以继续检查点',updated_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running')"
                )
        except Timeout:
            pass

    def lock(self):
        return FileLock(str(self.store.root / "pipeline.lock"), timeout=0, thread_local=False)

    def get(self, task_id):
        row = self.store.one("SELECT * FROM dialogue_tasks WHERE id=?", (task_id,))
        if row is None:
            raise HTTPException(404, "找不到对白任务")
        return task_public(row)

    def start(self, source_id, options: DialogueOptions):
        lock = self.lock()
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "管线正在运行，请等待完成后再启动对白任务") from exc
        try:
            if not self.store.one("SELECT 1 FROM sources WHERE id=?", (source_id,)):
                raise HTTPException(404, "找不到素材")
            config = self.runtime.current().model_copy(deep=True)
            task_id = uuid.uuid4().hex
            snapshot = config.model_dump(mode="json")
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
        lock = self.lock()
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "管线正在运行，请稍后重试") from exc
        try:
            row = self.store.one("SELECT * FROM dialogue_tasks WHERE id=?", (task_id,))
            if row is None:
                raise HTTPException(404, "找不到对白任务")
            if row["status"] not in {"failed", "interrupted"}:
                raise HTTPException(409, "只有失败或中断的任务可以重试")
            snapshot = json.loads(row["config"])
            profiles = snapshot.pop("_credential_profiles", {})
            config = Config.model_validate(snapshot)
            for role, reference in profiles.items():
                if reference:
                    profile = self.store.one(
                        "SELECT api_key FROM model_profiles WHERE id=?", (reference["value"],)
                    )
                    if profile:
                        getattr(config, role).api_key = profile["api_key"] or ""
            self.store.execute(
                "UPDATE dialogue_tasks SET status='queued',error=NULL,result=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )
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

    def _run(self, task_id, config, lock):
        try:
            row = self.store.one("SELECT * FROM dialogue_tasks WHERE id=?", (task_id,))
            self.store.execute(
                "UPDATE dialogue_tasks SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )

            def progress(value):
                self.store.execute(
                    "UPDATE dialogue_tasks SET progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (dumps(value), task_id),
                )

            result = execute_dialogue(
                config,
                row["source_id"],
                **json.loads(row["options"]),
                progress=progress,
                embedder=self.embedder,
            )
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
