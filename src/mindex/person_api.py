from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import Field, model_validator

from .adapters import analysis_options, request
from .config import StrictModel
from .person_processing import PersonClient, PersonOptions, PersonTasks, ensure_reference_quality
from .persons import CastConfirm, CastEntry, Entity, PersonHasLinkedShots, Persons, ShotReview, checked_box
from .processing_lock import ProcessingLock, index_write_lock
from .storage import dumps


class WorkAssignment(StrictModel):
    work_id: str


class ReferencePair(StrictModel):
    actor_id: str | None = None
    role_id: str | None = None
    entity: Entity | None = None

    @model_validator(mode="after")
    def target_present(self):
        if not self.actor_id and not self.role_id:
            raise ValueError("参考图必须指定演员或角色")
        return self


class SourceReference(StrictModel):
    actor_id: str | None = None
    role_id: str | None = None
    source_id: str
    timestamp: float = Field(ge=0)
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)


class Publish(StrictModel):
    content_id: str | None = None


class ParseImages(StrictModel):
    """逐张拆分：不传 image_ids 即拆全部图；客户端逐张调用以获得每张的进度。"""

    image_ids: list[str] | None = None


def parse_cast(config, image):
    """LLM proposes original-pixel coordinates for ONE cast screenshot; never writes catalogue identities."""
    service = config.analyzer
    content = [
        {
            "type": "text",
            "text": '拆分演员表截图。只输出 JSON {"entries":[{"image_id":"...","actor_name":"演员名",'
            '"foreign_name":"外文名或空串","role_name":"角色名或空串",'
            '"bbox":[x1,y1,x2,y2]}],"warnings":[]}。'
            "bbox 必须是原图像素坐标，不可按固定列数假定布局。每个主要演员条目一行。"
            "忽略导航小头像、同条目的重复头像、代表作影片与代表作中的角色。"
            "不明确或多人合照 bbox=null 并写入 warnings。不猜测缺失角色名。"
            "同一人物的不同时期（如老年叶淑柔）直接作为角色名输出：role_name=老年叶淑柔。",
        },
        {"type": "text", "text": f"image_id={image['id']}; 原图 {image['width']}×{image['height']}"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + base64.b64encode(image["raw"]).decode()},
        },
    ]
    extra = analysis_options(service)
    data = request(
        "POST",
        service.base_url.rstrip("/") + "/chat/completions",
        timeout=service.timeout,
        headers=service.headers(),
        json={
            "model": service.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 8000,
            **extra,
        },
    )
    text = data["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    parsed = json.loads(text)
    entries, warnings = [], [str(w) for w in parsed.get("warnings", [])]
    if not isinstance(parsed.get("entries"), list) or len(parsed["entries"]) > 200:
        raise ValueError("演员表候选条目格式无效")
    for raw in parsed["entries"]:
        try:
            entry = CastEntry.model_validate(raw)
            if entry.image_id != image["id"]:
                raise ValueError("模型返回未知原图 ID")
            if entry.bbox is not None:
                checked_box(entry.bbox, image["width"], image["height"])
            # 身份与单人确认都由用户决定；模型输出一律清空。
            entry.single_person = False
            entry.actor_id = entry.role_id = None
            entries.append(entry.model_dump())
        except ValueError as exc:
            warnings.append("有条目需要手动录入：" + str(exc))
    return {"entries": entries, "warnings": warnings}


def reference_checker(runtime, store):
    """添加参考图时的同步质检闭包：配置每次请求热生效，同一请求多张图只做一次 /health。

    不合格抛 ValueError（→422），人物服务不可用抛 ModelError（→503）；
    检测结果缓存到 person_features，处理任务预检直接复用。
    """
    config, client, space = None, None, None

    def check(item):
        nonlocal config, client, space
        config = config or runtime.current()
        client = client or PersonClient(config)
        space = ensure_reference_quality(store, config, client, item, space=space)

    return check


def person_router(runtime, store):
    router = APIRouter(prefix="/api/persons", tags=["人物库"])
    catalogue, tasks = Persons(store), PersonTasks(runtime, store)

    @router.get("")
    def listing():
        names = store.source_names()
        sources = [{**s, "name": names[s["id"]], "work_id": catalogue.work_for(s["id"])}
                   for s in store.sources()]
        # 待核对数量以分镜为单位，且与审核列表同一套分类规则。
        return {
            "entities": catalogue.entities(),
            "references": catalogue.references(),
            "sources": [{"id": s["id"], "name": s["name"], "work_id": s["work_id"]} for s in sources],
            "review_shots": catalogue.review_summary(),
            "coverage": [
                {**r, "data": json.loads(r["data"])} for r in store.rows("SELECT * FROM person_coverage")
            ],
            "tasks": [
                tasks.get(r["id"])
                for r in store.rows("SELECT id FROM person_tasks ORDER BY rowid DESC LIMIT 30")
            ],
            "releases": store.rows("SELECT * FROM search_releases ORDER BY rowid DESC LIMIT 30"),
        }

    @router.post("/entities", status_code=201)
    def create(value: Entity):
        return catalogue.save(value)

    @router.put("/entities/{identifier}")
    def update(identifier: str, value: Entity):
        return catalogue.save(value, identifier)

    @router.delete("/entities/{identifier}")
    def delete_entity(identifier: str):
        try:
            return catalogue.delete_entity(identifier)
        except PersonHasLinkedShots as exc:
            raise HTTPException(422, detail={"message": str(exc), "linked_shots": exc.linked_shots}) from exc

    @router.get("/entities/{identifier}/linked-shots")
    def linked_shots(
        identifier: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return catalogue.linked_shots(identifier, limit=limit, offset=offset)

    @router.put("/sources/{source_id}/work")
    def assign(source_id: str, body: WorkAssignment):
        with ProcessingLock(runtime.current(), source_id=source_id, work="persons"):
            catalogue.assign_work(source_id, body.work_id)
        return {"work_id": body.work_id}

    @router.post("/references", status_code=201)
    async def upload_reference(
        file: Annotated[UploadFile, File()],
        actor_id: Annotated[str | None, Form()] = None,
        role_id: Annotated[str | None, Form()] = None,
    ):
        from .uploads import save_upload

        with tempfile.TemporaryDirectory(dir=store.root) as directory:
            path = Path(directory) / "image"
            await save_upload(file, path, 20 * 1024 * 1024)
            return catalogue.add_reference(
                actor_id, path.read_bytes(), role_id=role_id, checker=reference_checker(runtime, store)
            )

    @router.put("/references/{identifier}")
    def reassign_reference(identifier: str, body: ReferencePair):
        return catalogue.update_reference(identifier, body.actor_id, body.entity, body.role_id)

    @router.delete("/references/{identifier}")
    def remove_reference(identifier: str):
        catalogue.reference(identifier)
        store.execute("DELETE FROM person_references WHERE id=?", (identifier,))
        return {"removed": identifier}

    def media(path):
        try:
            return FileResponse(store.resolve_media(path))
        except FileNotFoundError as exc:
            raise HTTPException(404, "图片缺失") from exc

    @router.get("/references/{identifier}/image")
    def reference_image(identifier: str):
        return media(catalogue.reference(identifier)["path"])

    @router.post("/references/from-source", status_code=201)
    def from_source(body: SourceReference):
        source = store.one("SELECT * FROM sources WHERE id=?", (body.source_id,))
        if not source or body.timestamp >= json.loads(source["media"])["duration"]:
            raise ValueError("素材或截图时间无效")
        actor_id = catalogue.pairing_actor(body.actor_id, body.role_id)
        if body.role_id and catalogue.work_for(body.source_id) not in catalogue.entity(body.role_id)["work_ids"]:
            raise ValueError("截图角色不属于素材的作品")
        from .shot_media import ShotMedia
        raw = ShotMedia(store).frame_bytes(body.source_id, body.timestamp)
        if body.bbox:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(raw)) as image:
                buffer = io.BytesIO()
                image.crop(checked_box(body.bbox, *image.size)).save(buffer, format="PNG")
                raw = buffer.getvalue()
        return catalogue.add_reference(
            actor_id,
            raw,
            {"source_id": body.source_id, "timestamp": body.timestamp, "bbox": body.bbox},
            role_id=body.role_id,
            checker=reference_checker(runtime, store),
        )

    @router.post("/imports", status_code=201)
    async def upload_import(files: Annotated[list[UploadFile], File()]):
        from .uploads import save_upload

        if not 1 <= len(files) <= 12:
            raise ValueError("一次上传 1–12 张截图")
        images = []
        with tempfile.TemporaryDirectory(dir=store.root) as directory:
            for i, upload in enumerate(files):
                path = Path(directory) / str(i)
                await save_upload(upload, path, 20 * 1024 * 1024)
                images.append(path.read_bytes())
        return catalogue.create_import(images)

    @router.get("/imports")
    def list_imports():
        return catalogue.list_imports()

    @router.get("/imports/{identifier}")
    def get_import(identifier: str):
        return catalogue.get_import(identifier)

    @router.post("/imports/{identifier}/images")
    async def append_import_images(identifier: str, files: Annotated[list[UploadFile], File()]):
        from .uploads import save_upload

        if not 1 <= len(files) <= 12:
            raise ValueError("一次追加 1–12 张截图")
        raws = []
        with tempfile.TemporaryDirectory(dir=store.root) as directory:
            for i, upload in enumerate(files):
                path = Path(directory) / str(i)
                await save_upload(upload, path, 20 * 1024 * 1024)
                raws.append(path.read_bytes())
        return catalogue.append_images(identifier, raws)

    @router.delete("/imports/{identifier}/images/{image_id}")
    def delete_import_image(identifier: str, image_id: str):
        return catalogue.delete_import_image(identifier, image_id)

    @router.post("/imports/{identifier}/parse")
    def parse(identifier: str, body: ParseImages | None = None):
        draft = catalogue.get_import(identifier)
        if draft["status"] != "draft":
            raise ValueError("只能解析尚未确认的草稿")
        images = {im["id"]: im for im in draft["images"]}
        wanted = body.image_ids if body and body.image_ids else list(images)
        unknown = [i for i in wanted if i not in images]
        if unknown:
            raise ValueError("原图不属于此草稿")
        entries, warnings = list(draft["entries"]), []
        state = draft.get("parse_state") or {}
        for image_id in wanted:
            image = images[image_id]
            number = next(i for i, im in enumerate(draft["images"], 1) if im["id"] == image_id)
            payload = {**image, "raw": store.resolve_media(image["path"]).read_bytes()}
            try:
                parsed = parse_cast(runtime.current(), payload)
            except Exception as exc:  # noqa: BLE001 — 单张失败保留已有条目，不阻塞其他图与手工流程
                state[image_id] = {"status": "failed", "error": str(exc)[:300]}
                warnings.append(f"原图 {number} 自动拆分失败，已有条目保留：{str(exc)[:200]}")
                continue
            entries = [e for e in entries if e["image_id"] != image_id] + parsed["entries"]
            warnings.extend(parsed["warnings"])
            state[image_id] = {"status": "parsed"}
        draft.update({"entries": entries, "warnings": warnings, "parse_state": state})
        store.execute("UPDATE person_imports SET data=? WHERE id=?", (dumps(draft), identifier))
        return catalogue.get_import(identifier)

    @router.put("/imports/{identifier}")
    def save_draft(identifier: str, body: CastConfirm):
        draft = catalogue.get_import(identifier)
        if draft["status"] != "draft":
            raise ValueError("演员表已确认")
        draft["entries"] = [e.model_dump() for e in body.entries]
        draft["work_id"] = body.work_id
        store.execute("UPDATE person_imports SET data=? WHERE id=?", (dumps(draft), identifier))
        return catalogue.get_import(identifier)

    @router.post("/imports/{identifier}/confirm")
    def confirm(identifier: str, body: CastConfirm):
        return catalogue.confirm_import(identifier, body, checker=reference_checker(runtime, store))

    @router.get("/imports/{identifier}/images/{image_id}")
    def import_image(identifier: str, image_id: str):
        image = next((im for im in catalogue.get_import(identifier)["images"] if im["id"] == image_id), None)
        if not image:
            raise HTTPException(404, "找不到原图")
        return media(image["path"])

    @router.post("/sources/{source_id}/process", status_code=202)
    def process(source_id: str, options: PersonOptions):
        return tasks.start(source_id, options)

    @router.get("/tasks/{identifier}")
    def get_task(identifier: str):
        return tasks.get(identifier)

    @router.post("/tasks/{identifier}/retry", status_code=202)
    def retry(identifier: str):
        return tasks.retry(identifier)

    @router.delete("/tasks/{identifier}")
    def delete_task(identifier: str):
        return tasks.delete(identifier)

    @router.get("/evidence")
    def evidence(
        source_id: str | None = None,
        status: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=200),
    ):
        clauses, args = [], []
        for key, value in [("source_id", source_id), ("status", status)]:
            if value:
                clauses.append(key + "=?")
                args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = store.one("SELECT count(*) AS n FROM person_evidence" + where, args)["n"]
        return {
            "items": catalogue.evidence(source_id, status, limit=limit, offset=offset, public=True),
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @router.get("/evidence/{identifier}")
    def one_evidence(identifier: str):
        row = store.one("SELECT * FROM person_evidence WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "找不到出镜证据")
        row["data"] = {k: v for k, v in json.loads(row["data"]).items() if k not in {"body_feature", "space"}}
        return {
            **{k: v for k, v in row.items() if k != "stage_id"},
            "image_url": "/api/persons/evidence/" + identifier + "/image",
        }

    @router.get("/evidence/{identifier}/image")
    def evidence_image(identifier: str):
        row = store.one("SELECT * FROM person_evidence WHERE id=?", (identifier,))
        candidates = []
        if row:
            candidates.append({**row, "data": json.loads(row["data"])})
            path = candidates[0]["data"].get("image_path")
            if path:
                try:
                    return media(path)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
        # Old releases are immutable; use current evidence first, then their original image/shot.
        for version in store.rows("SELECT data FROM person_versions ORDER BY created_at DESC,id"):
            candidates.extend(e for e in json.loads(version["data"]).get("evidence", [])
                              if e["id"] == identifier)
        for evidence in candidates:
            path = evidence.get("data", {}).get("image_path")
            if path:
                try:
                    return media(path)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
        for evidence in candidates:
            shot = store.one("SELECT thumbnail FROM segments WHERE id=? AND source_id=?",
                             (evidence.get("shot_id"), evidence.get("source_id")))
            if shot and shot["thumbnail"]:
                try:
                    return media(shot["thumbnail"])
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
        raise HTTPException(404, "证据截图缺失")

    @router.get("/review/shots")
    def review_shots(
        source_id: str | None = None,
        status: str | None = None,
        actor_id: str | None = None,
        role_id: str | None = None,
        no_candidate: bool = False,
        compact: bool = False,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return catalogue.review_shots(
            source_id, status, limit=limit, offset=offset,
            actor_id=actor_id, role_id=role_id, no_candidate=no_candidate, compact=compact,
        )

    @router.get("/review/shots/{shot_id}")
    def one_review_shot(shot_id: str, source_id: str | None = None):
        result = catalogue.review_shots(source_id, shot_id=shot_id, limit=1)
        if not result["items"]:
            raise HTTPException(404, "找不到人物出镜审核分镜")
        return result["items"][0]

    @router.post("/review/shot")
    def review_shot(body: ShotReview):
        return catalogue.shot_review(body)

    @router.post("/publish")
    def publish(body: Publish):
        with ProcessingLock(runtime.current()), index_write_lock(store.root):
            return catalogue.publish(body.content_id)

    @router.post("/releases/{identifier}/activate")
    def activate_release(identifier: str):
        row = store.one("SELECT * FROM search_releases WHERE id=?", (identifier,))
        if not row:
            raise ValueError("找不到检索发布记录")
        with ProcessingLock(runtime.current()), index_write_lock(store.root):
            store.activate_release(identifier)
        return row

    @router.delete("/releases/{identifier}")
    def delete_release(identifier: str):
        with ProcessingLock(runtime.current()), index_write_lock(store.root):
            return catalogue.delete_release(identifier)

    return router
