"""Live organization metadata, deliberately excluded from immutable embedding snapshots."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindex.shared.storage import Store

Visibility = Literal["visible", "hidden", "all"]


class EntityNotFound(ValueError):
    pass


class CurationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=200)
    notes: str = Field(default="", max_length=4000)
    favorite: bool = False
    hidden: bool = False

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value):
        return value.strip() or None if value is not None else None


SOURCE_SQL = """
SELECT s.*, c.display_name AS custom_name, COALESCE(c.notes,'') AS notes,
       COALESCE(c.favorite,0) AS favorite, COALESCE(c.hidden,0) AS hidden, c.updated_at,
       COALESCE(n.shot_count,0) AS shot_count, COALESCE(n.moment_count,0) AS moment_count,
       COALESCE(n.scene_count,0) AS scene_count
FROM sources s LEFT JOIN source_curation c ON c.id=s.id
LEFT JOIN (SELECT source_id, SUM(kind='shot') AS shot_count, SUM(kind='moment') AS moment_count,
                  SUM(kind='scene') AS scene_count
           FROM segments GROUP BY source_id) n ON n.source_id=s.id
"""

SEGMENT_SQL = """
SELECT g.*, s.name AS original_source_name, sc.display_name AS custom_source_name,
       c.display_name AS custom_name, COALESCE(c.notes,'') AS notes,
       COALESCE(c.favorite,0) AS favorite, COALESCE(c.hidden,0) AS hidden, c.updated_at,
       (COALESCE(sc.hidden,0) OR COALESCE(pc.hidden,0)) AS inherited_hidden,
       (COALESCE(c.hidden,0) OR COALESCE(sc.hidden,0) OR COALESCE(pc.hidden,0)) AS effective_hidden,
       (SELECT COUNT(*) FROM segments child WHERE child.parent_id=g.id) AS child_count,
       (SELECT COUNT(*)+1 FROM segments prior WHERE prior.source_id=g.source_id AND prior.kind=g.kind
          AND (prior.start,prior.end,prior.id)<(g.start,g.end,g.id)) AS position
