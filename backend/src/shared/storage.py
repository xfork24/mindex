from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindex.shared.backends.media import MediaStore
    from mindex.shared.backends.vectors import VectorStores
    from mindex.shared.config import Config

from mindex.shared.db.sqlite_backend import SqliteBackend
from mindex.shared.migrations import execute_script, migrate

# 检索热路径的整索引快照缓存（活动索引 + 一个固定预览索引足够）。
SNAPSHOT_CACHE_ENTRIES = 2
SNAPSHOT_CACHE_TOTAL_ENTRIES = 8
# 原地改写或删除 index_members 的维护操作用它显式使缓存失效。
SNAPSHOT_REVISION_KEY = "snapshots.rev"
_SNAPSHOT_CACHE: OrderedDict = OrderedDict()
_SNAPSHOT_LOCK = threading.Lock()


def bump_snapshot_revision(db) -> None:
    """在改写/删除 index_members 的同一事务里调用，使各进程的快照缓存失效。"""
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (SNAPSHOT_REVISION_KEY, uuid.uuid4().hex))


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def require_sqlite(config, action: str) -> None:
    """维护能力在 PG 元数据库下的显式拒绝（plans §11 阶段 1：未支持时不静默跳过）。"""
    if config.database.backend == "postgres":
        from mindex.shared.db.errors import UnsupportedOperation

        raise UnsupportedOperation(
            f"{action} 暂不支持 PostgreSQL 元数据库（备份迁移与 SQLite 专属维护在阶段 5 交付）；"
            "local 形态可继续使用该能力")


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


def _metadata_v12(db):
    """派生词法索引（plans/horizontal-scaling-plan.md §6.1）：SQL 内统计与评分的投影表。

    以冻结快照重建（search/lexical_store.py），不改变已发布内容；绑定 index_id。
    ``search_segments`` 是 eligible 关系的小投影（隐藏/时长/分析字段过滤下推 SQL）；
    ``lexical_docs`` 每个 segment 为 dialogue/analysis 各建行、缺文档长度计 0（分母）；
    ``lexical_terms`` 为精确 token 倒排（term 逐字节相等，不做模糊/大小写归一 collation）；
    ``lexical_index_meta`` 持久化分词器版本，版本不一致禁止构建/查询新派生索引。
    """
    execute_script(db, """
CREATE TABLE IF NOT EXISTS search_segments (
  index_id TEXT NOT NULL, segment_id TEXT NOT NULL, source_id TEXT NOT NULL,
  parent_id TEXT, kind TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
  annotation_projection TEXT NOT NULL, indexed_views TEXT NOT NULL,
  PRIMARY KEY(index_id, segment_id)
);
CREATE INDEX IF NOT EXISTS search_segments_source ON search_segments(index_id, source_id);
CREATE TABLE IF NOT EXISTS lexical_docs (
  index_id TEXT NOT NULL, segment_id TEXT NOT NULL, channel TEXT NOT NULL,
  doc_length INTEGER NOT NULL, PRIMARY KEY(index_id, segment_id, channel)
);
CREATE TABLE IF NOT EXISTS lexical_terms (
  index_id TEXT NOT NULL, channel TEXT NOT NULL, term TEXT NOT NULL,
  segment_id TEXT NOT NULL, tf INTEGER NOT NULL, PRIMARY KEY(index_id, channel, term, segment_id)
);
CREATE INDEX IF NOT EXISTS lexical_terms_lookup ON lexical_terms(index_id, channel, term);
CREATE TABLE IF NOT EXISTS lexical_index_meta (
  index_id TEXT PRIMARY KEY, tokenizer_version TEXT NOT NULL,
  segments INTEGER NOT NULL, docs INTEGER NOT NULL, terms INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
""")


def _metadata_v13(db):
    """分布式锁租约表（plans §7.1，阶段 3）：仅 PostgreSQL 形态使用（schema_pg 基线
    中的 lock_resources/lock_holders）；本地形态用 data_dir 文件锁，此步为空操作。"""


def _metadata_v15(db):
    """智能助手变更 outbox（plans §9 阶段 5）：仅 PostgreSQL 形态使用（schema_pg 基线
    的 agent_outbox）；本地形态由 agent 进程内的 ChangeFeed 直接承担。"""


