"""镜头媒体物化、时间区间与缓存租约回归。"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from shared.media import MediaError, command, probe
from shared.shot_media import ShotMedia
from shared.storage import Store


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path)
    store.execute(
        "INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)",
        ("s", "source", "missing-original.mp4", json.dumps({"duration": 2}), "v"),
    )
    for i, color in enumerate(("red", "blue")):
        path = tmp_path / f"{i}.mp4"
        command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color={color}:s=64x64:r=25:d=1",
                "-c:v",
                "libx264",
                str(path),
            ]
        )
        store.execute(
            "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(i), "s", "shot", i, i + 1, path.name, "", "v", None, "test"),
        )
    return store


def test_cross_shot_without_original(library, tmp_path):
    service = ShotMedia(library)
    refs = service.references("s", 0.5, 1.5)
    assert [r.start for r in refs] == [0.5, 0]
    assert refs[1].logical(0.2) == pytest.approx(0.7)
    assert refs[1].absolute(0.2) == pytest.approx(1.2)
    result = service.materialize("s", 0.5, 1.5)
    assert probe(result)["duration"] == pytest.approx(1, abs=0.05)
    service.audio("s", tmp_path / "audio.wav", 0.5, 1)
    service.frame("s", 1, tmp_path / "frame.png")
    service.frame("s", 2, tmp_path / "last.png")
    service.register("old/moment.mp4", "s", 0.5, 1.5)
    assert library.resolve_media("old/moment.mp4") == result
    assert service.materialize("s", 0, 1) == library.root / "0.mp4"


def test_missing_and_gapped_shots_fail(library):
    library.execute("UPDATE segments SET start=1.1 WHERE id='1'")
    with pytest.raises(MediaError, match="gap"):
        ShotMedia(library).references("s")
    library.execute("UPDATE segments SET start=1 WHERE id='1'")
    (library.root / "1.mp4").unlink()
    with pytest.raises(FileNotFoundError):
        ShotMedia(library).materialize("s")


def test_migration_preview_backup_and_recovery(library):
    from maintenance.clips import materialize_all, migrate
    from processing.subtitle_archive import archive_path
    from shared.config import Config

    # Explicitly archive a source without reading a nonexistent original.
    manifest = archive_path(library, "s")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": 1, "tracks": [], "utterances": {}, "files": {}}))
    library.execute(
        "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("moment", "s", "moment", 0.5, 1.5, "clips/old.mp4", "", "v", None, "test"),
    )
    config = Config(data_dir=library.root)
    before = library.path.read_bytes()
    report = migrate(config)
    assert not report["apply"]
    assert before == library.path.read_bytes()
    assert not (library.root / "backups").exists()
    report = migrate(config, apply=True, verify=True)
    assert report["sources"][0]["status"] == "verified"
    assert report["protected_before"] == report["protected_after"]
    assert __import__("pathlib").Path(report["backup"]).is_file()
    assert materialize_all(config)["restored"] == ["clips/old.mp4"]
    assert materialize_all(config)["restored"] == []


def test_audio_mix_frames_and_cache_lock(library, tmp_path):

    # Add an actual tone to only the second shot.
    target = library.root / "tone.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(library.root / "1.mp4"),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(target),
        ]
    )
    library.execute("UPDATE segments SET path='tone.mp4' WHERE id='1'")
    service = ShotMedia(library)
    result = service.materialize("s", 0.5, 1.5)
    assert probe(result)["audio"]
    frames = service.frames("s", 0.5, 1.5, 2)
    assert frames[0][0] >= 0.5 and frames[-1][0] < 1.5
    assert all(image.startswith("data:image/jpeg;base64,") for _, image in frames)
    with FileLock(str(result) + ".lock"):
        assert service.prune_cache(max_age=0, max_bytes=0) == []
        assert result.exists()
    assert service.prune_cache(max_age=0, max_bytes=0) == [result.name]


def test_business_modules_do_not_resolve_source_path():

    modules = [
        "api/__init__",
        "api/uploads",
        "admin/admin",
        "persons/person_api",
        "persons/person_processing",
        "processing/dialogue",
        "admin/segment_details",
        "studio/window",
        "studio/steps",
        "studio/api",
    ]
    root = Path(__file__).resolve().parents[1] / "backend" / "src"
    modules += [f"studio/workspace/{path.stem}" for path in sorted((root / "studio/workspace").glob("*.py"))]
    for name in modules:
        source = (root / f"{name}.py").read_text()
        assert 'resolve_media(source["path"])' not in source, name
        assert 'resolve_media(segment["source_path"])' not in source, name


def test_text_subtitle_archive_survives_source_removal(config, video_file, tmp_path):
    from processing.pipeline import Pipeline
    from processing.subtitle_archive import load_archive

    subtitle = tmp_path / "captions.srt"
    subtitle.write_text("1\n00:00:00,200 --> 00:00:00,800\nhello\n")
    source = tmp_path / "subtitled.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(subtitle),
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-c:v",
            "copy",
            "-c:s",
            "mov_text",
            str(source),
        ]
    )
    pipeline = Pipeline(config)
    sid = pipeline.prepare(source)
    row = pipeline.store.one("SELECT * FROM sources WHERE id=?", (sid,))
    pipeline.store.resolve_media(row["path"]).unlink()
    archive = load_archive(pipeline.store, sid)
    track = archive["tracks"][0]
    assert archive["utterances"][str(track["index"])][0]["text"] == "hello"
    assert archive["files"]
    pipeline.split(sid)
    assert not any((pipeline.store.root / s["path"]).exists() for s in pipeline.store.segments(sid, "moment"))


def test_http_playlist_range_and_head_without_original(library):

    from api import create_app
    from shared.config import Config

    library.execute(
        "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("moment", "s", "moment", 0.5, 1.5, "clips/missing.mp4", "", "v", None, "test"),
    )
    client = TestClient(create_app(Config(data_dir=library.root)))
    playlist = client.get("/api/admin/segments/moment/playlist")
    assert playlist.status_code == 200
    items = playlist.json()["items"]
    assert len(items) == 2 and items[1]["offset"] == 0.5
    assert client.head(items[0]["url"]).status_code == 200
    response = client.get("/api/admin/segments/moment/media", headers={"Range": "bytes=0-31"})
    assert response.status_code == 206 and len(response.content) == 32
    assert client.head("/api/admin/segments/moment/media").status_code == 200
    assert client.get("/api/admin/sources/s/media").status_code == 200


def test_migration_verification_retains_all_existing_media(library):
    from maintenance.clips import migrate
    from processing.subtitle_archive import archive_path
    from shared.config import Config

    manifest = archive_path(library, "s")
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"version": 1, "tracks": [], "utterances": {}, "files": {}}))
    legacy = library.root / "legacy.mp4"
    legacy.write_bytes(b"old entity bytes must remain unchanged until user acceptance")
    library.execute(
        "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("moment", "s", "moment", 0, 1, legacy.name, "", "v", None, "test"),
    )
    before = legacy.read_bytes()
    report = migrate(Config(data_dir=library.root), apply=True, verify=True)
    assert report["physical_deletion_enabled"] is False
    assert report["released_bytes"] == 0
    assert report["sources"][0]["files"][0]["status"] == "ready-for-deletion"
    assert legacy.read_bytes() == before


def test_concurrent_generation_is_atomic(library):

    service = ShotMedia(library)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: service.materialize("s", 0.2, 1.8), range(4)))
    assert len(set(paths)) == 1
    assert probe(paths[0])["duration"] == pytest.approx(1.6, abs=0.04)
    assert not list(paths[0].parent.glob("*.partial.mp4"))


def test_playlist_revision_changes_when_shot_is_replaced(library):
    from shared.shot_media import file_response

    service = ShotMedia(library)
    old = service.playlist("s")["items"][0]["url"]
    assert "?v=" in old
    # A repaired shot retains its ID and public route, but must bypass cached old bytes.
    library.execute("UPDATE segments SET path='1.mp4' WHERE id='0'")
    new = service.playlist("s")["items"][0]["url"]
    assert old.split("?")[0] == new.split("?")[0]
    assert old != new
    response = file_response(library.root / "1.mp4")
    assert response.headers["cache-control"] == "no-cache"
    assert "etag" not in response.headers  # FileResponse supplies validators during streaming.


def test_prune_verified_entities_preserves_shots_and_recovers(library):
    from maintenance.clips import materialize_all, migrate, protected_digest, prune
    from processing.subtitle_archive import archive_path
    from shared.config import Config

    config = Config(data_dir=library.root)
    archive = archive_path(library, "s")
    archive.parent.mkdir(parents=True)
    archive.write_text(json.dumps({"version": 1, "tracks": [], "utterances": {}, "files": {}}))
    library.execute(
        "INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("moment", "s", "moment", 0.5, 1.5, "clips/old.mp4", "", "v", None, "test"),
    )
    migration = migrate(config, apply=True, verify=True)
    materialize_all(config)
    # Unlinking a redundant hardlink must retain the shot and report zero released blocks.
    legacy = library.root / "clips/old.mp4"
    legacy.unlink()
    legacy.hardlink_to(library.root / "1.mp4")
    before = protected_digest(library)
    preview = prune(config, migration["report"])
    assert preview["estimated_released_allocated_bytes"] == 0
    assert preview["files"][0]["status"] == "ready"
    assert (library.root / "clips/old.mp4").exists()
    # A changed dependency must block deletion before any file is removed.
    library.execute("UPDATE clip_migration_files SET dependency_digest='stale'")
    with pytest.raises(ValueError, match="Stale"):
        prune(config, migration["report"], apply=True)
    assert (library.root / "clips/old.mp4").exists()
    library.execute(
        "UPDATE clip_migration_files SET dependency_digest=?", (preview["files"][0]["dependency_digest"],)
    )
    pinned = library.root / "pin.json"
    pinned.write_text(json.dumps({"path": "clips/old.mp4", "sha256": "fixed"}))
    assert prune(config, migration["report"], apply=True)["deleted_files"] == 0
    pinned.unlink()
    result = prune(config, migration["report"], apply=True)
    assert result["deleted_files"] == 1
    assert result["released_allocated_bytes"] == 0
    assert not (library.root / "clips/old.mp4").exists()
    assert all((library.root / f"{i}.mp4").exists() for i in (0, 1))
    assert protected_digest(library) == before
    assert prune(config, migration["report"], apply=True)["deleted_files"] == 0
    assert materialize_all(config)["restored"] == ["clips/old.mp4"]


def test_inference_interval_cleanup_on_failure(config, video_file):
    import pytest

    from processing.pipeline import Pipeline
    from shared.shot_media import ShotMedia

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    media = ShotMedia(pipeline.store, cache_probes=False)
    with pytest.raises(RuntimeError, match="inference failed"), media.inference_video(sid, 0.5, 2.5) as path:
        assert path.is_file()
        raise RuntimeError("inference failed")
    assert not path.exists()


def test_inference_with_audio_shots(config, video_file, tmp_path):
    from processing.pipeline import Pipeline
    from shared.media import command, probe
    from shared.shot_media import ShotMedia

    video = tmp_path / "audio.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(video_file),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=10",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(video),
        ]
    )
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video)
    with ShotMedia(pipeline.store, cache_probes=False).inference_video(sid, 0.5, 2.5) as path:
        info = probe(path)
        assert abs(info["duration"] - 2) < 0.1
        assert not info["audio"]
    assert not path.exists()


def test_request_local_dependency_checks_reuse_queries_and_probes(library, monkeypatch):
    from shared import shot_media

    probes, queries = [], []
    original_probe, original_rows = shot_media.probe, library.rows

    def probe(path):
        probes.append(path)
        return original_probe(path)

    def rows(sql, args=()):
        queries.append(sql)
        return original_rows(sql, args)

    monkeypatch.setattr(shot_media, "probe", probe)
    monkeypatch.setattr(library, "rows", rows)
    media = ShotMedia(library, cache_probes=False, reuse_checks=True)
    for _ in range(100):
        media.references("s", 0, 0.5)
    assert len(probes) == 1
    assert len(queries) == 2


def test_prewarm_probes_all_shots_without_reprobing(library, monkeypatch):
    from shared import shot_media

    probes = []
    original = shot_media.probe

    def probe(path):
        probes.append(str(path))
        return original(path)

    monkeypatch.setattr(shot_media, "probe", probe)
    media = ShotMedia(library, cache_probes=False, reuse_checks=True)
    # Prewarm the whole 0–2s span; both shot files must be probed exactly once.
    media.prewarm([("s", 0, 2)])
    assert sorted(probes) == sorted(str(library.root / f"{i}.mp4") for i in (0, 1))
    # Subsequent reference resolution for the same shots reuses the warmed cache.
    probes.clear()
    media.references("s", 0, 2)
    assert probes == []


def test_prewarm_skips_single_file_and_tolerates_missing_media(library):
    media = ShotMedia(library, cache_probes=False, reuse_checks=True)
    # A single-shot interval short-circuits (nothing to parallelise) and stays correct.
    media.prewarm([("s", 0, 0.5)])
    assert media.references("s", 0, 0.5)[0].shot_id == "0"
    # An unknown source is ignored rather than raising during best-effort warming.
    media.prewarm([("missing", 0, 1), ("s", 0, 2)])
    assert len(media.references("s", 0, 2)) == 2


def test_float_boundary_tail_is_not_a_concat_input(library):
    service = ShotMedia(library)
    refs = service.references("s", 0.5, 1 + 1.4210854715202004e-14)
    assert len(refs) == 1
    assert refs[0].shot_id == "0"
    # The same tolerance also applies at the beginning of an interval.
    refs = service.references("s", 1 - 1.4210854715202004e-14, 1.5)
    assert len(refs) == 1
    assert refs[0].shot_id == "1"
    assert len(service.references("s", 1, 1.0000005)) == 1


def test_ffmpeg_times_use_decimal_not_scientific_notation(library):
    service = ShotMedia(library)
    refs = service.references("s", 1 + 1.4210854715202004e-14, 1.0000005)
    args = service._inputs(refs, video_only=True)
    assert args[args.index("-ss") + 1] == "0.000000000"
    assert args[args.index("-t") + 1] == "0.000000500"
    assert "trim=duration=0.000000500" in args[args.index("-filter_complex") + 1]


def test_cached_stream_releases_lease_when_send_fails(tmp_path):

    from shared.shot_media import file_response

    path = tmp_path / "cache/shot-media/test.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture")
    response = file_response(path)

    async def send(message):
        raise OSError("模拟客户端断开")

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(OSError, match="客户端断开"):
        asyncio.run(response({"type": "http", "method": "GET", "headers": []}, receive, send))
    with FileLock(str(path) + ".lock", timeout=0):
        pass
