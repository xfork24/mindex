"""人物识别性能统计与诊断日志回归。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import create_app
from persons import profiling
from persons.person_models import Entity
from persons.persons import Persons
from shared.storage import Store, dumps


@pytest.fixture
def seeded(config):
    store = Store(config.data_dir)
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
        ("source", "影片", "source.mp4", dumps({"duration": 3}), "layout"),
    )
    (store.root / "shot.jpg").write_bytes(b"jpeg")
    for n in range(2):
        store.execute(
            "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"shot{n}", "source", "shot", n, n + 1, "source.mp4", "shot.jpg", "gen", None, "fixture"),
        )
    persons = Persons(store)
    work = persons.work_for("source")
    actor = persons.save(Entity(kind="actor", name="演员甲"))
    persons.save(Entity(kind="role", name="角色甲", work_ids=[work], actor_ids=[actor["id"]]))
    for n in range(2):
        store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"e{n}",
                "source",
                f"shot{n}",
                None,
                None,
                None,
                float(n),
                n + 0.4,
                "candidate",
                dumps({"work_id": work}),
            ),
        )
    return config


def test_profiling_disabled_adds_nothing(seeded, monkeypatch):
    monkeypatch.delenv(profiling.ENV_FLAG, raising=False)
    client = TestClient(create_app(seeded))
    for url in (
        "/api/persons/review/shots?source_id=source",
        "/api/persons/review/shots/shot0?source_id=source",
    ):
        response = client.get(url)
        assert response.status_code == 200
        assert "server-timing" not in response.headers


def test_profiling_enabled_reports_stages_on_every_path(seeded, monkeypatch):
    monkeypatch.setenv(profiling.ENV_FLAG, "1")
    client = TestClient(create_app(seeded))
    response = client.get("/api/persons/review/shots?source_id=source")
    timing = response.headers["server-timing"]
    for name in (
        "filters_sql",
        "scope_total_sql",
        "page_sql",
        "evidence_sql",
        "json_decode",
        "total",
        "middleware",
    ):
        assert name + ";dur=" in timing
    assert "evidence_sql;dur=" in timing and 'desc="n=2"' in timing  # 两个分镜，各一次证据查询
    assert response.json()["total"] == 2

    single = client.get("/api/persons/review/shots/shot0?source_id=source")
    assert "page_sql;dur=" in single.headers["server-timing"]


def test_profiling_does_not_change_review_payload(seeded, monkeypatch):
    client = TestClient(create_app(seeded))
    url = "/api/persons/review/shots?source_id=source"
    monkeypatch.delenv(profiling.ENV_FLAG, raising=False)
    plain = client.get(url).json()
    monkeypatch.setenv(profiling.ENV_FLAG, "1")
    assert client.get(url).json() == plain


def test_evidence_image_branches_are_labelled(seeded, monkeypatch):
    monkeypatch.setenv(profiling.ENV_FLAG, "1")
    client = TestClient(create_app(seeded))
    # e0 没有 image_path，走镜头缩略图回退，并经过历史版本扫描。
    response = client.get("/api/persons/evidence/e0/image")
    assert response.status_code == 200 and response.content == b"jpeg"
    assert response.headers["x-profile-branch"] == "shot_thumbnail"
    assert "versions_scan;dur=" in response.headers["server-timing"]
    assert client.get("/api/persons/evidence/nope/image").status_code == 404


def test_stage_is_noop_outside_profile():
    with profiling.stage("anything"):
        pass
    profiling.note("k", "v")
    with profiling.profiled("x") as profile:
        assert profile is None or profile.name == "x"


def test_profile_header_accumulates_repeated_stage(monkeypatch):
    monkeypatch.setenv(profiling.ENV_FLAG, "1")
    with profiling.profiled("x") as profile:
        for _ in range(3):
            with profiling.stage("loop"):
                pass
    assert profile.stages["loop"][1] == 3
    assert 'desc="n=3"' in profile.header()
