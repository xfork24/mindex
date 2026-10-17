"""Person catalogue, reviewable evidence and immutable retrieval releases.

Names never establish identity. Actors and work-scoped roles have separate IDs;
only accepted evidence is eligible for onscreen retrieval.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import uuid
from typing import Literal

from PIL import Image, ImageOps
from pydantic import Field, model_validator

from .config import StrictModel, fingerprint
from .storage import Store, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS person_entities(id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_sources(source_id TEXT PRIMARY KEY REFERENCES sources(id), work_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_references(id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, stage_id TEXT,
 path TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_imports(id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft');
CREATE TABLE IF NOT EXISTS person_features(id TEXT PRIMARY KEY, source_id TEXT, space TEXT NOT NULL,
 kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS person_features_source ON person_features(source_id,kind,space);
CREATE TABLE IF NOT EXISTS person_evidence(id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
 shot_id TEXT NOT NULL, actor_id TEXT, role_id TEXT, stage_id TEXT, start REAL NOT NULL, end REAL NOT NULL,
 status TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS person_evidence_source ON person_evidence(source_id,status,start,end);
CREATE TABLE IF NOT EXISTS person_coverage(source_id TEXT NOT NULL, cache_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_tasks(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, status TEXT NOT NULL,
 config TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}', error TEXT, result TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS person_versions(id TEXT PRIMARY KEY, data TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS search_releases(id TEXT PRIMARY KEY, content_id TEXT NOT NULL,
 person_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""


def init_person_schema(db):
    db.executescript(SCHEMA)
    # Keep the retired SQL columns for old databases/checkpoints only. They are
    # never accepted as configuration or exposed as person identities again.
    marker = "migration.person-roles-v2"
    db.execute("BEGIN IMMEDIATE")
    if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
        return
    entries = {
        r["id"]: {"id": r["id"], **json.loads(r["data"])} for r in db.execute("SELECT * FROM person_entities")
    }
    for identifier, entry in entries.items():
        value = normalize_entity(entry, entries)
        db.execute(
            "UPDATE person_entities SET kind=?,data=? WHERE id=?",
            (
                value["kind"],
                dumps({k: v for k, v in value.items() if k != "id"}),
                identifier,
            ),
        )
    db.execute(
        "UPDATE person_evidence SET role_id=COALESCE(stage_id,role_id),stage_id=NULL WHERE stage_id IS NOT NULL"
    )
    db.execute("UPDATE person_references SET stage_id=NULL WHERE stage_id IS NOT NULL")
    db.execute("INSERT INTO settings(key,value) VALUES(?, 'complete')", (marker,))


class Entity(StrictModel):
    """演员与作品角色是不同身份：演员跨作品共用；角色可归属一部或多部作品（跨电影角色）。"""

    kind: Literal["actor", "work", "role"]
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    work_ids: list[str] = Field(default_factory=list, max_length=50)
    actor_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def clean(self):
        self.name = self.name.strip()
        self.aliases = list(dict.fromkeys(x.strip() for x in self.aliases if x.strip()))
        if not self.name:
            raise ValueError("姓名不能为空")
        if self.kind == "role":
            self.work_ids = list(dict.fromkeys(self.work_ids))
            if not self.work_ids:
                raise ValueError("角色至少归属一部作品；跨电影角色可关联多部作品")
        else:
            self.work_ids = []
        return self


class CastEntry(StrictModel):
    """Cast rows carry identity only; staged characters are roles named like 老年叶淑柔."""

    image_id: str
    actor_name: str = Field(default="", max_length=200)
    foreign_name: str = ""
    role_name: str = ""
    actor_id: str | None = None
    role_id: str | None = None
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    # User must explicitly approve a single-person crop. Group photos are never accepted by guessing.
    single_person: bool = False

    @model_validator(mode="after")
    def identity_present(self):
        self.actor_name = self.actor_name.strip()
        self.foreign_name = self.foreign_name.strip()
        self.role_name = self.role_name.strip()
        if not self.actor_name and not self.actor_id:
            raise ValueError("请填写演员名或关联已有演员")
        return self


class CastConfirm(StrictModel):
    work_id: str
    entries: list[CastEntry]


class Review(StrictModel):
    """Internal per-observation correction used by cache validation and integrations."""

    actor_id: str | None = None
    role_id: str | None = None
    accept: bool = True
    reason: str = Field(min_length=1, max_length=1000)


class ShotSelection(StrictModel):
    """勾选一个出镜身份：演员 + 该作品中的可选角色。"""

    actor_id: str
    role_id: str | None = None


class ShotReview(StrictModel):
    source_id: str
    shot_id: str
    selections: list[ShotSelection] = Field(default_factory=list, max_length=50)


def uid():
    return uuid.uuid4().hex


def checked_box(box, width, height):
    if len(box) != 4:
        raise ValueError("照片区域必须有四个原图坐标")
    x1, y1, x2, y2 = box
    if not all(math.isfinite(x) for x in box) or not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("照片区域超出原图或坐标无效")
    result = tuple(round(x) for x in box)
    if result[2] <= result[0] or result[3] <= result[1]:
        raise ValueError("照片区域太小")
    return result


def normalize_entity(entry, lookup=None):
    """Normalize legacy rows and immutable snapshots without dropping identities."""
    entry = dict(entry)
    if entry.get("kind") == "stage":
        parent = (lookup or {}).get(entry.get("role_id"), {})
        entry.update(
            kind="role",
            work_ids=parent.get("work_ids", [parent["work_id"]] if parent.get("work_id") else []),
            actor_ids=entry.get("actor_ids") or parent.get("actor_ids", []),
        )
    for key in ("role_id", "age", "styling", "costume"):
        entry.pop(key, None)
    if entry.get("kind") == "role" and "work_ids" not in entry:
        entry["work_ids"] = [entry["work_id"]] if entry.get("work_id") else []
        entry.pop("work_id", None)
    return entry


class Persons:
    def __init__(self, store: Store):
        self.store = store

    def entity(self, identifier, kind=None):
        row = self.store.one("SELECT * FROM person_entities WHERE id=?", (identifier,))
        if not row or (kind and row["kind"] != kind):
            raise ValueError("找不到对应人物、作品或角色")
        return normalize_entity({"id": row["id"], **json.loads(row["data"])})

    def entities(self, kind=None):
        return [
            normalize_entity({"id": r["id"], **json.loads(r["data"])})
            for r in self.store.rows(
                "SELECT * FROM person_entities" + (" WHERE kind=?" if kind else "") + " ORDER BY rowid",
                (kind,) if kind else (),
            )
        ]

    def validate_entity(self, value):
        if value.kind == "role":
            for work in value.work_ids:
                self.entity(work, "work")
        for actor in value.actor_ids:
            self.entity(actor, "actor")

    def save(self, value: Entity, identifier=None):
        if identifier:
            old = self.entity(identifier)
            if old["kind"] != value.kind:
                raise ValueError("不能变更实体类型")
            if value.kind == "role":
                removed = set(old.get("work_ids", [])) - set(value.work_ids)
                if removed:
                    # Dropping a work invalidates role attribution of its evidence.
                    sources = {
                        r["source_id"]
                        for r in self.store.rows(
                            "SELECT source_id FROM person_sources WHERE work_id IN "
                            + "("
                            + ",".join("?" * len(removed))
                            + ")",
                            tuple(removed),
                        )
                    }
                    if sources and self.store.one(
                        "SELECT 1 FROM person_evidence WHERE role_id=? AND status='accepted' AND source_id IN "
                        + "("
                        + ",".join("?" * len(sources))
                        + ") LIMIT 1",
                        (identifier, *sources),
                    ):
                        raise ValueError("该作品下仍有已接受的出镜证据；请先重新核对或删除")
        self.validate_entity(value)
        identifier = identifier or uid()
        self.store.execute(
            "INSERT OR REPLACE INTO person_entities VALUES(?,?,?)",
            (identifier, value.kind, value.model_dump_json()),
        )
        return self.entity(identifier)

    def work_for(self, source_id):
        source = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if not source:
            raise ValueError("找不到素材")
        row = self.store.one("SELECT work_id FROM person_sources WHERE source_id=?", (source_id,))
        if row:
            return row["work_id"]
        work_id = fingerprint(["work", source_id])
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO person_entities VALUES(?,?,?)",
                (work_id, "work", Entity(kind="work", name=source["name"]).model_dump_json()),
            )
            db.execute("INSERT OR IGNORE INTO person_sources VALUES(?,?)", (source_id, work_id))
        return self.store.one("SELECT work_id FROM person_sources WHERE source_id=?", (source_id,))["work_id"]

    def assign_work(self, source_id, work_id):
        previous = self.work_for(source_id)
        self.entity(work_id, "work")
        if previous == work_id:
            return
        # Old evidence encodes the former work/role relationship and must be reviewed/reprocessed.
        with self.store.connect() as db:
            db.execute("UPDATE person_sources SET work_id=? WHERE source_id=?", (work_id, source_id))
            db.execute("DELETE FROM person_coverage WHERE source_id=?", (source_id,))
            db.execute("DELETE FROM person_evidence WHERE source_id=?", (source_id,))

    def image(self, raw, namespace="references"):
        with Image.open(io.BytesIO(raw)) as original:
            if original.width * original.height > 40_000_000:
                raise ValueError("图片尺寸过大")
            image = ImageOps.exif_transpose(original).convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        content = buffer.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        relative = f"persons/{namespace}/{digest}.png"
        path = self.store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": relative, "digest": digest, "width": image.width, "height": image.height}

    def add_reference(self, actor_id, raw, provenance=None):
        self.entity(actor_id, "actor")
        item = self.image(raw)
        existing = self.store.one(
            "SELECT id FROM person_references WHERE actor_id=? AND digest=? ORDER BY rowid LIMIT 1",
            (actor_id, item["digest"]),
        )
        identifier = existing["id"] if existing else fingerprint([actor_id, item["digest"]])
        self.store.execute(
            "INSERT OR IGNORE INTO person_references VALUES(?,?,?,?,?,?)",
            (identifier, actor_id, None, item["path"], item["digest"], dumps(provenance or {})),
        )
        return self.reference(identifier)

    def reference(self, identifier):
        row = self.store.one("SELECT * FROM person_references WHERE id=?", (identifier,))
        if not row:
            raise ValueError("找不到参考图")
        return {
            **{k: v for k, v in row.items() if k != "stage_id"},
            "data": json.loads(row["data"]),
            "url": "/api/persons/references/" + identifier + "/image",
        }

    def references(self):
        return [
            self.reference(r["id"])
            for r in self.store.rows("SELECT id FROM person_references ORDER BY rowid")
        ]

    def update_reference(self, identifier, actor_id, entity=None):
        """Save reference pairing and the selected actor's metadata atomically."""
        reference = self.reference(identifier)
        self.entity(actor_id, "actor")
        if entity is not None:
            if entity.kind != "actor":
                raise ValueError("参考图必须配对演员")
            self.validate_entity(entity)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM person_references WHERE id=?", (identifier,)).fetchone():
                raise ValueError("找不到参考图")
            if entity is not None:
                db.execute(
                    "UPDATE person_entities SET data=? WHERE id=?", (entity.model_dump_json(), actor_id)
                )
            duplicate = db.execute(
                "SELECT id FROM person_references WHERE actor_id=? AND digest=? AND id<>? LIMIT 1",
                (actor_id, reference["digest"], identifier),
            ).fetchone()
            if duplicate:
                db.execute("DELETE FROM person_references WHERE id=?", (identifier,))
                identifier = duplicate["id"]
            else:
                db.execute("UPDATE person_references SET actor_id=? WHERE id=?", (actor_id, identifier))
        return self.reference(identifier)

    def create_import(self, images):
        identifier = uid()
        value = {
            "images": [{"id": uid(), **self.image(raw, "imports")} for raw in images],
            "entries": [],
            "warnings": [],
        }
        self.store.execute("INSERT INTO person_imports(id,data) VALUES(?,?)", (identifier, dumps(value)))
        return self.get_import(identifier)

    def get_import(self, identifier):
        row = self.store.one("SELECT * FROM person_imports WHERE id=?", (identifier,))
        if not row:
            raise ValueError("找不到演员表草稿")
        value = json.loads(row["data"])
        for image in value["images"]:
            image["url"] = f"/api/persons/imports/{identifier}/images/{image['id']}"
        return {**value, "id": identifier, "status": row["status"]}

    def confirm_import(self, identifier, body: CastConfirm):
        draft = self.get_import(identifier)
        if draft["status"] != "draft":
            raise ValueError("演员表已确认；请在人物库追加或修改参考图")
        if not body.work_id:
            raise ValueError("请选择导入作品")
        self.entity(body.work_id, "work")
        images = {x["id"]: x for x in draft["images"]}
        # Validate and crop every entry before committing catalogue changes.
        crops = []
        for entry in body.entries:
            if entry.image_id not in images:
                raise ValueError("原图不属于此草稿")
            if entry.bbox is not None and not entry.single_person:
                raise ValueError("请确认照片框仅包含一个人物")
            with Image.open(self.store.resolve_media(images[entry.image_id]["path"])) as image:
                box = checked_box(entry.bbox, *image.size) if entry.bbox is not None else None
                buffer = io.BytesIO()
                if box:
                    image.crop(box).save(buffer, format="PNG")
                crops.append(buffer.getvalue() if box else None)
            if entry.actor_id:
                self.entity(entry.actor_id, "actor")
            if entry.role_id:
                self.entity(entry.role_id, "role")
        # One SQLite transaction including references; nested calls use the same connection.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT status FROM person_imports WHERE id=?", (identifier,)).fetchone()["status"]
                != "draft"
            ):
                raise ValueError("演员表已确认")

            class TransactionStore:
                root = self.store.root
                resolve_media = self.store.resolve_media

                def rows(_, sql, args=()):
                    return [dict(row) for row in db.execute(sql, args).fetchall()]

                def one(_, sql, args=()):
                    row = db.execute(sql, args).fetchone()
                    return dict(row) if row else None

                def execute(_, sql, args=()):
                    db.execute(sql, args)

            tx = Persons(TransactionStore())
            confirmed = []
            for entry, crop in zip(body.entries, crops):
                # Import is the primary creation path: reuse exact-name identities, create missing ones.
                actor_id = entry.actor_id
                if not actor_id:
                    matches = [
                        a
                        for a in tx.entities("actor")
                        if a["name"] == entry.actor_name
                        or (entry.foreign_name and entry.foreign_name in a.get("aliases", []))
                    ]
                    if len(matches) > 1:
                        raise ValueError("演员姓名有歧义，请明确关联已有演员")
                    actor_id = matches[0]["id"] if matches else None
                if not actor_id:
                    actor_id = tx.save(
                        Entity(
                            kind="actor",
                            name=entry.actor_name,
                            aliases=[entry.foreign_name] if entry.foreign_name else [],
                        )
                    )["id"]
                role_id = entry.role_id or (
                    next(
                        (
                            r["id"]
                            for r in tx.entities("role")
                            if r["name"] == entry.role_name and body.work_id in r.get("work_ids", [])
                        ),
                        None,
                    )
                    if entry.role_name
                    else None
                )
                if role_id:
                    # Linking (or name-reusing) a role unions actors and works; identity is never rebuilt.
                    role = tx.entity(role_id)
                    tx.save(
                        Entity.model_validate({k: v for k, v in role.items() if k != "id"}).model_copy(
                            update={
                                "actor_ids": list(dict.fromkeys([*role["actor_ids"], actor_id])),
                                "work_ids": list(dict.fromkeys([*role.get("work_ids", []), body.work_id])),
                            }
                        ),
                        role_id,
                    )
                elif entry.role_name:
                    role_id = tx.save(
                        Entity(
                            kind="role", name=entry.role_name, work_ids=[body.work_id], actor_ids=[actor_id]
                        )
                    )["id"]
                reference = (
                    tx.add_reference(
                        actor_id,
                        crop,
                        {"import_id": identifier, "image_id": entry.image_id, "bbox": entry.bbox},
                    )
                    if crop
                    else None
                )
                confirmed.append(
                    {
                        "actor_id": actor_id,
                        "role_id": role_id,
                        "reference_id": reference["id"] if reference else None,
                    }
                )
            db.execute(
                "UPDATE person_imports SET status='confirmed',data=? WHERE id=? AND status='draft'",
                (
                    dumps(
                        {**draft, "confirmed": confirmed, "entries": [e.model_dump() for e in body.entries]}
                    ),
                    identifier,
                ),
            )
        return confirmed

    def review(self, evidence_id, body: Review):
        """Correct one cached observation without expanding its temporal interval.

        The public review UI/API uses shot_review; keep this operation internal.
        """
        row = self.store.one("SELECT * FROM person_evidence WHERE id=?", (evidence_id,))
        if not row:
            raise ValueError("找不到出镜证据")
        actor_id = body.actor_id or row["actor_id"]
        if body.accept:
            self.entity(actor_id, "actor")
            if body.role_id:
                role = self.entity(body.role_id, "role")
                if (
                    self.work_for(row["source_id"]) not in role["work_ids"]
                    or actor_id not in role["actor_ids"]
                ):
                    raise ValueError("演员与作品角色关系不符")
        data = json.loads(row["data"]) | {
            "evidence_type": "manual",
            "confirmation": body.reason,
            "reviewed": True,
        }
        status = "accepted" if body.accept else "rejected"
        self.store.execute(
            "UPDATE person_evidence SET actor_id=?,role_id=?,status=?,data=? WHERE id=?",
            (actor_id, body.role_id, status, dumps(data), evidence_id),
        )
        return {"id": evidence_id, "status": status}

    def shot_review(self, body: ShotReview):
        """分镜级审核：勾选人物各生成一条覆盖整镜的 manual 证据，其余证据行驳回留档。"""
        shot = self.store.one(
            "SELECT * FROM segments WHERE id=? AND source_id=? AND kind='shot'",
            (body.shot_id, body.source_id),
        )
        if not shot:
            raise ValueError("找不到分镜")
        work_id = self.work_for(body.source_id)
        for selection in body.selections:
            self.entity(selection.actor_id, "actor")
            if selection.role_id:
                role = self.entity(selection.role_id, "role")
                if work_id not in role["work_ids"] or selection.actor_id not in role["actor_ids"]:
                    raise ValueError("演员与作品角色关系不符")
        rows = self.store.rows(
            "SELECT * FROM person_evidence WHERE source_id=? AND shot_id=? ORDER BY start,id",
            (body.source_id, body.shot_id),
        )
        images = [json.loads(r["data"]).get("image_path") for r in rows]
        fallback = next((p for p in images if p), shot["thumbnail"])
        marks = {
            (s.actor_id, s.role_id): fingerprint(
                ["shot-review-v1", body.source_id, body.shot_id, s.actor_id, s.role_id]
            )
            for s in body.selections
        }
        accepted = list(marks.values())
        with self.store.connect() as db:
            for (actor_id, role_id), identifier in marks.items():
                image = next((p for r, p in zip(rows, images) if r["actor_id"] == actor_id and p), fallback)
                db.execute(
                    "INSERT OR REPLACE INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        body.source_id,
                        body.shot_id,
                        actor_id,
                        role_id,
                        None,
                        shot["start"],
                        shot["end"],
                        "accepted",
                        dumps(
                            {
                                "evidence_type": "manual",
                                "confirmation": "shot_review",
                                "reviewed": True,
                                "work_id": work_id,
                                "score": 1.0,
                                "image_path": image,
                            }
                        ),
                    ),
                )
            for row in rows:
                if row["id"] in marks.values():
                    continue
                # Rejected rows keep reviewed=True so reprocessing never resurrects them.
                db.execute(
                    "UPDATE person_evidence SET status='rejected',data=? WHERE id=?",
                    (
                        dumps(json.loads(row["data"]) | {"reviewed": True, "confirmation": "shot_review"}),
                        row["id"],
                    ),
                )
        return {
            "shot_id": body.shot_id,
            "accepted": accepted,
            "rejected": sum(r["id"] not in marks.values() for r in rows),
        }

    def review_shots(self, source_id=None, status=None, *, limit=50, offset=0, shot_id=None):
        """按分镜分组返回证据；状态过滤只决定出现哪些分镜，卡内始终携带完整证据。"""
        clauses, args = [], []
        for key, value in [("e.source_id", source_id), ("e.status", status), ("e.shot_id", shot_id)]:
            if value:
                clauses.append(key + "=?")
                args.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self.store.one(
            "SELECT count(*) AS n FROM (SELECT DISTINCT e.shot_id FROM person_evidence e" + where + ")",
            args,
        )["n"]
        shots = self.store.rows(
            "SELECT s.id AS shot_id,s.source_id,s.start,s.end FROM segments s "
            "JOIN person_evidence e ON e.shot_id=s.id AND e.source_id=s.source_id"
            + where
            + " GROUP BY s.id ORDER BY s.source_id,s.start LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        items = []
        for shot in shots:
            rows = [
                {
                    **r,
                    "data": {
                        k: v for k, v in json.loads(r["data"]).items() if k not in {"body_feature", "space"}
                    },
                    "image_url": f"/api/persons/evidence/{r['id']}/image",
                }
                for r in self.store.rows(
                    "SELECT * FROM person_evidence WHERE source_id=? AND shot_id=? ORDER BY start,id",
                    (shot["source_id"], shot["shot_id"]),
                )
            ]
            items.append(
                {
                    **shot,
                    "counts": {
                        key: sum(r["status"] == key for r in rows)
                        for key in ("accepted", "candidate", "rejected")
                    },
                    "image_url": next((r["image_url"] for r in rows if r["data"].get("image_path")), None),
                    "evidence": rows,
                }
            )
        return {"items": items, "total": total}

    def evidence(self, source_id=None, status=None, *, limit=None, offset=0, public=False):
        clauses, args = [], []
        for key, value in [("source_id", source_id), ("status", status)]:
            if value:
                clauses.append(key + "=?")
                args.append(value)
        return [
            {
                **{k: v for k, v in r.items() if k != "stage_id"},
                "data": {
                    k: v
                    for k, v in json.loads(r["data"]).items()
                    if not public or k not in {"body_feature", "space"}
                },
                "image_url": f"/api/persons/evidence/{r['id']}/image",
            }
            for r in self.store.rows(
                "SELECT * FROM person_evidence"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY source_id,start,id"
                + (" LIMIT ? OFFSET ?" if limit is not None else ""),
                [*args, limit, offset] if limit is not None else args,
            )
        ]

    def publish(self, content_id=None):
        active = self.store.active_index()
        content_id = content_id or (active["id"] if active else None)
        if not content_id:
            raise ValueError("请先发布内容索引")
        content = self.store.index_version(content_id)
        if content["status"] != "ready":
            raise ValueError("内容索引尚未就绪")
        # A consistent catalogue/evidence/coverage snapshot, without model calls.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ready = db.execute("SELECT status FROM index_versions WHERE id=?", (content_id,)).fetchone()
            if not ready or ready["status"] != "ready":
                raise ValueError("内容索引尚未就绪")
            entities = [
                {"id": r["id"], **json.loads(r["data"])}
                for r in db.execute("SELECT * FROM person_entities ORDER BY id")
            ]
            evidence = [
                {**dict(r), "data": json.loads(r["data"])}
                for r in db.execute("SELECT * FROM person_evidence WHERE status='accepted' ORDER BY id")
            ]
            coverage = [
                dict(r) | {"data": json.loads(r["data"])}
                for r in db.execute("SELECT * FROM person_coverage ORDER BY cache_id")
            ]
            value = {"format": 1, "entities": entities, "evidence": evidence, "coverage": coverage}
            version = fingerprint(value)
            db.execute("INSERT OR IGNORE INTO person_versions(id,data) VALUES(?,?)", (version, dumps(value)))
            release = fingerprint([content_id, version])
            db.execute(
                "INSERT OR IGNORE INTO search_releases(id,content_id,person_id) VALUES(?,?,?)",
                (release, content_id, version),
            )
            if db.execute(
                "SELECT 1 FROM index_members WHERE index_id=? AND complete=0", (content_id,)
            ).fetchone():
                raise ValueError("内容索引仍有未完成片段")
            db.execute("INSERT OR REPLACE INTO settings VALUES('active',?)", (content_id,))
            db.execute("INSERT OR REPLACE INTO settings VALUES('active.release',?)", (release,))
        return {"release_id": release, "content_id": content_id, "person_version": version}

    def published(self, content_id, *, pinned=False, release_id=None):
        if release_id:
            release = self.store.one(
                "SELECT * FROM search_releases WHERE id=? AND content_id=?", (release_id, content_id)
            )
        elif pinned:
            release = self.store.one(
                "SELECT * FROM search_releases WHERE content_id=? ORDER BY rowid DESC LIMIT 1", (content_id,)
            )
        else:
            release = self.store.one(
                "SELECT r.* FROM search_releases r JOIN settings s ON s.value=r.id WHERE s.key='active.release' AND r.content_id=?",
                (content_id,),
            )
        row = (
            self.store.one("SELECT data FROM person_versions WHERE id=?", (release["person_id"],))
            if release and release["person_id"]
            else None
        )
        value = json.loads(row["data"]) if row else {"entities": [], "evidence": [], "coverage": []}
        lookup = {e["id"]: e for e in value["entities"]}
        value["entities"] = [normalize_entity(e, lookup) for e in value["entities"]]
        for evidence in value["evidence"]:
            evidence["role_id"] = evidence.pop("stage_id", None) or evidence["role_id"]
        return release, value


KIND_LABELS = {"actor": "演员", "work": "作品", "role": "角色"}


def _occurrences(name, text):
    # Avoid matching Ann inside Anna; Chinese registered names may form phrases.
    pattern = re.escape(name)
    if re.fullmatch(r"[\w\s.-]+", name, flags=re.ASCII):
        pattern = r"(?<!\w)" + pattern + r"(?!\w)"
    return list(re.finditer(pattern, text, flags=re.IGNORECASE))


def _fuzzy_union(pairs, base):
    """Distinct full role names sharing a typed suffix form a union; duplicate names stay ambiguous."""
    names = [name.casefold() for _, name in pairs]
    return (
        len(names) > 1
        and len(set(names)) == len(names)
        and all(name.endswith(base.casefold()) for name in names)
    )


def resolve_names(query, entities):
    """Resolve typed names to identities; returns (ids, recognized, remaining, variant_groups).

    Roles also match by significant suffix so 叶淑柔 recalls a role named 老年叶淑柔;
    such variants form one union group (any member counts), never an ambiguity error.
    Explicit person_ids selections stay precise singletons.
    """

    lookup = {e["id"]: e for e in entities}
    explicit = list(dict.fromkeys(query.person_ids))
    for identifier in explicit:
        if identifier not in lookup or lookup[identifier]["kind"] == "work":
            raise ValueError("请选择已发布人物版本中的演员或角色")
    matches = []
    for e in entities:
        if e["kind"] == "work":
            continue
        for name in [e["name"], *e.get("aliases", [])]:
            for m in _occurrences(name, query.text):
                matches.append((m.start(), m.end(), e["id"], name))
    # Suffix recall: a role whose full name is absent can still match its base name.
    for e in entities:
        if e["kind"] != "role":
            continue
        for name in [e["name"], *e.get("aliases", [])]:
            if _occurrences(name, query.text):
                continue
            shortest = max(2, (len(name) + 1) // 2)
            for length in range(len(name) - 1, shortest - 1, -1):
                found = _occurrences(name[-length:], query.text)
                if found:
                    m = found[0]
                    matches.append((m.start(), m.end(), e["id"], name[-length:]))
                    break
    # Longest registered name wins over contained names (老年叶淑柔 vs 叶淑柔).
    matches = [
        m
        for m in matches
        if not any(a <= m[0] and b >= m[1] and b - a > m[1] - m[0] for a, b, _, _ in matches)
    ]
    groups = {}
    for a, b, identifier, name in matches:
        groups.setdefault((a, b), []).append((identifier, name))
    recognized = []
    variant_groups = []
    for (a, b), group in sorted(groups.items()):
        options = list(dict.fromkeys(i for i, _ in group))
        selected = [i for i in options if i in explicit]
        union = (
            len(options) > 1
            and all(lookup[i]["kind"] == "role" for i in options)
            and _fuzzy_union([(i, lookup[i]["name"]) for i in options], query.text[a:b])
            and not selected
        )
        if len(options) > 1 and len(selected) != 1 and not union:
            raise ValueError(
                "姓名有歧义，请选择具体身份（演员与角色是不同身份）："
                + "、".join(
                    f"{KIND_LABELS.get(lookup[i]['kind'], lookup[i]['kind'])} {lookup[i]['name']} ({i})"
                    for i in options
                )
            )
        members = selected or options
        for identifier in members:
            if identifier not in explicit:
                explicit.append(identifier)
            recognized.append(
                {
                    "id": identifier,
                    "name": lookup[identifier]["name"],
                    "kind": lookup[identifier]["kind"],
                    "span": [a, b],
                }
            )
        variant_groups.append(members)
    remaining = query.text
    for a, b in sorted(groups, reverse=True):
        remaining = remaining[:a] + " " + remaining[b:]
    from_text = {i for g in variant_groups for i in g}
    singletons = [[i] for i in explicit if i not in from_text]
    return explicit, recognized, remaining.strip(" ，,。的与和及、"), [*singletons, *variant_groups]


def overlaps_for(segment, evidence, identifiers, relation):
    """identifiers are ids or variant groups (lists of ids); any member hits its group."""

    groups = [
        [
            e
            for e in evidence
            if any(
                i in (e["actor_id"], e["role_id"]) for i in (group if isinstance(group, list) else [group])
            )
            and e["source_id"] == segment["source_id"]
            and e["start"] < segment["end"]
            and e["end"] > segment["start"]
        ]
        for group in identifiers
    ]
    if not groups:
        return []
    if relation == "any":
        return list({e["id"]: e for g in groups for e in g}.values())
    if any(not g for g in groups):
        return []
    # Endpoint sweep: every group (selected identity or fuzzy variant union) overlaps at one instant.
    points = sorted({max(segment["start"], e["start"]) for g in groups for e in g})
    for t in points:
        if all(any(e["start"] <= t < min(e["end"], segment["end"]) for e in g) for g in groups):
            return list({e["id"]: e for g in groups for e in g}.values())
    return []


def purge_person_source(db, source_id):
    """Deletion creates sanitized replacement versions; surviving versions stay immutable."""
    for table in ("person_features", "person_evidence", "person_coverage", "person_tasks", "person_sources"):
        db.execute(f"DELETE FROM {table} WHERE source_id=?", (source_id,))
    for row in db.execute("SELECT * FROM person_versions").fetchall():
        value = json.loads(row["data"])
        if not any(e["source_id"] == source_id for e in value["evidence"] + value["coverage"]):
            continue
        value["evidence"] = [e for e in value["evidence"] if e["source_id"] != source_id]
        value["coverage"] = [c for c in value["coverage"] if c["source_id"] != source_id]
        version = fingerprint(value)
        db.execute("INSERT OR IGNORE INTO person_versions(id,data) VALUES(?,?)", (version, dumps(value)))
        for release in db.execute("SELECT * FROM search_releases WHERE person_id=?", (row["id"],)).fetchall():
            new_id = fingerprint([release["content_id"], version])
            db.execute(
                "INSERT OR IGNORE INTO search_releases(id,content_id,person_id) VALUES(?,?,?)",
                (new_id, release["content_id"], version),
            )
            db.execute(
                "UPDATE settings SET value=? WHERE key='active.release' AND value=?", (new_id, release["id"])
            )
            db.execute("DELETE FROM search_releases WHERE id=?", (release["id"],))
        db.execute("DELETE FROM person_versions WHERE id=?", (row["id"],))


def coverage_missing(snapshots, coverage):
    """A partially processed film still reports missing coverage outside processed intervals."""
    by_source = {}
    for c in coverage:
        by_source.setdefault(c["source_id"], []).append((c["data"]["start"], c["data"]["end"]))
    missing = set()
    for s in snapshots:
        end = s["start"]
        for a, b in sorted(by_source.get(s["source_id"], [])):
            if a > end + 1e-6:
                break
            end = max(end, b)
            if end >= s["end"] - 1e-6:
                break
        if end < s["end"] - 1e-6:
            missing.add(s["source_id"])
    return sorted(missing)
