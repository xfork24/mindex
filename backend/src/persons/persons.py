"""Person catalogue, reviewable evidence and immutable retrieval releases.

Names never establish identity. Actors and work-scoped roles have separate IDs;
only accepted evidence is eligible for onscreen retrieval.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
from collections import OrderedDict
from contextlib import nullcontext
from datetime import UTC, datetime
from threading import Lock

from PIL import Image, ImageOps, UnidentifiedImageError

from mindex.persons.person_models import (
    CastConfirm,
    Entity,
    PersonHasLinkedShots,
    Review,
    ShotReview,
    checked_box,
    frame_review_lock,
    uid,
)
from mindex.persons.profiling import stage
from mindex.shared.config import fingerprint
from mindex.shared.person_schema import normalize_entity
from mindex.shared.storage import Store, dumps

log = logging.getLogger(__name__)

# 检索热路径的已发布人物数据缓存：只保留最近使用的少数版本。
PUBLISHED_CACHE_ENTRIES = 2
_ENTITY_CACHE: OrderedDict = OrderedDict()
_EVIDENCE_CACHE: OrderedDict = OrderedDict()
# 历史版本的“证据 id -> 截图路径”小索引（每版本约几 MB 以内）；版本不可变，所以多留一些。
VERSION_IMAGE_CACHE_ENTRIES = 32
_VERSION_IMAGE_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = Lock()


def _cache_get(cache: OrderedDict, key):
    with _CACHE_LOCK:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _cache_put(cache: OrderedDict, key, value, limit=PUBLISHED_CACHE_ENTRIES):
    with _CACHE_LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)
    return value


def _reference_value(row):
    """person_references 行 → 接口形状（去掉 stage_id，解码 data，附图片地址）。"""
    value = {
        **{k: v for k, v in row.items() if k != "stage_id"},
        "data": json.loads(row["data"]),
        "url": "/api/persons/references/" + row["id"] + "/image",
    }
    if row["role_id"]:
        value["actor_ids"] = [row["actor_id"]]
    return value


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
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM search_releases WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("找不到检索发布记录")
            active = db.execute("SELECT value FROM settings WHERE key='active.release'").fetchone()
            if active and active["value"] == identifier:
                raise ValueError("这是当前生效的检索组合；请先恢复到其他组合再删除")
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
        return _reference_value(row)

    def references(self):
        # 一次查询取全部参考图，避免逐条 reference() 的 N+1 查询（人物库首屏会调用）。
        return [_reference_value(r) for r in self.store.rows("SELECT * FROM person_references ORDER BY rowid")]

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

    # 审核列表与汇总专用。与 SHOT_STATES 语义相同，区别是先把证据行用到的 JSON 字段投影成 ev
    # （走 person_evidence_review 覆盖索引，不读 data 溢出页），聚合与排名都只读 ev。
    # SHOT_STATES 保持原样：流水线按单个分镜调用它，不能被物化成全表扫描。
    @staticmethod
    def _rows(db, sql, args=()):
        return [dict(row) for row in db.execute(sql, args).fetchall()]

    def _build_review_tables(self, db, source_id=None, shot_id=None):
        """在当前连接里建两张临时表，供审核列表的多条语句共用：

        review_ev      证据行及其用到的 JSON 字段（经 person_evidence_review 覆盖索引读取，不碰 data 溢出页）。
        review_states  分镜级 accepted/candidate/rejected 计数，语义与 SHOT_STATES 相同。

        分镜状态只取决于该分镜自己的证据行，所以素材/分镜过滤直接下推到 review_ev。
        临时表只存在于本连接，不写主库；SHOT_STATES 保持原样，流水线按单个分镜调用它。
        """
        conditions, args = [], []
        for column, value in (("source_id", source_id), ("shot_id", shot_id)):
            if value:
                conditions.append(column + "=?")
                args.append(value)
        ev_where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        coverage_where = "".join(
            " AND " + column + "=?" for column, value in (("s.source_id", source_id), ("s.id", shot_id)) if value
        )
        coverage_args = list(args)
        review_ev_sql = (
            "CREATE TEMP TABLE review_ev AS SELECT id,source_id,shot_id,actor_id,role_id,status,start,"
            "json_extract(data,'$.confirmation') AS confirmation,json_extract(data,'$.shot_excluded') AS excluded,"
            "json_type(data,'$.score') AS score_type,json_extract(data,'$.score') AS score_value,"
            "json_extract(data,'$.reviewed_at') AS reviewed_at FROM person_evidence" + ev_where)
        if self.store.pg:
            # 连接池复用连接：临时表以 ON COMMIT DROP 在事务结束时消失，并清掉任何残留。
            db.execute("DROP TABLE IF EXISTS review_ev")
            db.execute(review_ev_sql.replace(
                "CREATE TEMP TABLE review_ev AS", "CREATE TEMP TABLE review_ev ON COMMIT DROP AS"), args)
        else:
            db.execute(review_ev_sql, args)
        review_states_sql = (
            "CREATE TEMP TABLE review_states AS WITH "
            "valid_pairs AS (SELECT DISTINCT ps.source_id,role.id AS role_id,performer.value AS actor_id "
            "FROM person_sources ps JOIN person_entities role ON role.kind='role' "
            "AND EXISTS (SELECT 1 FROM json_each(role.data,'$.work_ids') WHERE value=ps.work_id) "
            "JOIN json_each(role.data,'$.actor_ids') performer "
            "JOIN person_entities actor ON actor.id=performer.value AND actor.kind='actor'), "
            "marks AS (SELECT source_id,shot_id FROM review_ev WHERE status='accepted' "
            "AND confirmation='shot_review' GROUP BY source_id,shot_id), "
            "removed AS (SELECT source_id,json_extract(data,'$.shot_id') AS shot_id FROM person_coverage "
            "WHERE json_extract(data,'$.removed_confirmation')=1 GROUP BY source_id,shot_id) "
            "SELECT e.shot_id AS shot_id,e.source_id AS source_id, "
            "SUM(e.status='accepted' AND pair.role_id IS NOT NULL) AS accepted, "
            "SUM(e.status='candidate' AND pair.role_id IS NOT NULL) AS candidate, "
            "SUM(CASE WHEN e.status='rejected' AND e.confirmation='shot_review' AND marks.shot_id IS NULL "
            "AND (e.excluded=1 OR removed.shot_id IS NULL) THEN 1 ELSE 0 END) AS rejected "
            "FROM review_ev e JOIN segments s ON s.id=e.shot_id AND s.source_id=e.source_id "
            "LEFT JOIN valid_pairs pair ON pair.source_id=e.source_id AND pair.role_id=e.role_id AND pair.actor_id=e.actor_id "
            "LEFT JOIN marks ON marks.source_id=e.source_id AND marks.shot_id=e.shot_id "
            "LEFT JOIN removed ON removed.source_id=e.source_id AND removed.shot_id=e.shot_id "
            "WHERE s.kind='shot' GROUP BY e.shot_id,e.source_id "
            "UNION ALL SELECT DISTINCT s.id,s.source_id,0,0,0 FROM person_coverage c "
            "JOIN segments s ON s.id=json_extract(c.data,'$.shot_id') AND s.source_id=c.source_id "
            "WHERE s.kind='shot' AND NOT EXISTS (SELECT 1 FROM person_evidence e "
            "WHERE e.shot_id=s.id AND e.source_id=s.source_id)" + coverage_where
        )
        if self.store.pg:
            # PG：临时索引不能写 temp. 前缀；jsonb 元素按文本比较；布尔 SUM 显式 CASE。
            db.execute("CREATE INDEX review_ev_shot ON review_ev(source_id,shot_id)")
            db.execute("DROP TABLE IF EXISTS review_states")
            db.execute(
                review_states_sql
                .replace("CREATE TEMP TABLE review_states AS WITH",
                         "CREATE TEMP TABLE review_states ON COMMIT DROP AS WITH")
                .replace("JOIN json_each(role.data,'$.actor_ids') performer",
                         "CROSS JOIN jsonb_array_elements(role.data::jsonb->'actor_ids') performer")
                .replace("JOIN json_each(role.data,'$.work_ids')",
                         "CROSS JOIN jsonb_array_elements(role.data::jsonb->'work_ids')")
                .replace("performer.value AS actor_id", "(performer.value #>> '{}') AS actor_id")
                .replace("actor.id=performer.value", "actor.id=(performer.value #>> '{}')")
                .replace("value=ps.work_id", "(value #>> '{}')=ps.work_id")
                .replace("json_extract(data,'$.removed_confirmation')=1",
                         "(CASE WHEN (data::jsonb->'removed_confirmation')='true'::jsonb THEN 1"
                         " WHEN (data::jsonb->'removed_confirmation')='false'::jsonb THEN 0 ELSE 0 END)=1")
                .replace("SUM(e.status='accepted' AND pair.role_id IS NOT NULL)",
                         "SUM(CASE WHEN e.status='accepted' AND pair.role_id IS NOT NULL THEN 1 ELSE 0 END)")
                .replace("SUM(e.status='candidate' AND pair.role_id IS NOT NULL)",
                         "SUM(CASE WHEN e.status='candidate' AND pair.role_id IS NOT NULL THEN 1 ELSE 0 END)")
                .replace("s.id=json_extract(c.data,'$.shot_id')", "s.id=(c.data::jsonb->>'shot_id')"),
                coverage_args,
            )
            db.execute("CREATE INDEX review_states_shot ON review_states(source_id,shot_id)")
        else:
            db.execute("CREATE INDEX temp.review_ev_shot ON review_ev(source_id,shot_id)")
            db.execute(review_states_sql, coverage_args)
            db.execute("CREATE INDEX temp.review_states_shot ON review_states(source_id,shot_id)")

    def review_summary(self):
        """每个素材的分镜级状态计数；分类规则与审核列表完全一致。"""
        with self.store.connect() as db:
            self._build_review_tables(db)
            rows = self._rows(
                db,
                "SELECT source_id, CASE WHEN accepted>0 THEN 'accepted' "
                "WHEN candidate>0 OR rejected=0 THEN 'candidate' "
                "ELSE 'rejected' END AS bucket, COUNT(*) AS shots FROM review_states"
                " GROUP BY source_id, bucket",
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
        """按分镜分组返回证据；规则见 _review_shots。临时表在本连接内建一次，三条聚合语句与逐镜证据查询共用。"""
        with self.store.connect() as db:
            with stage("states_build"):
                self._build_review_tables(db, source_id, shot_id)
            return self._review_shots(
                db, source_id, status, limit=limit, offset=offset, shot_id=shot_id,
                actor_id=actor_id, role_id=role_id, no_candidate=no_candidate, compact=compact,
            )

    def _review_shots(
        self, db, source_id=None, status=None, *, limit=50, offset=0, shot_id=None,
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
            "k.accepted,k.candidate,k.rejected FROM segments s JOIN review_states k "
            "ON k.shot_id=s.id AND k.source_id=s.source_id" + where + "), "
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
                "CASE WHEN e.score_type IN ('integer','real') THEN e.score_value ELSE 0 END AS score,"
                "ROW_NUMBER() OVER (PARTITION BY e.source_id,e.shot_id ORDER BY "
                "CASE WHEN e.score_type IN ('integer','real') THEN e.score_value ELSE 0 END DESC,"
                "(e.status='accepted') DESC,COALESCE(e.reviewed_at,'') DESC,"
                "e.start,e.id) AS rank "
                "FROM review_ev e JOIN scoped s ON s.shot_id=e.shot_id AND s.source_id=e.source_id "
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
            with stage("filters_sql"):
                filters = self._rows(
                    db, query + "SELECT actor_id,role_id,COUNT(*) AS count FROM memberships "
                    "GROUP BY actor_id,role_id ORDER BY actor_id,role_id",
                    args,
                )
        with stage("scope_total_sql"):
            # classified 对 scoped 的每个分镜恰好产出一行（rank=1 左连接），直接数 scoped 即可，省掉排名。
            scope_total = db.execute(query + "SELECT COUNT(*) AS n FROM scoped", args).fetchone()["n"]
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
        if status == "rejected":
            confirmed_people = "'[]'"
        elif self.store.pg:
            confirmed_people = (
                "(SELECT COALESCE(jsonb_agg(jsonb_build_object("
                "'actor_id',actor_id,'role_id',role_id) ORDER BY actor_id,role_id),'[]'::jsonb)::text "
                "FROM (SELECT actor_id,role_id FROM confirmed c "
                "WHERE c.source_id=s.source_id AND c.shot_id=s.shot_id) q)")
        else:
            confirmed_people = (
                "(SELECT json_group_array(json_object('actor_id',actor_id,'role_id',role_id)) "
                "FROM (SELECT actor_id,role_id FROM confirmed c "
                "WHERE c.source_id=s.source_id AND c.shot_id=s.shot_id ORDER BY actor_id,role_id))")
        with stage("page_sql"):
            shots = self._rows(
                db, query + "SELECT s.*," + confirmed_people + " AS confirmed_people FROM classified s" + pair_where
                + " ORDER BY s.source_id,s.start,s.shot_id LIMIT ? OFFSET ?",
                [*pair_args, limit, offset],
            )
        items = []
        for shot in shots:
            if not compact:
                evidence_sql = ("SELECT * FROM person_evidence e WHERE source_id=? AND shot_id=? AND "
                                + self.VALID_ROLE + " ORDER BY start,id")
            elif self.store.pg:
                score = "(CASE WHEN (data::jsonb->>'score') ~ '^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$' THEN (data::jsonb->>'score')::numeric END)"
                original = "(CASE WHEN (data::jsonb->>'original_score') ~ '^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$' THEN (data::jsonb->>'original_score')::numeric END)"
                evidence_sql = (
                    "SELECT id,actor_id,role_id,status,jsonb_build_object("
                    "'score'," + score + ",'original_score'," + original + ","
                    "'evidence_type',(data::jsonb->>'evidence_type'),"
                    "'confirmation',(data::jsonb->>'confirmation'),"
                    "'image_path',(data::jsonb->>'image_path'))::text "
                    "AS data,jsonb_typeof(data::jsonb->'original_score') IS NOT NULL AS original_recorded "
                    "FROM person_evidence e WHERE source_id=? AND shot_id=? AND " + self.VALID_ROLE
                    + " ORDER BY start,id")
            else:
                evidence_sql = (
                    "SELECT id,actor_id,role_id,status,json_object("
                    "'score',json_extract(data,'$.score'),'original_score',json_extract(data,'$.original_score'),"
                    "'evidence_type',json_extract(data,'$.evidence_type'),'confirmation',json_extract(data,'$.confirmation'),"
                    "'image_path',json_extract(data,'$.image_path')) "
                    "AS data,json_type(data,'$.original_score') IS NOT NULL AS original_recorded "
                    "FROM person_evidence e WHERE source_id=? AND shot_id=? AND " + self.VALID_ROLE + " ORDER BY start,id")
            raw_rows = []
            with stage("evidence_sql"):
                fetched = self._rows(db, evidence_sql, (shot["source_id"], shot["shot_id"]))
            with stage("json_decode"):
                for row in fetched:
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
                         if r.get("data", {}).get("image_path")),
                        f"/api/admin/segments/{shot['shot_id']}/thumbnail",
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
        requested_content_id = content_id
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
            if requested_content_id is None:
                current = db.execute("SELECT value FROM settings WHERE key='active'").fetchone()
                if not current:
                    raise ValueError("请先发布内容索引")
                content_id = current['value']
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

    def release_for(self, content_id, *, pinned=False, release_id=None):
        if release_id:
            return self.store.one(
                "SELECT * FROM search_releases WHERE id=? AND content_id=?", (release_id, content_id)
            )
        if pinned:
            return self.store.one(
                "SELECT * FROM search_releases WHERE content_id=? ORDER BY rowid DESC LIMIT 1", (content_id,)
            )
        return self.store.one(
            "SELECT r.* FROM search_releases r JOIN settings s ON s.value=r.id WHERE s.key='active.release' AND r.content_id=?",
            (content_id,),
        )

    def retain_published(self, keep_person_ids: set) -> int:
        """淘汰缓存中不在 keep_person_ids 里的已发布人物版本（实体与证据索引）。返回淘汰条数。"""
        path = str(self.store.path)
        removed = 0
        with _CACHE_LOCK:
            for cache in (_ENTITY_CACHE, _EVIDENCE_CACHE):
                stale = [key for key in cache if key[0] == path and key[1] not in keep_person_ids]
                for key in stale:
                    del cache[key]
                removed += len(stale)
        return removed

    def _version_key(self, person_id):
        """person_versions 行按内容寻址、写入后不可变；长度进键，防同 id 被离线迁移改写后读到旧缓存。"""
        row = self.store.one("SELECT length(CAST(data AS BLOB)) AS n FROM person_versions WHERE id=?", (person_id,))
        return (str(self.store.path), person_id, row["n"]) if row else None

    def _version_image_index(self, version_id):
        """某个历史版本里 证据 id -> [{shot_id, source_id, data:{image_path}}]。

        版本行写入后不可变，所以每个版本只解析一次并缓存；解析交给 SQLite 的 json_each，
        不把几十 MB 的 JSON 读进 Python。键沿用 _version_key（含长度）。
        """
        key = self._version_key(version_id)
        if key is None:
            return {}
        cached = _cache_get(_VERSION_IMAGE_CACHE, key)
        if cached is not None:
            return cached
        index: dict = {}
        for row in self.store.rows(
            "SELECT json_extract(e.value,'$.id') AS id,json_extract(e.value,'$.shot_id') AS shot_id,"
            "json_extract(e.value,'$.source_id') AS source_id,json_extract(e.value,'$.data.image_path') AS image_path "
            "FROM person_versions v,json_each(v.data,'$.evidence') e WHERE v.id=?", (version_id,),
        ):
            index.setdefault(row["id"], []).append({
                "shot_id": row["shot_id"], "source_id": row["source_id"], "data": {"image_path": row["image_path"]},
            })
        return _cache_put(_VERSION_IMAGE_CACHE, key, index, VERSION_IMAGE_CACHE_ENTRIES)

    def version_evidence(self, evidence_id):
        """按版本从新到旧，惰性产出历史发布版本里同 id 的证据（找到可用截图即可停止遍历）。"""
        for version in self.store.rows("SELECT id FROM person_versions ORDER BY created_at DESC,id"):
            yield from self._version_image_index(version["id"]).get(evidence_id, [])

    def published_entities(self, release):
        """检索热路径：只取已发布版本的实体（几 KB），不解析数十 MB 的证据；结果进程内缓存、只读。"""
        person_id = release["person_id"] if release else None
        key = self._version_key(person_id) if person_id else None
        if key is None:
            return {"entities": []}
        cached = _cache_get(_ENTITY_CACHE, key)
        if cached is not None:
            return cached
        row = self.store.one(
            "SELECT json_extract(data,'$.entities') AS entities, json_extract(data,'$.format') AS format "
            "FROM person_versions WHERE id=?", (person_id,))
        entities = json.loads(row["entities"] or "[]")
        lookup = {e["id"]: e for e in entities}
        value = {"entities": [normalize_entity(e, lookup) for e in entities]}
        if row["format"] is not None:
            value["format"] = row["format"]
        return _cache_put(_ENTITY_CACHE, key, value)

    def published_evidence(self, release):
        """出镜检索才需要的证据与覆盖区间；缓存、只读。

        与 published() 的区别：剔除检索输出本来就会丢弃的 body_feature（约占 58%），并让重复的
        space 共用同一对象，所以常驻内存只有原始 JSON 解析结果的一小部分。
        """
        person_id = release["person_id"] if release else None
        key = self._version_key(person_id) if person_id else None
        if key is None:
            return {"evidence": [], "coverage": []}
        cached = _cache_get(_EVIDENCE_CACHE, key)
        if cached is not None:
            return cached
        row = self.store.one("SELECT data FROM person_versions WHERE id=?", (person_id,))
        data = json.loads(row["data"])
        spaces = {}
        for evidence in data["evidence"]:
            evidence["role_id"] = evidence.pop("stage_id", None) or evidence["role_id"]
            detail = evidence.get("data")
            if isinstance(detail, dict):
                detail.pop("body_feature", None)
                if detail.get("space") is not None:
                    detail["space"] = spaces.setdefault(dumps(detail["space"]), detail["space"])
        from mindex.persons.person_names import EvidenceIndex

        return _cache_put(_EVIDENCE_CACHE, key, {
            "evidence": data["evidence"], "coverage": data["coverage"],
            "evidence_index": EvidenceIndex(data["evidence"]),  # 区间索引随版本缓存，查询时不再逐条扫描
        })

    def published(self, content_id, *, pinned=False, release_id=None, source_id=None):
        release = self.release_for(content_id, pinned=pinned, release_id=release_id)
        if source_id and release and release["person_id"] and self.store.pg:
            # PG 聚合变体：jsonb 聚合 + 文本化（调用方 json.loads）。
            row = self.store.one(
                "SELECT jsonb_build_object("
                "'entities','[]'::jsonb,"
                "'evidence',COALESCE((SELECT jsonb_agg(value) FROM jsonb_array_elements(data::jsonb->'evidence') "
                "WHERE (value::jsonb->>'source_id')=?),'[]'::jsonb),"
                "'coverage',COALESCE((SELECT jsonb_agg(value) FROM jsonb_array_elements(data::jsonb->'coverage') "
                "WHERE (value::jsonb->>'source_id')=?),'[]'::jsonb))::text AS data "
                "FROM person_versions WHERE id=?", (source_id, source_id, release["person_id"]))
        elif source_id and release and release["person_id"]:
            row = self.store.one(
                "SELECT json_object('entities',json('[]'),'evidence',"
                "(SELECT json_group_array(json(value)) FROM json_each(data,'$.evidence') WHERE json_extract(value,'$.source_id')=?),"
                "'coverage',(SELECT json_group_array(json(value)) FROM json_each(data,'$.coverage') WHERE json_extract(value,'$.source_id')=?)) AS data "
                "FROM person_versions WHERE id=?", (source_id, source_id, release["person_id"]))
        else:
            row = (self.store.one("SELECT data FROM person_versions WHERE id=?", (release["person_id"],))
                   if release and release["person_id"] else None)
        value = json.loads(row["data"]) if row else {"entities": [], "evidence": [], "coverage": []}
        lookup = {e["id"]: e for e in value["entities"]}
        value["entities"] = [normalize_entity(e, lookup) for e in value["entities"]]
        for evidence in value["evidence"]:
            evidence["role_id"] = evidence.pop("stage_id", None) or evidence["role_id"]
        return release, value


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


def published_frame_paths(store, source_id=None):
    """不可变人物发布快照仍引用的展示文件不能作为任务垃圾删除。"""
    result = {}
    sql = ("SELECT DISTINCT json_extract(e.value,'$.source_id') AS source_id, "
           "json_extract(e.value,'$.data.image_path') AS path "
           "FROM person_versions v,json_each(v.data,'$.evidence') e "
           "WHERE json_type(e.value,'$.data.image_path')='text'")
    if source_id is not None:
        sql += " AND json_extract(e.value,'$.source_id')=?"
    for row in store.rows(sql, (source_id,) if source_id is not None else ()):
        result.setdefault(row['source_id'], set()).add(row['path'])
    return result


def retained_frame_paths(store, source_id=None, *, include_published=True):
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
    # 没有匹配证据的帧同样可能属于待审核镜头，不能只从 evidence 行推导保留集。
    sql = ("SELECT DISTINCT c.source_id,json_extract(f.value,'$.image_path') AS path "
           "FROM person_coverage c JOIN person_features p ON p.id=c.cache_id, "
           "json_each(p.data,'$.frames') f WHERE p.kind='shot' "
           "AND json_type(f.value,'$.image_path')='text' AND NOT EXISTS "
           "(SELECT 1 FROM person_evidence e WHERE e.source_id=c.source_id "
           "AND e.shot_id=json_extract(c.data,'$.shot_id') AND e.status='accepted')")
    if source_id is not None:
        sql += ' AND c.source_id=?'
    for row in store.rows(sql, (source_id,) if source_id is not None else ()):
        result.setdefault(row['source_id'], set()).add(row['path'])
    if include_published:
        for source, paths in published_frame_paths(store, source_id).items():
            result.setdefault(source, set()).update(paths)
    return result


def migrate_frames(config, *, source_id=None, apply=False):
    """Migrate display assets only. Feature vectors, cache IDs and releases stay intact."""
    from filelock import Timeout

    from mindex.shared import locks

    store = Store(config.data_dir)
    try:
        with locks.acquire_ordered(store.root, ["pipeline", "index-write"], exclusive=True,
                                   timeout={"index-write": 10}), \
                store.run("persons-migrate-frames") if apply else nullcontext():
            return _migrate_frames(config, store, source_id, apply)
    except Timeout:
        raise ValueError("有离线处理任务或索引发布正在运行，请稍后重试") from None


def _migrate_frames(config, store, source_id, apply):
    retained = retained_frame_paths(store, include_published=False)
    published = published_frame_paths(store)
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
            keep.update(path for paths in published.values() for path in paths if path.startswith(prefix))
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
        except Exception as exc:  # 按素材隔离失败，转换失败后绝不删除
            log.warning("人物素材 %s 帧布局转换失败", source, exc_info=True)
            report["error"] = str(exc)
    totals = {key: sum(r[key] for r in reports) for key in
              ("kept_frames", "reencoded", "unchanged", "deleted_files", "deleted_bytes",
               "rewritten_evidence", "rewritten_features")}
    return {"applied": apply, "sources": reports, "total": totals}


def encode_frame(raw, *, max_edge=0, image_format="PNG", jpeg_quality=85):
    try:
        original = Image.open(io.BytesIO(raw))
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        # 非图片或损坏的上传（参考图、演员表截图）是调用方输入错误：422 而不是 500。
        raise ValueError("图片无法解码、文件损坏或尺寸过大") from exc
    with original:
        if original.width * original.height > 40_000_000:
            raise ValueError("图片尺寸过大")
        try:
            image = ImageOps.exif_transpose(original).convert("RGB")
        except OSError as exc:
            raise ValueError("图片无法解码、文件损坏或尺寸过大") from exc
        if max_edge and max(image.size) > max_edge:
            image = ImageOps.contain(image, (max_edge, max_edge))
        buffer = io.BytesIO()
        image.save(buffer, format=image_format, **({"quality": jpeg_quality} if image_format == "JPEG" else {}))
    return buffer.getvalue(), image.width, image.height
