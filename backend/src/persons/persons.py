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
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from typing import Literal

from PIL import Image, ImageOps
from pydantic import Field, model_validator

from mindex.shared.config import StrictModel, fingerprint
from mindex.shared.storage import Store, dumps

SCHEMA = """
CREATE TABLE IF NOT EXISTS person_entities(id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_sources(source_id TEXT PRIMARY KEY REFERENCES sources(id), work_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS person_references(id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, stage_id TEXT,
 path TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL, role_id TEXT);
CREATE TABLE IF NOT EXISTS person_imports(id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft');
CREATE TABLE IF NOT EXISTS person_features(id TEXT PRIMARY KEY, source_id TEXT, space TEXT NOT NULL,
 kind TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS person_features_source ON person_features(source_id,kind,space);
CREATE TABLE IF NOT EXISTS person_evidence(id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
 shot_id TEXT NOT NULL, actor_id TEXT, role_id TEXT, stage_id TEXT, start REAL NOT NULL, end REAL NOT NULL,
 status TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS person_evidence_source ON person_evidence(source_id,status,start,end);
CREATE INDEX IF NOT EXISTS person_evidence_shot ON person_evidence(source_id,shot_id);
CREATE TABLE IF NOT EXISTS person_coverage(source_id TEXT NOT NULL, cache_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS person_coverage_shot
 ON person_coverage(source_id,json_extract(data,'$.shot_id'));
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
    # Serialize schema inspection and migration so concurrent workers cannot both add the column.
    db.execute("BEGIN IMMEDIATE")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(person_references)")}
    if "role_id" not in columns:
        db.execute("ALTER TABLE person_references ADD COLUMN role_id TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS person_references_role ON person_references(role_id)")
    # Keep the retired SQL columns for old databases/checkpoints only. They are
    # never accepted as configuration or exposed as person identities again.
    marker = "migration.person-roles-v2"
    if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
        migrate_actor_only_evidence(db)
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
    migrate_actor_only_evidence(db)


def migrate_actor_only_evidence(db):
    """One-time cleanup; retain every role pair, human rejection and published snapshot."""
    marker = "migration.person-role-only-v3"
    if db.execute("SELECT 1 FROM settings WHERE key=?", (marker,)).fetchone():
        return
    predicate = (
        "actor_id IS NOT NULL AND role_id IS NULL AND NOT (status='rejected' AND "
        "(COALESCE(json_extract(data,'$.reviewed'),0)=1 "
        "OR COALESCE(json_extract(data,'$.confirmation'),'')='shot_review'))"
    )
    # Deleted-only shots must remain reviewable under No candidates.
    for row in db.execute(
        "SELECT source_id,shot_id,MIN(start) AS start,MAX(end) AS end, "
        "MAX(status='accepted' AND COALESCE(json_extract(data,'$.confirmation'),'')='shot_review') "
        "AS removed_confirmation FROM person_evidence WHERE "
        + predicate + " GROUP BY source_id,shot_id"
    ).fetchall():
        key = fingerprint([marker, row["source_id"], row["shot_id"]])
        db.execute("INSERT OR IGNORE INTO person_coverage VALUES(?,?,?)", (
            row["source_id"], key, dumps({"shot_id": row["shot_id"], "start": row["start"],
                                        "end": row["end"], "migration": marker,
                                        "removed_confirmation": bool(row["removed_confirmation"])}),
        ))
    db.execute("DELETE FROM person_evidence WHERE " + predicate)
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
    """勾选一个出镜身份：演员 + 该作品中的角色。"""

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


class PersonHasLinkedShots(ValueError):
    def __init__(self, result):
        self.linked_shots = result
        examples = "；".join(
            f"{s['source_name']} {s['start']:.3f}–{s['end']:.3f} 秒（分镜 {s['shot_id']}）"
            for s in result["items"][:5]
        )
        super().__init__(
            f"该身份仍有关联出镜证据：{result['total']} 个未排除分镜；请先核对。{examples}"
        )


@contextmanager
def frame_review_lock(store):
    from filelock import Timeout

    from mindex.shared.processing_lock import SharedFileLock

    try:
        with SharedFileLock(store.root / "pipeline.lock"):
            yield
    except Timeout:
        raise ValueError("人物帧正在迁移或清理，请稍后重试审核") from None


class Persons:
    def __init__(self, store: Store):
        self.store = store

    def entity(self, identifier, kind=None):
        row = self.store.one("SELECT * FROM person_entities WHERE id=?", (identifier,))
        if not row or (kind and row["kind"] != kind):
            raise ValueError("找不到对应人物、作品或角色")
        return self._display_entity(row, self._default_work_names() if row["kind"] == "work" else {})

    def _default_work_names(self):
        return {
            fingerprint(["work", s["id"]]): (s["name"], s["display_name"] or s["name"])
            for s in self.store.rows(
                "SELECT s.id,s.name,c.display_name FROM sources s "
                "LEFT JOIN source_curation c ON c.id=s.id"
            )
        }

    @staticmethod
    def _display_entity(row, work_names):
        entity = normalize_entity({"id": row["id"], **json.loads(row["data"])})
        default = work_names.get(entity["id"]) if entity["kind"] == "work" else None
        # Only automatically named works follow their source; explicit work names stay independent.
        if default and entity["name"] == default[0]:
            entity["name"] = default[1]
        return entity

    def entities(self, kind=None):
        work_names = self._default_work_names() if kind in (None, "work") else {}
        return [
            self._display_entity(r, work_names)
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

    def validate_update(self, value: Entity, identifier=None):
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

    def save(self, value: Entity, identifier=None):
        self.validate_update(value, identifier)
        identifier = identifier or uid()
        self.store.execute(
            "INSERT OR REPLACE INTO person_entities VALUES(?,?,?)",
            (identifier, value.kind, value.model_dump_json()),
        )
        return self.entity(identifier)

    def delete_release(self, identifier):
        """删除一条已发布的检索组合；正在生效的组合受保护，须先恢复到其他组合。
        只删组合行；其人物快照若已无其他组合引用则一并清理，避免留下孤儿快照。"""
        row = self.store.one("SELECT * FROM search_releases WHERE id=?", (identifier,))
        if not row:
            raise ValueError("找不到检索发布记录")
        active = self.store.one("SELECT value FROM settings WHERE key='active.release'")
        if active and active["value"] == identifier:
            raise ValueError("这是当前生效的检索组合；请先恢复到其他组合再删除")
        with self.store.connect() as db:
            db.execute("DELETE FROM search_releases WHERE id=?", (identifier,))
            if row["person_id"] and not db.execute(
                "SELECT 1 FROM search_releases WHERE person_id=?", (row["person_id"],)
            ).fetchone():
                db.execute("DELETE FROM person_versions WHERE id=?", (row["person_id"],))
        return {"deleted": identifier}

    def linked_shots(self, identifier, *, limit=50, offset=0, db=None):
        """已接受分镜仅检查确认身份；待确认分镜检查候选身份，已排除不拦截。"""
        entity = self.entity(identifier)
        if entity["kind"] == "work":
            raise ValueError("作品暂不支持在页面删除")
        column = f"{entity['kind']}_id"
        identity = f"e.{column}"
        confirmed_role = self.VALID_ROLE
        for field in ("source_id", "actor_id", "role_id"):
            confirmed_role = confirmed_role.replace(f"e.{field}", f"confirmed.{field}")
        query = (
            "FROM person_evidence e JOIN segments s ON s.id=e.shot_id AND s.source_id=e.source_id "
            "JOIN sources src ON src.id=s.source_id "
            "LEFT JOIN source_curation sc ON sc.id=src.id "
            f"WHERE {identity}=? AND s.kind='shot' AND " + self.VALID_ROLE + " AND (e.status='accepted' OR "
            "(e.status='candidate' AND NOT EXISTS (SELECT 1 FROM person_evidence confirmed "
            "WHERE confirmed.source_id=e.source_id AND confirmed.shot_id=e.shot_id "
            "AND confirmed.status='accepted' AND " + confirmed_role + "))) "
            "GROUP BY s.id,s.source_id"
        )
        with nullcontext(db) if db is not None else self.store.connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM (SELECT s.id " + query + ")", (identifier,),
            ).fetchone()["n"]
            items = [
                dict(row) for row in connection.execute(
                    "SELECT s.id AS shot_id,s.source_id,COALESCE(sc.display_name,src.name) AS source_name,s.start,s.end,"
                    "SUM(e.status='accepted') AS accepted,SUM(e.status='candidate') AS candidate "
                    + query + " ORDER BY s.source_id,s.start,s.id LIMIT ? OFFSET ?",
                    (identifier, limit, offset),
                )
            ]
        return {"entity": entity, "items": items, "total": total, "limit": limit, "offset": offset}

    def delete_entity(self, identifier):
        """未排除的关联分镜阻碍删除；参考图随身份删除，审核留档及已发布快照不变。"""
        entity = self.entity(identifier)
        if entity["kind"] == "work":
            raise ValueError("作品暂不支持在页面删除")
        column = f"{entity['kind']}_id"
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            linked = self.linked_shots(identifier, db=db)
            if linked["total"]:
                raise PersonHasLinkedShots(linked)
            if entity["kind"] == "actor":
                for row in db.execute("SELECT * FROM person_entities WHERE kind='role'").fetchall():
                    role = normalize_entity({"id": row["id"], **json.loads(row["data"])})
                    if identifier not in role["actor_ids"]:
                        continue
                    value = Entity.model_validate({k: v for k, v in role.items() if k != "id"}).model_copy(
                        update={"actor_ids": [a for a in role["actor_ids"] if a != identifier]}
                    )
                    db.execute("UPDATE person_entities SET data=? WHERE id=?", (value.model_dump_json(), role["id"]))
            removed = db.execute(f"DELETE FROM person_references WHERE {column}=?", (identifier,)).rowcount
            db.execute("DELETE FROM person_entities WHERE id=?", (identifier,))
        return {"deleted": identifier, "removed_references": removed}

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
            prune_default_work(db, source_id)

    def image(self, raw, namespace="references", *, max_edge=0, image_format="PNG", jpeg_quality=85):
        content, width, height = encode_frame(raw, max_edge=max_edge, image_format=image_format,
                                             jpeg_quality=jpeg_quality)
        digest = hashlib.sha256(content).hexdigest()
        extension = "jpg" if image_format == "JPEG" else "png"
        relative = f"persons/{namespace}/{digest}.{extension}"
        path = self.store.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"path": relative, "digest": digest, "width": width, "height": height}

    def pairing_actor(self, actor_id, role_id, actor_ids=None):
        """A reference face belongs to one actor, even when its role has several performers."""
        if not role_id:
            self.entity(actor_id, "actor")
            return actor_id
        if actor_ids is None:
            actor_ids = self.entity(role_id, "role")["actor_ids"]
        if not actor_id and len(actor_ids) > 1:
            raise ValueError("角色有多名出演演员，请指定参考图中的演员")
        actor_id = actor_id or (actor_ids[0] if actor_ids else None)
        if actor_id not in actor_ids:
            raise ValueError("角色参考图必须归属该角色的一名出演演员")
        return actor_id

    def add_reference(self, actor_id, raw, provenance=None, role_id=None, checker=None):
        actor_id = self.pairing_actor(actor_id, role_id)
        item = self.image(raw)
        if checker is not None:
            # 重传已入库的旧图是无操作：跳过质检，不烧 GPU，也不用新策略拒绝历史参考。
            existing = self.store.one(
                "SELECT id FROM person_references WHERE actor_id=? AND role_id IS ? AND digest=? "
                "ORDER BY rowid LIMIT 1",
                (actor_id, role_id, item["digest"]),
            )
            if not existing:
                checker(item)
        with self.store.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM person_references WHERE actor_id=? AND role_id IS ? AND digest=? "
                "ORDER BY rowid LIMIT 1",
                (actor_id, role_id, item["digest"]),
            ).fetchone()
            # Pairings can move; IDs must not collide when the original pairing is recreated.
            identifier = existing["id"] if existing else uid()
            if not existing:
                db.execute(
                    "INSERT INTO person_references(id,actor_id,role_id,path,digest,data) VALUES(?,?,?,?,?,?)",
                    (identifier, actor_id, role_id, item["path"], item["digest"], dumps(provenance or {})),
                )
        return self.reference(identifier)

    def reference(self, identifier):
        row = self.store.one("SELECT * FROM person_references WHERE id=?", (identifier,))
        if not row:
            raise ValueError("找不到参考图")
        value = {
            **{k: v for k, v in row.items() if k != "stage_id"},
            "data": json.loads(row["data"]),
            "url": "/api/persons/references/" + identifier + "/image",
        }
        if row["role_id"]:
            value["actor_ids"] = [row["actor_id"]]
        return value

    def references(self):
        return [
            self.reference(r["id"])
            for r in self.store.rows("SELECT id FROM person_references ORDER BY rowid")
        ]

    def update_reference(self, identifier, actor_id=None, entity=None, role_id=None):
        """Save reference pairing and the selected identity's metadata atomically."""
        reference = self.reference(identifier)
        if entity is not None:
            if entity.kind != ("role" if role_id else "actor"):
                raise ValueError("角色参考图必须配对角色" if role_id else "参考图必须配对演员")
            self.validate_update(entity, role_id or actor_id)
        actor_ids = entity.actor_ids if role_id and entity is not None else None
        if role_id == reference["role_id"] and actor_id is None:
            # Editing metadata must not silently relabel the face to a different performer.
            actor_id = reference["actor_id"]
        actor_id = self.pairing_actor(actor_id, role_id, actor_ids)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM person_references WHERE id=?", (identifier,)).fetchone():
                raise ValueError("找不到参考图")
            if entity is not None:
                db.execute(
                    "UPDATE person_entities SET data=? WHERE id=?",
                    (entity.model_dump_json(), role_id or actor_id),
                )
            duplicate = db.execute(
                "SELECT id FROM person_references WHERE actor_id=? AND role_id IS ? AND digest=? "
                "AND id<>? LIMIT 1",
                (actor_id, role_id, reference["digest"], identifier),
            ).fetchone()
            if duplicate:
                db.execute("DELETE FROM person_references WHERE id=?", (identifier,))
                identifier = duplicate["id"]
            else:
                db.execute(
                    "UPDATE person_references SET actor_id=?,role_id=? WHERE id=?", (actor_id, role_id, identifier)
                )
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

    def append_images(self, identifier, raws):
        """向未确认草稿追加截图；与草稿内容相同的图跳过并记警告。"""
        draft = self.get_import(identifier)
        if draft["status"] != "draft":
            raise ValueError("演员表已确认，不能追加截图")
        known = {im["digest"] for im in draft["images"]}
        for raw in raws:
            item = self.image(raw, "imports")
            if item["digest"] in known:
                draft["warnings"] = [
                    *draft.get("warnings", []),
                    "追加的截图与草稿中已有图片内容相同，已忽略。",
                ]
                continue
            known.add(item["digest"])
            draft["images"].append({"id": uid(), **item})
        self.store.execute("UPDATE person_imports SET data=? WHERE id=?", (dumps(draft), identifier))
        return self.get_import(identifier)

    def delete_import_image(self, identifier, image_id):
        """删除一张历史演员表图片（草稿与已确认均可）：从草稿数据移除该图及其条目；
        图片删空后整行删除；已确认导入建立的实体与参考图不受影响。内容寻址文件
        仍被其他草稿引用时保留。"""
        draft = self.get_import(identifier)
        image = next((im for im in draft["images"] if im["id"] == image_id), None)
        if not image:
            raise ValueError("找不到原图")
        remaining = [im for im in draft["images"] if im["id"] != image_id]
        if remaining:
            draft["images"] = remaining
            draft["entries"] = [e for e in draft.get("entries", []) if e["image_id"] != image_id]
            self.store.execute("UPDATE person_imports SET data=? WHERE id=?", (dumps(draft), identifier))
        else:
            self.store.execute("DELETE FROM person_imports WHERE id=?", (identifier,))
        still_used = any(
            im.get("digest") == image["digest"]
            for row in self.store.rows("SELECT data FROM person_imports")
            for im in json.loads(row["data"]).get("images", [])
        )
        if not still_used:
            (self.store.root / image["path"]).unlink(missing_ok=True)
        return {"deleted": image_id, "remaining_images": len(remaining)}

    def list_imports(self):
        """历史演员表导入（含已确认），供导入页默认列出全部 cast 图片。"""
        items = []
        for row in self.store.rows("SELECT * FROM person_imports ORDER BY rowid DESC"):
            value = json.loads(row["data"])
            items.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "work_id": value.get("work_id"),
                    "entry_count": len(value.get("entries", [])),
                    "images": [
                        {
                            "id": image["id"],
                            "width": image.get("width"),
                            "height": image.get("height"),
                            "url": f"/api/persons/imports/{row['id']}/images/{image['id']}",
                        }
                        for image in value.get("images", [])
                    ],
                }
            )
        return items

    def get_import(self, identifier):
        row = self.store.one("SELECT * FROM person_imports WHERE id=?", (identifier,))
        if not row:
            raise ValueError("找不到演员表草稿")
        value = json.loads(row["data"])
        for image in value["images"]:
            image["url"] = f"/api/persons/imports/{identifier}/images/{image['id']}"
        return {**value, "id": identifier, "status": row["status"]}

    def confirm_import(self, identifier, body: CastConfirm, checker=None):
        draft = self.get_import(identifier)
        if draft["status"] != "draft":
            raise ValueError("演员表已确认；请在人物库追加或修改参考图")
        if not body.work_id:
            raise ValueError("请选择导入作品")
        self.entity(body.work_id, "work")
        images = {x["id"]: x for x in draft["images"]}
        # 批内去重：演员名+角色名相同且未显式关联现有身份的条目视为重复，
        # 只保留首个含照片框的，其余静默跳过并在结果中汇总。
        kept, groups, order = [], {}, []
        for entry in body.entries:
            if entry.actor_id or entry.role_id:
                kept.append(entry)
                continue
            key = (entry.actor_name, entry.role_name)
            if key not in groups:
                groups[key] = [entry]
                order.append(key)
                kept.append(entry)
            else:
                groups[key].append(entry)
        skipped_duplicates = []
        for key in order:
            members = groups[key]
            if len(members) < 2:
                continue
            chosen = next((e for e in members if e.bbox is not None), members[0])
            kept[kept.index(members[0])] = chosen
            skipped_duplicates.extend(
                {"actor_name": e.actor_name, "role_name": e.role_name} for e in members if e is not chosen
            )
        # Validate and crop every entry before committing catalogue changes.
        crops = []
        for index, entry in enumerate(kept):
            if entry.image_id not in images:
                raise ValueError("原图不属于此草稿")
            with Image.open(self.store.resolve_media(images[entry.image_id]["path"])) as image:
                box = checked_box(entry.bbox, *image.size) if entry.bbox is not None else None
                buffer = io.BytesIO()
                if box:
                    image.crop(box).save(buffer, format="PNG")
                raw = buffer.getvalue() if box else None
                crops.append(raw)
            if entry.actor_id:
                self.entity(entry.actor_id, "actor")
            if entry.role_id:
                self.entity(entry.role_id, "role")
            if checker is not None and raw:
                # 入库前逐条目质检（事务外）：任一不合格整批拒绝并点名条目；服务不可用同样整批拒绝。
                item = self.image(raw)
                if not entry.actor_id or not self.store.one(
                    "SELECT id FROM person_references WHERE actor_id=? AND role_id IS NULL AND digest=? "
                    "ORDER BY rowid LIMIT 1",
                    (entry.actor_id, item["digest"]),
                ):
                    try:
                        checker(item)
                    except ValueError as exc:
                        raise ValueError(
                            f"条目 {index + 1}（{entry.actor_name or '未命名'}）：{exc}"
                        ) from exc
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

                def connect(_):
                    return nullcontext(db)

                def rows(_, sql, args=()):
                    return [dict(row) for row in db.execute(sql, args).fetchall()]

                def one(_, sql, args=()):
                    row = db.execute(sql, args).fetchone()
                    return dict(row) if row else None

                def execute(_, sql, args=()):
                    db.execute(sql, args)

            tx = Persons(TransactionStore())
            confirmed = []
            ignored_existing = 0
            for entry, crop in zip(kept, crops):
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
                reference = None
                if crop:
                    # 与 add_reference 同口径（演员级参考图，role_id 为 NULL）：按内容寻址 digest 判断是否已存在。
                    digest = tx.image(crop)["digest"]
                    exists = db.execute(
                        "SELECT id FROM person_references WHERE actor_id=? AND role_id IS NULL AND digest=? "
                        "ORDER BY rowid LIMIT 1",
                        (actor_id, digest),
                    ).fetchone()
                    if exists:
                        ignored_existing += 1
                    reference = tx.add_reference(
                        actor_id,
                        crop,
                        {"import_id": identifier, "image_id": entry.image_id, "bbox": entry.bbox},
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
                        {**draft, "confirmed": confirmed, "entries": [e.model_dump() for e in kept]}
                    ),
                    identifier,
                ),
            )
        return {
            "confirmed": confirmed,
            "skipped_duplicates": skipped_duplicates,
            "ignored_existing": ignored_existing,
        }

    def review(self, evidence_id, body: Review):
        with frame_review_lock(self.store):
            return self._review(evidence_id, body)

    def _review(self, evidence_id, body: Review):
        """Correct one cached observation without expanding its temporal interval.

        The public review UI/API uses shot_review; keep this operation internal.
        """
        row = self.store.one("SELECT * FROM person_evidence WHERE id=?", (evidence_id,))
        if not row:
            raise ValueError("找不到出镜证据")
        actor_id = body.actor_id or row["actor_id"]
        if body.accept:
            self.entity(actor_id, "actor")
            if not body.role_id:
                raise ValueError("出镜审核必须指定角色")
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
        with frame_review_lock(self.store):
            return self._shot_review(body)

    def _shot_review(self, body: ShotReview):
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
            if not selection.role_id:
                raise ValueError("出镜审核必须指定角色")
            role = self.entity(selection.role_id, "role")
            if work_id not in role["work_ids"] or selection.actor_id not in role["actor_ids"]:
                raise ValueError("演员与作品角色关系不符")
        rows = self.store.rows(
            "SELECT * FROM person_evidence WHERE source_id=? AND shot_id=? ORDER BY start,id",
            (body.source_id, body.shot_id),
        )
        decoded_rows = [{**r, "data": json.loads(r["data"])} for r in rows]
        fallback = existing_frame(self.store, decoded_rows, shot["thumbnail"])
        marks = {
            (s.actor_id, s.role_id): fingerprint(
                ["shot-review-v1", body.source_id, body.shot_id, s.actor_id, s.role_id]
            )
            for s in body.selections
        }
        accepted = list(marks.values())
        reviewed_at = datetime.now(UTC).isoformat()
        with self.store.connect() as db:
            if not rows and not body.selections:
                # 无检测结果的已处理分镜也能排除，并在重跑时保留人工结论。
                db.execute(
                    "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        fingerprint(["shot-review-empty-v1", body.source_id, body.shot_id]),
                        body.source_id, body.shot_id, None, None, None,
                        shot["start"], shot["end"], "rejected",
                        dumps({"evidence_type": "manual", "confirmation": "shot_review", "reviewed": True}),
                    ),
                )
            for (actor_id, role_id), identifier in marks.items():
                image = existing_frame(self.store, [r for r in decoded_rows if r["actor_id"] == actor_id], fallback)
                original_score = self.original_score(decoded_rows, actor_id, role_id)
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
                                "original_score": original_score,
                                "reviewed_at": reviewed_at,
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
                        dumps(json.loads(row["data"]) | {"reviewed": True, "confirmation": "shot_review", "shot_excluded": not body.selections}),
                        row["id"],
                    ),
                )
        return {
            "shot_id": body.shot_id,
            "accepted": accepted,
            "rejected": sum(r["id"] not in marks.values() for r in rows),
        }

    @staticmethod
    def evidence_score(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None

    @classmethod
    def original_score(cls, rows, actor_id, role_id):
        """取该身份原始匹配最高分；整镜人工记录的固定 1.0 不属于原始分数。"""
        exact = []
        for row in rows:
            if row["actor_id"] != actor_id:
                continue
            data = row["data"]
            if data.get("evidence_type") == "track":
                continue  # inherited 1.0 must not become the next manual confirmation's original_score
            synthetic = data.get("evidence_type") == "manual" and data.get("confirmation") == "shot_review"
            score = cls.evidence_score(data.get("original_score") if synthetic else data.get("score"))
            if score is None:
                continue
            if role_id and row["role_id"] == role_id:
                exact.append(score)
        return max(exact, default=None)

    @classmethod
    def confirmed_scores(cls, people, rows):
        """补充每位确认人物的分数与来源；旧人工记录从保留的原始帧恢复分数。"""
        result = []
        for person in people:
            matching = [r for r in rows if r["status"] == "accepted" and r["actor_id"] == person["actor_id"]
                        and r["role_id"] == person["role_id"]]
            manual_rows = [r for r in matching if r["data"].get("evidence_type") == "manual"]
            if manual_rows:
                recorded = [cls.evidence_score(r["data"].get("original_score")) for r in manual_rows
                            if "original_score" in r["data"]]
                original = max((s for s in recorded if s is not None), default=None) if recorded else cls.original_score(
                    rows, person["actor_id"], person["role_id"],
                )
                score = original
            else:
                original = None
                score = max((s for r in matching if (s := cls.evidence_score(r["data"].get("score"))) is not None), default=None)
            result.append({**person, "score": score, "original_score": original, "manual": bool(manual_rows)})
        return result

    VALID_ROLE = (
        "EXISTS (SELECT 1 FROM person_entities role JOIN person_sources ps ON ps.source_id=e.source_id "
        "JOIN person_entities actor ON actor.id=e.actor_id AND actor.kind='actor' "
        "WHERE role.id=e.role_id AND role.kind='role' "
        "AND EXISTS (SELECT 1 FROM json_each(role.data,'$.work_ids') WHERE value=ps.work_id) "
        "AND EXISTS (SELECT 1 FROM json_each(role.data,'$.actor_ids') WHERE value=e.actor_id))"
    )

    SHOT_EXCLUSION = (
        "e.status='rejected' AND json_extract(e.data,'$.confirmation')='shot_review' "
        "AND NOT EXISTS (SELECT 1 FROM person_evidence mark WHERE mark.source_id=e.source_id "
        "AND mark.shot_id=e.shot_id AND mark.status='accepted' "
        "AND json_extract(mark.data,'$.confirmation')='shot_review') "
        "AND (json_extract(e.data,'$.shot_excluded')=1 OR NOT EXISTS ("
        "SELECT 1 FROM person_coverage removed WHERE removed.source_id=e.source_id "
        "AND json_extract(removed.data,'$.shot_id')=e.shot_id "
        "AND json_extract(removed.data,'$.removed_confirmation')=1))"
    )

    # 分镜级状态先按分镜聚合一次再过滤：逐行 EXISTS 会在上万条证据上重复求值。
    SHOT_STATES = (
        "(WITH valid_pairs AS (SELECT DISTINCT ps.source_id,role.id AS role_id,performer.value AS actor_id "
        "FROM person_sources ps JOIN person_entities role ON role.kind='role' "
        "AND EXISTS (SELECT 1 FROM json_each(role.data,'$.work_ids') WHERE value=ps.work_id) "
        "JOIN json_each(role.data,'$.actor_ids') performer "
        "JOIN person_entities actor ON actor.id=performer.value AND actor.kind='actor') "
        "SELECT e.shot_id,e.source_id, SUM(e.status='accepted' AND pair.role_id IS NOT NULL) AS accepted, "
        "SUM(e.status='candidate' AND pair.role_id IS NOT NULL) AS candidate, "
        "SUM(CASE WHEN e.status='rejected' "
        "AND json_extract(e.data,'$.confirmation')='shot_review' AND marks.shot_id IS NULL "
        "AND (json_extract(e.data,'$.shot_excluded')=1 OR removed.shot_id IS NULL) "
        "THEN 1 ELSE 0 END) AS rejected "
        "FROM person_evidence e JOIN segments s ON s.id=e.shot_id AND s.source_id=e.source_id "
        "LEFT JOIN valid_pairs pair ON pair.source_id=e.source_id AND pair.role_id=e.role_id AND pair.actor_id=e.actor_id "
        "LEFT JOIN (SELECT source_id,shot_id FROM person_evidence WHERE status='accepted' "
        "AND json_extract(data,'$.confirmation')='shot_review' GROUP BY source_id,shot_id) marks "
        "ON marks.source_id=e.source_id AND marks.shot_id=e.shot_id "
        "LEFT JOIN (SELECT source_id,json_extract(data,'$.shot_id') AS shot_id FROM person_coverage "
        "WHERE json_extract(data,'$.removed_confirmation')=1 GROUP BY source_id,shot_id) removed "
        "ON removed.source_id=e.source_id AND removed.shot_id=e.shot_id "
        "WHERE s.kind='shot' GROUP BY e.shot_id,e.source_id "
        "UNION ALL SELECT DISTINCT s.id,s.source_id,0,0,0 FROM person_coverage c "
        "JOIN segments s ON s.id=json_extract(c.data,'$.shot_id') AND s.source_id=c.source_id "
        "WHERE s.kind='shot' AND NOT EXISTS (SELECT 1 FROM person_evidence e "
        "WHERE e.shot_id=s.id AND e.source_id=s.source_id))"
    )

    def review_summary(self):
        """每个素材的分镜级状态计数；分类规则与审核列表完全一致。"""
        rows = self.store.rows(
            "SELECT source_id, CASE WHEN accepted>0 THEN 'accepted' "
            "WHEN candidate>0 OR rejected=0 THEN 'candidate' "
            "ELSE 'rejected' END AS bucket, COUNT(*) AS shots FROM "
            + self.SHOT_STATES
            + " GROUP BY source_id, bucket"
        )
        summary: dict[str, dict[str, int]] = {}
        for row in rows:
            summary.setdefault(row["source_id"], {"accepted": 0, "candidate": 0, "rejected": 0})[
                row["bucket"]
            ] = row["shots"]
        return summary

    def review_shots(
        self, source_id=None, status=None, *, limit=50, offset=0, shot_id=None,
        actor_id=None, role_id=None, no_candidate=False, compact=False,
    ):
        """按分镜分组返回证据。

        状态按分镜归类而不是逐帧：任一帧被确认，整个分镜就算已接受；只有完全没有
        已确认帧的分镜才仍算待确认。自动接受不代表已经完成人工审核。
        待确认分镜取全镜最高分候选归类；已接受分镜按全部已接受身份筛选，允许多分类命中。
        待确认与已接受的分类、计数与过滤均在分页之前完成，卡内证据不受身份过滤影响。
        已排除分镜不再按身份分类，忽略演员-角色和无候选筛选；历史证据保留供复核。
        """
        clauses, args = [], []
        for key, value in [("k.source_id", source_id), ("k.shot_id", shot_id)]:
            if value:
                clauses.append(key + "=?")
                args.append(value)
        if status == "accepted":
            clauses.append("k.accepted>0")
        elif status == "candidate":
            # 已处理但无检测结果的分镜也需要人工确认。
            clauses.append("k.accepted=0 AND (k.candidate>0 OR k.rejected=0)")
        elif status == "rejected":
            clauses.append("k.accepted=0 AND k.candidate=0 AND k.rejected>0")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        query = (
            "WITH scoped AS (SELECT s.id AS shot_id,s.source_id,s.start,s.end,"
            "k.accepted,k.candidate,k.rejected FROM segments s JOIN " + self.SHOT_STATES
            + " k ON k.shot_id=s.id AND k.source_id=s.source_id" + where + "), "
        )
        if status == "rejected":
            # 已人工排除的分镜只按素材、状态分页，无需再依赖身份表进行排名或归类。
            query += (
                "classified AS (SELECT s.*,NULL AS actor_id,NULL AS role_id,NULL AS score FROM scoped s) "
            )
            filters = []
        else:
            # 同分时优先当前已接受身份，再取最近的人工结论，最后以帧时间和 ID 保持稳定。
            query += (
                "ranked AS (SELECT e.source_id,e.shot_id,e.actor_id,e.role_id,"
                "CASE WHEN json_type(e.data,'$.score') IN ('integer','real') "
                "THEN json_extract(e.data,'$.score') ELSE 0 END AS score,"
                "ROW_NUMBER() OVER (PARTITION BY e.source_id,e.shot_id ORDER BY "
                "CASE WHEN json_type(e.data,'$.score') IN ('integer','real') "
                "THEN json_extract(e.data,'$.score') ELSE 0 END DESC,"
                "(e.status='accepted') DESC,COALESCE(json_extract(e.data,'$.reviewed_at'),'') DESC,"
                "e.start,e.id) AS rank "
                "FROM person_evidence e JOIN scoped s ON s.shot_id=e.shot_id AND s.source_id=e.source_id "
                "JOIN person_entities a ON a.id=e.actor_id AND a.kind='actor' WHERE " + self.VALID_ROLE + "), "
                "classified AS (SELECT s.*,r.actor_id,r.role_id,r.score FROM scoped s "
                "LEFT JOIN ranked r ON r.source_id=s.source_id AND r.shot_id=s.shot_id AND r.rank=1), "
                "confirmed AS (SELECT DISTINCT e.source_id,e.shot_id,e.actor_id,e.role_id FROM person_evidence e "
                "JOIN scoped s ON s.source_id=e.source_id AND s.shot_id=e.shot_id "
                "WHERE e.status='accepted' AND " + self.VALID_ROLE + "), "
                "memberships AS (SELECT * FROM confirmed UNION ALL "
                "SELECT source_id,shot_id,actor_id,role_id FROM classified "
                "WHERE accepted=0 AND (candidate>0 OR rejected=0) UNION ALL "
                "SELECT s.source_id,s.shot_id,NULL,NULL FROM classified s WHERE s.accepted>0 "
                "AND NOT EXISTS (SELECT 1 FROM confirmed c WHERE c.source_id=s.source_id AND c.shot_id=s.shot_id)) "
            )
            filters = self.store.rows(
                query + "SELECT actor_id,role_id,COUNT(*) AS count FROM memberships GROUP BY actor_id,role_id",
                args,
            )
        scope_total = self.store.one(query + "SELECT COUNT(*) AS n FROM classified", args)["n"]
        pair_where, pair_args = "", list(args)
        membership_match = "m.source_id=s.source_id AND m.shot_id=s.shot_id"
        if status == "rejected":
            total = scope_total
        elif no_candidate:
            pair_where = " WHERE EXISTS (SELECT 1 FROM memberships m WHERE " + membership_match + " AND m.actor_id IS NULL)"
            total = sum(f["count"] for f in filters if f["actor_id"] is None)
        elif actor_id:
            pair_where = " WHERE EXISTS (SELECT 1 FROM memberships m WHERE " + membership_match + " AND m.actor_id=? AND m.role_id IS ?)"
            pair_args.extend([actor_id, role_id])
            total = sum(
                f["count"] for f in filters if f["actor_id"] == actor_id and f["role_id"] == role_id
            )
        else:
            total = scope_total
        confirmed_people = ("'[]'" if status == "rejected" else
            "(SELECT json_group_array(json_object('actor_id',actor_id,'role_id',role_id)) "
            "FROM (SELECT actor_id,role_id FROM confirmed c "
            "WHERE c.source_id=s.source_id AND c.shot_id=s.shot_id ORDER BY actor_id,role_id))")
        shots = self.store.rows(
            query + "SELECT s.*," + confirmed_people + " AS confirmed_people FROM classified s" + pair_where
            + " ORDER BY s.source_id,s.start,s.shot_id LIMIT ? OFFSET ?",
            [*pair_args, limit, offset],
        )
        items = []
        for shot in shots:
            evidence_sql = (
                "SELECT id,actor_id,role_id,status,json_object("
                "'score',json_extract(data,'$.score'),'original_score',json_extract(data,'$.original_score'),"
                "'evidence_type',json_extract(data,'$.evidence_type'),'confirmation',json_extract(data,'$.confirmation'),"
                "'image_path',json_extract(data,'$.image_path')) "
                "AS data,json_type(data,'$.original_score') IS NOT NULL AS original_recorded "
                "FROM person_evidence e WHERE source_id=? AND shot_id=? AND " + self.VALID_ROLE + " ORDER BY start,id" if compact else
                "SELECT * FROM person_evidence e WHERE source_id=? AND shot_id=? AND " + self.VALID_ROLE + " ORDER BY start,id"
            )
            raw_rows = []
            for row in self.store.rows(evidence_sql, (shot["source_id"], shot["shot_id"])):
                data = {k: v for k, v in json.loads(row["data"]).items() if k not in {"body_feature", "space"}}
                if compact and not row["original_recorded"]:
                    data.pop("original_score", None)
                raw_rows.append({**row, "data": data})
            people = self.confirmed_scores(json.loads(shot["confirmed_people"]), raw_rows)
            recognized = {}
            for row in raw_rows:
                if not row["actor_id"] or row["data"].get("evidence_type") == "manual":
                    continue
                key = (row["actor_id"], row["role_id"])
                person = recognized.setdefault(key, {"actor_id": key[0], "role_id": key[1], "score": None, "observations": 0})
                person["observations"] += 1
                score = self.evidence_score(row["data"].get("score"))
                if score is not None:
                    person["score"] = max(person["score"] if person["score"] is not None else score, score)
            if compact:
                unique = {}
                for row in raw_rows:
                    key = (row["actor_id"], row["role_id"], row["status"])
                    unique.setdefault(key, {k: row[k] for k in ("id", "actor_id", "role_id", "status")})
                rows = list(unique.values())
            else:
                rows = [{**r, "image_url": f"/api/persons/evidence/{r['id']}/image"} for r in raw_rows]
            items.append(
                {
                    **{k: shot[k] for k in ("shot_id", "source_id", "start", "end")},
                    "candidate": {
                        "actor_id": shot["actor_id"], "role_id": shot["role_id"], "score": shot["score"],
                    } if shot["actor_id"] else None,
                    "confirmed_people": people,
                    "recognized_people": list(recognized.values()),
                    "counts": {
                        key: shot[key]
                        for key in ("accepted", "candidate", "rejected")
                    },
                    "image_url": next(
                        (f"/api/persons/evidence/{r['id']}/image" for r in sorted(raw_rows, key=frame_rank)
                         if r.get("data", {}).get("image_path")), None,
                    ),
                    "evidence": rows,
                }
            )
        return {"items": items, "total": total, "scope_total": scope_total, "filters": filters}

    def evidence(self, source_id=None, status=None, *, limit=None, offset=0, public=False):
        clauses, args = ([self.VALID_ROLE] if public else []), []
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
                "SELECT * FROM person_evidence e"
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY source_id,start,id"
                + (" LIMIT ? OFFSET ?" if limit is not None else ""),
                [*args, limit, offset] if limit is not None else args,
            )
        ]

    def publish(self, content_id=None, *, source_id=None, progress=None):
        active = self.store.active_index()
        content_id = content_id or (active["id"] if active else None)
        if not content_id:
            raise ValueError("请先发布内容索引")
        content = self.store.index_version(content_id)
        if content["status"] != "ready":
            raise ValueError("内容索引尚未就绪")
        count = self.store.one(
            "SELECT COUNT(DISTINCT shot_id) AS n FROM ("
            "SELECT json_extract(data,'$.shot_id') AS shot_id,source_id FROM person_coverage UNION ALL "
            "SELECT shot_id,source_id FROM person_evidence WHERE status='accepted') "
            + ("WHERE source_id=?" if source_id else ""),
            (source_id,) if source_id else (),
        )["n"]
        if progress:
            progress({"stage": "person_index", "completed": 0, "total": count, "unit": "shots"})
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
                for r in db.execute("SELECT * FROM person_evidence e WHERE status='accepted' AND " + self.VALID_ROLE + " ORDER BY id")
            ]
            coverage = [
                dict(r) | {"data": json.loads(r["data"])}
                for r in db.execute("SELECT * FROM person_coverage ORDER BY cache_id")
            ]
            value = {"format": 2, "entities": entities, "evidence": evidence, "coverage": coverage}
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
        if progress:
            progress({"stage": "published", "completed": count, "total": count, "unit": "shots"})
        return {"release_id": release, "content_id": content_id, "person_version": version}

    def published(self, content_id, *, pinned=False, release_id=None, source_id=None):
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
            self.store.one(
                "SELECT json_object('entities',json('[]'),'evidence',"
                "(SELECT json_group_array(json(value)) FROM json_each(data,'$.evidence') WHERE json_extract(value,'$.source_id')=?),"
                "'coverage',(SELECT json_group_array(json(value)) FROM json_each(data,'$.coverage') WHERE json_extract(value,'$.source_id')=?)) AS data "
                "FROM person_versions WHERE id=?", (source_id, source_id, release['person_id']))
            if source_id and release and release['person_id'] else
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


