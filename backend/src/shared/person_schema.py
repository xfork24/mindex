"""人物库的 SQLite 表结构、迁移与实体规范化。

放在 shared 层，使 `Store` 的 v1 迁移（`storage.METADATA_STEPS`）不必依赖 persons 包；人物业务逻辑仍在
`persons/persons.py`。
"""

from __future__ import annotations

import json

from shared.config import fingerprint
from shared.migrations import execute_script
from shared.storage import dumps

SCHEMA = (
    "\n"
    "CREATE TABLE IF NOT EXISTS person_entities(id TEXT PRIMARY KEY, kind TEXT NOT "
    "NULL, data TEXT NOT NULL);\n"
    "CREATE TABLE IF NOT EXISTS person_sources(source_id TEXT PRIMARY KEY "
    "REFERENCES sources(id), work_id TEXT NOT NULL);\n"
    "CREATE TABLE IF NOT EXISTS person_references(id TEXT PRIMARY KEY, actor_id "
    "TEXT NOT NULL, stage_id TEXT,\n"
    " path TEXT NOT NULL, digest TEXT NOT NULL, data TEXT NOT NULL, role_id TEXT);\n"
    "CREATE TABLE IF NOT EXISTS person_imports(id TEXT PRIMARY KEY, data TEXT NOT "
    "NULL, status TEXT NOT NULL DEFAULT 'draft');\n"
    "CREATE TABLE IF NOT EXISTS person_features(id TEXT PRIMARY KEY, source_id "
    "TEXT, space TEXT NOT NULL,\n"
    " kind TEXT NOT NULL, data TEXT NOT NULL);\n"
    "CREATE INDEX IF NOT EXISTS person_features_source ON "
    "person_features(source_id,kind,space);\n"
    "CREATE TABLE IF NOT EXISTS person_evidence(id TEXT PRIMARY KEY, source_id "
    "TEXT NOT NULL,\n"
    " shot_id TEXT NOT NULL, actor_id TEXT, role_id TEXT, stage_id TEXT, start "
    "REAL NOT NULL, end REAL NOT NULL,\n"
    " status TEXT NOT NULL, data TEXT NOT NULL);\n"
    "CREATE INDEX IF NOT EXISTS person_evidence_source ON "
    "person_evidence(source_id,status,start,end);\n"
    "CREATE INDEX IF NOT EXISTS person_evidence_shot ON "
    "person_evidence(source_id,shot_id);\n"
    "-- 出镜审核列表只需要证据行的少数 JSON 字段。data 平均近 7 KB（含 "
    "body_feature），读它会触发溢出页 I/O，\n"
    "-- 这个覆盖索引让审核列表的状态聚合与排名完全不读 data 列。\n"
    "CREATE INDEX IF NOT EXISTS person_evidence_review ON person_evidence(\n"
    " source_id,shot_id,status,actor_id,role_id,start,id,\n"
    " json_extract(data,'$.confirmation'),json_extract(data,'$.shot_excluded'),\n"
    " json_type(data,'$.score'),json_extract(data,'$.score')"
    ",json_extract(data,'$.reviewed_at'));\n"
    "CREATE TABLE IF NOT EXISTS person_coverage(source_id TEXT NOT NULL, cache_id "
    "TEXT PRIMARY KEY, data TEXT NOT NULL);\n"
    "CREATE INDEX IF NOT EXISTS person_coverage_shot\n"
    " ON person_coverage(source_id,json_extract(data,'$.shot_id'));\n"
    "CREATE TABLE IF NOT EXISTS person_tasks(id TEXT PRIMARY KEY, source_id TEXT "
    "NOT NULL, status TEXT NOT NULL,\n"
    " config TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL DEFAULT "
    "'{}', error TEXT, result TEXT,\n"
    " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL "
    "DEFAULT CURRENT_TIMESTAMP);\n"
    "CREATE TABLE IF NOT EXISTS person_versions(id TEXT PRIMARY KEY, data TEXT NOT "
    "NULL,\n"
    " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);\n"
    "CREATE TABLE IF NOT EXISTS search_releases(id TEXT PRIMARY KEY, content_id "
    "TEXT NOT NULL,\n"
    " person_id TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);\n"
)


def init_person_schema(db):
    """metadata 库 v1 迁移的人物部分：幂等，可在调用方事务内执行（由 ``migrate`` 串行化）。"""
    execute_script(db, SCHEMA)
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
        "UPDATE person_evidence SET role_id=COALESCE(stage_id,role_id),stage_id=NULL WHERE stage_id IS NOT "
        "NULL"
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
        "AS removed_confirmation FROM person_evidence WHERE " + predicate + " GROUP BY source_id,shot_id"
    ).fetchall():
        key = fingerprint([marker, row["source_id"], row["shot_id"]])
        db.execute(
            "INSERT OR IGNORE INTO person_coverage VALUES(?,?,?)",
            (
                row["source_id"],
                key,
                dumps(
                    {
                        "shot_id": row["shot_id"],
                        "start": row["start"],
                        "end": row["end"],
                        "migration": marker,
                        "removed_confirmation": bool(row["removed_confirmation"]),
                    }
                ),
            ),
        )
    db.execute("DELETE FROM person_evidence WHERE " + predicate)
    db.execute("INSERT INTO settings(key,value) VALUES(?, 'complete')", (marker,))


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
