from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from mindex.config import Config
from mindex.media import command
from mindex.types import Annotation


@pytest.fixture(scope="session")
def video_file(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    path = root / "测试素材.mp4"
    command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=160x120:r=25:d=8",
            "-filter_complex",
            "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


@pytest.fixture
def config(tmp_path):
    config = Config(data_dir=tmp_path / "data")
    config.embedding.dimensions = 8
    return config


class FixtureEmbedder:
    """Tests only: color-based vectors, never exposed as a usable retrieval model."""

    def __init__(self, config: Config):
        self.config = config
        self.calls = 0

    def identity(self):
        return {
            "model": self.config.embedding.model,
            "revision": "fixture-revision",
            "quantization": self.config.embedding.quantization,
            "dimensions": 8,
            "normalized": True,
            "fps": self.config.video.fps,
            "max_frames": self.config.video.max_frames,
            "max_pixels": self.config.video.max_pixels,
            "implementation": "test-only",
        }

    def text(self, texts, role="document"):
        self.calls += len(texts)
        results = []
        for text in texts:
            vector = np.zeros(8, dtype=np.float32)
            vector[0 if "红" in text else 1 if "蓝" in text else 2] = 1
            results.append(vector.tolist())
        return results

    def video(self, path: Path):
        self.calls += 1
        raw = command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ]
        )
        color = np.frombuffer(raw, np.uint8)
        vector = np.zeros(8)
        # RGB channel order -> textual red, blue, green fixture indices.
        vector[{0: 0, 1: 2, 2: 1}[int(np.argmax(color))]] = 1
        return vector.tolist()


class FixtureAnalyzer:
    def __init__(self, embedder):
        self.embedder = embedder
        self.calls = 0

    def analyze(self, path, dialogue):
        self.calls += 1
        color = ["红", "蓝", "绿"][int(np.argmax(self.embedder.video(path)[:3]))]
        return Annotation(
            description=f"{color}色的测试画面", scene=["室内"], emotions=["平静"], shot_size=["近景"]
        )

    def group_scenes(self, units):
        return [list(range(len(units)))]
