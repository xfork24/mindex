from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from mindex.shared.migrations import execute_script, migrate

# 检索热路径的整索引快照缓存（活动索引 + 一个固定预览索引足够）。
SNAPSHOT_CACHE_ENTRIES = 2
# 原地改写或删除 index_members 的维护操作用它显式使缓存失效。
SNAPSHOT_REVISION_KEY = "snapshots.rev"
_SNAPSHOT_CACHE: OrderedDict = OrderedDict()
_SNAPSHOT_LOCK = threading.Lock()


def bump_snapshot_revision(db) -> None:
    """在改写/删除 index_members 的同一事务里调用，使各进程的快照缓存失效。"""
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (SNAPSHOT_REVISION_KEY, uuid.uuid4().hex))


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


METADATA_SCHEMA_V1 = """
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
"""


def _add_missing_columns(db, columns) -> None:
    for table, column, definition in columns:
        if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _metadata_v1(db) -> None:
    """引入版本号时的完整 schema；对 user_version=0 的旧库幂等（补齐后来新增的列）。"""
    from mindex.shared.person_schema import init_person_schema

    execute_script(db, METADATA_SCHEMA_V1)
    init_person_schema(db)
    _add_missing_columns(db, (
        ("sources", "layout_config", "TEXT"), ("dialogue_tasks", "publication", "TEXT"),
        ("person_tasks", "publication", "TEXT"), ("clip_migration_files", "dependency_digest", "TEXT"),
        ("representations", "cache_key", "TEXT"),
    ))
    db.execute("CREATE INDEX IF NOT EXISTS representations_cache ON representations(cache_key)")


def _metadata_v2(db) -> None:
    """用户文案模板（studio/script_templates.py）：内置模板来自 YAML，只有复制出的用户模板入库。

    body 为 JSON：version、description、instructions、constraints。"""
    execute_script(db, """
CREATE TABLE IF NOT EXISTS script_templates (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, kind TEXT NOT NULL,
  base_id TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
""")


def _metadata_v3(db) -> None:
    """选题挖掘（topics/）：档案、数据源、候选条目、挖掘任务、选题卡片、素材缺口与导入的日历事件。

    JSON 列（body/config/options/progress/result/publication）存 UTF-8 JSON 文本。``topic_runs`` 沿用
    后台任务表的列约定（``source_id`` 存档案 ID），以复用 ``BackgroundTaskRunner`` 与 ``task_progress``。
    子表不声明外键，删除档案由 ``topics/profiles.delete`` 在一个事务里级联。"""
    execute_script(db, """
CREATE TABLE IF NOT EXISTS topic_profiles (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL, asset_paths TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS topic_sources (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL, secret TEXT,
  last_fetch_at TEXT, last_error TEXT, last_count INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_sources_profile ON topic_sources(profile_id);
CREATE TABLE IF NOT EXISTS topic_items (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, source_id TEXT NOT NULL, dedup_key TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', url TEXT, published_at TEXT, event_at TEXT,
  hot REAL, fetched_at TEXT NOT NULL,
  UNIQUE(profile_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS topic_items_profile ON topic_items(profile_id, fetched_at);
CREATE TABLE IF NOT EXISTS topic_runs (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, trigger TEXT NOT NULL DEFAULT 'manual',
  status TEXT NOT NULL, config TEXT NOT NULL, options TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
  error TEXT, result TEXT, publication TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS topic_runs_profile ON topic_runs(source_id, created_at);
CREATE TABLE IF NOT EXISTS topic_cards (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, run_id TEXT, title TEXT NOT NULL, body TEXT NOT NULL,
  total REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'new', job_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_cards_profile ON topic_cards(profile_id, status, total);
CREATE TABLE IF NOT EXISTS topic_gaps (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, card_id TEXT, card_ids TEXT NOT NULL DEFAULT '[]',
  visual TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_gaps_profile ON topic_gaps(profile_id, status);
CREATE TABLE IF NOT EXISTS topic_calendar_events (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, date TEXT NOT NULL, title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '', recurring INTEGER NOT NULL DEFAULT 0,
  has_year INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS topic_calendar_events_source ON topic_calendar_events(source_id);
""")