_DOCUMENT = r"(?:记录|档案|资料|信件|书信|日记|传记|报道|报告|照片|画像)"
_REFERENCE = re.compile(r"(?:关于|有关)[^，,。.;；！？!?：:\n]{1,40}?" + _DOCUMENT)
_POSSESSIVE_DOCUMENT = re.compile(r"的(?:相关)?" + _DOCUMENT)
AUTO_PERSON_QUERY_VERSION = "document-reference-v1"


def has_document_reference(text):
    """Whether auto name interpretation can differ from the legacy co-appearance rule."""
    return bool(_REFERENCE.search(text) or _POSSESSIVE_DOCUMENT.search(text))


def _document_reference(text, start, end):
    # Only clear documentary references are relaxed. Separate visible occurrences of
    # the same person, coordinated subjects and explicit selections remain strict.
    return bool(
        _POSSESSIVE_DOCUMENT.match(text, end)
        or any(m.start() <= start and end <= m.end() for m in _REFERENCE.finditer(text))
    )


def resolve_names(query, entities):
    """Resolve typed names to identities; returns (ids, recognized, remaining, variant_groups).

    Roles also match by significant suffix so 叶淑柔 recalls a role named 老年叶淑柔;
    such variants form one union group (any member counts), never an ambiguity error.
    Explicit person_ids selections stay precise singletons.
    """

    lookup = {e["id"]: e for e in entities}
    explicit = list(dict.fromkeys(query.person_ids))
    selected_ids = set(explicit)
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
    onscreen_spans = []
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
        reference = (
            query.person_mode == "auto"
            and not selected_ids.intersection(members)
            and _document_reference(query.text, a, b)
        )
        for identifier in members:
            if not reference and identifier not in explicit:
                explicit.append(identifier)
            recognized.append(
                {
                    "id": identifier,
                    "name": lookup[identifier]["name"],
                    "kind": lookup[identifier]["kind"],
                    "span": [a, b],
                    **({"usage": "reference"} if reference else {}),
                }
            )
        if not reference:
            variant_groups.append(members)
            onscreen_spans.append((a, b))
    remaining = query.text
    for a, b in sorted(onscreen_spans, reverse=True):
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


