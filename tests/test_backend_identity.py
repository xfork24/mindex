"""共享资源必须按库 UUID 隔离；bundle 切换后仍使用源库身份。"""
from __future__ import annotations

import hashlib
import io
import os
from uuid import uuid4

import pytest

from mindex.shared.backends import open_context
from mindex.shared.config import Config


@pytest.mark.requires_chroma_server
def test_same_index_in_two_libraries_has_independent_vectors():
    from mindex.shared.backends.vectors import ChromaServerVectorStores, chroma_http_client
    from mindex.shared.config import ChromaServerConfig

    url = os.environ.get("MINDEX_TEST_CHROMA_SERVER")
    if not url:
        pytest.skip("需要隔离 Chroma 服务")
    section = ChromaServerConfig(url=url)
    identities = [str(uuid4()), str(uuid4())]
    stores = [ChromaServerVectorStores(section, library_id=identity) for identity in identities]
    try:
        a, b = [store.open("same-index", create=True) for store in stores]
        a.upsert_many([("same-id", [1., 0.], {"id": "a", "source_id": "s", "kind": "shot"}, "video", "a")])
        b.upsert_many([("same-id", [0., 1.], {"id": "b", "source_id": "s", "kind": "shot"}, "video", "b")])
        assert a.vectors(["same-id"])["same-id"] == [1., 0.]
        assert b.vectors(["same-id"])["same-id"] == [0., 1.]
        a.delete(["same-id"])
        assert b.count() == 1
    finally:
        client = chroma_http_client(section)
        for identity in identities:
            client.delete_collection("lib-" + identity.replace("-", "") + "-idx-same-index")


@pytest.mark.requires_s3
def test_same_media_key_and_cache_directory_are_isolated_by_library(tmp_path, monkeypatch):
    endpoint = os.environ.get("MINDEX_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip("需要隔离 MinIO 服务")
    import boto3

    from mindex.shared.backends.s3 import S3MediaStore
    from mindex.shared.config import S3Config

    access = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
    secret = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", access)
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", secret)
    client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access,
                          aws_secret_access_key=secret, region_name="us-east-1")
    bucket = "mindex-identity-" + uuid4().hex[:12]
    client.create_bucket(Bucket=bucket)
    section = S3Config(endpoint_url=endpoint, bucket=bucket, prefix="shared")
    a, b = [S3MediaStore(section, library_id=str(uuid4()), cache_dir=tmp_path / "same-cache") for _ in range(2)]
    try:
        for media, payload in ((a, b"first"), (b, b"second")):
            media.put_immutable("same-key", io.BytesIO(payload), sha256=hashlib.sha256(payload).hexdigest())
        with a.materialize("same-key") as first, b.materialize("same-key") as second:
            assert first != second
            assert first.read_bytes() == b"first" and second.read_bytes() == b"second"
        a.delete_version("same-key")
        assert b.stat("same-key").size == 6
    finally:
        for media in (a, b):
            media.delete_version("same-key")
        client.delete_bucket(Bucket=bucket)


