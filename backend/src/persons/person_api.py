"""人物库、出镜证据与处理任务的 REST 接口。"""

from __future__ import annotations

import base64
import io
import json
import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from filelock import Timeout
from PIL import Image
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from admin.source_tasks import IndexTasks
from models.adapters import ModelError, request
from models.qwen import analysis_options
from persons.person_models import (
    CastConfirm,
    CastEntry,
    Entity,
    PersonHasLinkedShots,
    ShotReview,
    checked_box,
)
from persons.person_processing import (
    EvidenceReferences,
    PersonClient,
    PersonOptions,
    PersonTasks,
    cosine,
    ensure_reference_quality,
    face_match,
)
from persons.person_thumbnails import import_thumbnail
from persons.persons import Persons
from persons.profiling import note as profiling_note
from persons.profiling import profiled, stage
from shared.config import StrictModel, fingerprint
from shared.images import image_png
from shared.processing_lock import ProcessingLock
from shared.shot_media import ShotMedia
from shared.storage import dumps
from shared.uploads import save_upload

log = logging.getLogger(__name__)


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


IDENTIFY_MAX_BYTES = 20 * 1024 * 1024


def _ranked(observation, references, key, person_config):
    """按 key（role_id / actor_id）分组取最大余弦，返回 [(score, entity_id, reference)] 降序。

    参考图质量门槛与匹配流程（face_match / matched_reference）一致；同分时按 ID 倒序，
    与 face_match 的排序保持一致，使首位角色与 accepted_role 对得上。
    """
    best = {}
    for reference in references:
        identifier = reference.get(key)
        if not identifier or reference.get("face_quality", 0) < person_config.min_reference_quality:
            continue
        score = cosine(observation.get("face_feature"), reference.get("face_feature"))
        current = best.get(identifier)
        if (
            current is None
            or score > current[0]
            or (score == current[0] and reference["id"] < current[1]["id"])
        ):
            best[identifier] = (score, reference)
    return sorted(((s, i, r) for i, (s, r) in best.items()), key=lambda x: (x[0], x[1]), reverse=True)