def prune_default_work(db, source_id):
    """Remove an unused automatic work, preserving explicitly named works and cast imports."""
    work_id = fingerprint(["work", source_id])
    source = db.execute("SELECT name FROM sources WHERE id=?", (source_id,)).fetchone()
    work = db.execute("SELECT data FROM person_entities WHERE id=? AND kind='work'", (work_id,)).fetchone()
    if not source or not work:
        return
    value = json.loads(work["data"])
    if value.get("name") != source["name"] or value.get("aliases"):
        return
    if db.execute("SELECT 1 FROM person_sources WHERE work_id=?", (work_id,)).fetchone():
        return
    if db.execute(
        "SELECT 1 FROM person_entities, json_each(person_entities.data,'$.work_ids') "
        "WHERE person_entities.kind='role' AND json_each.value=?", (work_id,),
    ).fetchone():
        return
    if db.execute(
        "SELECT 1 FROM person_imports WHERE json_extract(data,'$.work_id')=?", (work_id,),
    ).fetchone():
        return
    db.execute("DELETE FROM person_entities WHERE id=?", (work_id,))


def purge_person_source(db, source_id):
    """Deletion creates sanitized replacement versions; surviving versions stay immutable."""
    for table in ("person_features", "person_evidence", "person_coverage", "person_tasks", "person_sources"):
        db.execute(f"DELETE FROM {table} WHERE source_id=?", (source_id,))
    prune_default_work(db, source_id)
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


