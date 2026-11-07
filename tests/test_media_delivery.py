"""媒体 HTTP 出口（plans §8 阶段 5）：S3 预签名重定向、Range 代理回退、下载保留 pin。

经 ``MINDEX_TEST_POSTGRES_DSN`` + ``MINDEX_TEST_S3_ENDPOINT`` 双门控（真实 PG + MinIO）。
"""

from __future__ import annotations

import hashlib
import os

import httpx
import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_s3, pytest.mark.slow]

PG = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
S3 = os.environ.get("MINDEX_TEST_S3_ENDPOINT", "")
ACCESS = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
SECRET = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")


@pytest.fixture
def media_app(tmp_path, monkeypatch):
    if not (PG and S3):
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN 与 MINDEX_TEST_S3_ENDPOINT")
    import boto3
    import psycopg

    from api import create_app
    from shared.config import Config
    from shared.db.migrate_pg import migrate_pg

    client = boto3.client(
        "s3", endpoint_url=S3, aws_access_key_id=ACCESS, aws_secret_access_key=SECRET, region_name="us-east-1"
    )
    bucket = "mindex-test-media"
    try:
        client.create_bucket(Bucket=bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})

    with psycopg.connect(PG, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(PG) as conn:
        migrate_pg(conn)
    monkeypatch.setenv("MINDEX_DATABASE_DSN", PG)
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", ACCESS)
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", SECRET)
    config = Config(
        data_dir=tmp_path / "data",
        database={"backend": "postgres"},
        storage={"backend": "s3", "s3": {"endpoint_url": S3, "bucket": bucket, "prefix": "libs/test/"}},
    )
    config.embedding.dimensions = 8
    app = create_app(config)
    payload = b"\x00\x01video-payload" * 5000
    sha = hashlib.sha256(payload).hexdigest()
    app.state.backends.media.put_immutable(
        "clips/s1/shot/a.mp4", __import__("io").BytesIO(payload), sha256=sha
    )
    app.state.backends.media.put_immutable(
        "thumbnails/a.jpg",
        __import__("io").BytesIO(b"\xff\xd8thumb"),
        sha256=hashlib.sha256(b"\xff\xd8thumb").hexdigest(),
    )
    # 最小已发布索引：sources/segments/index_versions/index_members
    store = app.state.store
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key) VALUES('s1','素材','sources/s1/a.mp4','{}','k')"
    )
    store.execute(
        "INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
        "VALUES('seg1','s1','shot',0,5,'clips/s1/shot/a.mp4','thumbnails/a.jpg','g','r')"
    )
    store.execute(
        "INSERT INTO index_versions(id,spec,status) VALUES('i1','{\"format\":2,\"mode\":\"auto\"}','ready')"
    )
    store.execute(
        "INSERT INTO index_members(index_id,segment_id,snapshot,complete) VALUES('i1','seg1',?,1)",
        (
            '{"id":"seg1","source_id":"s1","kind":"shot","start":0,"end":5,'
            '"path":"clips/s1/shot/a.mp4","thumbnail":"thumbnails/a.jpg",'
            '"source_name":"素材","source_duration":5,"generation":"g","reason":"r",'
            '"views":{},"indexed_views":["video"]}',
        ),
    )
    store.execute("INSERT INTO settings(key,value) VALUES('active','i1')")
    return TestClient(app), payload


def test_get_media_redirects_to_working_presigned_url(media_app):

    client, payload = media_app
    response = client.get("/api/media/seg1", follow_redirects=False)
    assert response.status_code == 307
    url = response.headers["location"]
    assert "X-Amz-Signature" in url
    fetched = httpx.get(url, timeout=10)
    assert fetched.status_code == 200 and fetched.content == payload


def test_range_request_is_proxied_with_partial_content(media_app):
    client, payload = media_app
    response = client.get("/api/media/seg1", headers={"Range": "bytes=10-99"})
    assert response.status_code == 206  # 代理回退支持断点/seek
    assert response.content == payload[10:100]
    assert response.headers["content-range"].startswith("bytes 10-99/")


def test_head_media_has_length_without_redirect(media_app):
    client, payload = media_app
    response = client.head("/api/media/seg1", follow_redirects=False)
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(payload)


def test_thumbnail_and_download_pin(media_app):
    client, _ = media_app
    thumb = client.get("/api/thumbnails/seg1", follow_redirects=False)
    assert thumb.status_code == 307
    media = client.get("/api/media/seg1", follow_redirects=False)
    assert media.status_code == 307
    segment = client.get("/api/segments/seg1")
    assert segment.status_code == 200
    from shared.media_delivery import DownloadPins

    pins = DownloadPins(client.app.state.store, client.app.state.backends.library_id)
    assert pins.pinned("clips/s1/shot/a.mp4")  # 签发即登记下载保留（防止签发后立刻被清理）


def test_pins_expire_and_prune(media_app):

    from shared.media_delivery import DownloadPins

    client, _ = media_app
    pins = DownloadPins(client.app.state.store, client.app.state.backends.library_id)
    pins.pin("k", expires=-400)  # 已过宽限期
    assert not pins.pinned("k")
    assert pins.prune() >= 1
