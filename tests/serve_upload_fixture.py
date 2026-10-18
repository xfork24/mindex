"""素材工作台浏览器验收：仅临时数据和工程测试模型。"""

import argparse
import time
from pathlib import Path

import uvicorn
from conftest import FixtureAnalyzer, FixtureEmbedder
from test_persons import FakeClient, Tracker

from mindex import person_api, person_processing
from mindex import pipeline as pipeline_module
from mindex.api import create_app
from mindex.config import Config
from mindex.media import command
from mindex.person_processing import PersonProcessor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8018)
    parser.add_argument("--embedding-delay", type=float, default=0)
    parser.add_argument("--analysis-delay", type=float, default=0)
    parser.add_argument("--runtime-settings", action="store_true")
    parser.add_argument("--progress-fixture", action="store_true")
    args = parser.parse_args()
    config = Config(data_dir=args.data_dir.resolve(), runtime_overrides=args.runtime_settings)
    config.embedding.dimensions = 8
    config.data_dir.mkdir(parents=True, exist_ok=True)
    video = config.data_dir / "工程样本.mp4"
    args_video = ["ffmpeg", "-v", "error", "-y"]
    if args.progress_fixture:
        for color in ("red", "blue", "green"):
            args_video += ["-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=10:d=2"]
        args_video += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]"]
    else:
        args_video += ["-f", "lavfi", "-i", "color=c=green:s=160x120:r=10:d=2"]
    command([*args_video, str(video)])

    class SlowEmbedder(FixtureEmbedder):
        def video(self, path):
            time.sleep(args.embedding_delay)
            return super().video(path)

    embedder = SlowEmbedder(config)
    class SlowAnalyzer(FixtureAnalyzer):
        def analyze(self, path, dialogue):
            time.sleep(args.analysis_delay)
            return super().analyze(path, dialogue)

    pipeline_module.make_analyzer = lambda _: SlowAnalyzer(embedder)
    person_processing.PersonProcessor = lambda cfg: PersonProcessor(cfg, FakeClient(), Tracker)
    person_api.PersonClient = lambda cfg: FakeClient()
    print("TEST ONLY: deterministic fixture models, temporary source library.")
    uvicorn.run(create_app(config, embedder), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