def frame_rank(row):
    """Stable display ordering; inherited track confidence is not a face score."""
    data = row["data"]
    synthetic = data.get("evidence_type") == "manual" and data.get("confirmation") == "shot_review"
    score = Persons.evidence_score(data.get("original_score") if synthetic else data.get("score"))
    return (row.get("status") != "accepted", data.get("evidence_type") == "track",
            score is None, -(score or 0), row["id"])


def existing_frame(store, rows, fallback):
    for row in sorted(rows, key=frame_rank):
        path = row["data"].get("image_path")
        if path and (store.root / path).is_file():
            return path
    return fallback


def retained_frame_paths(store, source_id=None):
    groups, result = {}, {}
    for row in store.rows("SELECT * FROM person_evidence" + (" WHERE source_id=?" if source_id else ""),
                          (source_id,) if source_id else ()):
        row["data"] = json.loads(row["data"])
        groups.setdefault((row["source_id"], row["shot_id"]), []).append(row)
    for (source, _), rows in groups.items():
        paths = result.setdefault(source, set())
        if not any(r["status"] == "accepted" for r in rows):
            paths.update(r["data"]["image_path"] for r in rows if r["data"].get("image_path"))
            continue
        identities = set()
        for row in sorted(rows, key=frame_rank):
            identity = (row["actor_id"], row["role_id"])
            path = row["data"].get("image_path")
            if row["status"] == "accepted" and path and identity not in identities:
                paths.add(path)
                identities.add(identity)
    return result