@pytest.mark.requires_postgres
@pytest.mark.requires_s3
@pytest.mark.requires_chroma_server
@pytest.mark.parametrize("legacy_namespace", [False, True])
def test_bundle_roundtrip_rebinds_resources_before_copying(tmp_path, monkeypatch, legacy_namespace):
    dsn, endpoint, chroma = [os.environ.get(key) for key in (
        "MINDEX_TEST_POSTGRES_DSN", "MINDEX_TEST_S3_ENDPOINT", "MINDEX_TEST_CHROMA_SERVER")]
    if not all((dsn, endpoint, chroma)):
        pytest.skip("需要隔离 PG、MinIO 和 Chroma 服务")
    import boto3
    import psycopg

    from mindex.maintenance.migration import activate_bundle, export_bundle, load_bundle, verify_bundle
    from mindex.processing.indexing import VectorIndex
    from mindex.shared.db.migrate_pg import migrate_pg
    from mindex.shared.storage import dumps

    access = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
    secret = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", access)
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", secret)
    client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access,
                          aws_secret_access_key=secret, region_name="us-east-1")
    bucket = "mindex-rebind-" + uuid4().hex[:12]
    client.create_bucket(Bucket=bucket)
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    with psycopg.connect(dsn) as db:
        migrate_pg(db)
    local = Config(data_dir=tmp_path / "local")
    origin = open_context(local)
    payload = b"required-media"
    path = origin.store.root / "sources/input.bin"
    path.parent.mkdir()
    path.write_bytes(payload)
    origin.store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('s','素材',?,'{}','k')",
                         ("sources/input.bin",))
    origin.store.execute("INSERT INTO index_versions(id,spec,status) VALUES('version',?,'ready')", (dumps({"format": 2}),))
    origin.store.execute("INSERT INTO representations(id,index_id,segment_id,view,document) VALUES('r','version','seg','video','v')")
    VectorIndex(origin.store.root, "version", create=True).upsert(
        "r", [1., 0.], {"id": "seg", "source_id": "s", "kind": "shot"}, "video", "v")
    bundle = tmp_path / "bundle"
    export_bundle(local, bundle, with_media=True, with_vectors=True)
    cluster = Config(data_dir=tmp_path / "cluster", database={"backend": "postgres"},
                     vector={"backend": "chroma_server", "chroma_server": {"url": chroma}},
                     storage={"backend": "s3", "s3": {"endpoint_url": endpoint, "bucket": bucket, "min_free_gb": 0}})
    temporary_identity = open_context(cluster).library_id
    assert temporary_identity != origin.library_id
    try:
        assert load_bundle(cluster, bundle, with_vectors=True)["consistent"]
        active = open_context(cluster)
        assert active.library_id == origin.library_id
        assert active.vectors.open("version").vectors(["r"])["r"] == [1., 0.]
        with active.media.open_read("sources/input.bin") as source:
            assert source.read() == payload
        assert activate_bundle(cluster, bundle)["activated"]
        from mindex.shared import files

        files.save_bytes(active.store, active.store.root / "studio/job/job.json", b'{"title":"shared"}')
        if legacy_namespace:
            from mindex.shared.backends.s3 import S3MediaStore
            from mindex.shared.backends.vectors import chroma_http_client

            old_media = S3MediaStore(cluster.storage.s3, cache_dir=tmp_path / "legacy")
            for key in active.media.iter_prefix(""):
                with active.media.open_read(key) as body:
                    old_media.put_immutable(key, body, sha256=active.media.stat(key).sha256)
            old_vectors = VectorIndex(active.store.root, "version", create=True,
                                      client=chroma_http_client(cluster.vector.chroma_server))
            old_vectors.upsert("r", [1., 0.], {"id": "seg", "source_id": "s", "kind": "shot"}, "video", "v")
        back = tmp_path / "back-bundle"
        export_bundle(cluster, back, with_media=True, with_vectors=True, legacy_namespace=legacy_namespace)
        restored = Config(data_dir=tmp_path / "restored")
        assert load_bundle(restored, back, with_vectors=True)["consistent"]
        assert verify_bundle(restored, back)["consistent"]
        assert (restored.data_dir / "sources/input.bin").read_bytes() == payload
        assert (restored.data_dir / "studio/job/job.json").read_bytes() == b'{"title":"shared"}'
        assert open_context(restored).library_id == origin.library_id
    finally:
        from mindex.shared.backends.vectors import chroma_http_client

        chroma_http_client(cluster.vector.chroma_server).delete_collection(
            "lib-" + origin.library_id.replace("-", "") + "-idx-version")
        if legacy_namespace:
            chroma_http_client(cluster.vector.chroma_server).delete_collection("idx-version")
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            if page.get("Contents"):
                client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": item["Key"]} for item in page["Contents"]]})
        client.delete_bucket(Bucket=bucket)
