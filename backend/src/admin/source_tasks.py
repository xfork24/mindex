"""素材工作与独立检索更新：冻结配置、持久检查点、共用 CLI 管线锁。"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException

from mindex.processing.pipeline import Pipeline
from mindex.shared.background_tasks import BackgroundTaskRunner, public_task
from mindex.shared.config import Config, StrictModel, absolutize_config_paths, relativize_config_paths
from mindex.shared.processing_lock import ProcessingLock
from mindex.shared.queue_dispatch import insert_ledger
from mindex.shared.storage import dumps
from mindex.shared.task_cancel import TaskCancelled

log = logging.getLogger(__name__)

Work = Literal["shots", "dialogue", "analysis", "persons"]


class WorkOptions(StrictModel):
    publish: bool = False


class PublishOptions(StrictModel):
    work: Work
    scope: Literal["shot_moment", "scene"] = "shot_moment"


# 发布范围到索引粒度的映射。旧值 moment/extra 仅出现在历史任务行的 options 里，
# retry 直接透传 scope 不经过 PublishOptions 校验，因此保留映射以保真旧行为。
PUBLISH_KINDS = {
    "shot_moment": ("moment", "shot"),
    "scene": ("scene",),
    "moment": ("moment",),
    "extra": ("shot", "scene"),
}


def freeze_config(config, store):
    # Python mode preserves integers used by historical index fingerprints.
    snapshot = config.model_dump()
    snapshot["data_dir"] = str(config.data_dir)
    # 数据目录内部的路径相对 data_dir.parent 存储，使目录移动/改名后仍可解析；
    # 外部路径（model_root、外部清单）保持绝对，由 restore_config 回读时还原。
    snapshot = relativize_config_paths(config.data_dir, snapshot)
    snapshot["_credential_profiles"] = {
        role: row["value"] if row else None
        for role in ("asr", "embedding", "analyzer", "person")
        for row in [store.one("SELECT value FROM settings WHERE key=?", ("active.profile." + role,))]
    }
    return dumps(snapshot)


def restore_config(raw, runtime, store):
    snapshot = json.loads(raw)
    profiles = snapshot.pop("_credential_profiles", {})
    # 快照里的可移植路径按当前 data_dir 还原为绝对路径（镜像 load_config 的 rebase），
    # 使历史任务在数据目录移动/改名后仍能解析 data_dir / 部署清单 / 字幕字体。
    snapshot = absolutize_config_paths(runtime.base.data_dir, snapshot)
    config = Config.model_validate(snapshot)
    preserve_numbers(config, snapshot)
    # 部署后端不进入冻结快照；恢复任务必须使用当前节点的配置和目录。
    for key in ("database", "vector", "storage", "queue", "locks", "gpu", "server", "data_dir"):
        setattr(config, key, getattr(runtime.base, key))
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


def publish_work(config, source_id, work, embedder=None, *, progress=None, scope="shot_moment"):
    from mindex.persons.persons import Persons
    from mindex.processing.indexing import IndexBuilder

    pipeline = Pipeline(config)
    if work == "persons":
        active = pipeline.store.active_index()
        if not active:
            raise ValueError("请先发布内容索引；请单独执行该素材的嵌入更新")
        if not pipeline.store.one(
            "SELECT 1 FROM index_members WHERE index_id=? AND json_extract(snapshot,'$.source_id')=? LIMIT 1",
            (active["id"], source_id),
        ):
            raise ValueError("该素材尚未发布内容索引；请单独执行该素材的嵌入更新")
        if progress:
            progress({"stage": "publish", "message": "正在发布人物检索"})
        result = Persons(pipeline.store).publish(source_id=source_id, progress=progress)
        return result
    index_id = IndexBuilder(config, embedder).build(
        activate=True,
        source_id=source_id,
        kinds=PUBLISH_KINDS[scope],
        channels=[{"shots": "visual", "analysis": "analysis", "dialogue": "dialogue"}[work]],
        progress=progress,
    )
    return {"index_id": index_id}


def publication_attempt(config, source_id, work, *, embedder=None, save=None, progress=None, scope="shot_moment"):
    """发布失败不改变处理完成；运行标记在网络调用前落库。"""
    value = {"status": "running", "error": None}
    latest_progress = None

    def notify(current):
        nonlocal latest_progress
        latest_progress = {**current, "phase": "publish"}
        if progress:
            progress(latest_progress)
        if save:
            save({"status": "running", "error": None, "progress": latest_progress})

    if save:
        save(value)
    try:
        notify({"stage": "index_wait"})
        value = {"status": "complete", "error": None, **publish_work(
            config, source_id, work, embedder, progress=notify, scope=scope
        )}
    except TaskCancelled as exc:
        # 取消不是发布失败：记录 cancelled 后上抛，由任务置为 cancelled 终态。
        value = {"status": "cancelled", "error": str(exc)}
        if latest_progress:
            value["progress"] = latest_progress
        if save:
            save(value)
        raise
    except Exception as exc:  # 发布错误独立持久化，不丢失处理结果
        log.warning("素材 %s 处理后发布失败", source_id, exc_info=True)
        value = {"status": "failed", "error": str(exc)[:2000]}
    if latest_progress:
        value["progress"] = latest_progress
    if save:
        save(value)
    return value


def person_setup(store, source_id, work_id=None):
    """资料预检仅读取数据库和参考图，不隐式建立作品。"""
    from mindex.persons.persons import Persons

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
                and r.get("role_id") == role["id"]
                and (store.root / r["path"]).is_file()
                for r in references
            ):
                missing.append(f"角色「{role['name']}」的演员缺少角色参考图")
    return {
        "work_id": work_id,
        "works": works,
        "roles": len(roles),
        "actors": len({a for r in roles for a in r["actor_ids"]}),
        "ready": not missing,
        "missing": missing,
        "library_url": "/persons",
    }


class SourceTasks(BackgroundTaskRunner):
    table = "source_tasks"
    label = "素材任务"
    busy = "GPU 独占锁或该素材同类任务正在运行"
    not_retryable = "仅失败、中断或检索更新失败的任务可以重试"
    thread_prefix = "source"

    def lock(self, source_id, work, options=None):
        from mindex.shared.processing_lock import PublicationLock, task_lock_work

        values = options.model_dump() if options is not None and hasattr(options, "model_dump") else options
        kind = PublicationLock if work == 'publish' else ProcessingLock
        return kind(self.runtime.current(), source_id=source_id, work=task_lock_work(work, values))

    def row_lock(self, row):
        return self.lock(row["source_id"], row["work"], json.loads(row["options"]))

    def validate(self, source_id, work):
        source = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not source:
            raise HTTPException(404, "找不到素材")
        if work != "shots" and not source["prepared"]:
            raise HTTPException(409, "请先完成分镜处理")
        return source

    def start(self, source_id, work, options):
        def create():
            source = self.validate(source_id, options.work if work == "publish" else work)
            if work == "publish":
                from mindex.admin.curation import Library

                info = Library(self.store).source(source_id)["work"][options.work]
                if options.scope == "scene" and options.work not in {"shots", "dialogue", "analysis"}:
                    raise HTTPException(422, "scene检索更新仅支持分镜、对白和 LLM 分析")
                if not info["can_publish_scene" if options.scope == "scene" else "can_publish"]:
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
            insert_ledger(self.store, self.table, task_id,
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
            return task_id, config

        return self.submit(self.lock(source_id, work, options), create)

    def restore(self, row):
        return restore_config(row["config"], self.runtime, self.store)

    def admit(self, row, lock, cancels):
        if row["work"] == "publish":
            # 等待 GPU 准入期间（仍为 queued）也可取消。
            lock.admit(check=lambda: cancels.check(row["id"]))

    def execute(self, task_id, row, config, save):
        options = json.loads(row["options"])
        pipeline = Pipeline(config)
        if row["work"] == "publish":
            publication = publication_attempt(
                config,
                row["source_id"],
                options["work"],
                scope=options.get("scope", "moment"),
                embedder=self.embedder,
                save=lambda v: save("publication", v),
                progress=lambda v: save("progress", v),
            )
            save("result", publication)
            if publication["status"] == "failed":
                raise ValueError(publication["error"])
            return
        result = json.loads(row["result"]) if row["result"] else None
        if result is None:
            if row["work"] == "shots":
                pipeline.detect_source(row["source_id"], lambda v: save("progress", v))
                pipeline.split(row["source_id"], lambda v: save("progress", v))
            else:
                pipeline.analyze(row["source_id"], lambda v: save("progress", v))
            result = {"processing_status": "complete"}
            save("result", result)
        if options["publish"]:
            save('publication', enqueue_publications(
                self.runtime, self.store, row['source_id'], row['work'], self.embedder,
            ))
        else:
            save("publication", {"status": "not_requested"})


def enqueue_publications(runtime, store, source_id, work, embedder=None):
    """Automatic chaining creates the same independent tasks as manual clicks."""
    from mindex.admin.curation import Library

    manager = SourceTasks(runtime, store, embedder)
    info = Library(store).source(source_id)['work'][work]
    tasks = []
    for scope in ('shot_moment', 'scene') if work in {'shots', 'dialogue', 'analysis'} else ('shot_moment',):
        field = 'search' if scope == 'shot_moment' else 'scene_search'
        if work != 'persons' and not info.get(field, {}).get('expected_segments'):
            continue
        # Attach to a live execution rather than creating a duplicate subtask.
        existing = store.one(
            "SELECT * FROM source_tasks WHERE source_id=? AND work='publish' "
            "AND json_extract(options,'$.work')=? "
            "AND json_extract(options,'$.scope')=? "
            "AND status IN ('queued','running') ORDER BY rowid DESC LIMIT 1",
            (source_id, work, scope),
        )
        task = public_task(existing) if existing else manager.start(
            source_id, 'publish', PublishOptions(work=work, scope=scope),
        )
        tasks.append(task['id'])
    return {'status': 'queued' if tasks else 'complete', 'task_ids': tasks}


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

    @router.post("/source-tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return manager.cancel(task_id)

    @router.get("/sources/{source_id}/person-setup")
    def setup(source_id: str, work_id: str | None = None):
        if not store.one("SELECT id FROM sources WHERE id=?", (source_id,)):
            raise HTTPException(404, "找不到素材")
        return person_setup(store, source_id, work_id)

    return router
