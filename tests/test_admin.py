from __future__ import annotations

import json

import pytest
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient

from mindex.admin.curation import CurationPatch, Library
from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder, VectorIndex
from mindex.processing.pipeline import Pipeline
from mindex.search.search import SearchEngine
from mindex.shared.media import cut
from mindex.shared.storage import Store
from mindex.shared.types import TextSearch


@pytest.fixture
def library(config, video_file):
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    return pipeline, source, client


def test_unindexed_admin_browse_media_relationship_and_paging(library):
    pipeline, source, client = library
    assert pipeline.store.active_index() is None
    for url in ["/admin", "/admin/", "/assets/admin/admin.js", "/assets/admin/admin.css"]:
        assert client.get(url).status_code == 200
    result = client.get("/api/admin/sources", params={"q": "测试"}).json()
    assert result["total"] == 1
    assert result["items"][0]["shot_count"] == 3
    assert result["items"][0]["moment_count"] == 5
    assert "path" not in result["items"][0]
    detail = client.get(f"/api/admin/sources/{source}").json()
    assert detail["prepared"] and detail["missing_artifacts"] == 0
    endpoint = f"/api/admin/sources/{source}/segments"
    first = client.get(endpoint, params={"page_size": 2}).json()
    second = client.get(endpoint, params={"page_size": 2, "page": 2}).json()
    assert first["total"] == 3 and len(first["items"]) == 2 and len(second["items"]) == 1
    assert first["items"][-1]["start"] < second["items"][0]["start"]
    shot = first["items"][0]
    moments = client.get(endpoint, params={"kind": "moment", "parent_id": shot["id"]}).json()
    assert moments["total"] == shot["child_count"] == 1
    moment = moments["items"][0]
    assert moment["parent_id"] == shot["id"]
    assert second["items"][0]["position"] == 3
    assert client.get(f"/api/admin/segments/{moment['parent_id']}").json()["id"] == shot["id"]
    response = client.get(moment["media_url"], headers={"Range": "bytes=0-127"})
    assert response.status_code == 206 and len(response.content) == 128
    assert client.head(moment["media_url"]).status_code == 200
    assert client.get(moment["thumbnail_url"]).status_code == 200
    response = client.get(moment["media_url"] + "?download=true")
    assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
    row = pipeline.store.one("SELECT thumbnail FROM segments WHERE id=?", (moment["id"],))
    pipeline.store.resolve_media(row["thumbnail"]).unlink()
    assert client.get(moment["thumbnail_url"]).status_code == 404
    assert client.get(f"/api/admin/sources/{source}").json()["missing_artifacts"] == 1


def test_edit_metadata_hidden_inheritance_restore_and_search_names(library):
    pipeline, source, client = library
    source_url = f"/api/admin/sources/{source}"
    endpoint = source_url + "/segments"
    result = client.patch(
        source_url, json={"display_name": " 中文预告片 ", "notes": "<script>备注</script>", "favorite": True}
    )
    assert result.status_code == 200
    assert result.json()["name"] == "中文预告片" and result.json()["original_name"] == "测试素材.mp4"
    assert (
        client.get("/api/admin/sources", params={"q": "预告片", "favorite_only": True}).json()["total"] == 1
    )
    assert client.get("/api/admin/sources", params={"q": "测试素材.mp4"}).json()["total"] == 1
    assert client.get("/api/status").json()["sources"][0]["name"] == "中文预告片"
    shot = client.get(endpoint).json()["items"][0]
    moment = client.get(endpoint, params={"kind": "moment", "parent_id": shot["id"]}).json()["items"][0]
    segment_url = f"/api/admin/segments/{moment['id']}"
    client.patch(segment_url, json={"display_name": "片段 A", "favorite": True, "hidden": True})
    client.patch(f"/api/admin/segments/{shot['id']}", json={"hidden": True})
    client.patch(source_url, json={"hidden": True})
    assert client.get("/api/admin/sources").json()["total"] == 0
    assert client.get("/api/admin/sources", params={"visibility": "hidden"}).json()["total"] == 1
    assert client.get(endpoint, params={"kind": "moment"}).json()["total"] == 0
    client.patch(source_url, json={"hidden": False})
    inherited = client.get(segment_url).json()
    assert inherited["hidden"] and inherited["effective_hidden"] and inherited["inherited_hidden"]
    client.patch(f"/api/admin/segments/{shot['id']}", json={"hidden": False})
    inherited = client.get(segment_url).json()
    assert inherited["hidden"] and inherited["effective_hidden"] and not inherited["inherited_hidden"]
    client.patch(segment_url, json={"hidden": False})
    assert client.get(endpoint, params={"kind": "moment", "favorite_only": True}).json()["total"] == 1
    # Persistence across a new database/HTTP application instance; partial updates retain other fields.
    restored = TestClient(create_app(pipeline.config)).get(segment_url).json()
    assert restored["name"] == "片段 A" and restored["favorite"] and not restored["effective_hidden"]
    assert client.get(source_url).json()["notes"] == "<script>备注</script>"
    client.patch(source_url, json={"display_name": " "})
    assert client.get(source_url).json()["name"] == "测试素材.mp4"