def migrate_frames(config, *, source_id=None, apply=False):
    """Migrate display assets only. Feature vectors, cache IDs and releases stay intact."""
    from filelock import Timeout

    from mindex.shared.processing_lock import SharedFileLock, index_write_lock

    store = Store(config.data_dir)
    guard = SharedFileLock(store.root / "pipeline.lock", exclusive=True)
    write = index_write_lock(store.root)
    write.timeout = 10
    try:
        with guard, write, store.run("persons-migrate-frames") if apply else nullcontext():
            return _migrate_frames(config, store, source_id, apply)
    except Timeout:
        raise ValueError("有离线处理任务或索引发布正在运行，请稍后重试") from None


def _migrate_frames(config, store, source_id, apply):
    retained = retained_frame_paths(store)
    base = store.root / "persons/frames"
    sources = set(retained)
    if base.is_dir():
        sources.update(p.name for p in base.iterdir() if p.is_dir())
    if source_id:
        sources = {source_id}
    reports = []
    for source in sorted(sources):
        report = {"source_id": source, "kept_frames": 0, "reencoded": 0, "unchanged": 0,
                      "deleted_files": 0, "deleted_bytes": 0, "rewritten_evidence": 0, "rewritten_features": 0, "error": None}
        reports.append(report)
        try:
            directory = (base / source).resolve()
            if not directory.is_relative_to(base.resolve()) or directory == base.resolve():
                raise ValueError("非法 source_id")
            # Defensive protection for imported/cross-source references during a scoped migration.
            prefix = f"persons/frames/{source}/"
            mapping = {}
            keep = {path for owner, paths in retained.items() if owner != source
                    for path in paths if path.startswith(prefix)}
            for relative in sorted(retained.get(source, set())):
                path = (store.root / relative).resolve()
                if not path.is_relative_to(directory):
                    continue  # thumbnails and external namespaces are never migrated or deleted
                with Image.open(path) as image:
                    unchanged = (image.format == "JPEG" and path.suffix == ".jpg" and
                                 (not config.person.frames_max_edge or max(image.size) <= config.person.frames_max_edge))
                if unchanged:
                    target = relative
                    report["unchanged"] += 1
                else:
                    # Preview encodes in memory only; no directories/files/DB writes.
                    if apply:
                        target = Persons(store).image(path.read_bytes(), "frames/" + source,
                            max_edge=config.person.frames_max_edge, image_format="JPEG",
                            jpeg_quality=config.person.frames_jpeg_quality)["path"]
                    else:
                        content, _, _ = encode_frame(path.read_bytes(), max_edge=config.person.frames_max_edge,
                                                     image_format="JPEG", jpeg_quality=config.person.frames_jpeg_quality)
                        target = f"persons/frames/{source}/{hashlib.sha256(content).hexdigest()}.jpg"
                    report["reencoded"] += 1
                mapping[relative] = target
                keep.add(target)
            report["kept_frames"] = len(keep)
            # All references to a converted image follow it, including shared rejected rows.
            with store.connect() as db:
                if apply:
                    db.execute("BEGIN IMMEDIATE")
                for table, kind, counter in (("person_evidence", "", "rewritten_evidence"),
                                             ("person_features", " AND kind='shot'", "rewritten_features")):
                    for row in db.execute(f"SELECT id,data FROM {table} WHERE source_id=?{kind}", (source,)).fetchall():
                        data = json.loads(row["data"])
                        frames = [data] if table == "person_evidence" else data.get("frames", [])
                        changed = False
                        for frame in frames:
                            old = frame.get("image_path")
                            if old in mapping and mapping[old] != old:
                                frame["image_path"] = mapping[old]
                                changed = True
                        if changed:
                            report[counter] += 1
                            if apply:
                                db.execute(f"UPDATE {table} SET data=? WHERE id=?", (dumps(data), row["id"]))
            # Only after commit; a failed/missing retained image aborts deletion for this source.
            from mindex.maintenance.clean import _iter_files, _prune_empty_parents
            for path in _iter_files(directory):
                if path.relative_to(store.root).as_posix() not in keep:
                    report["deleted_files"] += 1
                    report["deleted_bytes"] += path.stat().st_size
                    if apply:
                        path.unlink()
                        _prune_empty_parents(path, base)
        except Exception as exc:  # noqa: BLE001 — isolate sources, never delete after a failed conversion
            report["error"] = str(exc)
    totals = {key: sum(r[key] for r in reports) for key in
              ("kept_frames", "reencoded", "unchanged", "deleted_files", "deleted_bytes",
               "rewritten_evidence", "rewritten_features")}
    return {"applied": apply, "sources": reports, "total": totals}


def encode_frame(raw, *, max_edge=0, image_format="PNG", jpeg_quality=85):
    with Image.open(io.BytesIO(raw)) as original:
        if original.width * original.height > 40_000_000:
            raise ValueError("图片尺寸过大")
        image = ImageOps.exif_transpose(original).convert("RGB")
        if max_edge and max(image.size) > max_edge:
            image = ImageOps.contain(image, (max_edge, max_edge))
        buffer = io.BytesIO()
        image.save(buffer, format=image_format, **({"quality": jpeg_quality} if image_format == "JPEG" else {}))
    return buffer.getvalue(), image.width, image.height
