"""SQL 特征测试：钉住当前 SQLite 语义，作为阶段 1 PostgreSQL 适配器的对照基线。

水平扩展方案（plans/horizontal-scaling-plan.md §5）要求逐条登记 SQL 并显式双方言，
不做通用转译。这里用业务 schema 的真实表形状，把代码依赖的语义逐项固定下来：
REPLACE/IGNORE 的冲突行为、rowid 身份与排序、数值 OR 真值、布尔聚合、类型亲和、
json_extract、时间戳格式与事务可见性。阶段 1 起，同一组断言经
``MINDEX_TEST_POSTGRES_DSN`` 门控对 PostgreSQL 适配器参数化运行（plans §12）。
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from shared.storage import Store, dumps

# 复用业务表形状（storage.py METADATA_SCHEMA_V1 的子集 + persons 的 search_releases）：
# 未列全列的写入必须暴露「REPLACE 重置默认列 / NOT NULL 失败」这类语义。
SCHEMA = """
CREATE TABLE sources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
  media TEXT NOT NULL, layout_key TEXT NOT NULL, prepared INTEGER NOT NULL DEFAULT 0);
CREATE TABLE model_profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, name TEXT NOT NULL,
  provider TEXT NOT NULL, config TEXT NOT NULL, api_key TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(role, name));
CREATE TABLE media_references (
  path TEXT PRIMARY KEY, source_id TEXT NOT NULL, start REAL NOT NULL,
  end REAL NOT NULL, version TEXT NOT NULL, refs TEXT NOT NULL);
CREATE TABLE index_members (
  index_id TEXT NOT NULL, segment_id TEXT NOT NULL, snapshot TEXT NOT NULL,
  complete INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(index_id, segment_id));