def identify_faces(observations, references, entities, person_config, top_k):
    """对每张有特征的人脸分别按角色、按演员给出前 top_k 个候选；纯计算，不调用模型。

    是否达到接受条件沿用自动确认规则：人脸质量 ≥ min_face_quality、相似度 ≥ face_threshold，
    且领先第二名至少 face_margin（因此只有首位可能被接受）。
    """
    faces = []
    for observation in observations:
        if observation.get("face_feature") is None:
            continue
        quality = observation.get("face_quality", 0)
        face = {"face_bbox": observation.get("face_bbox"), "face_quality": quality}
        for field, key in (("roles", "role_id"), ("actors", "actor_id")):
            ranked = _ranked(observation, references, key, person_config)
            candidates = []
            for index, (score, identifier, reference) in enumerate(ranked[:top_k]):
                runner = max((s for j, (s, _, _) in enumerate(ranked) if j != index), default=-1.0)
                entity = entities.get(identifier) or {}
                candidates.append(
                    {
                        "id": identifier,
                        "name": entity.get("name"),
                        "kind": entity.get("kind", "role" if key == "role_id" else "actor"),
                        "score": round(score, 4),
                        "accepted": bool(
                            quality >= person_config.min_face_quality
                            and score >= person_config.face_threshold
                            and score - runner >= person_config.face_margin
                        ),
                        "reference_id": reference["id"],
                        "reference_url": f"/api/persons/references/{reference['id']}/image",
                    }
                )
            face[field] = candidates
        role, score, accepted = face_match(observation, references, person_config)
        face["accepted_role"] = (
            {"id": role, "name": (entities.get(role) or {}).get("name"), "score": round(score, 4)}
            if accepted
            else None
        )
        faces.append(face)
    return faces


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
    def listing(compact: bool = False):
        names = store.source_names()
        # 一次取齐素材→作品映射；只有尚未登记作品的素材才走 work_for（会创建默认作品）。
        works = {
            r["source_id"]: r["work_id"] for r in store.rows("SELECT source_id,work_id FROM person_sources")
        }
        sources = [
            {**s, "name": names[s["id"]], "work_id": works.get(s["id"]) or catalogue.work_for(s["id"])}
            for s in store.sources()
        ]
        # 待核对数量以分镜为单位，且与审核列表同一套分类规则。
        return {
            "entities": catalogue.entities(),
            "references": catalogue.references(),
            "sources": [{"id": s["id"], "name": s["name"], "work_id": s["work_id"]} for s in sources],
            "review_shots": catalogue.review_summary() if not compact else {},
            # 精简模式只给每个素材的覆盖分镜数（页面只显示计数），不逐行下发覆盖记录。
            **(
                {
                    "coverage_counts": {
                        r["source_id"]: r["n"]
                        for r in store.rows(
                            "SELECT source_id,COUNT(*) AS n FROM person_coverage GROUP BY source_id"
                        )
                    }
                }
                if compact
                else {
                    "coverage": [
                        {**r, "data": json.loads(r["data"])}
                        for r in store.rows("SELECT * FROM person_coverage")
                    ]
                }
            ),
            "tasks": tasks.recent(30),
        }

    @router.get("/releases")
    def releases():
        """检索组合列表与当前生效组合；设置页「索引版本」使用。

        active 与创作召回签名读取的是同一来源（store.active_index）：内容索引 ID
        与检索发布版 ID。检索发布版由「内容索引 + 人物快照」决定，二者任一变化，
        检索发布版 ID 都会变化；当前生效指针与内容索引不匹配时 release_id 为 None。
        """
        active = store.active_index()
        return {
            "active": {
                "content_id": active["id"],
                "release_id": active.get("search_release_id"),
                "person_id": active.get("person_version"),
            }
            if active
            else None,
            "releases": store.rows("SELECT * FROM search_releases ORDER BY rowid DESC LIMIT 30"),
        }

    @router.get("/filters")
    def filters():
        # Search selectors need identity relationships, not evidence, task logs,
        # reference images or frame coverage payloads.
        sources = store.rows(
            "SELECT s.id,COALESCE(c.display_name,s.name) AS name,p.work_id FROM sources s "
            "LEFT JOIN source_curation c ON c.id=s.id "
            "LEFT JOIN person_sources p ON p.source_id=s.id "
            "WHERE COALESCE(c.hidden,0)=0 ORDER BY s.name,s.id"
        )
        for source in sources:
            source["work_id"] = source["work_id"] or fingerprint(["work", source["id"]])
        return {
            "entities": catalogue.entities("actor") + catalogue.entities("role"),
            "sources": sources,
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

    def media(path, headers=None):
        try:
            # 延迟导入：保持异常捕获范围，依赖加载失败时仍走原有降级路径。
            from shared.media_delivery import materialized_response

            return materialized_response(store.materialize_media(path), headers=headers)
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
        if (
            body.role_id
            and catalogue.work_for(body.source_id) not in catalogue.entity(body.role_id)["work_ids"]
        ):
            raise ValueError("截图角色不属于素材的作品")

        raw = ShotMedia(store).frame_bytes(body.source_id, body.timestamp)
        if body.bbox:
            # 延迟导入：媒体、数值或服务运行依赖仅在实际执行该功能时加载。

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
            with store.materialize_media(image["path"]) as path:
                payload = {**image, "raw": path.read_bytes()}
            try:
                parsed = parse_cast(runtime.current(), payload)
            except Exception as exc:  # 单张失败保留已有条目，不阻塞其他图与手工流程
                log.warning("演员表原图 %s 自动拆分失败", image_id, exc_info=True)
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
    def import_image(identifier: str, image_id: str, thumbnail: bool = False):
        image = next((im for im in catalogue.get_import(identifier)["images"] if im["id"] == image_id), None)
        if not image:
            raise HTTPException(404, "找不到原图")
        if thumbnail:
            try:
                path = import_thumbnail(store, image)
            except FileNotFoundError as exc:
                raise HTTPException(404, "图片缺失") from exc
            return FileResponse(
                path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"}
            )
        return media(image["path"])

    @router.post("/sources/{source_id}/process", status_code=202)
    def process(source_id: str, options: PersonOptions):
        return tasks.start(source_id, options)

    def identify_image(path, top_k):

        config = runtime.current()
        raw = image_png(path, config.video.max_pixels)
        normalized = path.with_name("query.png")
        normalized.write_bytes(raw)
        with Image.open(normalized) as image:
            size = list(image.size)
        # 请求超时用人物识别档案配置的值（设置页可改）。
        client = PersonClient(config)
        space = client.identity()
        extracted = client.image(normalized)
        if extracted.get("space") != space:
            raise ModelError("特征提取响应空间与任务固定空间不符")
        resolver = EvidenceReferences(config, store)
        references = resolver.candidates(space)
        entities = {e["id"]: e for e in catalogue.entities()}
        return {
            "faces": identify_faces(
                extracted.get("observations", []), references, entities, config.person, top_k
            ),
            "image_size": size,
            "references_total": len(resolver.references),
            # 没有当前空间特征缓存（或缓存不合格）的参考图不参与比对；重新处理人物或重新添加后才覆盖。
            "uncovered_references": len(resolver.references) - len(references),
            "space_model": space.get("model"),
        }

    @router.post("/identify")
    async def identify(
        file: Annotated[UploadFile, File()],
        top_k: Annotated[int, Form(ge=1, le=20)] = 5,
    ):
        """上传一张图片识别画面中的演员与角色；只读，只比对已缓存的参考图特征。"""

        with tempfile.TemporaryDirectory(dir=store.root) as directory:
            path = Path(directory) / "upload"
            await save_upload(file, path, IDENTIFY_MAX_BYTES)
            return await run_in_threadpool(identify_image, path, top_k)

    @router.get("/tasks/{identifier}")
    def get_task(identifier: str):
        return tasks.get(identifier)

    @router.post("/tasks/{identifier}/retry", status_code=202)
    def retry(identifier: str):
        return tasks.retry(identifier)

    @router.post("/tasks/{identifier}/cancel")
    def cancel_task(identifier: str):
        return tasks.cancel(identifier)

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
        clauses.append(catalogue.VALID_ROLE)
        where = " WHERE " + " AND ".join(clauses)
        total = store.one("SELECT count(*) AS n FROM person_evidence e" + where, args)["n"]
        return {
            "items": catalogue.evidence(source_id, status, limit=limit, offset=offset, public=True),
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @router.get("/evidence/{identifier}")
    def one_evidence(identifier: str):
        row = store.one(
            "SELECT * FROM person_evidence e WHERE id=? AND " + catalogue.VALID_ROLE, (identifier,)
        )
        if not row:
            raise HTTPException(404, "找不到出镜证据")
        row["data"] = {k: v for k, v in json.loads(row["data"]).items() if k not in {"body_feature", "space"}}
        return {
            **{k: v for k, v in row.items() if k != "stage_id"},
            "image_url": "/api/persons/evidence/" + identifier + "/image",
        }

    @router.get("/evidence/{identifier}/image")
    def evidence_image(identifier: str):
        with profiled("evidence_image") as profile:
            result = _evidence_image(identifier)
            if profile:
                profile.apply(result)
            return result

    # 同一证据 id 的截图在复核后可能换帧，所以只缓存很短时间，
    # 既省掉翻页回看时的重复请求，又不会长时间显示旧图。
    EVIDENCE_IMAGE_HEADERS = {"Cache-Control": "private, max-age=300"}

    def evidence_media(path):
        """证据截图都落在 persons/ 下，不会是需要现场重建的 moment/scene 媒体引用。

        文件不在磁盘时 store.resolve_media 会继续扫描整张 index_members（十万行 JSON，约 3 秒）
        才抛 FileNotFoundError；这里先确认文件存在，缺失就直接按 404 处理。
        """
        if str(path).startswith("persons/"):
            resolved = (store.root / path).resolve()
            if resolved.is_relative_to(store.root) and not resolved.is_file():
                raise HTTPException(404, "图片缺失")
        return media(path, EVIDENCE_IMAGE_HEADERS)

    def _evidence_image(identifier: str):
        with stage("row_lookup"):
            row = store.one("SELECT * FROM person_evidence WHERE id=?", (identifier,))
        candidates = []
        if row:
            candidates.append({**row, "data": json.loads(row["data"])})
            path = candidates[0]["data"].get("image_path")
            if path:
                try:
                    with stage("file_response"):
                        result = evidence_media(path)
                    profiling_note("branch", "current_path")
                    return result
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
        # Old releases are immutable; use current evidence first, then their original image/shot.
        # 历史版本按新到旧惰性遍历：每个版本的“证据 id -> 截图路径”索引只解析一次并缓存。
        with stage("versions_scan"):
            for evidence in catalogue.version_evidence(identifier):
                candidates.append(evidence)
                path = evidence["data"].get("image_path")
                if path:
                    try:
                        result = evidence_media(path)
                        profiling_note("branch", "version_path")
                        return result
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
        with stage("thumbnail_fallback"):
            for evidence in candidates:
                shot = store.one(
                    "SELECT thumbnail FROM segments WHERE id=? AND source_id=?",
                    (evidence.get("shot_id"), evidence.get("source_id")),
                )
                if shot and shot["thumbnail"]:
                    try:
                        result = media(shot["thumbnail"], EVIDENCE_IMAGE_HEADERS)
                        profiling_note("branch", "shot_thumbnail")
                        return result
                    except HTTPException as exc:
                        if exc.status_code != 404:
                            raise
        profiling_note("branch", "missing")
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
        response: Response = None,
    ):
        with profiled("review_shots", response):
            return catalogue.review_shots(
                source_id,
                status,
                limit=limit,
                offset=offset,
                actor_id=actor_id,
                role_id=role_id,
                no_candidate=no_candidate,
                compact=compact,
            )

    @router.get("/review/shots/{shot_id}")
    def one_review_shot(shot_id: str, source_id: str | None = None, response: Response = None):
        with profiled("one_review_shot", response):
            result = catalogue.review_shots(source_id, shot_id=shot_id, limit=1)
        if not result["items"]:
            raise HTTPException(404, "找不到人物出镜审核分镜")
        return result["items"][0]

    @router.post("/review/shot")
    def review_shot(body: ShotReview):
        return catalogue.shot_review(body)

    @contextmanager
    def release_write():
        try:
            with ProcessingLock(runtime.current()):
                yield
        except Timeout as exc:
            raise HTTPException(409, "处理锁被占用，请稍后重试人物发布操作") from exc

    @router.post("/publish")
    def publish(body: Publish):
        if runtime.current().queue.backend == "postgres":
            return IndexTasks(runtime, store).run_here(
                {"action": "person-publish", "index_id": body.content_id}
            )
        with release_write():
            return catalogue.publish(body.content_id)

    @router.post("/releases/{identifier}/activate")
    def activate_release(identifier: str):
        row = store.one("SELECT * FROM search_releases WHERE id=?", (identifier,))
        if not row:
            raise ValueError("找不到检索发布记录")
        if runtime.current().queue.backend == "postgres":
            IndexTasks(runtime, store).run_here({"action": "activate-release", "release_id": identifier})
            return row
        with release_write():
            store.activate_release(identifier)
        return row

    @router.delete("/releases/{identifier}")
    def delete_release(identifier: str):
        with release_write():
            return catalogue.delete_release(identifier)

    return router