FROM segments g JOIN sources s ON s.id=g.source_id
LEFT JOIN segment_curation c ON c.id=g.id
LEFT JOIN source_curation sc ON sc.id=s.id
LEFT JOIN segment_curation pc ON pc.id=g.parent_id
"""


def organization(row: dict) -> dict:
    return {
        "custom_name": row["custom_name"],
        "notes": row["notes"],
        "favorite": bool(row["favorite"]),
        "hidden": bool(row["hidden"]),
        "updated_at": row["updated_at"],
    }


def source_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["custom_name"] or row["name"],
        "original_name": row["name"],
        "media": json.loads(row["media"]),
        "prepared": bool(row["prepared"]),
        "shot_count": row["shot_count"],
        "moment_count": row["moment_count"],
        "scene_count": row["scene_count"],
        **organization(row),
    }


def segment_public(row: dict) -> dict:
    prefix = {"shot": "镜头", "moment": "精细片段", "scene": "场景"}[row["kind"]]
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "kind": row["kind"],
        "name": row["custom_name"] or f"{prefix} {row['start']:.3f}–{row['end']:.3f}s",
        "source_name": row["custom_source_name"] or row["original_source_name"],
        "original_source_name": row["original_source_name"],
        "start": row["start"],
        "end": row["end"],
        "duration": row["end"] - row["start"],
        "parent_id": row["parent_id"],
        "position": row["position"],
        "child_count": row["child_count"],
        "reason": row["reason"],
        "effective_hidden": bool(row["effective_hidden"]),
        "inherited_hidden": bool(row["inherited_hidden"]),
        "media_url": f"/api/admin/segments/{row['id']}/media",
        "media_revision": hashlib.sha256(
            f"{row['path']}:{row['start']}:{row['end']}".encode()).hexdigest()[:20],
        "thumbnail_url": f"/api/admin/segments/{row['id']}/thumbnail",
        **organization(row),
    }


class Library:
    def __init__(self, store: Store):
        self.store = store

    def source_row(self, identifier: str) -> dict:
        row = self.store.one(SOURCE_SQL + " WHERE s.id=?", (identifier,))
        if row is None:
            raise EntityNotFound("找不到素材")
        return row

    def segment_row(self, identifier: str) -> dict:
        row = self.store.one(SEGMENT_SQL + " WHERE g.id=?", (identifier,))
        if row is None:
            raise EntityNotFound("找不到片段")
        return row

    def source(self, identifier: str, *, detail="full") -> dict:
        row = self.source_row(identifier)
        if detail == "summary":
            return source_public(row)
        # Bounded artifact checks support a real status even before an index exists.
        from mindex.shared.shot_media import ShotMedia
        segments = self.store.segments(identifier)
        missing = segments
        media = ShotMedia(self.store, reuse_checks=True)
        media._sources[identifier] = row
        media._shots[identifier] = [s for s in segments if s['kind'] == 'shot']
        for shot in media._shots[identifier]:
            path = (self.store.root / shot['path']).resolve()
            if path.is_relative_to(self.store.root) and path.is_file():
                media._paths[shot['path']] = path
        references = {r['path']: r for r in self.store.rows(
            "SELECT path,source_id,start,end FROM media_references WHERE source_id=?", (identifier,))}
        references.update({s['path']: references.get(s['path'], s) for s in segments
                           if s['kind'] in ('moment', 'scene')})
        def available(relative):
            if relative in media._paths:
                return True
            path = (self.store.root / relative).resolve()
            if not path.is_relative_to(self.store.root):
                return False
            if path.is_file():
                return True
            ref = references.get(relative)
            if ref:
                try:
                    media.references(ref['source_id'], ref['start'], ref['end'])
                    return True
                except (ValueError, OSError):
                    pass
            return False
        missing_count = sum(not available(s[field]) for s in missing for field in ('path', 'thumbnail'))
        from mindex.processing.dialogue import current_dialogue, indexed_dialogue, relative_dialogue
        from mindex.search.search import channel_coverage
        from mindex.shared.config import fingerprint
        from mindex.shared.media import MediaError

        channel = self.store.one(
            "SELECT * FROM channel_data WHERE source_id=? AND channel='dialogue'", (identifier,)
        )
        dialogue = json.loads(channel["data"]) if channel else {}
        if not channel:
            dialogue = current_dialogue(self.store, identifier) or {}
            if not dialogue:
                legacy = self.store.one(
                    "SELECT data FROM transcripts WHERE source_id=? ORDER BY rowid DESC LIMIT 1",
                    (identifier,),
                )
                dialogue = json.loads(legacy["data"]) if legacy else {}
        if channel and channel["status"] == "processing":
            from math import ceil

            checkpoints = self.store.root / "dialogue" / identifier / channel["profile"]
            dialogue["completed_chunks"] = sum(p.stem.isdigit() for p in checkpoints.glob("*.json"))
            dialogue["total_chunks"] = ceil(json.loads(row["media"])["duration"] / 30)
        tracks = dialogue.get("subtitle_tracks")
        if tracks is None:
            try:
                from mindex.processing.subtitle_archive import load_archive
                tracks = load_archive(self.store, identifier)["tracks"]
            except (MediaError, FileNotFoundError):
                tracks = []
        annotations = self.store.one(
            "SELECT COUNT(DISTINCT a.segment_id) AS n FROM annotations a JOIN segments s ON s.id=a.segment_id WHERE s.source_id=?",
            (identifier,),
        )["n"]
        channel_states = {
            "visual": {
                "status": "complete" if row["prepared"] and not missing_count else "partial",
                "processed_segments": len(missing),
            },
            "dialogue": {
                key: dialogue.get(key)
                for key in (
                    "status",
                    "source",
                    "selected_track",
                    "subtitle_file",
                    "fallback_reason",
                    "error",
                    "completed_chunks",
                    "total_chunks",
                    "content_version",
                    "model_identity",
                    "input_fingerprint",
                )
            },
            "analysis": {
                "status": "complete"
                if annotations == len(missing) and annotations
                else "partial"
                if annotations
                else "not_processed",
                "processed_segments": annotations,
            },
        }
        channel_states["dialogue"]["status"] = channel_states["dialogue"]["status"] or "not_processed"
        ready_dialogue = indexed_dialogue(current_dialogue(self.store, identifier))
        channel_states["dialogue"]["content_segments"] = (
            sum(
                bool(relative_dialogue(ready_dialogue, segment["start"], segment["end"]))
                for segment in self.store.segments(identifier, "moment")
            )
            if ready_dialogue
            else 0
        )
        channel_states["visual"].update(
            input_fingerprint=fingerprint([row["id"], row["layout_key"]]),
            content_version=fingerprint([row["id"], row["layout_key"], sorted(s["path"] for s in missing)]),
            model_identity={"processor": "ffmpeg", "layout_key": row["layout_key"]},
        )
        analysis_rows = self.store.rows(
            "SELECT a.segment_id,a.profile,a.data FROM annotations a JOIN segments s ON s.id=a.segment_id "
            "WHERE s.source_id=? ORDER BY a.rowid DESC",
            (identifier,),
        )
        latest = {}
        for record in analysis_rows:
            latest.setdefault(record["segment_id"], record)
        identities = {}
        for record in latest.values():
            value = json.loads(record["data"])
            model = value.get("model_identity", {"profile": record["profile"]})
            identities[fingerprint(model)] = model
        channel_states["analysis"].update(
            content_version=fingerprint(latest) if latest else None,
            input_fingerprint=fingerprint(sorted({r["profile"] for r in latest.values()}))
            if latest
            else None,
            model_identity=list(identities.values()) or None,
        )
        for name, stages in (("visual", ("detect", "prepare")), ("analysis", ("analyze",))):
            marks = ",".join("?" for _ in stages)
            run = self.store.one(
                f"SELECT status,message FROM runs WHERE source_id=? AND stage IN ({marks}) ORDER BY id DESC LIMIT 1",
                (identifier, *stages),
            )
            if run:
                channel_states[name]["processing_status"] = run["status"]
                channel_states[name]["error"] = run["message"] if run["status"] == "failed" else None
        task = self.store.one(
            "SELECT * FROM dialogue_tasks WHERE source_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (identifier,),
        )
        from mindex.admin.dialogue_tasks import task_public
        from mindex.processing.subtitle_uploads import uploaded_subtitles

        active = self.store.active_index()
        published = self.store.snapshots(active["id"], identifier) if active else []
        result = {
            **source_public(row),
            "missing_artifacts": missing_count,
            "completed_shots": sum(available(s["path"]) and available(s["thumbnail"]) for s in media._shots[identifier]),
            "channels": channel_states,
            "published_channels": channel_coverage(self.store, source_id=identifier, snapshots=published),
            "subtitle_tracks": tracks,
            "subtitle_files": uploaded_subtitles(self.store, identifier),
            "dialogue_task": task_public(task) if task else None,
            "runs": self.store.rows(
                "SELECT * FROM runs WHERE source_id=? ORDER BY id DESC LIMIT 5", (identifier,)
            ),
        }
        from mindex.admin.source_work import source_work
        result["work"] = source_work(self.store, result, segments=segments, analysis_rows=analysis_rows,
                                     active=active, published=published)
        return result

    def list_sources(self, q: str, visibility: Visibility, favorite_only: bool, page: int, page_size: int):
        clauses, args = [], []
        if q.strip():
            clauses.append(
                "(INSTR(LOWER(s.name),LOWER(?))>0 OR INSTR(LOWER(COALESCE(c.display_name,'')),LOWER(?))>0)"
            )
            args.extend([q.strip(), q.strip()])
        if visibility != "all":
            clauses.append("COALESCE(c.hidden,0)=?")
            args.append(int(visibility == "hidden"))
        if favorite_only:
            clauses.append("COALESCE(c.favorite,0)=1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self.store.one("SELECT COUNT(*) AS n FROM (" + SOURCE_SQL + where + ")", args)["n"]
        rows = self.store.rows(
            SOURCE_SQL + where + " ORDER BY LOWER(COALESCE(c.display_name,s.name)),s.id LIMIT ? OFFSET ?",
            [*args, page_size, (page - 1) * page_size],
        )
        return {
            "items": [source_public(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def list_segments(
        self,
        source_id: str,
        kind: str,
        parent_id: str | None,
        visibility: Visibility,
        favorite_only: bool,
        page: int,
        page_size: int,
    ):
        self.source_row(source_id)
        clauses, args = ["g.source_id=?", "g.kind=?"], [source_id, kind]
        if parent_id:
            parent = self.segment_row(parent_id)
            if parent["source_id"] != source_id or parent["kind"] != "shot" or kind != "moment":
                raise ValueError("父镜头必须属于当前素材，且仅能用于筛选精细片段")
            clauses.append("g.parent_id=?")
            args.append(parent_id)
        if visibility != "all":
            clauses.append("(COALESCE(c.hidden,0) OR COALESCE(sc.hidden,0) OR COALESCE(pc.hidden,0))=?")
            args.append(int(visibility == "hidden"))
        if favorite_only:
            clauses.append("COALESCE(c.favorite,0)=1")
        where = " WHERE " + " AND ".join(clauses)
        total = self.store.one("SELECT COUNT(*) AS n FROM (" + SEGMENT_SQL + where + ")", args)["n"]
        rows = self.store.rows(
            SEGMENT_SQL + where + " ORDER BY g.start,g.end,g.id LIMIT ? OFFSET ?",
            [*args, page_size, (page - 1) * page_size],
        )
        return {
            "items": [segment_public(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def patch(self, kind: Literal["source", "segment"], identifier: str, value: CurationPatch):
        self.source_row(identifier) if kind == "source" else self.segment_row(identifier)
        changes = value.model_dump(exclude_unset=True)
        if not changes:
            raise ValueError("请提供需要修改的字段")
        table = kind + "_curation"
        # Update only the supplied fields in one transaction, preserving concurrent edits.
        with self.store.connect() as db:
            db.execute(f"INSERT OR IGNORE INTO {table}(id) VALUES(?)", (identifier,))
            assignments = ",".join(f"{name}=?" for name in changes)
            db.execute(
                f"UPDATE {table} SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=?",
                [*changes.values(), identifier],
            )
        return self.source(identifier) if kind == "source" else segment_public(self.segment_row(identifier))

    def overlays(self) -> tuple[dict, dict]:
        sources = {
            r["id"]: r
            for r in self.store.rows(
                "SELECT s.id,s.name,c.display_name,c.hidden FROM sources s LEFT JOIN source_curation c ON c.id=s.id"
            )
        }
        segments = {
            r["id"]: r
            for r in self.store.rows(
                "SELECT g.id,g.parent_id,c.display_name,c.favorite,c.hidden FROM segments g LEFT JOIN segment_curation c ON c.id=g.id"
            )
        }
        return sources, segments

    @staticmethod
    def decorate(snapshot: dict, sources: dict, segments: dict) -> dict:
        source = sources.get(snapshot["source_id"], {})
        segment = segments.get(snapshot["id"], {})
        parent = segments.get(segment.get("parent_id") or snapshot.get("parent_id"), {})
        return {
            **snapshot,
            "source_name": source.get("display_name") or source.get("name") or snapshot["source_name"],
            "display_name": segment.get("display_name"),
            "favorite": bool(segment.get("favorite")),
            "effective_hidden": bool(source.get("hidden") or segment.get("hidden") or parent.get("hidden")),
        }