def _metadata_v14(db):
    """预签名下载保留记录（plans §8 阶段 5）：仅 PostgreSQL 形态使用（schema_pg 基线
    中的 download_pins）；本地形态无预签名，此步为空操作。"""


def _metadata_v16(db):
    from mindex.shared.db.scaling_schema import SQLITE_FILES

    execute_script(db, SQLITE_FILES)


# metadata.sqlite3 的有序迁移：只追加，不修改已发布的步骤（见 shared/migrations.py）。
def _metadata_v17(db):
    from mindex.shared.db.scaling_schema import LEDGER_TABLES

    for table in LEDGER_TABLES:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "queue_attempt_id" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN queue_attempt_id TEXT")
        if "queue_fencing_token" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN queue_fencing_token INTEGER NOT NULL DEFAULT 0")


def _metadata_v18(db):
    db.execute("CREATE TABLE IF NOT EXISTS migration_runs (migration_id TEXT PRIMARY KEY,source_sha256 TEXT NOT NULL,"
               "status TEXT NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")


def _metadata_v19(db):
    from mindex.shared.db.scaling_schema import SQLITE_DURABLE_INDEX

    execute_script(db, SQLITE_DURABLE_INDEX)


def _metadata_v20(db):
    from mindex.shared.db.scaling_schema import SQLITE_DIRECTORIES

    execute_script(db, SQLITE_DIRECTORIES)


def _metadata_v21(db):
    from mindex.shared.db.scaling_schema import SQLITE_DELIVERY

    execute_script(db, SQLITE_DELIVERY)


def _metadata_v22(db):
    from mindex.shared.db.scaling_schema import SQLITE_ADMISSION

    execute_script(db, SQLITE_ADMISSION)


METADATA_STEPS = (_metadata_v1, _metadata_v2, _metadata_v3, _metadata_v4, _metadata_v5, _metadata_v6, _metadata_v7, _metadata_v8, _metadata_v9, _metadata_v10, _metadata_v11, _metadata_v12, _metadata_v13, _metadata_v14, _metadata_v15, _metadata_v16, _metadata_v17, _metadata_v18, _metadata_v19, _metadata_v20, _metadata_v21, _metadata_v22)


def migrate_metadata(db) -> int:
    """把已打开的 metadata 库升级到最新版本（不切换日志模式；连接须用 ``sqlite3.Row``）；返回最终版本。"""
    return migrate(db, METADATA_STEPS)


