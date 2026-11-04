"""PostgreSQL 后端验收（plans §11 阶段 1）：真实 PG16 的 schema/JSON/BLOB/排序/约束/并发语义。

经 ``MINDEX_TEST_POSTGRES_DSN`` 门控，缺失时整文件 skip（CI 专项启用真实后端矩阵，
plans §12；本地：docker run postgres:16-alpine -p 55432:5432 …）。覆盖：

- 迁移器：空库基线、幂等重跑、版本检查与 library.id 生成；
- SQL 特征：与 tests/test_sql_characteristics.py 同组断言在 PG 适配器上的对照；
- SQLite → PG → SQLite 往返校验（含 BLOB/REAL/TEXT/rowid 保序）；
- 功能冒烟：PgStore 上的 settings/REPLACE/快照 json 过滤/curation 数值真值/租约互斥。
"""

from __future__ import annotations

import json
import os

import pytest

from mindex.shared.db.errors import IntegrityViolation, SchemaMismatch
from mindex.shared.db.migrate_pg import migrate_pg
from mindex.shared.storage import Store

pytestmark = pytest.mark.requires_postgres

DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")


def _psycopg():
    import psycopg

    return psycopg


@pytest.fixture
def pg_dsn(tmp_path_factory):
    if not DSN:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN（真实 PostgreSQL 16）")
    return DSN


@pytest.fixture
def fresh_pg(pg_dsn):
    """每个用例一个空库：重建 public schema 并应用基线迁移。"""
    psycopg = _psycopg()
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(pg_dsn) as conn:
        migrate_pg(conn)
    yield pg_dsn


@pytest.fixture
def pg_store(fresh_pg, tmp_path, monkeypatch):
    from mindex.shared.config import Config
    from mindex.shared.storage import PgStore

    config = Config(data_dir=tmp_path / "data")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", fresh_pg)
    store = PgStore(config)
    assert store.pg
    yield store


def test_migrate_baseline_is_idempotent(fresh_pg, pg_dsn):
    psycopg = _psycopg()
    with psycopg.connect(pg_dsn) as conn:  # 重跑：版本一致，幂等跳过
        assert migrate_pg(conn) >= 1
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()
        assert row[0] >= 1
        library = conn.execute("SELECT value FROM settings WHERE key='library.id'").fetchone()
        assert library and len(library[0]) == 36


def test_store_rejects_unmigrated_database(pg_dsn, tmp_path, monkeypatch):
    psycopg = _psycopg()
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    from mindex.shared.config import Config
    from mindex.shared.storage import PgStore

    monkeypatch.setenv("MINDEX_DATABASE_DSN", pg_dsn)
    config = Config(data_dir=tmp_path / "data")
    with pytest.raises(SchemaMismatch, match="mindex migrate"):
        PgStore(config)  # 未初始化的库给出唯一修复入口


def test_missing_dsn_is_a_clear_error(fresh_pg, tmp_path, monkeypatch):
    from mindex.shared.config import Config
    from mindex.shared.storage import PgStore

    monkeypatch.delenv("MINDEX_DATABASE_DSN", raising=False)
    with pytest.raises(RuntimeError, match="MINDEX_DATABASE_DSN"):
        PgStore(Config(data_dir=tmp_path / "data"))


# ---------------------------------------------------------------- SQL 特征对照


def test_pg_replace_resets_defaults_and_moves_rowid(pg_store):
    db = pg_store
    db.execute("INSERT INTO model_profiles(role,name,provider,config,api_key) "
               "VALUES('writer','a','http','{}','secret')")
    first = db.one("SELECT id FROM model_profiles WHERE name='a'")["id"]
    db.execute("INSERT OR REPLACE INTO model_profiles(id,role,name,provider,config) "
               "VALUES(?, 'writer','a','http','{\"v\":2}')", (first,))
    row = db.one("SELECT * FROM model_profiles WHERE name='a'")
    assert row["api_key"] is None  # 未列出列重置为默认（与 SQLite REPLACE 一致）
    assert row["id"] == first
    # 登记的次要约束语义：无 id 的 REPLACE 命中 UNIQUE(role,name) 时 PG 报统一冲突
    # 异常（SQLite 会删旧插新）；runtime_settings 已按 IntegrityError 转用户错误。
    with pytest.raises(IntegrityViolation):
        db.execute("INSERT OR REPLACE INTO model_profiles(role,name,provider,config) "
                   "VALUES('writer','a','http','{\"v\":3}')")
    kept = db.one("SELECT * FROM model_profiles WHERE name='a'")
    assert kept["id"] == first and kept["api_key"] is None
    assert db.one("SELECT count(*) AS n FROM model_profiles")["n"] == 1


