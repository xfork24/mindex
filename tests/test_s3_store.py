"""S3 MediaStore 验收（plans §8 阶段 4B）：真实 S3 兼容服务（MinIO）。

经 ``MINDEX_TEST_S3_ENDPOINT`` 门控（另需 ``MINDEX_TEST_S3_KEY/SECRET``，缺省
mindex/mindex-test）。覆盖：put 幂等与键冲突、multipart 大文件、stat 的 etag≠sha256、
materialize 的 staging pin 与摘要校验、presign 短期读 URL、iter_prefix、
版本命名删除、本地/远端往返。
"""

from __future__ import annotations

import hashlib
import io
import os

import httpx
import pytest

from shared.backends.s3 import S3MediaStore
from shared.config import S3Config

pytestmark = pytest.mark.requires_s3

ENDPOINT = os.environ.get("MINDEX_TEST_S3_ENDPOINT", "")
ACCESS = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
SECRET = os.environ.get("MINDEX_TEST_S3_SECRET", "mindex-test")


@pytest.fixture(scope="module")
def bucket() -> str:
    if not ENDPOINT:
        pytest.skip("需要 MINDEX_TEST_S3_ENDPOINT（S3 兼容服务，如 MinIO）")
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        region_name="us-east-1",
    )
    name = "mindex-test-store"
    try:
        client.create_bucket(Bucket=name)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    # 每轮清空测试前缀（compose/CI 自动创建隔离 bucket 并清理，plans §12）
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=name, Prefix="libraries/"):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=name, Delete={"Objects": objects})
    return name


@pytest.fixture
def store(bucket, tmp_path, monkeypatch):
    monkeypatch.setenv("MINDEX_S3_ACCESS_KEY", ACCESS)
    monkeypatch.setenv("MINDEX_S3_SECRET_KEY", SECRET)
    section = S3Config(endpoint_url=ENDPOINT, bucket=bucket, prefix="libraries/lib-test/", region="us-east-1")
    return S3MediaStore(section, cache_dir=tmp_path / "cache")


def test_roundtrip_put_stat_read_iter_delete(store):
    payload = b"video-bytes" * 1000
    sha = hashlib.sha256(payload).hexdigest()
    obj = store.put_immutable("sources/a/origin.mp4", __import__("io").BytesIO(payload), sha256=sha)
    assert obj.sha256 == sha
    stat = store.stat("sources/a/origin.mp4", with_sha256=True)
    assert stat.size == len(payload) and stat.sha256 == sha
    assert stat.etag != sha  # ETag 不是内容 SHA256（plans §8 的严格区分）
    with store.open_read("sources/a/origin.mp4") as body:
        assert hashlib.sha256(body.read()).hexdigest() == sha
    assert list(store.iter_prefix("sources/")) == ["sources/a/origin.mp4"]
    # 幂等：同 key 同内容重复上传 no-op；不同内容键冲突
    assert store.put_immutable(
        "sources/a/origin.mp4", __import__("io").BytesIO(payload), sha256=sha
    ).size == len(payload)
    with pytest.raises(ValueError, match="不可变键"):
        store.put_immutable(
            "sources/a/origin.mp4",
            __import__("io").BytesIO(b"other"),
            sha256=hashlib.sha256(b"other").hexdigest(),
        )


def test_multipart_large_object(store):

    payload = os.urandom(20 * 1024 * 1024)  # 20 MiB：跨过 multipart 阈值
    sha = hashlib.sha256(payload).hexdigest()
    store.put_immutable("clips/big.mp4", io.BytesIO(payload), sha256=sha)
    assert store.stat("clips/big.mp4").size == len(payload)
    assert store.stat("clips/big.mp4").etag.count("-") >= 1  # multipart ETag 形如 N-M
    with store.materialize("clips/big.mp4") as path:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


def test_materialize_uses_local_cache_and_versions(store):

    payload = b"versioned-content"
    sha = hashlib.sha256(payload).hexdigest()
    store.put_immutable("studio/job-1/render.mp4", io.BytesIO(payload), sha256=sha)
    with store.materialize("studio/job-1/render.mp4") as path:
        first = path
        assert path.is_file() and path.read_bytes() == payload
    with store.materialize("studio/job-1/render.mp4") as path:
        assert path == first  # 内容寻址缓存：二次物化命中本地缓存
    # 版本命名空间：同 key 的 v2 写入 version 路径，主键不受影响
    v2 = b"version-two-content"
    store.put_immutable(
        "studio/job-1/render.mp4", io.BytesIO(v2), sha256=hashlib.sha256(v2).hexdigest(), version="v2"
    )
    assert store.stat("studio/job-1/render.mp4").sha256 == sha  # 主键内容未变
    assert store.stat("studio/job-1/render.mp4", version="v2").sha256 == hashlib.sha256(v2).hexdigest()
    # 无版本主键已存在且内容不同 → 键冲突（不可变键语义）
    with pytest.raises(ValueError):
        store.put_immutable("studio/job-1/render.mp4", io.BytesIO(v2), sha256=hashlib.sha256(v2).hexdigest())
    store.delete_version("studio/job-1/render.mp4", version="v2")
    with pytest.raises(FileNotFoundError):
        store.stat("studio/job-1/render.mp4", version="v2")


def test_presigned_url_is_fetchable(store):

    payload = b"presign-me"
    sha = hashlib.sha256(payload).hexdigest()
    store.put_immutable("exports/x.bin", io.BytesIO(payload), sha256=sha)
    url = store.presign_get("exports/x.bin", expires=120)
    assert url and "X-Amz-Signature" in url
    response = httpx.get(url, timeout=10)
    assert response.status_code == 200 and response.content == payload


def test_missing_key_raises_cleanly(store):
    with pytest.raises(FileNotFoundError):
        store.stat("does/not/exist.bin")
    with pytest.raises(FileNotFoundError):
        with store.materialize("does/not/exist.bin"):
            pass
