"""persons 审核/发布查询的 SQLite ↔ PostgreSQL 一致性（plans §5 阶段 5）。

同一份种子数据分别写入 SQLite 与 PG，比较审核摘要、审核列表、发布快照与版本
证据索引的结果——确保 JSON 查询的双方言重写语义一致（json_extract/json_each/
json_group_array/布尔与数值比较）。
"""

from __future__ import annotations

import json
import os

import pytest

from mindex.persons.persons import Persons
from mindex.shared.storage import Store, dumps

pytestmark = pytest.mark.requires_postgres

DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")


@pytest.fixture
def stores(tmp_path, monkeypatch):
    import psycopg

    from mindex.shared.config import Config
    from mindex.shared.db.migrate_pg import migrate_pg
    from mindex.shared.storage import PgStore

    if not DSN:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", DSN)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(DSN) as conn:
        migrate_pg(conn)
    sqlite = Store(tmp_path / "sqlite")
    postgres = PgStore(Config(data_dir=tmp_path / "pg", database={"backend": "postgres"}))
    for store in (sqlite, postgres):
        _seed(store)
    return sqlite, postgres


def _seed(store):
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('s1','素材','p','{}','k')")
    for shot, start, end in (("shot1", 0, 1), ("shot2", 1, 2), ("shot3", 2, 3)):
        store.execute(
            "INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (shot, "s1", "shot", start, end, f"clips/{shot}.mp4", f"{shot}.jpg", "g", "r"))
    store.execute("INSERT INTO person_entities(id,kind,data) VALUES('w1','work',?)",
                  (dumps({"id": "w1", "name": "作品", "kind": "work"}),))
    store.execute("INSERT INTO person_entities(id,kind,data) VALUES('r1','role',?)",
                  (dumps({"id": "r1", "name": "主角", "kind": "role",
                          "work_ids": ["w1"], "actor_ids": ["a1"]}),))
    store.execute("INSERT INTO person_entities(id,kind,data) VALUES('a1','actor',?)",
                  (dumps({"id": "a1", "name": "演员", "kind": "actor"}),))
    store.execute("INSERT INTO person_sources(source_id,work_id) VALUES('s1','w1')")
    evidence = {
        "e1": ("shot1", "accepted", {"confirmation": "shot_review", "score": 0.9,
                                     "reviewed_at": "2026-01-01T00:00:00", "shot_excluded": False,
                                     "image_path": "persons/frames/s1/e1.jpg"}),
        "e2": ("shot2", "candidate", {"score": 0.5}),
        "e3": ("shot3", "rejected", {"score": 0.2, "reviewed": True, "shot_excluded": True}),
    }
    for evidence_id, (shot, status, data) in evidence.items():
        store.execute(
            "INSERT INTO person_evidence(id,source_id,shot_id,actor_id,role_id,start,end,status,data) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (evidence_id, "s1", shot, "a1", "r1", 0.0, 1.0, status, dumps({"id": evidence_id, **data})))
    store.execute("INSERT INTO person_coverage VALUES('s1','cov1',?)",
                  (dumps({"shot_id": "shot3", "removed_confirmation": True}),))
    version = {
        "entities": [{"id": "r1", "name": "主角", "kind": "role", "work_ids": ["w1"], "actor_ids": ["a1"]},
                     {"id": "a1", "name": "演员", "kind": "actor"}],
        "evidence": [{"id": "e1", "source_id": "s1", "shot_id": "shot1", "actor_id": "a1",
                      "role_id": "r1", "start": 0, "end": 1, "status": "accepted",
                      "data": {"score": 0.9, "image_path": "persons/frames/s1/e1.jpg"}}],
        "coverage": [],
        "format": 2,
    }
    store.execute("INSERT INTO person_versions(id,data) VALUES('v1',?)", (dumps(version),))
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('i1','{}','ready')")
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES('rel1','i1','v1')")


def test_review_queries_match_between_backends(stores):
    sqlite, postgres = stores
    assert Persons(sqlite).review_summary() == Persons(postgres).review_summary()
    left = Persons(sqlite).review_shots(limit=50)
    right = Persons(postgres).review_shots(limit=50)
    assert json.loads(dumps(left)) == json.loads(dumps(right))


def test_published_snapshot_matches_between_backends(stores):
    sqlite, postgres = stores
    left_release, left = Persons(sqlite).published("i1", pinned=True, source_id="s1")
    right_release, right = Persons(postgres).published("i1", pinned=True, source_id="s1")
    assert left_release["id"] == right_release["id"] == "rel1"
    assert json.loads(dumps(left)) == json.loads(dumps(right))


def test_released_entities_and_version_images_match(stores):
    sqlite, postgres = stores
    sqlite_release = Persons(sqlite).release_for("i1", pinned=True)
    pg_release = Persons(postgres).release_for("i1", pinned=True)
    assert (Persons(sqlite).published_entities(sqlite_release)
            == Persons(postgres).published_entities(pg_release))
    assert (Persons(sqlite)._version_image_index("v1")
            == Persons(postgres)._version_image_index("v1"))