def test_pg_rowid_identity_and_latest_ordering(pg_store):
    db = pg_store
    for i in range(3):
        db.execute("INSERT INTO search_releases(id,content_id) VALUES(?,?)", (f"r{i}", "idx"))
    latest = db.one("SELECT id FROM search_releases WHERE content_id=? ORDER BY rowid DESC LIMIT 1", ("idx",))
    assert latest["id"] == "r2"
    # REPLACE 后 rowid 移到末尾（SET rowid=DEFAULT）：最新选择跟随替换而非插入时间
    db.execute("INSERT OR REPLACE INTO search_releases(id,content_id) VALUES('r0','idx')")
    latest = db.one("SELECT id FROM search_releases WHERE content_id=? ORDER BY rowid DESC LIMIT 1", ("idx",))
    assert latest["id"] == "r0"
    # 显式 rowid 导入（装载器路径）后必须推进序列：下一条默认 rowid 不回卷
    from mindex.shared.db.migrate_pg import fix_sequences

    with db.connect() as conn:
        conn.execute("INSERT INTO search_releases(rowid,id,content_id) VALUES(100,'r100','idx')")
        fix_sequences(conn, ["search_releases"])
    db.execute("INSERT INTO search_releases(id,content_id) VALUES('r-next','idx')")
    top = db.one("SELECT rowid,id FROM search_releases ORDER BY rowid DESC LIMIT 1")
    assert top["id"] == "r-next" and top["rowid"] > 100


def test_pg_insert_or_ignore_and_unique_mapping(pg_store):
    db = pg_store
    db.execute("INSERT OR IGNORE INTO settings VALUES('k','first')")
    db.execute("INSERT OR IGNORE INTO settings VALUES('k','second')")
    assert db.one("SELECT value FROM settings WHERE key='k'")["value"] == "first"
    with pytest.raises(IntegrityViolation):  # 统一异常：业务 except sqlite3.IntegrityError 分支命中
        db.execute("INSERT INTO settings VALUES('k','clash')")


def test_pg_current_timestamp_shape(pg_store):
    db = pg_store
    db.execute("INSERT INTO runs(stage,source_id,status) VALUES('detect','s','running')")
    stamp = db.one("SELECT started_at FROM runs")["started_at"]
    assert len(stamp) == 19 and stamp[10] == " " and stamp[13] == ":"  # 与 SQLite CURRENT_TIMESTAMP 同形


def test_pg_row_access_and_types(pg_store):
    db = pg_store
    db.execute("INSERT INTO media_references VALUES('m','s',5.5,6.25,'v','[]')")
    db.execute("INSERT INTO topic_gaps(id,profile_id,visual,embedding,embedding_space,created_at,updated_at) "
               "VALUES('g','p','v',?,'space','2026-01-01 00:00:00','2026-01-01 00:00:00')", (b"\x00\x01\xff",))
    row = db.one("SELECT path,start,end FROM media_references")
    assert row["start"] == 5.5 and isinstance(row["start"], float)
    blob = db.one("SELECT embedding FROM topic_gaps WHERE id='g'")["embedding"]
    assert bytes(blob) == b"\x00\x01\xff"  # BLOB → BYTEA → bytes 往返
    # 统一连接上的行对象同时支持名称与位置访问（Store.rows 转成 dict 与 SQLite 一致）
    with db.connect() as conn:
        raw = conn.execute("SELECT path,start,end FROM media_references").fetchone()
        assert raw["start"] == 5.5 and raw[1] == 5.5 and dict(raw)["path"] == "m"


