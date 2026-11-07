"""Browser verification only; never use these vectors for real retrieval."""

from __future__ import annotations

import argparse
from pathlib import Path

import mindex  # noqa: F401 — 安装根包自举，后续使用包内短导入
import uvicorn

from api import create_app
from conftest import FixtureAnalyzer, FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder
from processing.indexing import IndexBuilder
from processing.pipeline import Pipeline
from settings import api as settings_api
from shared.config import Config
from shared.media import command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    config = Config(data_dir=args.data_dir.resolve())
    config.embedding.dimensions = 8
    config.studio.recall.granularity = "shot"
    config.data_dir.mkdir(parents=True, exist_ok=True)
    video = config.data_dir / "工程测试专用_颜色样本.mp4"
    if not video.exists():
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
                "color=c=red:s=320x240:r=25:d=1",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x240:r=25:d=1",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=320x240:r=25:d=8",
                "-filter_complex",
                "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
                "-map",
                "[v]",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ]
        )
    embedder = FixtureEmbedder(config)
    # Settings identity probes must use the fixture too, never a configured model service.
    settings_api.make_embedder = lambda _: embedder
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    with pipeline.lock():
        pipeline.prepare(video)
        pipeline.transcribe()
        pipeline.analyze()
        # shot 供创作召回，moment 供检索页默认粒度。
        IndexBuilder(config, embedder).build(kinds=("shot", "moment"), activate=True)
    print("TEST ONLY: deterministic color vectors; no real model quality is evaluated.")
    uvicorn.run(
        create_app(
            config,
            embedder,
            storyboarder=FixtureStoryboarder(config),
            speaker=FixtureSpeaker(config),
        ),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
