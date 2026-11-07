"""权威资源引用闭包：业务媒体、检查点、目录历史及外部回执共用。"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from shared.directory_files import document_digest, validate_entry

PATH_FIELDS = frozenset(
    {
        "path",
        "thumbnail",
        "source_path",
        "image_path",
        "sample_path",
        "object_key",
        "$bytes",
        "input_path",
        "output_path",
        "audio_path",
        "video_path",
    }
)
PREFIXES = (
    "sources/",
    "clips/",
    "thumbnails/",
    "persons/",
    "music/",
    "voices/",
    "studio/",
    "topics/",
    "plans/",
    "transcripts/",
    "annotations/",
    "subtitles/",
    "objects/",
    "uploads/",
    "external/",
)
REFERENCE_COLUMNS = {
    "sources": ("path",),
    "segments": ("path", "thumbnail"),
    "index_members": ("snapshot",),
    "media_references": ("refs",),
    "person_references": ("path", "data"),
    "person_versions": ("data",),
    "person_evidence": ("data",),
    "person_imports": ("data",),
    "voices": ("sample_path",),
    "topic_sources": ("config",),
}


def path_values(value, *, field=None):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from path_values(item, field=key)
    elif isinstance(value, list):
        for item in value:
            yield from path_values(item, field=field)
    elif isinstance(value, str) and field in PATH_FIELDS and value.startswith(PREFIXES):
        path = PurePosixPath(value)
        if ".." in path.parts or path.is_absolute() or "\n" in value:
            raise ValueError("权威资源引用路径越界")
        yield value


def _rows(db, table, fields, *, page=256):
    cursor = 0
    sequence = "id" if table in {"voices", "model_profiles", "runs", "studio_events"} else "rowid"
    while True:
        rows = db.execute(
            f"SELECT {sequence} AS sequence,{','.join(fields)} FROM {table} "
            f"WHERE {sequence}>? ORDER BY {sequence} LIMIT ?",
            (cursor, page),
        ).fetchall()
        if not rows:
            return
        yield from rows
        cursor = rows[-1]["sequence"]


def referenced_objects(store, *, connection=None):
    if connection is None:
        with store.connect() as db:
            yield from referenced_objects(store, connection=db)
        return
    db = connection
    for row in _rows(db, "shared_files", ("object_key", "size")):
        if row["size"] >= 0:
            yield row["object_key"]

    for row in _rows(db, "directory_manifests", ("revision", "path", "document"), page=32):
        document = json.loads(row["document"])
        if document_digest(document) != row["revision"] or document.get("path") != row["path"]:
            raise ValueError("目录历史清单身份不符，拒绝备份或删除")
        for key, entry in document["files"].items():
            validate_entry(row["path"], key, entry)
            yield entry["object_key"]
    for table, fields in REFERENCE_COLUMNS.items():
        for row in _rows(db, table, fields):
            for field in fields:
                raw = row[field]
                if not raw:
                    continue
                try:
                    value = json.loads(raw)
                except (ValueError, TypeError):
                    value = raw
                for logical in path_values(value, field=field):
                    pointer = db.execute(
                        "SELECT object_key,size FROM shared_files WHERE path=?", (logical,)
                    ).fetchone()
                    if pointer:
                        if pointer["size"] >= 0:
                            yield pointer["object_key"]
                    else:
                        yield logical
    for row in _rows(db, "music_tracks", ("id", "suffix")):
        logical = f"music/{row['id']}{row['suffix']}"
        pointer = db.execute("SELECT object_key,size FROM shared_files WHERE path=?", (logical,)).fetchone()
        yield pointer["object_key"] if pointer and pointer["size"] >= 0 else logical
    for row in _rows(db, "external_calls", ("receipt",)):
        if row["receipt"]:
            yield from path_values(json.loads(row["receipt"]))
    for row in _rows(db, "uploaded_assets", ("object_key",)):
        yield row["object_key"]
    for row in db.execute(
        "SELECT pending_key FROM upload_sessions WHERE state IN ('pending','completing') "
        "AND pending_key IS NOT NULL"
    ).fetchall():
        yield row["pending_key"]
    rows = db.execute(
        "SELECT p.object_key FROM upload_parts p JOIN upload_sessions s ON s.id=p.session_id "
        "WHERE s.state IN ('pending','completing')"
    ).fetchall()
    for row in rows:
        yield row["object_key"]


def referenced(store, key, *, connection=None):
    return any(value == key for value in referenced_objects(store, connection=connection))


def required_objects(store):
    """正式迁移/备份必须存在的权威资源；归档原片与可重建片段单独保留可选语义。"""
    for key in referenced_objects(store):
        if key.startswith(("objects/", "uploads/", "external/", "pending/", "persons/", "music/", "voices/")):
            yield key
    with store.connect() as db:

        def authority(key):
            pointer = db.execute("SELECT object_key,size FROM shared_files WHERE path=?", (key,)).fetchone()
            return pointer["object_key"] if pointer and pointer["size"] >= 0 else key

        for row in _rows(db, "sources", ("id", "path", "prepared")):
            if (
                not row["prepared"]
                or not db.execute(
                    "SELECT 1 FROM segments WHERE source_id=? AND kind='shot' LIMIT 1", (row["id"],)
                ).fetchone()
            ):
                yield authority(row["path"])
        for row in _rows(db, "segments", ("path", "thumbnail", "kind")):
            if row["kind"] == "shot":
                yield authority(row["path"])
                if row["thumbnail"]:
                    yield authority(row["thumbnail"])
