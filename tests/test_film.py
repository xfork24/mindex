from __future__ import annotations

import csv
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from api import create_app
from cli import app
from conftest import FixtureEmbedder
from diagnostics.film import export_source, source_report
from processing.indexing import IndexBuilder, VectorIndex, check_index
from processing.pipeline import Pipeline
from search.search import SearchEngine
from shared import media
from shared.media import command, probe
from shared.types import TextSearch

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


class NoModels:
    def __getattr__(self, name):
        raise AssertionError(f"Unexpected model invocation: {name}")


def test_movie_plan_split_resume_and_manifest(config, video_file, monkeypatch):
    mkv = video_file.with_name("中文电影 <测试>.mkv")
    command(["ffmpeg", "-v", "error", "-y", "-i", str(video_file), "-c", "copy", str(mkv)])
    pipeline = Pipeline(config, analyzer=NoModels(), transcriber=NoModels())
    source = pipeline.detect(mkv)
    report = source_report(config, source)
    assert not report["prepared"]
    assert report["planned_shots"] == 3 and report["planned_moments"] == 5
    assert report["materialized"] == {}

    # Detection is durable even if clip encoding fails halfway through.
    monkeypatch.setattr(pipeline.detector, "detect", lambda path: pytest.fail("redetected film"))
    real_segment = pipeline._segment
    counter = 0

    def interrupted(*args):
        nonlocal counter
        counter += 1
        if counter == 4:
            raise RuntimeError("interrupted encoder")
        return real_segment(*args)

    monkeypatch.setattr(pipeline, "_segment", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        pipeline.split(source)
    assert not source_report(config, source)["prepared"]
    monkeypatch.setattr(pipeline, "_segment", real_segment)
    pipeline.prepare(mkv)
    assert source_report(config, source)["materialized"] == {"shot": 3, "moment": 5}
    assert not source_report(config, source)["missing_artifacts"]
    shot = pipeline.store.segments(source, "shot")[0]
    pipeline.store.resolve_media(shot["path"]).unlink()
    assert source_report(config, source)["missing_artifacts"]
    pipeline.split(source)
    assert not source_report(config, source)["missing_artifacts"]

    exported = export_source(config, source)
    from pathlib import Path

    root = Path(exported["output"])
    page = (root / "review.html").read_text()
    assert "&lt;测试&gt;" in page and "<测试>" not in page
    assert 'preload="none"' in page
    manifest = json.loads((root / "manifest.json").read_text())
    assert len(manifest["segments"]) == 8 and len(manifest["shots"]) == 3
    with (root / "segments.csv").open(encoding="utf-8-sig") as handle:
        assert len(list(csv.DictReader(handle))) == 8
    for segment in manifest["segments"]:
        assert probe(pipeline.store.resolve_media(segment["path"]))["duration"] > 0


def test_visual_index_does_not_require_asr_or_analysis(config, video_file):
    pipeline = Pipeline(config, analyzer=NoModels(), transcriber=NoModels())
    source = pipeline.prepare(video_file)
    embedder = FixtureEmbedder(config)
    builder = IndexBuilder(config, embedder)
    version = builder.build(mode="visual", source_id=source, activate=True)
    assert not check_index(pipeline.store, version)
    # visual 默认索引 moment 与 shot：5 个片段 + 2 个短镜头 + 长镜头 3 个窗口 = 10 条表示，
    # 但相同时间区间的向量只请求一次，嵌入调用仍是 5 次。
    assert len(VectorIndex(config.data_dir, version).ids()) == 10
    assert embedder.calls == 5
    assert builder.build(mode="visual", source_id=source) == version
    assert embedder.calls == 5
    assert pipeline.store.rows("SELECT * FROM transcripts") == []
    assert pipeline.store.rows("SELECT * FROM annotations") == []
    snapshots = pipeline.store.snapshots(version)
    assert all(s["views"] == {} and s["annotation_status"] == "not_analyzed" for s in snapshots)
    result = SearchEngine(config, embedder).text(TextSearch(text="红色画面"))
    assert result["index_mode"] == "visual"
    assert result["results"][0]["start"] == 0
    assert {m["route"] for m in result["results"][0]["matches"]} == {"video"}
    response = TestClient(create_app(config, embedder)).post("/api/search/text", json={"text": "蓝色"})
    assert response.status_code == 200 and response.json()["results"][0]["start"] == pytest.approx(1)
    # 新版索引按已发布快照增量合并：活动索引已覆盖全部镜头，只重建 shot 不产生新版本，也不丢弃 moment。
    shots_version = builder.build(mode="visual", source_id=source, kinds=("shot",))
    assert shots_version == version
    assert not check_index(pipeline.store, shots_version)
    assert pipeline.store.active_index()["id"] == version
    assert {s["kind"] for s in pipeline.store.snapshots(version)} == {"moment", "shot"}
    with pytest.raises(ValueError, match="不生成语义场景"):
        builder.build(mode="visual", kinds=("scene",))


def test_movie_cli_stages(tmp_path, video_file):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("data_dir: ./movie-data\n")
    runner = CliRunner()
    prefix = ["--config", str(config_path)]
    result = runner.invoke(app, [*prefix, "detect", str(video_file)])
    assert result.exit_code == 0, result.output
    source = result.output.strip().splitlines()[-1]
    result = runner.invoke(app, [*prefix, "inspect", source])
    assert result.exit_code == 0
    assert json.loads(result.output)["planned_shots"] == 3
    result = runner.invoke(app, [*prefix, "split", source])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, [*prefix, "export", source])
    assert result.exit_code == 0 and json.loads(result.output)["segments"] == 8
    result = runner.invoke(app, [*prefix, "index", "--help"])
    assert result.exit_code == 0 and "visual" in result.output


def test_long_shot_thumbnail_does_not_scan_all_frames(monkeypatch, tmp_path):
    monkeypatch.setattr(media, "probe", lambda p: {"duration": 7200})
    monkeypatch.setattr(media, "frame_timestamps", lambda p: pytest.fail("full movie scan"))
    calls = []
    monkeypatch.setattr(media, "atomic_media", lambda args, output: calls.append(args))
    media.thumbnail(tmp_path / "movie.mkv", tmp_path / "thumb.jpg")
    assert float(calls[0][1]) == pytest.approx(3599.95)
