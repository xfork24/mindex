"""素材工作与独立检索更新：冻结配置、持久检查点、共用 CLI 管线锁。"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from filelock import Timeout

from .config import Config, StrictModel
from .pipeline import Pipeline
from .processing_lock import ProcessingLock, recover_tasks
from .storage import dumps

Work = Literal["shots", "dialogue", "analysis", "persons"]


class WorkOptions(StrictModel):
    publish: bool = True


class PublishOptions(StrictModel):
    work: Work


def freeze_config(config, store):
    # Python mode preserves integers used by historical index fingerprints.
    snapshot = config.model_dump()
    snapshot["data_dir"] = str(config.data_dir)
    snapshot["_credential_profiles"] = {
        role: row["value"] if row else None
        for role in ("asr", "embedding", "analyzer", "person")
        for row in [store.one("SELECT value FROM settings WHERE key=?", ("active.profile." + role,))]
    }
    return dumps(snapshot)


def restore_config(raw, runtime, store):
    snapshot = json.loads(raw)
    profiles = snapshot.pop("_credential_profiles", {})
    config = Config.model_validate(snapshot)
    preserve_numbers(config, snapshot)
    for role in ("asr", "embedding", "analyzer", "person"):
        reference = profiles.get(role)
        profile = (
            store.one("SELECT api_key FROM model_profiles WHERE id=?", (reference,)) if reference else None
        )
        getattr(config, role).api_key = (
            profile["api_key"] or "" if profile else getattr(runtime.base, role).api_key
        )
    return config


def preserve_numbers(model, snapshot):
    # 历史指纹区分 4 与 4.0；校验后的浮点转换不能改变冻结配置身份。
    for key, value in snapshot.items():
        current = getattr(model, key, None)
        if isinstance(value, dict) and isinstance(current, StrictModel):
            preserve_numbers(current, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(model, key, value)


def publish_work(config, source_id, work, embedder=None, *, progress=None):
    from .indexing import IndexBuilder
    from .persons import Persons

    pipeline = Pipeline(config)
    if work == "persons":
        active = pipeline.store.active_index()
        if not active or not pipeline.store.one(
            "SELECT 1 FROM index_members WHERE index_id=? AND json_extract(snapshot,'$.source_id')=? LIMIT 1",
            (active["id"], source_id),
        ):
            IndexBuilder(config, embedder).build(activate=True, source_id=source_id, progress=progress)
        if progress:
            progress({"stage": "publish", "message": "正在发布人物检索"})
        from .processing_lock import index_write_lock
        with index_write_lock(pipeline.store.root):
            result = Persons(pipeline.store).publish()
        if progress:
            progress({"stage": "published"})
        return result
    channel = {"shots": "visual", "dialogue": "dialogue", "analysis": "analysis"}[work]
    index_id = IndexBuilder(config, embedder).build(
        activate=True,
        source_id=source_id,
        channels=[channel] if pipeline.store.active_index() else None,
        progress=progress,
    )
    return {"index_id": index_id}


def publication_attempt(config, source_id, work, *, embedder=None, save=None, progress=None):
    """发布失败不改变处理完成；运行标记在网络调用前落库。"""
    value = {"status": "running", "error": None}
    if save:
        save(value)
    try:
        value = {"status": "complete", "error": None, **publish_work(
            config, source_id, work, embedder, progress=progress
        )}
    except Exception as exc:  # noqa: BLE001 — 发布错误独立持久化，不丢失处理结果
        value = {"status": "failed", "error": str(exc)[:2000]}
    if save:
        save(value)
    return value


def public_task(row):
    return {
        k: json.loads(v) if v and k in {"options", "progress", "result", "publication"} else v
        for k, v in row.items()
        if k != "config"
    }


def person_setup(store, source_id, work_id=None):
    """资料预检仅读取数据库和参考图，不隐式建立作品。"""
    from .persons import Persons

    catalogue = Persons(store)
    linked = store.one("SELECT work_id FROM person_sources WHERE source_id=?", (source_id,))
    work_id = work_id or (linked["work_id"] if linked else None)
    works = catalogue.entities("work")
    roles = [r for r in catalogue.entities("role") if work_id in r["work_ids"]]
    actors = {a["id"] for a in catalogue.entities("actor")}
    references = catalogue.references()
    missing = []
    if not work_id or not any(w["id"] == work_id for w in works):
        missing.append("请选择关联作品")
    elif not roles:
        missing.append("关联作品尚未登记角色")
    for role in roles:
        if not role["actor_ids"]:
            missing.append(f"角色「{role['name']}」尚未关联演员")
        for actor in role["actor_ids"]:
            if actor not in actors:
                missing.append(f"角色「{role['name']}」的演员资料缺失")
            elif not any(
                r["actor_id"] == actor
                and r.get("role_id") in (None, role["id"])
                and (store.root / r["path"]).is_file()
                for r in references
            ):
                missing.append(f"角色「{role['name']}」的演员缺少参考图")
    return {
        "work_id": work_id,
        "works": works,
        "roles": len(roles),
        "actors": len({a for r in roles for a in r["actor_ids"]}),
        "ready": not missing,
        "missing": missing,
        "library_url": "/persons",
    }


class SourceTasks:
    def __init__(self, runtime, store, embedder=None):
        self.runtime, self.store, self.embedder = runtime, store, embedder
        self.threads = {}
        recover_tasks(store, "source_tasks")

    def lock(self, source_id, work):
        return ProcessingLock(self.runtime.current(), source_id=source_id, work=work)

    def get(self, task_id):
        row = self.store.one("SELECT * FROM source_tasks WHERE id=?", (task_id,))
        if not row:
            raise HTTPException(404, "找不到素材任务")
        return public_task(row)

    def validate(self, source_id, work):
        source = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not source:
            raise HTTPException(404, "找不到素材")
        if work != "shots" and not source["prepared"]:
            raise HTTPException(409, "请先完成分镜处理")
        return source

    def start(self, source_id, work, options):
        lock = self.lock(source_id, work)
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "GPU 独占锁或该素材同类任务正在运行，请稍后启动") from exc
        try:
            source = self.validate(source_id, options.work if work == "publish" else work)
            if work == "publish":
                from .curation import Library

                if not Library(self.store).source(source_id)["work"][options.work]["can_publish"]:
                    raise HTTPException(409, "该工作尚无可更新检索的处理结果")
            config = self.runtime.current().model_copy(deep=True)
            if work == "shots" and not source["prepared"] and source["layout_config"]:
                layout = json.loads(source["layout_config"])
                config.detector = (
                    type(config.detector)
                    .model_validate(layout["detector"])
                    .model_copy(update=layout["detector"])
                )
                config.video = (
                    type(config.video).model_validate(layout["video"]).model_copy(update=layout["video"])
                )
            task_id = uuid.uuid4().hex
            self.store.execute(
                "INSERT INTO source_tasks(id,source_id,work,status,config,options) VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    source_id,
                    work,
                    "queued",
                    freeze_config(config, self.store),
                    options.model_dump_json(),
                ),
            )
            self.launch(task_id, config, lock)
            return self.get(task_id)
        except BaseException:
            lock.release()
            raise

    def retry(self, task_id):
        row = self.store.one("SELECT * FROM source_tasks WHERE id=?", (task_id,))
        if not row:
            raise HTTPException(404, "找不到素材任务")
        lock = self.lock(row["source_id"], row["work"])
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "GPU 独占锁或该素材同类任务正在运行，请稍后重试") from exc
        try:
            row = self.store.one("SELECT * FROM source_tasks WHERE id=?", (task_id,))
            if not row:
                raise HTTPException(404, "找不到素材任务")
            publication = json.loads(row["publication"] or "{}")
            if row["status"] not in {"failed", "interrupted"} and publication.get("status") != "failed":
                raise HTTPException(409, "仅失败、中断或检索更新失败的任务可以重试")
            config = restore_config(row["config"], self.runtime, self.store)
            self.store.execute(
                "UPDATE source_tasks SET status='queued',error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )
            self.launch(task_id, config, lock)
            return self.get(task_id)
        except BaseException:
            lock.release()
            raise

    def launch(self, task_id, config, lock):
        thread = threading.Thread(target=self.run, args=(task_id, config, lock), daemon=True)
        self.threads[task_id] = thread
        thread.start()

    def run(self, task_id, config, lock):
        try:
            row = self.store.one("SELECT * FROM source_tasks WHERE id=?", (task_id,))
            self.store.execute(
                "UPDATE source_tasks SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,)
            )

            def save(field, value):
                self.store.execute(
                    f"UPDATE source_tasks SET {field}=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (dumps(value), task_id),
                )

            options = json.loads(row["options"])
            pipeline = Pipeline(config)
            result = json.loads(row["result"]) if row["result"] else None
            if row["work"] == "publish":
                publication = publication_attempt(
                    config,
                    row["source_id"],
                    options["work"],
                    embedder=self.embedder,
                    save=lambda v: save("publication", v),
                    progress=lambda v: save("progress", v),
                )
                save("result", publication)
                if publication["status"] == "failed":
                    raise ValueError(publication["error"])
            else:
                if result is None:
                    if row["work"] == "shots":
                        pipeline.detect_source(row["source_id"], lambda v: save("progress", v))
                        pipeline.split(row["source_id"], lambda v: save("progress", v))
                    else:
                        pipeline.analyze(row["source_id"], lambda v: save("progress", v))
                    result = {"processing_status": "complete"}
                    save("result", result)
                if options["publish"]:
                    save("progress", {"stage": "publish", "message": "处理完成，正在更新检索"})
                    publication_attempt(
                        config,
                        row["source_id"],
                        row["work"],
                        embedder=self.embedder,
                        save=lambda v: save("publication", v),
                        progress=lambda v: save("progress", v),
                    )
                else:
                    save("publication", {"status": "not_requested"})
            self.store.execute(
                "UPDATE source_tasks SET status='complete',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (task_id,),
            )
        except BaseException as exc:  # noqa: BLE001 — 后台任务必须记录终态并释放管线锁
            self.store.execute(
                "UPDATE source_tasks SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc)[:2000], task_id),
            )
        finally:
            lock.release()
            self.threads.pop(task_id, None)


def source_tasks_router(runtime, store, embedder=None):
    router = APIRouter(prefix="/api/admin", tags=["素材处理任务"])
    manager = SourceTasks(runtime, store, embedder)

    @router.post("/sources/{source_id}/shots", status_code=202)
    def shots(source_id: str, options: WorkOptions | None = None):
        return manager.start(source_id, "shots", options or WorkOptions())

    @router.post("/sources/{source_id}/analysis", status_code=202)
    def analysis(source_id: str, options: WorkOptions | None = None):
        return manager.start(source_id, "analysis", options or WorkOptions())

    @router.post("/sources/{source_id}/publish", status_code=202)
    def publish(source_id: str, options: PublishOptions):
        return manager.start(source_id, "publish", options)

    @router.get("/source-tasks/{task_id}")
    def get(task_id: str):
        return manager.get(task_id)

    @router.post("/source-tasks/{task_id}/retry", status_code=202)
    def retry(task_id: str):
        return manager.retry(task_id)

    @router.get("/sources/{source_id}/person-setup")
    def setup(source_id: str, work_id: str | None = None):
        if not store.one("SELECT id FROM sources WHERE id=?", (source_id,)):
            raise HTTPException(404, "找不到素材")
        return person_setup(store, source_id, work_id)

    return router
