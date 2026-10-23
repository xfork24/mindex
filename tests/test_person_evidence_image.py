from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.persons import persons as persons_module
from mindex.shared.storage import Store, dumps


@pytest.fixture
def world(config):
    persons_module._VERSION_IMAGE_CACHE.clear()
    store = Store(config.data_dir)
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)",
        ("source", "影片", "source.mp4", dumps({"duration": 3}), "layout"),
    )
    store.execute(
        "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("shot0", "source", "shot", 0, 1, "source.mp4", "thumb.jpg", "gen", None, "fixture"),
    )
    for name, body in (("current.jpg", b"current"), ("new.jpg", b"newer-version"), ("old.jpg", b"older-version"),
                       ("thumb.jpg", b"thumbnail")):
        (store.root / name).write_bytes(body)

    def evidence(identifier, image_path):
        store.execute(
            "INSERT INTO person_evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identifier, "source", "shot0", None, None, None, 0.0, 0.4, "rejected",
             dumps({"image_path": image_path} if image_path else {})),
        )

    def version(identifier, created, evidence_rows):
        store.execute(
            "INSERT INTO person_versions(id,data,created_at) VALUES(?,?,?)",
            (identifier, dumps({"format": 2, "entities": [], "coverage": [], "evidence": evidence_rows}), created),
        )

    def published(identifier, path):
        return {"id": identifier, "shot_id": "shot0", "source_id": "source", "data": {"image_path": path}}

    return store, evidence, version, published, TestClient(create_app(config))


def get(client, identifier):
    return client.get(f"/api/persons/evidence/{identifier}/image")


def test_current_path_wins_and_is_cacheable_for_a_short_time(world):
    _, evidence, version, published, client = world
    evidence("e", "current.jpg")
    version("v1", "2026-01-01", [published("e", "old.jpg")])
    response = get(client, "e")
    assert response.content == b"current"
    assert response.headers["cache-control"] == "private, max-age=300"


def test_missing_current_file_falls_back_to_newest_version_then_older(world):
    _, evidence, version, published, client = world
    evidence("e", "gone.jpg")
    version("old", "2026-01-01", [published("e", "old.jpg")])
    version("new", "2026-02-01", [published("e", "new.jpg")])
    assert get(client, "e").content == b"newer-version"
    # 最新版本里的文件也缺失时，退到更早的版本。
    version("newest", "2026-03-01", [published("e", "also-gone.jpg")])
    persons_module._VERSION_IMAGE_CACHE.clear()
    assert get(client, "e").content == b"newer-version"
    (world[0].root / "new.jpg").unlink()
    assert get(client, "e").content == b"older-version"


def test_evidence_without_image_uses_version_then_shot_thumbnail(world):
    _, evidence, version, published, client = world
    evidence("no-image", None)
    assert get(client, "no-image").content == b"thumbnail"  # 没有任何版本也能回退到镜头缩略图
    version("v", "2026-01-01", [published("no-image", "old.jpg")])
    assert get(client, "no-image").content == b"older-version"


def test_published_only_evidence_and_unknown_ids(world):
    _, _, version, published, client = world
    version("v", "2026-01-01", [published("only-in-version", "old.jpg")])
    assert get(client, "only-in-version").content == b"older-version"
    assert get(client, "unknown").status_code == 404


def test_thumbnail_missing_returns_404(world):
    store, evidence, _, _, client = world
    evidence("e", None)
    (store.root / "thumb.jpg").unlink()
    assert get(client, "e").status_code == 404


def test_each_version_is_parsed_once(world, monkeypatch):
    _, evidence, version, published, client = world
    evidence("e", None)
    version("v", "2026-01-01", [published("e", "old.jpg")])
    scans = []
    original = Store.rows

    def counting(self, sql, args=()):
        if "json_each" in sql:
            scans.append(sql)
        return original(self, sql, args)

    monkeypatch.setattr(Store, "rows", counting)
    for _ in range(3):
        assert get(client, "e").content == b"older-version"
    assert len(scans) == 1