def test_pg_begin_immediate_serializes_same_library(pg_store):
    db = pg_store
    with db.connect() as conn:  # 持有同库互斥锁
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO settings VALUES('hold','1')")
        psycopg = _psycopg()
        with psycopg.connect(db.backend.dsn, autocommit=True) as other:
            held = other.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND granted").fetchone()[0]
        assert held >= 1  # advisory lock 可见；事务提交后自动释放
    assert pg_store.one("SELECT value FROM settings WHERE key='hold'")["value"] == "1"


def test_pg_snapshot_json_text_filter(pg_store):
    db = pg_store
    db.execute("INSERT INTO index_members VALUES('i','s1','{\"source_id\":\"src1\"}',1)")
    db.execute("INSERT INTO index_members VALUES('i','s2','{}',1)")
    rows = db.rows("SELECT snapshot FROM index_members WHERE json_extract(snapshot,'$.source_id')=?", ("src1",))
    assert len(rows) == 1 and json.loads(rows[0]["snapshot"])["source_id"] == "src1"


def test_pg_curation_boolean_truth(pg_store, tmp_path):
    """curation 的 PG 方言（CASE 化）与 SQLite 数值 OR 真值结果一致。"""
    from mindex.admin.curation import segment_sql

    db = pg_store
    db.execute("INSERT INTO sources VALUES('src','名','p','{}','k',0,NULL)")
    db.execute("INSERT INTO segments VALUES('seg1','src','shot',0,1,'p','t','g',NULL,'r')")
    db.execute("INSERT INTO segments VALUES('seg2','src','shot',1,2,'p','t','g',NULL,'r')")
    db.execute("INSERT INTO source_curation(id,hidden) VALUES('src',1)")  # 素材隐藏 → 全部生效
    db.execute("INSERT INTO segment_curation(id,hidden) VALUES('seg2',1)")
    sql = segment_sql(db) + " WHERE g.source_id=?"
    rows = {r["id"]: r for r in db.rows(sql, ("src",))}
    assert rows["seg1"]["effective_hidden"] == 1 and rows["seg2"]["effective_hidden"] == 1
    visible = db.rows(sql + " AND (CASE WHEN COALESCE(c.hidden,0)<>0 OR COALESCE(sc.hidden,0)<>0 "
                      "OR COALESCE(pc.hidden,0)<>0 THEN 1 ELSE 0 END)=%s", ("src", 0))
    assert visible == []


# ---------------------------------------------------------------- 往返校验


def _seed_sqlite(store: Store):
    store.execute("INSERT INTO sources VALUES('s1','素材','sources/s1/a.mp4','{}','layout',1,NULL)")
    store.execute("INSERT INTO segments VALUES('seg1','s1','shot',0.5,1.5,'clips/x','t.jpg','g1',NULL,'r')")
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('idx','{\"format\":2}','ready')")
    store.execute("INSERT INTO index_members VALUES('idx','seg1','{\"id\":\"seg1\"}',1)")
    store.execute("INSERT OR REPLACE INTO representations VALUES('rep1','idx','seg1','visual','doc','ck')")
    store.execute("INSERT INTO settings VALUES('active','idx')")
    store.execute("INSERT INTO media_references VALUES('clips/x','s1',0.25,0.75,'shot-boundary-v1','[]')")
    store.execute("INSERT INTO topic_gaps(id,profile_id,visual,embedding,embedding_space,created_at,updated_at) "
                  "VALUES('g1','p1','v',?,'space','2026-01-01 00:00:00','2026-01-01 00:00:00')",
                  (b"\x89PNG\r\n",))
    store.execute("INSERT INTO model_profiles(role,name,provider,config) VALUES('writer','默认','p','{}')")
    with store.connect() as db:
        run_id = db.execute("INSERT INTO runs(stage,source_id,status) VALUES('detect','s1','complete')").lastrowid
    assert run_id == 1
    store.execute("INSERT INTO search_releases(id,content_id) VALUES('rel','idx')")


