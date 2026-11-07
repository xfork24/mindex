"""迁移秘密字段登记：已登记配置裁剪，未知载体只报告位置、不改写用户内容。"""

from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import parse_qsl, urlsplit

import yaml

from maintenance.backup_db import SECRET_FIELDS, database, json_container, schema
from maintenance.backup_files import safe_path

CONFIG_COLUMNS = {
    "sources": {"media", "layout_config"},
    "index_versions": {"spec"},
    "model_profiles": {"config"},
    "source_tasks": {"config", "options"},
    "dialogue_tasks": {"config", "options"},
    "person_tasks": {"config", "options"},
    "index_tasks": {"config", "options"},
    "studio_tasks": {"config", "options"},
    "topic_runs": {"config", "options"},
    "topic_sources": {"config"},
    "tool_sources": {"connection"},
}
NON_SECRET_KEYS = {
    "context_token",
    "range_token",
    "tokenizer_version",
    "fencing_token",
    "queue_fencing_token",
    "input_tokens",
    "output_tokens",
    "max_tokens",
    "token_count",
    "tokens_per_second",
}
_PATTERN = re.compile(
    (
        "(?:^|[._-])(?:api[-_]?key|access[-_]?key|private[-_]?key|apikey|token|secret|password|passwd|passp"
        "hrase|passcode|authorization|auth|credential)(?:$|[._-])"
    ),
    re.I,
)


def secret_key(key):
    key = str(key).lower()
    return (
        key not in NON_SECRET_KEYS
        and not key.endswith(("_env", "_ref", "_reference"))
        and (key in SECRET_FIELDS or _PATTERN.search(key) is not None)
    )


def redact_config(value):
    if isinstance(value, dict):
        return {
            key: "" if secret_key(key) and isinstance(item, str) else redact_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


def redact_metadata(path):
    with database(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        tables = schema(db)
        for table in ("migration_objects", "migration_stages", "migration_runs"):
            db.execute(f"DELETE FROM {table}")
        for table in sorted(tables):
            for row in db.execute(f"SELECT rowid AS sequence,* FROM {table}").fetchall():
                changes = {}
                for column in row.keys():
                    if column == "sequence":
                        continue
                    value = row[column]
                    if column in SECRET_FIELDS or (table == "settings" and secret_key(row["key"])):
                        if value:
                            changes[column] = None if column != "value" else ""
                        continue
                    registered = column in CONFIG_COLUMNS.get(table, set()) or (
                        table == "settings"
                        and column == "value"
                        and row["key"].startswith(("override.", "profile."))
                    )
                    parsed = json_container(value) if registered else None
                    if parsed is not None:
                        clean = json.dumps(redact_config(parsed), ensure_ascii=False, separators=(",", ":"))
                        if clean != value:
                            changes[column] = clean
                if changes:
                    db.execute(
                        f"UPDATE {table} SET "
                        + ",".join(f"{column}=?" for column in changes)
                        + " WHERE rowid=?",
                        (*changes.values(), row["sequence"]),
                    )
        db.commit()
        db.execute("VACUUM")
        db.execute("PRAGMA journal_mode=DELETE")


def locations(value, path):
    if isinstance(value, dict):
        for key, item in value.items():
            location = path + "." + str(key)
            if secret_key(key) and isinstance(item, str) and item.strip():
                yield location
            else:
                yield from locations(item, location)
    elif isinstance(value, list):
        for number, item in enumerate(value):
            yield from locations(item, f"{path}[{number}]")
    elif isinstance(value, str) and value.startswith(("http://", "https://", "postgresql://")):
        parsed = urlsplit(value)
        if parsed.password or parsed.username:
            yield path + ".url_credentials"
        for key, item in parse_qsl(parsed.query):
            if secret_key(key) and item:
                yield path + ".url_query." + key


def scan_bundle(source):
    found = []
    with database(f"{(source / 'metadata.sqlite3').as_uri()}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for table in sorted(schema(db)):
            cursor = db.execute(f"SELECT rowid AS sequence,* FROM {table}")
            while rows := cursor.fetchmany(256):
                for row in rows:
                    for key in row.keys():
                        if key == "sequence":
                            continue
                        value = row[key]
                        parsed = json_container(value)
                        location = f"{table}[{row['sequence']}].{key}"
                        if secret_key(key) and isinstance(value, str) and value.strip():
                            found.append(location)
                        elif parsed is not None:
                            found.extend(locations(parsed, location))
                        elif isinstance(value, str):
                            found.extend(locations(value, location))
    manifest = source / "media_manifest.json"
    if manifest.is_file():
        for key in json.loads(manifest.read_text()):
            file = safe_path(source / "data", key)
            if file.suffix not in {".json", ".yaml", ".yml"}:
                continue

            try:
                if file.suffix == ".json":
                    value = json.loads(file.read_text())
                else:
                    value = yaml.safe_load(file.read_text())
            except (ValueError, UnicodeError, yaml.YAMLError):
                continue  # 用户自由内容不是可解析配置，不改写它。
            found.extend(locations(value, "media:" + key))
    return sorted(set(found))
