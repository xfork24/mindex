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
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL,
                  source_id TEXT, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
                );
            """)

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
        except Exception as exc:
            self.execute(
                "UPDATE runs SET status='failed',message=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc)[:2000], run_id),
            )
            raise
        else:
            self.execute(
                "UPDATE runs SET status='complete',finished_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,)
            )

    def sources(self) -> list[dict]:
        return self.rows("SELECT * FROM sources ORDER BY name,id")

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
            "SELECT i.* FROM index_versions i JOIN settings s ON s.value=i.id WHERE s.key='active'"
        )
        if row:
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

    def snapshots(self, index_id: str) -> list[dict]:
        rows = self.rows("SELECT snapshot FROM index_members WHERE index_id=? AND complete=1", (index_id,))
        return [json.loads(row["snapshot"]) for row in rows]

    def resolve_media(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError(relative)
        return path
