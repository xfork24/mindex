"""Add image queries without changing the established text/video space fingerprint."""

from __future__ import annotations

import base64
from pathlib import Path

from .images import image_png
from .llama_cpp import PREFIX, SUFFIX, LlamaEmbedder


class LlamaImageEmbedder(LlamaEmbedder):
    def image(self, path: Path) -> list[float]:
        png = image_png(path, self.config.video.max_pixels)
        return self._embed(
            [
                {"type": "text", "text": PREFIX},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
                },
                {"type": "text", "text": SUFFIX},
            ]
        )
