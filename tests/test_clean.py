"""mindex clean：预览不删、apply 删除、保留判定（索引版本/检查点引用/attempt 引用）、
锁冲突退出与宽限期边界。时间一律用 os.utime 回拨 mtime 伪造。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mindex.cli import app
from mindex.config import Config
from mindex.storage import Store

runner = CliRunner()
OLD = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def make_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data_dir: {tmp_path / 'data'}\n"
        "runtime_overrides: false\n",
        encoding="utf-8",
    )
    return path


def invoke(config_path: Path, *args):
    return runner.invoke(app, ["--config", str(config_path), "clean", *args])


def age(path: Path, days: int) -> Path:
    os.utime(path, (OLD, OLD - days * 86400))
    return path


def write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def add_segment(store: Store, source_id: str, segment_id: str, name: str) -> dict:
    segment = {
        "id": segment_id,
        "source_id": source_id,
        "kind": "moment",
        "start": 0.0,
        "end": 1.0,
        "path": f"clips/{source_id}/moment/{name}.mp4",
        "thumbnail": f"thumbnails/{name}.jpg",
        "generation": "g",
        "parent_id": None,
        "reason": "test",
    }
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO sources(id,name,path,media,layout_key,prepared) "
            "VALUES(?,?,?,?,'layout',1)",
            (source_id, source_id, f"sources/{source_id}/{name}.mp4", "{}"),
        )
        db.execute(
            "INSERT OR REPLACE INTO segments"
            "(id,source_id,kind,start,end,path,thumbnail,generation,parent_id,reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (segment_id, source_id, "moment", 0.0, 1.0, segment["path"],
             segment["thumbnail"], "g", None, "test"),
        )
    return segment


def categories(report: dict) -> dict:
    return {item["category"]: item for item in report["categories"]}


def report_of(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ---------------------------------------------------------------- 基础行为


def test_preview_deletes_nothing(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    ds = write(root / "sub" / ".DS_Store")
    crash = write(root / "asr-abc" / "part.json")
    store = Store(root)
    add_segment(store, "s1", "seg1", "seg1")
    context = write(root / "context" / "seg1.mp4")
    age(context, 40)

    report = report_of(invoke(config_path))

    assert report["applied"] is False
    names = categories(report)
    assert names["ds_store"]["item_count"] == 1
    assert names["temp_crash"]["item_count"] == 1
    assert names["context_cache"]["item_count"] == 1
    assert ds.exists() and crash.exists() and context.exists()


def test_apply_removes_crash_temp_and_ds_store(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    ds = write(root / "sub" / ".DS_Store")
    crash = write(root / "asr-abc" / "part.json")

    report = report_of(invoke(config_path, "--apply"))

    assert report["applied"] is True
    assert not ds.exists() and not crash.exists()
    assert categories(report)["temp_crash"]["result"] == {"deleted": 1}


def test_context_expiry_threshold(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    add_segment(store, "s1", "seg-old", "old")
    add_segment(store, "s1", "seg-new", "new")
    stale = age(write(root / "context" / "seg-old.mp4"), 40)
    fresh = write(root / "context" / "seg-new.mp4")

    invoke(config_path, "--apply")

    assert not stale.exists()
    assert fresh.exists()


def test_shot_media_cache_expiry(tmp_path):
    """cache/shot-media：过期合成媒体连同其锁文件与过期探测缓存一起删，未过期保留。"""
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    Store(root)
    stale = age(write(root / "cache" / "shot-media" / "abc.mp4"), 40)
    stale_lock = age(write(root / "cache" / "shot-media" / "abc.mp4.lock"), 40)
    stale_probe = age(write(root / "cache" / "shot-media" / "probes" / "h1.json"), 40)
    fresh = write(root / "cache" / "shot-media" / "def.mp4")

    report = report_of(invoke(config_path, "--apply"))
    names = categories(report)

    assert names["shot_media_cache"]["level"] == "B"
    assert names["shot_media_cache"]["item_count"] == 2
    assert not stale.exists() and not stale_lock.exists() and not stale_probe.exists()
    assert fresh.exists()


def test_shot_media_cache_keeps_locked_files(tmp_path):
    """缓存租约（播放/渲染持有 .lock）期间不删对应合成文件。"""
    from filelock import FileLock

    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    Store(root)
    serving = age(write(root / "cache" / "shot-media" / "live.mp4"), 40)
    lock_file = age(write(root / "cache" / "shot-media" / "live.mp4.lock"), 40)
    deletable = age(write(root / "cache" / "shot-media" / "idle.mp4"), 40)
    with FileLock(str(lock_file), thread_local=False):
        report = report_of(invoke(config_path, "--apply"))

    names = categories(report)["shot_media_cache"]
    assert serving.exists() and lock_file.exists()
    assert not deletable.exists()
    assert names["item_count"] == 1 and names["kept"] == {"locked": 1}



# ---------------------------------------------------------------- 孤儿判定


def test_orphans_respect_snapshot_references(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    add_segment(store, "s1", "seg-a", "a")
    add_segment(store, "s1", "seg-b", "b")
    clip_a = write(root / "clips" / "s1" / "moment" / "a.mp4")
    clip_b = write(root / "clips" / "s1" / "moment" / "b.mp4")
    clip_c = write(root / "clips" / "s1" / "moment" / "c.mp4")
    # 保留版本（active）的快照引用 a；随后 a 的 segment 行被删。
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO index_versions VALUES('v1','{}','ready','2026-01-01')")
        db.execute(
            "INSERT OR REPLACE INTO index_members VALUES('v1','seg-a',?,1)",
            (json.dumps({"path": "clips/s1/moment/a.mp4", "thumbnail": "thumbnails/a.jpg"}),),
        )
        db.execute("INSERT OR REPLACE INTO settings VALUES('active','v1')")
        db.execute("DELETE FROM segments WHERE id='seg-a'")

    report = report_of(invoke(config_path, "--apply"))

    assert clip_a.exists(), "保留索引快照引用的文件不可判孤儿"
    assert clip_b.exists(), "segment 行引用的文件不可判孤儿"
    assert not clip_c.exists()
    assert categories(report)["orphan_clips"]["result"] == {"deleted": 1}


def test_orphan_voices_and_person_png(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    with store.connect() as db:
        db.execute("INSERT INTO voices(name,kind,provider,voice_id,sample_path) "
                   "VALUES('v','clone','self','vid','voices/v1.wav')")
        db.execute("INSERT INTO person_references VALUES('r1','actor1',NULL,"
                   "'persons/references/d1.png','d1','{}',NULL)")
        db.execute("INSERT INTO person_imports VALUES('i1',?, 'confirmed')",
                   (json.dumps({"images": [{"digest": "d3"}]}),))
    kept_voice = write(root / "voices" / "v1.wav")
    orphan_voice = write(root / "voices" / "orphan.wav")
    kept_ref = write(root / "persons" / "references" / "d1.png")
    orphan_ref = write(root / "persons" / "references" / "d2.png")
    kept_import = write(root / "persons" / "imports" / "d3.png")

    invoke(config_path, "--apply")

    assert kept_voice.exists() and not orphan_voice.exists()
    assert kept_ref.exists() and kept_import.exists() and not orphan_ref.exists()


def test_unregistered_source_grace(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    fresh = write(root / "sources" / "fresh.mp4")  # 宽限期内
    stale = write(root / "sources" / "stale.mp4")
    age(stale, 3)

    report = report_of(invoke(config_path))

    names = categories(report)
    assert names["orphan_sources"]["item_count"] == 1
    assert names["orphan_sources"]["items"][0]["path"].endswith("stale.mp4")
    invoke(config_path, "--apply")
    assert fresh.exists() and not stale.exists()


# ---------------------------------------------------------------- 索引版本


def seed_index_version(store: Store, version_id: str, created_at: str, *,
                       status: str = "ready", members: int = 1) -> None:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(store.root / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    collection = client.get_or_create_collection(
        "idx-" + version_id, metadata={"hnsw:space": "cosine"}, embedding_function=None
    )
    collection.upsert(ids=[f"{version_id}-0"], embeddings=[[0.1] * 8],
                      documents=["d"], metadatas=[{"view": "video"}])
    snapshot = json.dumps({"path": f"clips/x/{version_id}.mp4",
                           "thumbnail": f"thumbnails/{version_id}.jpg"})
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO index_versions(id,spec,status,created_at) "
                   "VALUES(?,?,?,?)",
                   (version_id, json.dumps({"space": {}}), status, created_at))
        for i in range(members):
            db.execute("INSERT OR REPLACE INTO index_members(index_id,segment_id,snapshot,complete) "
                       "VALUES(?,?,?,1)",
                       (version_id, f"seg-{version_id}-{i}", snapshot))
            db.execute("INSERT OR REPLACE INTO representations"
                       "(id,index_id,segment_id,view,document,cache_key) VALUES(?,?,?,?,?,?)",
                       (f"rep-{version_id}-{i}", version_id, f"seg-{version_id}-{i}",
                        "video", "d", f"cache-{version_id}-{i}"))
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                   ("index_report." + version_id, "{}"))


def test_index_versions_retention(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    # active=最旧 v1；最近两个非活动 ready 是 v4/v3；v2 应删；v5 building 应保留。
    seed_index_version(store, "v1", "2026-01-01 00:00:00")
    seed_index_version(store, "v2", "2026-01-02 00:00:00")
    seed_index_version(store, "v3", "2026-01-03 00:00:00")
    seed_index_version(store, "v4", "2026-01-04 00:00:00")
    seed_index_version(store, "v5", "2026-01-05 00:00:00", status="building")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active','v1')")
        db.execute("INSERT OR REPLACE INTO search_releases(id,content_id,person_id) "
                   "VALUES('rel2','v2','pv2')")
        db.execute("INSERT OR REPLACE INTO person_versions(id,data) VALUES('pv2','{}')")

    report = report_of(invoke(config_path, "--apply"))

    names = categories(report)
    assert [item["path"] for item in names["index_versions"]["items"]] == ["v2"]
    assert set(names["index_versions"]["kept"]["versions"]) == {"v1", "v3", "v4", "v5"}

    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(root / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    from chromadb.errors import NotFoundError

    with pytest.raises(NotFoundError):
        client.get_collection("idx-v2", embedding_function=None)
    for kept in ("v1", "v3", "v4", "v5"):
        client.get_collection("idx-" + kept, embedding_function=None)
    assert store.one("SELECT * FROM index_versions WHERE id='v2'") is None
    assert store.one("SELECT * FROM index_members WHERE index_id='v2'") is None
    assert store.one("SELECT * FROM representations WHERE index_id='v2'") is None
    assert store.one("SELECT value FROM settings WHERE key='index_report.v2'") is None
    assert store.one("SELECT * FROM search_releases WHERE id='rel2'") is None
    assert store.one("SELECT * FROM person_versions WHERE id='pv2'") is None
    assert store.active_index()["id"] == "v1"


# ---------------------------------------------------------------- 检查点 / runs


def test_profile_checkpoints_only_stale_and_foreign(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    config = Config(data_dir=root)
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO channel_data VALUES('s1','dialogue','p-now',"
                   "'complete','{}',CURRENT_TIMESTAMP)")
    current_asr = age(write(root / "transcripts" / "s1" / config.asr_key / "0.json"), 40)
    stale_asr = age(write(root / "transcripts" / "s1" / "asr-old" / "0.json"), 40)
    fresh_foreign = write(root / "transcripts" / "s1" / "asr-old2" / "0.json")
    dialogue_now = age(write(root / "dialogue" / "s1" / "p-now" / "0.json"), 40)
    dialogue_old = age(write(root / "dialogue" / "s1" / "p-old" / "0.json"), 40)

    invoke(config_path, "--apply")

    assert current_asr.exists(), "当前 asr_key 检查点即使过期也保留"
    assert not stale_asr.exists()
    assert fresh_foreign.exists(), "未过期的异指纹目录保留"
    assert dialogue_now.exists(), "channel_data 现行对白 profile 保留"
    assert not dialogue_old.exists()


def test_runs_history_prunes_completed_only(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    cutoff = datetime.now(UTC) - timedelta(days=100)
    old = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    with store.connect() as db:
        db.execute("INSERT INTO runs(stage,status,finished_at) VALUES('detect','complete',?)", (old,))
        db.execute("INSERT INTO runs(stage,status) VALUES('detect','running')")
        db.execute("INSERT INTO runs(stage,status,finished_at) VALUES('detect','failed',"
                   "datetime('now'))")
    remaining_running = store.one("SELECT * FROM runs WHERE status='running'")["id"]

    invoke(config_path, "--apply")

    rows = store.rows("SELECT stage,status,finished_at FROM runs ORDER BY id")
    # 过期的 complete 已删；running 与未过期 failed 保留；apply 自身追加一条 clean 审计行。
    assert [row["status"] for row in rows if row["stage"] == "detect"] == ["running", "failed"]
    assert any(row["stage"] == "clean" and row["status"] == "complete" for row in rows)
    assert remaining_running in {row["id"] for row in store.rows("SELECT id FROM runs")}


# ---------------------------------------------------------------- studio


def studio_job(root: Path, name: str, *, execution="idle", attempt="a1") -> Path:
    directory = root / "studio" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "job.json").write_text(json.dumps({
        "schema": 1,
        "steps": {"recall": {"status": "complete", "attempt": attempt}},
        "execution": {"status": execution},
    }), encoding="utf-8")
    return directory


def test_studio_attempts_keep_referenced_and_latest(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    job = studio_job(root, "j1")
    (job / "checkpoints.json").write_text(json.dumps({
        "candidates/shot-01.json": {"path": "attempts/recall-a2/candidates/shot-01.json"},
    }), encoding="utf-8")
    current = write(job / "attempts" / "recall-a1" / "x.json")
    referenced = write(job / "attempts" / "recall-a2" / "candidates" / "shot-01.json")
    stale = age(write(job / "attempts" / "recall-a3" / "x.json"), 40)
    sidecar = age(write(job / "attempts" / "recall-a3.json"), 40)

    report = report_of(invoke(config_path, "--apply"))

    assert current.exists(), "步骤当前 attempt 保留"
    assert referenced.exists(), "检查点注册表引用的 attempt 保留"
    assert not stale.exists() and not sidecar.exists()
    assert categories(report)["studio_attempts"]["result"]["deleted"] == 2


def test_studio_running_job_skipped(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    job = studio_job(root, "j1", execution="running")
    stale = age(write(job / "attempts" / "recall-a9" / "x.json"), 40)

    report = report_of(invoke(config_path, "--apply"))

    assert stale.exists()
    assert categories(report)["studio_attempts"]["kept"]["running_jobs"] == ["j1"]


def test_studio_lock_skips_categories(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    from filelock import FileLock

    job = studio_job(root, "j1")
    stale_preview = age(write(job / "previews" / "quick-v1-k.mp4"), 40)
    stale_attempt = age(write(job / "attempts" / "recall-a9" / "x.json"), 40)
    stale_export = age(write(job / "exports" / "bundle.zip"), 40)
    (root / "studio").mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "studio" / "studio.lock"), timeout=0):
        result = invoke(config_path, "--apply")

    report = report_of(result)
    names = categories(report)
    for category in ("studio_previews", "studio_attempts", "studio_exports"):
        assert names[category]["skipped"], category
    assert stale_preview.exists() and stale_attempt.exists() and stale_export.exists()


# ---------------------------------------------------------------- 锁与宽限


def test_pipeline_lock_conflict_exit_code(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    write(root / ".DS_Store")
    from mindex.processing_lock import SharedFileLock

    with SharedFileLock(root / "pipeline.lock", exclusive=True):
        result = invoke(config_path, "--apply")

    assert result.exit_code == 1
    assert "正在运行" in result.output


def test_subtitle_cache_keeps_uploads(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    stale = age(write(root / "subtitles" / "s1" / "abc.json"), 40)
    upload = age(write(root / "subtitles" / "s1" / "uploads" / "sha" / "subtitle.srt"), 40)

    invoke(config_path, "--apply")

    assert not stale.exists()
    assert upload.exists(), "用户上传字幕原件永不清理"
