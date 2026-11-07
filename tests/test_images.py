"""图像解码、缩放与检索请求回归。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from diagnostics.e2e import freeze
from shared.images import image_png
from shared.media import content_hash


def test_image_preprocessing_applies_orientation_alpha_and_pixel_limit(tmp_path):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (100, 50), "blue").save(path, exif=exif)
    with Image.open(io.BytesIO(image_png(path, 10000))) as image:
        assert image.size == (50, 100)
        assert image.mode == "RGB"
    Image.new("RGBA", (1000, 500), (0, 0, 0, 0)).save(path, format="PNG")
    with Image.open(io.BytesIO(image_png(path, 10000))) as image:
        assert image.width * image.height <= 10000
        assert image.getpixel((0, 0)) == (255, 255, 255)


def test_image_preprocessing_rejects_animation_and_broken_media(tmp_path):
    path = tmp_path / "animated.png"
    Image.new("RGB", (20, 20), "red").save(
        path, save_all=True, append_images=[Image.new("RGB", (20, 20), "blue")], duration=100
    )
    with pytest.raises(ValueError, match="静态图片"):
        image_png(path, 10000)
    path.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="无法解码"):
        image_png(path, 10000)


def test_freeze_image_labels_pins_media_and_rejects_changed_image(tmp_path):
    picture = tmp_path / "blue.png"
    Image.new("RGB", (20, 20), "blue").save(picture)
    labels = tmp_path / "images.draft.jsonl"
    labels.write_text(
        json.dumps(
            {
                "id": "test-only-image",
                "split": "image",
                "image": picture.name,
                "sha256": content_hash(picture),
                "label_status": "confirmed",
                "relevant": [{"segment_id": "test-only", "grade": 3}],
            }
        )
    )
    frozen = Path(freeze(tmp_path, labels, "test-fixture"))
    assert json.loads(frozen.read_text())["image"] == str(picture.resolve())
    picture.write_bytes(b"changed")
    with pytest.raises(ValueError, match="媒体查询文件发生变化"):
        freeze(tmp_path, labels, "test-fixture")