def _metadata_v4(db) -> None:
    """用户品牌模板（studio/brands.py）：内置模板来自 YAML，只有复制出的用户模板入库。

    body 为 JSON：description、version、subtitle、overlay、intro、outro、watermark；素材以 ``brand/…`` 相对路径引用。"""
    execute_script(db, """
CREATE TABLE IF NOT EXISTS brand_templates (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, base_id TEXT, body TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
""")


def _metadata_v5(db) -> None:
    """曲库（studio/music_library.py）：曲目 ID 即音频内容 sha256，文件在 ``data_dir/music/<sha256><suffix>``。

    beats/moods/tags 为 JSON 数组；analysis_status 为 pending、ready 或 failed。"""
    execute_script(db, """
CREATE TABLE IF NOT EXISTS music_tracks (
  id TEXT PRIMARY KEY, suffix TEXT NOT NULL, title TEXT NOT NULL, artist TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL, license TEXT NOT NULL, attribution TEXT NOT NULL, expires_at TEXT,
  duration REAL NOT NULL, bpm REAL, beats TEXT NOT NULL DEFAULT '[]', energy REAL, brightness REAL,
  moods TEXT NOT NULL DEFAULT '[]', tags TEXT NOT NULL DEFAULT '[]',
  analysis_status TEXT NOT NULL DEFAULT 'pending', analysis_error TEXT, analysis_version INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
""")


def _metadata_v6(db) -> None:
    """第三方工具源（studio/tools/registry.py）：connection/tools/artifact_hosts 为 JSON；
    ``secret`` 为密钥 JSON（只写不读，不含密钥的备份导出时清空）。"""
    execute_script(db, """
CREATE TABLE IF NOT EXISTS tool_sources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL, connection TEXT NOT NULL,
  tools TEXT NOT NULL, artifact_hosts TEXT NOT NULL DEFAULT '[]', secret TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
""")


def _metadata_v7(db) -> None:
    execute_script(db, """
CREATE TABLE IF NOT EXISTS delivery_templates (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL, asset_paths TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
""")


def _metadata_v8(db) -> None:
    """Seed once; tombstones prevent deleted IDs being resurrected or reused."""
    import yaml

    execute_script(db, """
CREATE TABLE IF NOT EXISTS delivery_platforms (
  id TEXT PRIMARY KEY, body TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
""")
    for key in ("bilibili", "douyin", "youtube"):
        source = Path(__file__).resolve().parent.parent / "studio" / "platforms" / f"{key}.yaml"
        body = yaml.safe_load(source.read_text(encoding="utf-8"))
        db.execute("INSERT OR IGNORE INTO delivery_platforms(id,body,created_at,updated_at) VALUES(?,?,?,?)",
                   (key, dumps(body), "2026-01-01T00:00:00", "2026-01-01T00:00:00"))


def _metadata_v9(db):
    execute_script(db, """
CREATE TABLE IF NOT EXISTS media_groups (
 id TEXT PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('source','music')),
 name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND name=trim(name)), description TEXT NOT NULL DEFAULT '',
 updated_at TEXT NOT NULL, UNIQUE(type,name)
);
CREATE TABLE IF NOT EXISTS media_group_members (
 id TEXT PRIMARY KEY,
 group_id TEXT NOT NULL REFERENCES media_groups(id) ON DELETE CASCADE,
 source_id TEXT REFERENCES sources(id) ON DELETE CASCADE,
 music_id TEXT REFERENCES music_tracks(id) ON DELETE CASCADE,
 CHECK((source_id IS NOT NULL) != (music_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS group_source_unique ON media_group_members(group_id,source_id);
CREATE UNIQUE INDEX IF NOT EXISTS group_music_unique ON media_group_members(group_id,music_id);
""")