CREATE TABLE search_releases (id TEXT PRIMARY KEY, content_id TEXT, person_id TEXT);
CREATE TABLE source_curation (
  id TEXT PRIMARY KEY, display_name TEXT, notes TEXT NOT NULL DEFAULT '',
  favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE segment_curation (
  id TEXT PRIMARY KEY, display_name TEXT, notes TEXT NOT NULL DEFAULT '',
  favorite INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE segments (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL, kind TEXT NOT NULL,
  start REAL NOT NULL, end REAL NOT NULL);
CREATE TABLE media_groups (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, UNIQUE(type, name));
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL,
  source_id TEXT, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT);
"""


@pytest.fixture
def db(tmp_path):
    """独立 SQLite 连接（Row 工厂，与 Store.connect 相同配置）。

    阶段 1 将本 fixture 参数化为 sqlite/postgres 两个后端，断言保持不变。
    """
    connection = sqlite3.connect(tmp_path / "characteristics.sqlite3", timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    connection.commit()
    yield connection
    connection.close()


def test_insert_or_replace_resets_defaults_and_moves_rowid(db):
    """REPLACE 是「删旧行再插入」：未列出的默认列被重置，rowid 变化。

    admin/curation、channel_index、runtime_settings 等大量使用 INSERT OR REPLACE；
    PG 侧必须按表显式决定「更新现有行」还是「重置为新行」（plans §2）。
    """
    db.execute(
        "INSERT INTO model_profiles(role,name,provider,config,api_key) "
        "VALUES('writer','a','http','{}','secret')"
    )
    first_id = db.execute("SELECT id FROM model_profiles WHERE name='a'").fetchone()["id"]
    db.execute(
        "INSERT OR REPLACE INTO model_profiles(id,role,name,provider,config) "
        "VALUES(?, 'writer','a','http','{\"v\":2}')",
        (first_id,),
    )
    row = db.execute("SELECT * FROM model_profiles WHERE name='a'").fetchone()
    assert json.loads(row["config"]) == {"v": 2}
    assert row["api_key"] is None  # 未列出的列回到默认（NULL），旧值不保留
    assert row["id"] == first_id  # 显式指定 PK 时保留；省略 PK 时会分配新 rowid
    db.execute(
        "INSERT OR REPLACE INTO model_profiles(role,name,provider,config) "
        "VALUES('writer','a','http','{\"v\":3}')"
    )
    replaced = db.execute("SELECT * FROM model_profiles WHERE name='a'").fetchone()
    assert replaced["id"] != first_id  # 省略 PK：旧行被删，新行拿新 rowid
    assert db.execute("SELECT count(*) AS n FROM model_profiles").fetchone()["n"] == 1


def test_insert_or_replace_without_required_column_fails(db):
    """REPLACE 不豁免 NOT NULL：media_groups.py 更新组描述时必须列出全部必填列。"""
    db.execute("INSERT INTO media_groups(id,type,name,updated_at) VALUES('g1','source','组','2026')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT OR REPLACE INTO media_groups(id,type,name,description) "
            "VALUES('g1','source','组','新描述')"
        )
    kept = db.execute("SELECT * FROM media_groups WHERE id='g1'").fetchone()
    assert kept["description"] == "" and kept["updated_at"] == "2026"  # 失败则旧行原样保留


def test_on_conflict_update_preserves_row(db):
    """media_groups 的 ON CONFLICT(id) DO UPDATE 保留未更新列与行身份——与 REPLACE 不同。"""
    db.execute(
        "INSERT INTO media_groups(id,type,name,description,updated_at) "
        "VALUES('g1','source','组','旧','2026-01')"
    )
    db.execute(
        "INSERT INTO media_groups(id,type,name,description,updated_at) "
        "VALUES('g1','source','组','新','2026-02') "
        "ON CONFLICT(id) DO UPDATE SET description=excluded.description"
    )
    row = db.execute("SELECT * FROM media_groups WHERE id='g1'").fetchone()
    assert row["description"] == "新" and row["updated_at"] == "2026-01"  # 未提及列保留


def test_insert_or_ignore_first_row_wins(db):
    """INSERT OR IGNORE：冲突保留先入行（UNIQUE(role,name) 与 PRIMARY KEY 都适用）。"""
    db.execute("INSERT INTO model_profiles(role,name,provider,config) VALUES('writer','a','p1','{}')")
    db.execute(
        "INSERT OR IGNORE INTO model_profiles(role,name,provider,config) VALUES('writer','a','p2','{}')"
    )
    row = db.execute("SELECT provider FROM model_profiles WHERE name='a'").fetchone()
    assert row["provider"] == "p1"
    db.execute(
        "INSERT OR IGNORE INTO index_members(index_id,segment_id,snapshot,complete) VALUES('i','s1','{}',1)"
    )
    db.execute(
        "INSERT OR IGNORE INTO index_members(index_id,segment_id,snapshot) VALUES('i','s1','{\"v\":2}')"
    )
    kept = db.execute("SELECT snapshot FROM index_members WHERE segment_id='s1'").fetchone()
    assert json.loads(kept["snapshot"]) == {}  # pipeline 注册片段依赖「先到先得」


def test_numeric_or_truth_and_filter_equality(db):
    """curation 的有效隐藏过滤依赖 SQLite 的整数真值：``(...) = 1`` 可比较、可作参数。"""
    db.executemany("INSERT INTO source_curation(id,hidden) VALUES(?,?)", [("s1", 1), ("s2", 0)])
    db.executemany("INSERT INTO segment_curation(id,hidden) VALUES(?,?)", [("seg1", 0), ("seg2", 2)])
    db.execute("INSERT INTO segments VALUES('seg1','s1','shot',0,1)")
    db.execute("INSERT INTO segments VALUES('seg2','s2','shot',0,1)")
    rows = db.execute(
        "SELECT seg.id, (COALESCE(sc.hidden,0) OR COALESCE(c.hidden,0)) AS effective_hidden "
        "FROM segments seg LEFT JOIN segment_curation sc ON sc.id=seg.id "
        "LEFT JOIN source_curation c ON c.id=seg.source_id"
    ).fetchall()
    truth = {row["id"]: row["effective_hidden"] for row in rows}
    assert truth == {"seg1": 1, "seg2": 1}  # OR 归一为整数 0/1（2 为真）
    visible = db.execute(
        "SELECT seg.id FROM segments seg LEFT JOIN segment_curation sc ON sc.id=seg.id "
        "LEFT JOIN source_curation c ON c.id=seg.source_id "
        "WHERE (COALESCE(sc.hidden,0) OR COALESCE(c.hidden,0)) = ?",
        (0,),
    ).fetchall()
    assert not visible  # 数值参数 0 与 OR 结果可直接比较（PG 侧需显式布尔化改写）


def test_boolean_aggregation_sum(db):
    """``SUM(kind='shot')`` 式布尔聚合（curation/clean 统计口径）。"""
    db.executemany(
        "INSERT INTO segments VALUES(?,?,?,?,?)",
        [("a", "s", "shot", 0, 1), ("b", "s", "moment", 0, 2), ("c", "s", "shot", 1, 3)],
    )
    row = db.execute("SELECT count(*) AS n, SUM(kind='shot') AS shots FROM segments").fetchone()
    assert row["n"] == 3 and row["shots"] == 2


def test_rowid_latest_and_update_target(db):
    """「ORDER BY rowid DESC LIMIT 1 取最新」与「UPDATE ... WHERE rowid=?」。

    涉及 search_releases、channel_data、scene_plans、dialogue、shot_repair 等十余处
    （plans §3）；PG 侧为这些表增加显式顺序字段，历史按原 rowid 导入。
    """
    for i in range(3):
        db.execute("INSERT INTO search_releases(id,content_id) VALUES(?,?)", (f"r{i}", "idx"))
    latest = db.execute(
        "SELECT id FROM search_releases WHERE content_id='idx' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert latest["id"] == "r2"  # rowid 反映插入顺序
    target = db.execute("SELECT rowid FROM search_releases WHERE id='r1'").fetchone()["rowid"]
    db.execute("UPDATE search_releases SET person_id='p' WHERE rowid=?", (target,))
    assert db.execute("SELECT person_id FROM search_releases WHERE id='r1'").fetchone()["person_id"] == "p"
    db.execute("DELETE FROM search_releases WHERE id='r2'")
    db.execute("INSERT INTO search_releases(id,content_id) VALUES('r3','idx')")
    after = db.execute(
        "SELECT id FROM search_releases WHERE content_id='idx' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert after["id"] == "r3"  # 无 AUTOINCREMENT 的表可能复用已删最大 rowid，顺序仍单调推进


def test_row_factory_name_and_position_access(db):
    row = db.execute("SELECT id, name FROM model_profiles LIMIT 0").fetchone()
    assert row is None
    db.execute("INSERT INTO model_profiles(role,name,provider,config) VALUES('writer','a','p','{}')")
    row = db.execute("SELECT role,name FROM model_profiles").fetchone()
    assert row["name"] == row[1] == "a"  # 统一行对象必须同时支持名称与位置访问（plans §5）


def test_json_extract_semantics(db):
    """snapshots() 按 ``json_extract(snapshot,'$.source_id')`` 过滤：缺键得 NULL。"""
    db.execute("INSERT INTO index_members VALUES('i','s1','{\"source_id\":\"src1\"}',1)")
    db.execute("INSERT INTO index_members VALUES('i','s2','{}',1)")
    hit = db.execute(
        "SELECT segment_id FROM index_members WHERE json_extract(snapshot,'$.source_id')=?", ("src1",)
    ).fetchall()
    assert [row["segment_id"] for row in hit] == ["s1"]
    nulls = db.execute(
        "SELECT count(*) AS n FROM index_members WHERE json_extract(snapshot,'$.source_id') IS NULL"
    ).fetchone()
    assert nulls["n"] == 1  # 缺键/非 JSON 均为 NULL；PG 侧保留 TEXT 原文 + 受控 cast（plans §5）


def test_type_affinity_and_text_sorting(db):
    """SQLite 宽松类型：TEXT 列收数字会转文本并按字典序排序；REAL 列收 '5' 转 5。

    SQL 数值/文本排序差异纳入 SQLite↔PG 对照（plans §5）。
    """
    db.execute("INSERT INTO sources VALUES('a','2','p','{}','k',0)")
    db.execute("INSERT INTO sources VALUES('b','10','p','{}','k',0)")
    ordered = [row["name"] for row in db.execute("SELECT name FROM sources ORDER BY name")]
    assert ordered == ["10", "2"]  # TEXT 列按字典序：'10' < '2'
    db.execute("INSERT INTO media_references VALUES('m','s','5','6','v','[]')")
    real = db.execute("SELECT start,end FROM media_references WHERE path='m'").fetchone()
    assert (real["start"], real["end"]) == (5.0, 6.0)  # REAL 亲和把 '5' 存为 5.0
    assert isinstance(real["start"], float)


def test_unique_and_integrity_error_mapping(db):
    """UNIQUE/NOT NULL 冲突在 SQLite 抛 sqlite3.IntegrityError。

    media_groups.py、runtime_settings.py 按此把冲突转成用户可见错误；统一异常
    层（阶段 1）必须让 PG 的等价错误同样落到这些分支（plans §5）。
    """
    db.execute("INSERT INTO model_profiles(role,name,provider,config) VALUES('writer','a','p','{}')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO model_profiles(role,name,provider,config) VALUES('writer','a','p','{}')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO model_profiles(role,provider,config) VALUES('writer','p','{}')")


def test_current_timestamp_format(db):
    """SQLite CURRENT_TIMESTAMP 是 'YYYY-MM-DD HH:MM:SS'（UTC、无时区、无 T）。

    运行时配置/任务表的 updated_at 依赖该格式往返；PG 侧默认值必须显式格式化为
    同一形状（plans §5：已有时间戳首版保留 TEXT 与 UTC 固定格式）。
    """
    db.execute("INSERT INTO runs(stage,source_id,status) VALUES('detect','s','running')")
    stamp = db.execute("SELECT started_at FROM runs").fetchone()["started_at"]
    assert len(stamp) == 19 and stamp[4] == "-" and stamp[10] == " " and stamp[13] == ":"


def test_executemany_atomic_within_transaction(db):
    """executemany（channel_index 检查点、backup、clean 批量写）整体受事务保护。"""
    try:
        db.executemany(
            "INSERT INTO sources VALUES(?,?,?,?,?,?)",
            [
                ("s1", "甲", "p", "{}", "k", 0),
                ("s2", None, "p", "{}", "k", 0),  # NOT NULL 失败
            ],
        )
    except sqlite3.IntegrityError:
        pass
    db.rollback()  # Store.connect 异常路径统一回滚
    assert db.execute("SELECT count(*) AS n FROM sources").fetchone()["n"] == 0


def test_store_connect_commit_and_rollback_visibility(tmp_path):
    """Store.connect：正常退出提交、异常回滚；独立连接只看到已提交数据。"""
    store = Store(tmp_path / "data")
    with pytest.raises(RuntimeError):
        with store.connect() as db:
            db.execute("INSERT INTO settings VALUES('k','v')")
            raise RuntimeError("boom")
    assert store.one("SELECT value FROM settings WHERE key='k'") is None
    with store.connect() as db:
        db.execute("INSERT INTO settings VALUES('k','v')")
    assert store.one("SELECT value FROM settings WHERE key='k'")["value"] == "v"
    # with 块正常退出即提交：新连接立即可见（迁移本身可能写入其他 settings 键）
    fresh = store.one("SELECT value FROM settings WHERE key='k'")
    assert fresh is not None and fresh["value"] == "v"


def test_begin_immediate_serializes_writers(tmp_path):
    """BEGIN IMMEDIATE 持写锁：另一连接的写等待 busy timeout 后失败。"""
    store = Store(tmp_path / "data")
    holder = store.connect()
    db = holder.__enter__()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO settings VALUES('k','v')")

        def clash():
            other = sqlite3.connect(store.path, timeout=0.2)
            try:
                other.execute("INSERT INTO settings VALUES('k2','v')")
                other.commit()
            except sqlite3.OperationalError:
                failures.append("locked")
            finally:
                other.close()

        failures: list[str] = []
        thread = threading.Thread(target=clash)
        thread.start()
        thread.join()
        assert failures == ["locked"]  # 写锁被持有：对方等满 busy timeout 后失败
        assert store.one("SELECT value FROM settings WHERE key='k2'") is None
    finally:
        holder.__exit__(None, None, None)
    # 持有方提交释放后，其他写入方可见
    assert store.one("SELECT value FROM settings WHERE key='k'")["value"] == "v"


def test_json_roundtrip_preserves_canonical_text(tmp_path):
    """dumps() 的 canonical JSON（sort_keys）是快照/配置列的存储原文。

    阶段 1 保留 TEXT 原文（字节、键顺序与数值表示不变），不做全库 JSONB 改写
    （plans §5）。
    """
    store = Store(tmp_path / "data")
    value = {"b": 1, "a": {"z": [1, 2], "y": "中文"}}
    with store.connect() as db:
        db.execute("INSERT INTO transcripts VALUES('s','p',?)", (dumps(value),))
    raw = store.one("SELECT data FROM transcripts")["data"]
    assert raw == '{"a": {"y": "中文", "z": [1, 2]}, "b": 1}'
    assert json.loads(raw) == value