def test_curation_validation_and_404(library, config, video_file):
    pipeline, source, client = library
    endpoint = f"/api/admin/sources/{source}"
    for path in [
        "/api/admin/sources/unknown",
        "/api/admin/segments/unknown",
        "/api/admin/segments/unknown/media",
    ]:
        assert client.get(path).status_code == 404
    assert client.patch("/api/admin/sources/unknown", json={"hidden": True}).status_code == 404
    for body in [
        {},
        {"path": "../file"},
        {"display_name": "x" * 201},
        {"notes": None},
        {"notes": "x" * 4001},
    ]:
        assert client.patch(endpoint, json=body).status_code == 422
    for query in [{"page": 0}, {"page_size": 101}, {"visibility": "bad"}]:
        assert client.get("/api/admin/sources", params=query).status_code == 422
    shot = pipeline.store.segments(source, "shot")[0]
    assert client.get(endpoint + "/segments", params={"parent_id": shot["id"]}).status_code == 422
    assert client.get(endpoint + "/segments", params={"kind": "scene"}).status_code == 422
    # Parents from another film cannot leak into the current film's relationship filter.
    short = config.data_dir / "other.mp4"
    cut(video_file, 0, 0.5, short)
    other = pipeline.prepare(short)
    other_shot = pipeline.store.segments(other, "shot")[0]
    assert (
        client.get(
            endpoint + "/segments", params={"kind": "moment", "parent_id": other_shot["id"]}
        ).status_code
        == 422
    )


def test_live_curation_filters_text_video_without_mutating_index(library, config, video_file):
    pipeline, source, client = library
    embedder = FixtureEmbedder(config)
    builder = IndexBuilder(config, embedder)
    version = builder.build(mode="visual", activate=True)
    original_snapshots = pipeline.store.snapshots(version)
    original_spec = pipeline.store.active_index()["spec"]
    original_ids = VectorIndex(config.data_dir, version).ids()
    shots = pipeline.store.segments(source, "shot")
    red = next(s for s in original_snapshots if s["start"] == 0)
    manager = Library(pipeline.store)
    manager.patch("source", source, CurationPatch(display_name="新素材名"))
    manager.patch(
        "segment", red["id"], CurationPatch(display_name="红色收藏", favorite=True, notes="不嵌入的备注")
    )
    result = SearchEngine(config, embedder).text(TextSearch(text="红色"))
    assert result["results"][0]["source_name"] == "新素材名"
    assert result["results"][0]["display_name"] == "红色收藏"
    manager.patch("segment", shots[0]["id"], CurationPatch(hidden=True))
    result = SearchEngine(config, embedder).text(TextSearch(text="红色"))
    assert all(r["id"] != red["id"] for r in result["results"])
    short = config.data_dir / "query.mp4"
    cut(video_file, 0, 0.5, short)
    result = SearchEngine(config, embedder).video(short)
    assert all(r["id"] != red["id"] for r in result["results"])
    manager.patch("source", source, CurationPatch(hidden=True))
    assert SearchEngine(config, embedder).text(TextSearch(text="红色"))["results"] == []
    manager.patch("source", source, CurationPatch(hidden=False))
    assert all(
        r["id"] != red["id"] for r in SearchEngine(config, embedder).text(TextSearch(text="红色"))["results"]
    )
    manager.patch("segment", shots[0]["id"], CurationPatch(hidden=False))
    assert SearchEngine(config, embedder).text(TextSearch(text="红色"))["results"][0]["id"] == red["id"]
    assert client.get(f"/api/segments/{red['id']}").json()["display_name"] == "红色收藏"
    assert pipeline.store.snapshots(version) == original_snapshots
    assert pipeline.store.active_index()["spec"] == original_spec
    assert VectorIndex(config.data_dir, version).ids() == original_ids
    calls = embedder.calls
    assert builder.build(mode="visual") == version and embedder.calls == calls


def test_existing_database_migration_preserves_sources(tmp_path):
    import sqlite3

    root = tmp_path / "old"
    root.mkdir()
    with sqlite3.connect(root / "metadata.sqlite3") as db:
        db.execute(
            "CREATE TABLE sources(id TEXT PRIMARY KEY, name TEXT NOT NULL,path TEXT NOT NULL,media TEXT NOT NULL,layout_key TEXT NOT NULL,prepared INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,?)",
            ("old-source", "旧素材", "sources/old.mp4", json.dumps({"duration": 1}), "old-layout", 0),
        )
    store = Store(root)
    source = Library(store).source("old-source")
    assert source["name"] == "旧素材" and not source["hidden"] and not source["favorite"]
    assert source["notes"] == "" and store.active_index() is None