class Store:
    # 直构 Store 仅用于本地暂存；open_context 为业务入口绑定库身份。
    library_id: str = ""
    deployment_form: str = "local"
    backend_identity: str = ""
    deployment_config: Config | None = None
    vectors: VectorStores | None = None
    media: MediaStore | None = None

    @classmethod
    def open(cls, config) -> Store:
        """经 BackendContext 构造（plans/horizontal-scaling-plan.md §5）。

        阶段 0 恒等于 ``Store(config.data_dir)``；阶段 1 起按部署形态返回 SQLite 或
        PostgreSQL 元数据库的统一 Store。集群调用链（API/worker）必须经此入口或
        显式传入 BackendContext；``Store(root)`` 只服务本地工具、测试与暂存 SQLite。
        """
        from mindex.shared.backends import open_context

        return open_context(config).store

    def close(self):
        context = getattr(self, "_backend_context", None)
        if context is not None:
            context.close()

    def __enter__(self):
        context = getattr(self, "_backend_context", None)
        if context is not None and context.closed:
            raise RuntimeError("后端上下文已关闭")
        return self

    def __exit__(self, *_):
        self.close()

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "metadata.sqlite3"
        self.backend = SqliteBackend(self.path)
        # 具名锁后端（open_context 按配置覆盖；直构 Store 视为本地文件锁）。
        self.locks_backend = "file"
        # 媒体后端（open_context 注入；直构 Store 为 None，按本地文件系统解析）。
        self.media = None
        from filelock import FileLock

        # 首次建库的 WAL 切换不总遵循 SQLite busy_timeout；跨线程/进程串行初始化。
        with FileLock(str(self.root / ".metadata-init.lock"), timeout=30):
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                migrate(db, METADATA_STEPS)
        # 创作详情热路径的短 TTL 缓存：同一轮 UI 交互里反复读 active_index / visibility_key
        # 会重复开关 sqlite 连接。缓存只影响冲突展示，构建仍在锁内用新读校验签名。
        self._hot_cache: dict[str, tuple[float, object]] = {}
        self._hot_cache_lock = threading.Lock()

    @property
    def pg(self) -> bool:
        """元数据库是否为 PostgreSQL（pg-single/cluster 形态）。"""
        return self.backend.backend == "postgres"

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
    def connect(self):
        """一个事务：正常退出提交、异常回滚（SQLite 为每调用新连接；PG 为池检出）。"""
        context = getattr(self, "_backend_context", None)
        if context is not None and context.closed:
            raise RuntimeError("后端上下文已关闭")
        with self.backend.connect() as db:
            yield db

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

    def activate(self, index_id: str, *, execution=None):
        if self._defer_publication("index", index_id, execution=execution):
            return
        with self.connect() as db:
            self._activate_in_db(db, index_id)

    def _defer_publication(self, kind, identifier, *, execution=None):
        from mindex.shared.db.fencing import current_attempt, publication_scope

        attempt = execution.attempt if execution is not None else current_attempt()
        if not self.pg or attempt is None:
            return False
        if attempt.library_id != self.library_id or attempt.backend.dsn != self.backend.dsn:
            from mindex.shared.db.locks_pg import LeaseLost

            raise LeaseLost("发布目标与任务所属素材库不一致")
        attempt.check()
        method = self._activate_in_db if kind == "index" else self._activate_release_in_db

        def defer():
            with publication_scope(self, only_attempt=True):
                attempt.defer((self.library_id, "active"), lambda db: method(db, identifier))

        if execution is not None:
            execution.run(defer)
        else:
            defer()
        return True

    def _activate_in_db(self, db, index_id):
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

    def activate_release(self, release_id: str, *, execution=None):
        if self._defer_publication("release", release_id, execution=execution):
            return
        with self.connect() as db:
            self._activate_release_in_db(db, release_id)

    def _activate_release_in_db(self, db, release_id):
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
        path = self._snapshot_cache_scope()
        with _SNAPSHOT_LOCK:
            stale = [key for key in _SNAPSHOT_CACHE if key[0] == path and key[1] not in keep]
            for key in stale:
                del _SNAPSHOT_CACHE[key]
        return len(stale)

    @property
    def cache_identity(self):
        from mindex.shared.config import fingerprint

        if not self.library_id and not self.pg:
            row = self.one("SELECT value FROM settings WHERE key='library.id'")
            identity = row["value"] if row else ""
        else:
            identity = self.library_id
        return (identity, self.backend.backend,
                fingerprint(getattr(self.backend, "dsn", str(self.path))),
                getattr(self, "backend_identity", ""))

    def _snapshot_cache_scope(self):
        return self.cache_identity

    def snapshots_shared(self, index_id: str) -> list[dict]:
        """进程内共享的整索引快照，供只读的检索热路径使用；调用方不得修改返回的列表及其中的字典。

        签名先于加载取得：加载期间若有写入，缓存里是较新数据却挂着旧签名，下次比对不一致会重新加载。
        """
        signature = self.snapshot_signature(index_id)
        key = (self._snapshot_cache_scope(), index_id)
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
            while len(_SNAPSHOT_CACHE) > SNAPSHOT_CACHE_TOTAL_ENTRIES:
                _SNAPSHOT_CACHE.popitem(last=False)
        return loaded

    def media_available(self, relative: str) -> bool:
        media = getattr(self, "media", None)
        if media is not None and getattr(media, "backend", "local") == "s3":
            try:
                from mindex.shared import files

                entry = files.pointer(self, relative)
                if entry is not None and entry["size"] < 0:
                    return False
                obj = media.stat(entry["object_key"] if entry is not None else relative)
                return entry is None or (obj.size == entry["size"] and obj.sha256 == entry["sha256"])
            except FileNotFoundError:
                pass
            except Exception:  # noqa: BLE001 — 远端不可达按不可用处理，由上层给出诊断
                return False
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            return False
        if path.is_file() and getattr(media, "backend", "local") != "s3":
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
        """兼容 Path 入口；长期处理和响应应使用 materialize_media 持有整个上下文。"""
        from mindex.shared.db.fencing import current_attempt

        attempt = current_attempt()
        if self.pg and attempt is not None:
            if attempt.library_id != self.library_id or attempt.backend.dsn != self.backend.dsn:
                raise ValueError("媒体与任务所属素材库不一致")
            return attempt.retain((self.library_id, "media", relative), self.materialize_media(relative))
        with self.materialize_media(relative) as path:
            return path

    @contextmanager
    def materialize_media(self, relative: str):
        """解析权威文件指针，物化 pin 覆盖调用方的完整使用期。"""
        local = (self.root / relative).resolve()
        if not local.is_relative_to(self.root):
            raise FileNotFoundError(relative)
        media = getattr(self, "media", None)
        if media is not None and getattr(media, "backend", "local") == "s3":
            from mindex.shared import files

            entry = files.pointer(self, relative)
            if entry is not None and entry["size"] < 0:
                raise FileNotFoundError(relative)
            key = entry["object_key"] if entry is not None else relative
            try:
                context = media.materialize(key)
                cached = context.__enter__()
            except FileNotFoundError:
                if entry is not None:
                    raise  # 权威对象缺失，不能以旧的节点缓存掩盖损坏或删除。
            else:
                try:
                    if entry is not None:
                        with Path(cached).open("rb") as handle:
                            digest = hashlib.file_digest(handle, "sha256").hexdigest()
                        if Path(cached).stat().st_size != entry["size"] or digest != entry["sha256"]:
                            raise ValueError(f"共享媒体摘要与元数据库指针不一致：{relative}")
                    yield Path(cached)
                finally:
                    context.__exit__(None, None, None)
                return
            reference = self.one("SELECT source_id,start,end FROM media_references WHERE path=?", (relative,))
            if reference is None:
                reference = self.one("SELECT source_id,start,end FROM segments WHERE path=? "
                                     "AND kind IN ('moment','scene')", (relative,))
            if reference:
                from mindex.shared.shot_media import ShotMedia

                with ShotMedia(self).materialized(reference['source_id'], reference['start'], reference['end']) as path:
                    yield path
                return
            raise FileNotFoundError(relative)
        yield self._resolve_local_media(relative)

    def _resolve_local_media(self, relative: str) -> Path:
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


