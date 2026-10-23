from __future__ import annotations

import pytest

from mindex.persons.person_models import Entity
from mindex.persons.persons import Persons
from mindex.shared.storage import Store, dumps


@pytest.fixture
def scenario(config):
    store = Store(config.data_dir)
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
        ("source", "影片", "source.mp4", dumps({"duration": 9}), "layout"),
    )
    for n in range(6):
        store.execute(
            "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"shot{n}", "source", "shot", n, n + 1, "source.mp4", "source.mp4", "gen", None, "fixture"),
        )
    persons = Persons(store)
    work = persons.work_for("source")
    actor = persons.save(Entity(kind="actor", name="演员甲"))
    stranger = persons.save(Entity(kind="actor", name="演员乙"))  # 没有任何角色，配对无效
    role = persons.save(Entity(kind="role", name="角色甲", work_ids=[work], actor_ids=[actor["id"]]))
    a, r, x = actor["id"], role["id"], stranger["id"]

    def evidence(identifier, shot, actor_id, role_id, status, **data):
        store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identifier, "source", shot, actor_id, role_id, None, 0.1, 0.4, status, dumps(data)),
        )

    evidence("auto", "shot0", a, r, "accepted", score=0.9, evidence_type="track")
    evidence("cand-hi", "shot1", a, r, "candidate", score=0.8)
    evidence("cand-lo", "shot1", a, r, "candidate", score=0.5)
    evidence("cand-invalid", "shot1", x, None, "candidate", score=0.99)
    evidence("excluded", "shot2", None, None, "rejected", confirmation="shot_review", shot_excluded=True)
    evidence("rejected-then-marked", "shot3", a, r, "rejected", confirmation="shot_review")
    evidence("mark", "shot3", a, r, "accepted", confirmation="shot_review", evidence_type="manual", score=1.0,
             original_score=0.7, reviewed_at="2026-01-01T00:00:00+00:00")
    evidence("plain-rejected", "shot5", a, r, "rejected", score=0.2)  # 无 shot_review 标记，不算已排除
    store.execute(  # 无检测结果、只有覆盖记录的分镜
        "INSERT INTO person_coverage(source_id,cache_id,data) VALUES(?,?,?)",
        ("source", "cov", dumps({"shot_id": "shot4", "start": 4, "end": 5})),
    )
    return persons


def states(persons, **scope):
    with persons.store.connect() as db:
        persons._build_review_tables(db, **scope)
        return sorted(tuple(r) for r in db.execute(
            "SELECT shot_id,source_id,accepted,candidate,rejected FROM review_states"))


def legacy_states(persons):
    return sorted(tuple(r.values()) for r in persons.store.rows(
        "SELECT shot_id,source_id,accepted,candidate,rejected FROM " + persons.SHOT_STATES + " k"))


def test_review_states_table_matches_shot_states_view(scenario):
    expected = legacy_states(scenario)
    assert expected  # 场景非空
    assert states(scenario) == expected
    assert states(scenario, source_id="source") == expected
    only = [row for row in expected if row[0] == "shot1"]
    assert states(scenario, source_id="source", shot_id="shot1") == only  # 过滤下推不改变该分镜的结果


def test_covering_index_exists_and_listing_does_not_need_it(scenario):
    names = {r["name"] for r in scenario.store.rows(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='person_evidence'")}
    assert "person_evidence_review" in names
    scenario.store.execute("DROP INDEX person_evidence_review")  # 旧库在迁移前没有这个索引，结果必须相同
    assert scenario.review_shots("source", "accepted")["total"] == 2


def test_status_buckets_partition_scope_and_summary_agrees(scenario):
    everything = scenario.review_shots("source", limit=200)
    parts = {s: scenario.review_shots("source", s, limit=200) for s in ("accepted", "candidate", "rejected")}
    assert everything["total"] == everything["scope_total"] == 6
    assert sum(p["total"] for p in parts.values()) == 6
    assert {i["shot_id"] for i in parts["accepted"]["items"]} == {"shot0", "shot3"}
    assert {i["shot_id"] for i in parts["rejected"]["items"]} == {"shot2"}
    assert {i["shot_id"] for i in parts["candidate"]["items"]} == {"shot1", "shot4", "shot5"}
    summary = scenario.review_summary()["source"]
    assert summary == {s: p["total"] for s, p in parts.items()}


def test_candidate_ranking_and_filters_are_stable(scenario):
    candidate = scenario.review_shots("source", "candidate", limit=200)
    shot1 = next(i for i in candidate["items"] if i["shot_id"] == "shot1")
    # 无效配对（分数 0.99）不参与排名，最高分有效候选胜出。
    assert shot1["candidate"]["score"] == 0.8 and shot1["candidate"]["actor_id"]
    keys = [(f["actor_id"] or "", f["role_id"] or "") for f in candidate["filters"]]
    assert keys == sorted(keys)
    only_none = scenario.review_shots("source", "candidate", no_candidate=True, limit=200)
    assert {i["shot_id"] for i in only_none["items"]} == {"shot4"}