def test_delete_source_purges_all_but_source_file(library):
    """删除素材：向量/库行/派生文件全清，唯独保留源视频；studio 任务引用时拒绝。"""
    import shutil

    pipeline, source, client = library
    store = pipeline.store
    segments = store.rows("SELECT * FROM segments WHERE source_id=?", (source,))
    assert segments

    index_id = "ix-del-test"
    store.execute(
        "INSERT INTO index_versions(id,spec,status) VALUES(?,?,?)", (index_id, "{}", "active")
    )
    vector_index = VectorIndex(store.root, index_id, create=True)
    rep_ids = []
    for segment in segments:
        rep_id = f"rep-{segment['id']}"
        vector_index.upsert(rep_id, [0.1, 0.2, 0.3], segment, "video", "视频")
        store.execute(
            "INSERT INTO representations(id,index_id,segment_id,view,document) VALUES(?,?,?,?,?)",
            (rep_id, index_id, segment["id"], "video", "视频"),
        )
        store.execute(
            "INSERT INTO index_members(index_id,segment_id,snapshot,complete) VALUES(?,?,?,1)",
            (index_id, segment["id"], "{}"),
        )
        rep_ids.append(rep_id)

    store.put_json("transcripts", "source_id", source, "k", {"utterances": []})
    store.put_json("scene_plans", "source_id", source, "k", {"shots": []})
    store.put_json("annotations", "segment_id", segments[0]["id"], "k", {"a": 1})
    store.execute("INSERT INTO source_curation(id,notes) VALUES(?,'x')", (source,))
    store.execute("INSERT INTO segment_curation(id,notes) VALUES(?,'x')", (segments[0]["id"],))
    (store.root / "transcripts" / source / "k").mkdir(parents=True, exist_ok=True)
    (store.root / "transcripts" / source / "k" / "raw").mkdir(parents=True, exist_ok=True)
    (store.root / "transcripts" / source / "k" / "raw" / "0.json").write_text("{}")
    (store.root / "plans" / f"{source}.json").write_text("{}")
    (store.root / "reports" / source).mkdir(parents=True, exist_ok=True)
    (store.root / "reports" / source / "film.md").write_text("report")
    context_file = store.root / "context" / f"{segments[0]['id']}.mp4"
    context_file.parent.mkdir(parents=True, exist_ok=True)
    context_file.write_bytes(b"context")
    source_file = store.resolve_media(store.one("SELECT path FROM sources WHERE id=?", (source,))["path"])
    assert source_file.is_file()

    # studio 任务仍引用该素材 → 拒绝删除
    job_dir = store.root / "studio" / "20261015-000000-del"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(json.dumps({"timeline": [{"video": {"source_id": source}}]}))
    refused = client.delete(f"/api/admin/sources/{source}")
    assert refused.status_code == 409
    assert "20261015-000000-del" in refused.json()["detail"]
    shutil.rmtree(job_dir)

    result = client.delete(f"/api/admin/sources/{source}")
    assert result.status_code == 200, result.text
    summary = result.json()
    assert summary["deleted"]["segments"] == len(segments)
    assert summary["deleted"]["vectors"] == len(rep_ids)
    assert summary["kept"]["source_file"] == source_file.name

    for table, field in [
        ("sources", "id"),
        ("segments", "source_id"),
        ("representations", "segment_id"),
        ("index_members", "segment_id"),
        ("annotations", "segment_id"),
        ("segment_curation", "id"),
        ("source_curation", "id"),
        ("transcripts", "source_id"),
        ("scene_plans", "source_id"),
        ("runs", "source_id"),
    ]:
        assert store.one(f"SELECT 1 FROM {table} WHERE {field}=?", (source,)) is None, table
    assert store.one("SELECT 1 FROM segments WHERE id=?", (segments[0]["id"],)) is None
    assert vector_index.ids() == set()

    assert source_file.is_file()
    assert not (store.root / "clips" / source).exists()
    assert not (store.root / "transcripts" / source).exists()
    assert not (store.root / "plans" / f"{source}.json").exists()
    assert not (store.root / "reports" / source).exists()
    assert not context_file.exists()
    for segment in segments:
        assert not (store.root / segment["thumbnail"]).exists()

    assert client.get("/api/admin/sources").json()["total"] == 0
    assert client.delete(f"/api/admin/sources/{source}").status_code == 404