class PgStore(Store):
    """PostgreSQL 元数据库的统一 Store（阶段 1 的 pg-single 形态）。

    与 ``Store(root)`` 共用全部业务方法；差别只在连接层：池化 PG 连接 + 受控方言
    翻译（shared/db）。``root`` 在此形态仅为节点缓存/临时目录（plans §4），媒体/
    向量/锁仍走本地实现。构造**不执行任何 DDL**——启动只校验 schema 版本，集群
    迁移由 ``mindex migrate`` 独立执行（plans §4）。
    """

    def __init__(self, config):
        import os

        from mindex.shared.db.pg_backend import PgBackend

        self.root = config.data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "metadata.sqlite3"  # 兼容属性；该形态不读写此文件
        self._hot_cache: dict[str, tuple[float, object]] = {}
        self._hot_cache_lock = threading.Lock()
        section = config.database.postgres
        dsn = os.environ.get(section.dsn_env, "").strip()
        if not dsn:
            raise RuntimeError(
                f"database.backend=postgres 需要在环境变量 {section.dsn_env} 提供 DSN"
                "（凭据只从环境变量读取，不写入配置文件）")
        # 先用一次引导连接完成版本校验并读取 library_id；锁键/缓存租户键都用它（plans §2）。
        from mindex.shared.db.migrate_pg import check_schema

        pool_options = {key: getattr(section, key) for key in (
            "pool_min", "pool_max", "control_pool_max", "connection_budget",
            "checkout_timeout", "control_checkout_timeout")}
        with PgBackend(dsn, library_id="bootstrap", **pool_options).connect() as db:
            check_schema(db)
            row = db.execute("SELECT value FROM settings WHERE key=?", ("library.id",)).fetchone()
        if row is None:  # 基线迁移未跑或未建库：给出唯一修复入口
            raise RuntimeError("PostgreSQL 元数据库缺少 library.id；请先执行 mindex migrate")
        self.library_id = row[0]
        self.backend = PgBackend(dsn, library_id=self.library_id, **pool_options,
                                 queue_backend=config.queue.backend)
        self.locks_backend = config.locks.backend
        self.queue_backend = config.queue.backend
