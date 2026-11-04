"""S3 形态的素材删除（plans §8 阶段 5）：对象按前缀删除、下载保留期内跳过。

经 ``MINDEX_TEST_POSTGRES_DSN`` + ``MINDEX_TEST_S3_ENDPOINT`` 双门控。
"""

from __future__ import annotations

import hashlib
import io
import os

import pytest

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_s3]

PG = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
S3 = os.environ.get("MINDEX_TEST_S3_ENDPOINT", "")
ACCESS = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
SECRET = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")


@pytest.fixture
def store_and_media(tmp_path, monkeypatch):
    if not (PG and S3):
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN 与 MINDEX_TEST_S3_ENDPOINT")
    import boto3
    import psycopg

    from mindex.shared.backends.s3 import S3MediaStore
    from mindex.shared.config import Config, S3Config
    from mindex.shared.db.migrate_pg import migrate_pg
    from mindex.shared.storage import PgStore

    client = boto3.client("s3", endpoint_url=S3, aws_access_key_id=ACCESS,
                          aws_secret_access_key=SECRET, region_name="us-east-1")
    bucket = "mindex-test-purge"
    try:
        client.create_bucket(Bucket=bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
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
    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"},
                    storage={"backend": "s3", "s3": {"endpoint_url": S3, "bucket": bucket,
                                                     "prefix": "libs/"}})
    store = PgStore(config)
    media = S3MediaStore(S3Config(endpoint_url=S3, bucket=bucket, prefix="libs/"),
                         cache_dir=tmp_path / "cache")
    return store, media


def _put(media, key: str, payload: bytes):
    media.put_immutable(key, io.BytesIO(payload), sha256=hashlib.sha256(payload).hexdigest())


def test_purge_deletes_source_objects_but_keeps_pinned(store_and_media):
    from mindex.maintenance.purge import purge_source
    from mindex.shared.media_delivery import DownloadPins

    store, media = store_and_media
    _put(media, "clips/s1/shot/a.mp4", b"clip")
    _put(media, "clips/s1/shot/b.mp4", b"clip2")
    _put(media, "transcripts/s1/asr/0.json", b"{}")
    _put(media, "thumbnails/a.jpg", b"thumb")
    _put(media, "clips/s2/shot/c.mp4", b"other")  # 其他素材不受影响
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('s1','素材','sources/s1/a.mp4','{}','k')")
    store.execute("INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
                  "VALUES('seg1','s1','shot',0,5,'clips/s1/shot/a.mp4','thumbnails/a.jpg','g','r')")
    pins = DownloadPins(store, store.library_id)
    pins.pin("clips/s1/shot/b.mp4", 900)  # 预签名仍有效：跳过删除

    result = purge_source(store, "s1", media=media, pins=pins)
    assert result["deleted"]["files"] >= 2
    remaining = set(media.iter_prefix(""))
    assert "clips/s1/shot/a.mp4" not in remaining
    assert "clips/s1/shot/b.mp4" in remaining  # 下载保留期内不删
    assert "transcripts/s1/asr/0.json" not in remaining
    assert "thumbnails/a.jpg" not in remaining
    assert "clips/s2/shot/c.mp4" in remaining
    assert store.one("SELECT id FROM sources WHERE id='s1'") is None


def test_purge_local_backend_ignores_media_argument(store_and_media, tmp_path):
    """local 形态行为不变（media 参数缺省/本地时仍走文件系统）。"""
    store, _ = store_and_media
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('s9','素材','p','{}','k')")
    clip = store.root / "clips" / "s9" / "shot" / "x.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"x")
    from mindex.maintenance.purge import purge_source

    result = purge_source(store, "s9")
    assert result["deleted"]["files"] == 1 and not clip.exists()