def test_roundtrip_sqlite_pg_sqlite(tmp_path, fresh_pg, monkeypatch):
    from mindex.shared.backends import open_context as _open_context
    from mindex.shared.config import Config
    from mindex.shared.config import Config as _Config
    from mindex.shared.db.transfer import copy_pg_to_sqlite, copy_sqlite_to_pg, table_digests
    from mindex.shared.storage import PgStore

    origin_dir = tmp_path / "sqlite-origin"
    origin = _open_context(_Config(data_dir=origin_dir)).store  # 铸 library.id（装载后身份随行）
    _seed_sqlite(origin)
    before = table_digests(origin)

    monkeypatch.setenv("MINDEX_DATABASE_DSN", fresh_pg)
    pg = PgStore(Config(data_dir=tmp_path / "pg-cache"))
    assert origin.one("SELECT value FROM settings WHERE key='library.id'") is not None
    counts = copy_sqlite_to_pg(origin, pg)
    assert counts["sources"] == 1 and counts["segments"] == 1 and counts["topic_gaps"] == 1

    after_pg = table_digests(pg)
    for table, rows in before.items():
        if rows:
            assert after_pg[table] == rows, f"{table} 装载 PG 后不一致"

    # 装载后新增默认 rowid 不与导入值冲突
    pg.execute("INSERT INTO search_releases(id,content_id) VALUES('rel2','idx')")
    assert pg.one("SELECT max(rowid) AS m FROM search_releases")["m"] >= 2
    pg_snapshot = table_digests(pg)  # 以「装载 + 追加」后的 PG 状态为回程基准

    back = copy_pg_to_sqlite(pg, tmp_path / "sqlite-back")
    assert back["sources"] == 1
    restored = Store(tmp_path / "sqlite-back")
    after_back = table_digests(restored)
    for table, rows in pg_snapshot.items():
        if rows:
            assert after_back[table] == rows, f"{table} 回到 SQLite 后不一致"


def test_open_context_pg_single_form(fresh_pg, tmp_path, monkeypatch):
    from mindex.shared.backends import open_context
    from mindex.shared.config import Config
    from mindex.shared.storage import PgStore

    monkeypatch.setenv("MINDEX_DATABASE_DSN", fresh_pg)
    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"})
    ctx = open_context(config)
    assert ctx.form == "pg-single"
    assert isinstance(ctx.store, PgStore)
    assert ctx.library_id == ctx.store.library_id
    assert ctx.media.backend == "local" and ctx.locks.backend == "file"  # 媒体/锁仍为本地实现
    # RuntimeConfig 覆盖层读写走同一 PG settings 表
    ctx.store.execute("INSERT OR REPLACE INTO settings VALUES('override.search','{}')")
    assert ctx.store.one("SELECT value FROM settings WHERE key='override.search'")["value"] == "{}"


def test_maintenance_refusal_on_pg(fresh_pg, tmp_path, monkeypatch):
    from mindex.shared.config import Config
    from mindex.shared.db.errors import UnsupportedOperation
    from mindex.shared.storage import PgStore, require_sqlite

    monkeypatch.setenv("MINDEX_DATABASE_DSN", fresh_pg)
    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"})
    PgStore(config)  # 校验可构造
    with pytest.raises(UnsupportedOperation, match="阶段 5"):
        require_sqlite(config, "业务备份导出")


def test_create_app_boots_on_pg_single(fresh_pg, tmp_path, monkeypatch, video_file):
    """pg-single 冒烟：API 装配、健康检查、覆盖层 settings 读写与管理路由全部走 PG。"""
    from fastapi.testclient import TestClient

    from mindex.api import create_app
    from mindex.shared.config import Config

    monkeypatch.setenv("MINDEX_DATABASE_DSN", fresh_pg)
    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"})
    app = create_app(config)
    assert app.state.backends.form == "pg-single"
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200 and health.json()["version"]
        # 覆盖层热合并（RuntimeConfig.current 每请求读 PG settings/model_profiles）
        assert client.get("/api/settings").status_code == 200
        body = client.put("/api/settings/business", json={"search": {"candidates": 50}}).json()
        assert body["search"]["candidates"] == 50
        # 管理路由：素材组织视图（SOURCE_SQL_PG 的 SUM CASE 化）+ 数值真值过滤
        assert client.get("/api/admin/sources").status_code == 200
        assert client.get("/api/admin/sources/none/segments").status_code == 404
