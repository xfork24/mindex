from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "metadata.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS clip_migration_files (
                  path TEXT PRIMARY KEY, source_id TEXT NOT NULL, version TEXT NOT NULL,
                  status TEXT NOT NULL, dependency_digest TEXT,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS media_references (
                  path TEXT PRIMARY KEY, source_id TEXT NOT NULL, start REAL NOT NULL,
                  end REAL NOT NULL, version TEXT NOT NULL, refs TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                  media TEXT NOT NULL, layout_key TEXT NOT NULL, prepared INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS segments (
                  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
                  kind TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
                  path TEXT NOT NULL, thumbnail TEXT NOT NULL, generation TEXT NOT NULL,
                  parent_id TEXT, reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS segments_source ON segments(source_id, kind);
                CREATE INDEX IF NOT EXISTS segments_parent ON segments(parent_id);
                CREATE INDEX IF NOT EXISTS segments_timeline ON segments(source_id,kind,start,end,id);
                CREATE TABLE IF NOT EXISTS source_curation (
                  id TEXT PRIMARY KEY REFERENCES sources(id), display_name TEXT,
                  notes TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
                  hidden INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS segment_curation (
                  id TEXT PRIMARY KEY REFERENCES segments(id), display_name TEXT,
                  notes TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
                  hidden INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS transcripts (
                  source_id TEXT NOT NULL, profile TEXT NOT NULL, data TEXT NOT NULL,
                  PRIMARY KEY(source_id, profile)
                );
                CREATE TABLE IF NOT EXISTS annotations (
                  segment_id TEXT NOT NULL, profile TEXT NOT NULL, data TEXT NOT NULL,
                  PRIMARY KEY(segment_id, profile)
                );
                CREATE TABLE IF NOT EXISTS scene_plans (
                  source_id TEXT NOT NULL, profile TEXT NOT NULL, data TEXT NOT NULL,
                  PRIMARY KEY(source_id, profile)
                );
                CREATE TABLE IF NOT EXISTS index_versions (
                  id TEXT PRIMARY KEY, spec TEXT NOT NULL, status TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS index_members (
                  index_id TEXT NOT NULL, segment_id TEXT NOT NULL, snapshot TEXT NOT NULL,
                  complete INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(index_id, segment_id)
                );
                CREATE TABLE IF NOT EXISTS representations (
                  id TEXT PRIMARY KEY, index_id TEXT NOT NULL, segment_id TEXT NOT NULL,
                  view TEXT NOT NULL, document TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS representations_version ON representations(index_id);
                CREATE TABLE IF NOT EXISTS channel_data (
                  source_id TEXT NOT NULL, channel TEXT NOT NULL, profile TEXT NOT NULL,
                  status TEXT NOT NULL, data TEXT NOT NULL,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY(source_id,channel)
                );
                CREATE TABLE IF NOT EXISTS dialogue_tasks (
                  id TEXT PRIMARY KEY, source_id TEXT, status TEXT NOT NULL,
                  config TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
                  error TEXT, result TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS source_tasks (
                  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, work TEXT NOT NULL, status TEXT NOT NULL,
                  config TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
                  error TEXT, result TEXT, publication TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS model_profiles (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  role TEXT NOT NULL, name TEXT NOT NULL, provider TEXT NOT NULL,
                  config TEXT NOT NULL, api_key TEXT,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  UNIQUE(role, name)
                );
                CREATE TABLE IF NOT EXISTS voices (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, provider TEXT NOT NULL,
                  voice_id TEXT NOT NULL, sample_path TEXT, status TEXT NOT NULL DEFAULT 'ready',
                  notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL,
                  source_id TEXT, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
                );
            """)
            from .persons import init_person_schema

            init_person_schema(db)
            for table, column in (("sources", "layout_config"), ("dialogue_tasks", "publication"),
                                  ("person_tasks", "publication"), ("clip_migration_files", "dependency_digest")):
                if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            columns = {row[1] for row in db.execute("PRAGMA table_info(representations)")}
            if "cache_key" not in columns:
                db.execute("ALTER TABLE representations ADD COLUMN cache_key TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS representations_cache ON representations(cache_key)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()) -> None:
        with self.connect() as db:
            db.execute(sql, args)

    @contextmanager
    def run(self, stage: str, source_id: str | None = None):
        with self.connect() as db:
            run_id = db.execute(
                "INSERT INTO runs(stage,source_id,status) VALUES(?,?,?)", (stage, source_id, "running")
            ).lastrowid
        try:
            yield
        except (Exception, KeyboardInterrupt) as exc:
            self.execute(
                "UPDATE runs SET status='failed',message=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                ((str(exc) or type(exc).__name__)[:2000], run_id),
            )
            raise
        else:
            self.execute(
                "UPDATE runs SET status='complete',finished_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,)
            )

    def sources(self) -> list[dict]:
        return self.rows("SELECT * FROM sources ORDER BY name,id")

    def source_ids(self) -> set[str]:
        """素材 id 全集；创建创作任务时校验所选素材确实存在。"""
        return {row["id"] for row in self.rows("SELECT id FROM sources")}

    def segments(self, source_id: str | None = None, kind: str | None = None) -> list[dict]:
        clauses, args = [], []
        for key, value in [("source_id", source_id), ("kind", kind)]:
            if value is not None:
                clauses.append(f"{key}=?")
                args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.rows("SELECT * FROM segments" + where + " ORDER BY source_id,start,end,kind", args)

    def json_value(self, table: str, id_field: str, identifier: str, profile: str):
        if (table, id_field) not in {
            ("annotations", "segment_id"),
            ("transcripts", "source_id"),
            ("scene_plans", "source_id"),
        }:
            raise ValueError("Unsupported JSON table")
        row = self.one(f"SELECT data FROM {table} WHERE {id_field}=? AND profile=?", (identifier, profile))
        return json.loads(row["data"]) if row else None

    def put_json(self, table: str, id_field: str, identifier: str, profile: str, value):
        if (table, id_field) not in {
            ("annotations", "segment_id"),
            ("transcripts", "source_id"),
            ("scene_plans", "source_id"),
        }:
            raise ValueError("Unsupported JSON table")
        self.execute(
            f"INSERT OR REPLACE INTO {table}({id_field},profile,data) VALUES(?,?,?)",
            (identifier, profile, dumps(value)),
        )

    def active_index(self) -> dict | None:
        row = self.one(
            "SELECT i.*,r.id AS search_release_id,r.person_id AS person_version FROM index_versions i "
            "JOIN settings s ON s.value=i.id AND s.key='active' "
            "LEFT JOIN settings a ON a.key='active.release' "
            "LEFT JOIN search_releases r ON r.id=a.value AND r.content_id=i.id"
        )
        if row:
            row["spec"] = json.loads(row["spec"])
        return row

    def index_version(self, index_id: str) -> dict:
        row = self.one("SELECT * FROM index_versions WHERE id=?", (index_id,))
        if row is None:
            raise ValueError("找不到索引版本")
        row["spec"] = json.loads(row["spec"])
        return row

    def activate(self, index_id: str):
        with self.connect() as db:
            version = db.execute("SELECT status FROM index_versions WHERE id=?", (index_id,)).fetchone()
            if not version or version["status"] != "ready":
                raise ValueError("Only a ready index can be activated")
            pending = db.execute(
                "SELECT count(*) FROM index_members WHERE index_id=? AND complete=0", (index_id,)
            ).fetchone()[0]
            if pending:
                raise ValueError("Index has unfinished segments")
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active',?)", (index_id,))
            release = db.execute(
                "SELECT id FROM search_releases WHERE content_id=? ORDER BY rowid DESC LIMIT 1", (index_id,)
            ).fetchone()
            if release:
                db.execute("INSERT OR REPLACE INTO settings VALUES('active.release',?)", (release['id'],))
            else:
                current = db.execute(
                    "SELECT r.person_id FROM search_releases r JOIN settings s ON s.value=r.id WHERE s.key='active.release'"
                ).fetchone()
                from .config import fingerprint
                person_id = current['person_id'] if current else None
                release_id = fingerprint([index_id, person_id])
                db.execute("INSERT OR IGNORE INTO search_releases(id,content_id,person_id) VALUES(?,?,?)",
                           (release_id, index_id, person_id))
                db.execute("INSERT OR REPLACE INTO settings VALUES('active.release',?)", (release_id,))

    def activate_release(self, release_id: str):
        with self.connect() as db:
            row = db.execute("SELECT r.*,i.status FROM search_releases r JOIN index_versions i ON i.id=r.content_id WHERE r.id=?", (release_id,)).fetchone()
            if not row or row['status'] != 'ready':
                raise ValueError("检索组合不存在或内容索引未就绪")
            if db.execute("SELECT 1 FROM index_members WHERE index_id=? AND complete=0", (row['content_id'],)).fetchone():
                raise ValueError("内容索引有未完成片段")
            db.execute("INSERT OR REPLACE INTO settings VALUES('active',?)", (row['content_id'],))
            db.execute("INSERT OR REPLACE INTO settings VALUES('active.release',?)", (release_id,))

    def snapshots(self, index_id: str) -> list[dict]:
        rows = self.rows("SELECT snapshot FROM index_members WHERE index_id=? AND complete=1", (index_id,))
        return [json.loads(row["snapshot"]) for row in rows]

    def media_available(self, relative: str) -> bool:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            return False
        if path.is_file():
            return True
        reference = self.one("SELECT source_id,start,end FROM media_references WHERE path=?", (relative,))
        if reference is None:
            reference = self.one("SELECT source_id,start,end FROM segments WHERE path=? "
                                 "AND kind IN ('moment','scene')", (relative,))
        if reference:
            from .shot_media import ShotMedia
            try:
                ShotMedia(self).references(reference['source_id'], reference['start'], reference['end'])
                return True
            except (ValueError, OSError):
                pass
        return False

    def resolve_media(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise FileNotFoundError(relative)
        reference = self.one("SELECT * FROM media_references WHERE path=?", (relative,))
        if reference and reference['version'] == 'shot-boundary-v1':
            from .shot_media import ShotMedia
            return ShotMedia(self).materialize(reference['source_id'], reference['start'], reference['end'])
        if not path.is_file():
            if reference is None:
                reference = self.one("SELECT source_id,start,end FROM segments WHERE path=? "
                                     "AND kind IN ('moment','scene')", (relative,))
            if reference is None:
                for row in self.rows("SELECT snapshot FROM index_members"):
                    snapshot = json.loads(row["snapshot"])
                    if snapshot.get("path") == relative and snapshot.get("kind") in {"moment", "scene"}:
                        reference = snapshot
                        break
            if reference:
                from .shot_media import ShotMedia
                return ShotMedia(self).materialize(reference['source_id'], reference['start'], reference['end'])
            raise FileNotFoundError(relative)
        return path