def _metadata_v10(db):
    # 为系统默认组保留名称；旧自定义组只改显示名，ID、成员和任务引用保持原样。
    for row in db.execute("SELECT id,type FROM media_groups WHERE name='未分类'").fetchall():
        suffix = 1
        while True:
            name = "未分类（自定义）" if suffix == 1 else f"未分类（自定义 {suffix}）"
            if not db.execute("SELECT 1 FROM media_groups WHERE type=? AND name=?", (row["type"], name)).fetchone():
                break
            suffix += 1
        db.execute("UPDATE media_groups SET name=? WHERE id=?", (name, row["id"]))


def _metadata_v11(db):
    # 选题缺口向量缓存：按嵌入空间身份指纹失效，避免每次打分重嵌全部开放缺口。
    # 用 _add_missing_columns 保证幂等：user_version 被重置的旧库重跑迁移不会因列已存在而失败。
    _add_missing_columns(db, (
        ("topic_gaps", "embedding", "BLOB"),
        ("topic_gaps", "embedding_space", "TEXT"),
    ))


# metadata.sqlite3 的有序迁移：只追加，不修改已发布的步骤（见 shared/migrations.py）。
METADATA_STEPS = (_metadata_v1, _metadata_v2, _metadata_v3, _metadata_v4, _metadata_v5, _metadata_v6, _metadata_v7, _metadata_v8, _metadata_v9, _metadata_v10, _metadata_v11)


def migrate_metadata(db) -> int:
    """把已打开的 metadata 库升级到最新版本（不切换日志模式；连接须用 ``sqlite3.Row``）；返回最终版本。"""
    return migrate(db, METADATA_STEPS)


