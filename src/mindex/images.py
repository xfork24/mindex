"""Decode still-image queries and send a bounded, orientation-correct RGB PNG."""

from __future__ import annotations

import io
import math
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SOURCE_PIXELS = 40_000_000


def image_png(path: Path, max_pixels: int) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as original:
                if original.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("图片查询支持 JPEG、PNG、WebP 静态图片")
                if getattr(original, "n_frames", 1) != 1:
                    raise ValueError("请上传静态图片；动态图片请转换为视频查询")
                if original.width * original.height > MAX_SOURCE_PIXELS:
                    raise ValueError("原始图片不能超过 4000 万像素")
                if max(original.size) / min(original.size) > 200:
                    raise ValueError("图片宽高比不能超过 200，请裁剪后重试")
                original.load()
                image = ImageOps.exif_transpose(original)
                if image.mode in {"1", "P"}:
                    image = image.convert("RGBA")
                ratio = min(1, math.sqrt(max_pixels / (image.width * image.height)))
                if ratio < 1:
                    image = image.resize(
                        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                        Image.Resampling.LANCZOS,
                    )
                image = image.convert("RGBA")
                background = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(background, image).convert("RGB")
                output = io.BytesIO()
                image.save(output, format="PNG")
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("图片无法解码、文件损坏或尺寸过大") from exc
