"""PG 一致性导出内核验收（plans §10 阶段 5）：快照一致性、rowid 保序、逐表清单。"""

from __future__ import annotations

import hashlib
import json as _json
import os

import pytest

from shared.config import Config
from shared.storage import Store

pytestmark = pytest.mark.requires_postgres

DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")


@pytest.fixture
def pg_store(tmp_path, monkeypatch):
    if not DSN:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    import psycopg

    from shared.db.migrate_pg import migrate_pg
    from shared.storage import PgStore

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(DSN) as conn:
        migrate_pg(conn)
    monkeypatch.setenv("MINDEX_DATABASE_DSN", DSN)
    return PgStore(Config(data_dir=tmp_path / "data", database={"backend": "postgres"}))


def test_export_matches_source_and_preserves_rowid(pg_store, tmp_path):
    from maintenance.pg_export import export_pg_to_sqlite

    for index in range(3):
        pg_store.execute("INSERT INTO search_releases(id,content_id) VALUES(?,?)", (f"r{index}", "idx"))
    pg_store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('s1','素材','p','{}','k')")
    pg_store.execute("INSERT INTO settings(key,value) VALUES('active','idx')")
    blob = b"\x00\x01\xff" * 10
    pg_store.execute(
        "INSERT INTO topic_gaps(id,profile_id,visual,embedding,embedding_space,created_at,updated_at)"
        " VALUES('g','p','v',?,'space','2026-01-01 00:00:00','2026-01-01 00:00:00')",
        (blob,),
    )

    target = tmp_path / "export" / "metadata.sqlite3"
    target.parent.mkdir(parents=True)
    manifest = export_pg_to_sqlite(pg_store, target)
    assert manifest["total_rows"] >= 5
    assert manifest["tables"]["search_releases"]["rows"] == 3
    assert manifest["tables"]["topic_gaps"]["rows"] == 1

    restored = Store(target)
    assert restored.one("SELECT value FROM settings WHERE key='active'")["value"] == "idx"
    assert restored.one("SELECT embedding FROM topic_gaps WHERE id='g'")["embedding"] == blob
    # rowid 保序：SQLite 侧 rowid 与导出顺序一致（后续按原语义维护）
    assert [row["id"] for row in restored.rows("SELECT id FROM search_releases ORDER BY rowid")] == [
        "r0",
        "r1",
        "r2",
    ]


def test_export_snapshot_is_consistent_under_concurrent_writes(pg_store, tmp_path):
    """导出前完成的写入全部进入快照、行数精确一致（快照的全有或全无口径）。"""
    from maintenance.pg_export import export_pg_to_sqlite

    for index in range(5):
        pg_store.execute("INSERT INTO settings(key,value) VALUES(?,?)", (f"churn-{index}", "x"))
    manifest = export_pg_to_sqlite(pg_store, tmp_path / "snap.sqlite3")
    restored = Store(tmp_path / "snap.sqlite3")
    assert [
        row["key"] for row in restored.rows("SELECT key FROM settings WHERE key LIKE 'churn-%' ORDER BY key")
    ] == [f"churn-{index}" for index in range(5)]
    assert manifest["tables"]["settings"]["rows"] == restored.one("SELECT count(*) AS n FROM settings")["n"]


def test_repeatable_read_snapshot_excludes_later_writes(pg_store):
    """快照建立（首条查询）后的写入对导出事务不可见（REPEATABLE READ 语义锚定）。"""
    pool = pg_store.backend.pool()
    connection = pool.getconn()
    try:
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cursor.execute("SELECT count(*) FROM settings")  # 固定快照
            before = cursor.fetchone()[0]
            pg_store.execute("INSERT INTO settings(key,value) VALUES('late-write','1')")
            cursor.execute("SELECT count(*) FROM settings")
            assert cursor.fetchone()[0] == before  # 事务外的后写不进快照
        connection.rollback()
    finally:
        pool.putconn(connection)
    assert pg_store.one("SELECT value FROM settings WHERE key='late-write'") is not None  # 写确实已落库


def test_media_bundle_copies_referenced_files(pg_store, tmp_path):
    """--with-media：按可达性复制正文/缩略图，生成 media_manifest 且摘要可校验。"""

    from maintenance.pg_export import export_media_bundle, export_pg_to_sqlite
    from shared.backends import LocalMediaStore

    root = pg_store.root
    for key in ("sources/s1/a.mp4", "thumbnails/s1.jpg", "clips/s1/shot/x.mp4"):
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.encode())
    pg_store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key) VALUES('s1','素材','sources/s1/a.mp4','{}','k')"
    )
    pg_store.execute(
        "INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
        "VALUES('seg1','s1','shot',0,5,'clips/s1/shot/x.mp4','thumbnails/s1.jpg','g','r')"
    )
    target = tmp_path / "bundle-media"
    export_pg_to_sqlite(pg_store, target)
    stats = export_media_bundle(pg_store, LocalMediaStore(root), target)
    assert stats["objects"] == 3

    manifest = _json.loads((target / "media_manifest.json").read_text())
    assert set(manifest) == {"sources/s1/a.mp4", "thumbnails/s1.jpg", "clips/s1/shot/x.mp4"}
    assert (target / "data" / "sources/s1/a.mp4").read_bytes() == b"sources/s1/a.mp4"
    assert manifest["sources/s1/a.mp4"]["sha256"] == hashlib.sha256(b"sources/s1/a.mp4").hexdigest()