class Store:
    @classmethod
    def open(cls, config) -> Store:
        """经 BackendContext 构造（plans/horizontal-scaling-plan.md §5）。

        阶段 0 恒等于 ``Store(config.data_dir)``；阶段 1 起按部署形态返回 SQLite 或
        PostgreSQL 元数据库的统一 Store。集群调用链（API/worker）必须经此入口或
        显式传入 BackendContext；``Store(root)`` 只服务本地工具、测试与暂存 SQLite。
        """
        from mindex.shared.backends import open_context

        return open_context(config).store

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "metadata.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            migrate(db, METADATA_STEPS)
        # 创作详情热路径的短 TTL 缓存：同一轮 UI 交互里反复读 active_index / visibility_key
        # 会重复开关 sqlite 连接。缓存只影响冲突展示，构建仍在锁内用新读校验签名。
        self._hot_cache: dict[str, tuple[float, object]] = {}
        self._hot_cache_lock = threading.Lock()

    def _hot_cached(self, key: str, ttl: float, producer):
        import time

        with self._hot_cache_lock:
            entry = self._hot_cache.get(key)
            now = time.monotonic()
            if entry is not None and now - entry[0] < ttl:
                return entry[1]
        value = producer()
        with self._hot_cache_lock:
            self._hot_cache[key] = (time.monotonic(), value)
        return value

    def active_index_cached(self, ttl: float = 1.0) -> dict | None:
        """``active_index`` 的短 TTL 缓存版本，供只读展示的热路径使用。"""
        return self._hot_cached("active_index", ttl, self.active_index)

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

    def source_names(self) -> dict[str, str]:
        """Current display names; raw source rows remain immutable processing inputs."""
        return {
            row["id"]: row["display_name"] or row["name"]
            for row in self.rows(
                "SELECT s.id,s.name,c.display_name FROM sources s "
                "LEFT JOIN source_curation c ON c.id=s.id"
            )
        }

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
            db.execute("BEGIN IMMEDIATE")
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
                from mindex.shared.config import fingerprint
                person_id = current['person_id'] if current else None
                release_id = fingerprint([index_id, person_id])
                db.execute("INSERT OR IGNORE INTO search_releases(id,content_id,person_id) VALUES(?,?,?)",
                           (release_id, index_id, person_id))
                db.execute("INSERT OR REPLACE INTO settings VALUES('active.release',?)", (release_id,))

    def activate_release(self, release_id: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.*,i.status FROM search_releases r JOIN index_versions i ON i.id=r.content_id WHERE r.id=?", (release_id,)).fetchone()
            if not row or row['status'] != 'ready':
                raise ValueError("检索组合不存在或内容索引未就绪")
            if db.execute("SELECT 1 FROM index_members WHERE index_id=? AND complete=0", (row['content_id'],)).fetchone():
                raise ValueError("内容索引有未完成片段")
            db.execute("INSERT OR REPLACE INTO settings VALUES('active',?)", (row['content_id'],))
            db.execute("INSERT OR REPLACE INTO settings VALUES('active.release',?)", (release_id,))

    def snapshots(self, index_id: str, source_id: str | None = None) -> list[dict]:
        rows = self.rows("SELECT snapshot FROM index_members WHERE index_id=? AND complete=1"
                         + (" AND json_extract(snapshot,'$.source_id')=?" if source_id else ""),
                         (index_id, source_id) if source_id else (index_id,))
        return [json.loads(row["snapshot"]) for row in rows]

    def snapshot_signature(self, index_id: str) -> tuple:
        """索引快照是否变化的廉价签名：增删行、完成状态、内容长度或显式修订号任一变化都会改变它。"""
        row = self.one(
            "SELECT count(*) AS n, COALESCE(sum(complete),0) AS c, COALESCE(sum(length(snapshot)),0) AS l, "
            "(SELECT value FROM settings WHERE key=?) AS r FROM index_members WHERE index_id=?",
            (SNAPSHOT_REVISION_KEY, index_id),
        )
        return (row["n"], row["c"], row["l"], row["r"])

    def retain_snapshots(self, keep: set[str]) -> int:
        """淘汰本库快照缓存中不在 keep 里的索引版本（活动指针移走后，旧版本不会再被默认路径用到）。返回淘汰条数。"""
        path = str(self.path)
        with _SNAPSHOT_LOCK:
            stale = [key for key in _SNAPSHOT_CACHE if key[0] == path and key[1] not in keep]
            for key in stale:
                del _SNAPSHOT_CACHE[key]
        return len(stale)

    def snapshots_shared(self, index_id: str) -> list[dict]:
        """进程内共享的整索引快照，供只读的检索热路径使用；调用方不得修改返回的列表及其中的字典。

        签名先于加载取得：加载期间若有写入，缓存里是较新数据却挂着旧签名，下次比对不一致会重新加载。
        """
        signature = self.snapshot_signature(index_id)
        key = (str(self.path), index_id)
        with _SNAPSHOT_LOCK:
            cached = _SNAPSHOT_CACHE.get(key)
            if cached is not None and cached[0] == signature:
                _SNAPSHOT_CACHE.move_to_end(key)
                return cached[1]
        loaded = self.snapshots(index_id)
        with _SNAPSHOT_LOCK:
            _SNAPSHOT_CACHE[key] = (signature, loaded)
            _SNAPSHOT_CACHE.move_to_end(key)
            # 容量按数据目录计：同一数据目录最多保留最近使用的 SNAPSHOT_CACHE_ENTRIES 个版本。
            siblings = [k for k in _SNAPSHOT_CACHE if k[0] == key[0]]
            for stale in siblings[: max(0, len(siblings) - SNAPSHOT_CACHE_ENTRIES)]:
                del _SNAPSHOT_CACHE[stale]
        return loaded

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
            from mindex.shared.shot_media import ShotMedia
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
            from mindex.shared.shot_media import ShotMedia
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
                from mindex.shared.shot_media import ShotMedia
                return ShotMedia(self).materialize(reference['source_id'], reference['start'], reference['end'])
            raise FileNotFoundError(relative)
        return path
