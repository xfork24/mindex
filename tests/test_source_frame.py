from __future__ import annotations

import io
import json

import pytest
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient
from PIL import Image

from mindex.api import create_app
from mindex.processing.pipeline import Pipeline


@pytest.fixture
def frame(config, video_file):
    pipeline = Pipeline(config)
    source_id = pipeline.prepare(video_file)
    row = pipeline.store.one("SELECT media FROM sources WHERE id=?", (source_id,))
    duration = float(json.loads(row["media"])["duration"])
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    return client, f"/api/admin/sources/{source_id}/frame", duration


def decode(response) -> Image.Image:
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content[:2] == b"\xff\xd8"
    return Image.open(io.BytesIO(response.content))


def test_frame_returns_jpeg_with_requested_width(frame):
    client, url, duration = frame
    for t in (0.0, duration / 2, duration):
        image = decode(client.get(url, params={"t": t, "width": 64}))
        assert image.format == "JPEG" and image.width == 64
    default = client.get(url, params={"t": 0.5})
    assert decode(default).width == 768
    assert "private" in default.headers["cache-control"]
    largest = client.get(url, params={"t": 0.5, "width": 1024})
    assert decode(largest).width == 1024
    # 红 → 蓝 → 绿：中点画面应以绿色为主，证明确实按时间点抽帧。
    red, green, blue = decode(client.get(url, params={"t": 5, "width": 32})).convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert green > red and green > blue


@pytest.mark.parametrize(
    "params",
    [
        {"t": -0.1},
        {"t": "nan"},
        {"t": "inf"},
        {"t": 1000},
        {"t": 0.5, "width": 15},
        {"t": 0.5, "width": 1025},
        {},
    ],
)
def test_frame_rejects_out_of_range(frame, params):
    client, url, _ = frame
    assert client.get(url, params=params).status_code == 422


def test_frame_rejects_just_past_duration(frame):
    client, url, duration = frame
    assert client.get(url, params={"t": duration + 0.01}).status_code == 422


def test_frame_unknown_source(frame):
    client, _, _ = frame
    response = client.get("/api/admin/sources/missing/frame", params={"t": 0})
    assert response.status_code == 404
    assert response.json()["detail"] == "找不到素材"


def test_frame_etag_revalidation(frame):
    client, url, _ = frame
    params = {"t": 1.5, "width": 64}
    first = client.get(url, params=params)
    etag = first.headers["etag"]
    assert first.status_code == 200 and etag.startswith('"') and etag.endswith('"')
    assert client.get(url, params=params).headers["etag"] == etag

    for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        cached = client.get(url, params=params, headers={"If-None-Match": header})
        assert cached.status_code == 304 and not cached.content
        assert cached.headers["etag"] == etag

    stale = client.get(url, params=params, headers={"If-None-Match": '"other"'})
    assert stale.status_code == 200 and stale.content[:2] == b"\xff\xd8"

    # 宽度或时间点不同 → 新 ETag，旧 ETag 不命中。
    for changed in ({"t": 1.5, "width": 128}, {"t": 2.5, "width": 64}):
        response = client.get(url, params=changed, headers={"If-None-Match": etag})
        assert response.status_code == 200 and response.headers["etag"] != etag
