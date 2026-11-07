"""mindex clean：预览不删、apply 删除、保留判定（索引版本/检查点引用/attempt 引用）、
锁冲突退出与宽限期边界。时间一律用 os.utime 回拨 mtime 伪造。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from filelock import FileLock
from typer.testing import CliRunner

from cli import app
from maintenance.clean import clean_library
from shared.config import Config
from shared.storage import Store

runner = CliRunner()
OLD = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def make_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"data_dir: {tmp_path / 'data'}\nruntime_overrides: false\n",
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
            (
                segment_id,
                source_id,
                "moment",
                0.0,
                1.0,
                segment["path"],
                segment["thumbnail"],
                "g",
                None,
                "test",
            ),
        )
    return segment


def categories(report: dict) -> dict:
    return {item["category"]: item for item in report["categories"]}


def report_of(result) -> dict:
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("{"))
    return json.loads("\n".join(lines[start:]))


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
    """过期媒体和探测缓存删除；锁文件保留 inode，未过期项保留。"""
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
    assert not stale.exists() and stale_lock.exists() and not stale_probe.exists()
    assert fresh.exists()


def test_shot_media_cache_keeps_locked_files(tmp_path):
    """缓存租约（播放/渲染持有 .lock）期间不删对应合成文件。"""

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
        db.execute(
            "INSERT INTO voices(name,kind,provider,voice_id,sample_path) "
            "VALUES('v','clone','self','vid','voices/v1.wav')"
        )
        db.execute(
            "INSERT INTO person_references VALUES('r1','actor1',NULL,"
            "'persons/references/d1.png','d1','{}',NULL)"
        )
        db.execute(
            "INSERT INTO person_imports VALUES('i1',?, 'confirmed')",
            (json.dumps({"images": [{"digest": "d3"}]}),),
        )
    kept_voice = write(root / "voices" / "v1.wav")
    orphan_voice = age(write(root / "voices" / "orphan.wav"), 3)
    kept_ref = write(root / "persons" / "references" / "d1.png")
    orphan_ref = age(write(root / "persons" / "references" / "d2.png"), 3)
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


@pytest.mark.parametrize("kind", ["image", "voice"])
def test_orphan_upload_cleanup_rechecks_references_and_grace(tmp_path, kind):
    from maintenance.clean import Scan, orphan_person_png, orphan_voices

    config = Config(data_dir=tmp_path)
    store = Store(tmp_path)
    rule = orphan_person_png if kind == "image" else orphan_voices
    relative = "persons/references/digest.png" if kind == "image" else "voices/sample.wav"
    path = write(tmp_path / relative)
    now = datetime.now(UTC)
    assert rule(Scan(config, store, now)) is None, "正在上传或检查的文件需要宽限期"
    age(path, 3)
    action = rule(Scan(config, store, now))
    assert action and len(action.items) == 1
    if kind == "image":
        store.execute(
            "INSERT INTO person_references VALUES(?,?,?,?,?,?,?)",
            ("ref", "actor", None, relative, "digest", "{}", None),
        )
    else:
        store.execute(
            "INSERT INTO voices(name,kind,provider,voice_id,sample_path) VALUES(?,?,?,?,?)",
            ("voice", "clone", "self", "vid", relative),
        )
    assert action.execute() == {"deleted": 0}
    assert path.exists(), "预览后建立引用的文件不能删除"


def test_nested_partial_cleanup_preserves_sources_and_rechecks_references(tmp_path):
    from maintenance.clean import Scan, temp_crash

    config = Config(data_dir=tmp_path)
    store = Store(tmp_path)
    junk = write(tmp_path / "analysis/source/annotation.json.random.partial")
    referenced = write(tmp_path / "clips/source/upload.partial")
    add_segment(store, "s", "shot", "s")
    store.execute("UPDATE segments SET path=? WHERE id=?", ("clips/source/upload.partial", "shot"))
    late = write(tmp_path / "transcripts/source/input.partial")
    archived = write(tmp_path / "studio/job/versions/preserved.partial")
    action = temp_crash(Scan(config, store, datetime.now(UTC)))
    assert {item.path for item in action.items} == {
        junk.relative_to(tmp_path).as_posix(),
        late.relative_to(tmp_path).as_posix(),
    }
    store.execute("UPDATE sources SET path=? WHERE id=?", (late.relative_to(tmp_path).as_posix(), "s"))
    assert action.execute() == {"deleted": 1}
    assert not junk.exists()
    assert referenced.exists() and late.exists() and archived.exists()


def test_temp_cleanup_does_not_interrupt_online_queries(tmp_path):
    from maintenance.clean import Scan, temp_crash

    config = Config(data_dir=tmp_path)
    store = Store(tmp_path)
    fresh = write(tmp_path / "query-image-live/image.png")
    empty = tmp_path / "query-starting"
    empty.mkdir()
    stale = write(tmp_path / "query-window-crashed/window.mp4")
    resumed = write(tmp_path / "query-resumed/input.mp4")
    for path in (stale, resumed):
        age(path, 3)
        age(path.parent, 3)
    scan = Scan(config, store, datetime.now(UTC))
    action = temp_crash(scan)
    assert {item.path for item in action.items} == {"query-window-crashed", "query-resumed"}
    resumed.touch()
    assert action.execute() == {"deleted": 1}
    assert fresh.exists() and empty.exists() and resumed.exists()
    assert not stale.exists()


def test_index_cleanup_rechecks_studio_and_resumable_task_pins(tmp_path, monkeypatch):
    from maintenance.clean import Scan
    from maintenance.clean_index import index_versions_rule, retained_versions

    config = Config(data_dir=tmp_path)
    config.clean.index_keep_versions = 0
    store = Store(tmp_path)
    for identifier in ("studio", "release", "resumable", "junk"):
        store.execute(
            "INSERT INTO index_versions(id,spec,status,created_at) VALUES(?,?,?,?)",
            (identifier, "{}", "ready", "2026-01-01"),
        )
    action = index_versions_rule(Scan(config, store, datetime.now(UTC)))
    assert len(action.items) == 4
    write(
        tmp_path / "studio/job/workspace.json",
        json.dumps({"versions": [{"parameters": {"index_id": "studio", "person_release": "rel"}}]}).encode(),
    )
    store.execute("INSERT INTO search_releases(id,content_id) VALUES(?,?)", ("rel", "release"))
    store.execute(
        "INSERT INTO source_tasks(id,source_id,work,status,config,options,progress) VALUES(?,?,?,?,?,?,?)",
        ("task", "source", "visual", "interrupted", "{}", "{}", '{"index_id":"resumable"}'),
    )
    deleted = []

    def delete(store, identifier):
        deleted.append(identifier)
        return {"deleted": identifier}

    monkeypatch.setattr("maintenance.clean_index.delete_index_version", delete)
    assert action.execute() == {"deleted": ["junk"]}
    assert deleted == ["junk"]
    assert set(retained_versions(store, 0)["keep"]) == {"studio", "release", "resumable"}
    write(tmp_path / "studio/job/workspace.json", b"{interrupted write")
    assert not retained_versions(store, 0)["deletable"], "无法读取引用时保守保留"


def test_task_index_pins_expire_but_running_tasks_remain_protected(tmp_path):
    from maintenance.clean_index import retained_versions

    store = Store(tmp_path)
    for identifier, status in (("stale", "interrupted"), ("live", "running")):
        store.execute(
            "INSERT INTO index_versions(id,spec,status,created_at) VALUES(?,?,?,?)",
            (identifier, "{}", "building", "2025-01-01"),
        )
        store.execute(
            "INSERT INTO source_tasks(id,source_id,work,status,config,options,progress,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                identifier,
                "source",
                "publish",
                status,
                "{}",
                "{}",
                json.dumps({"index_id": identifier}),
                "2025-01-01",
            ),
        )
    result = retained_versions(store, 0, building_max_age_days=7)
    assert result["keep"] == ["live"]
    assert [v["id"] for v in result["deletable"]] == ["stale"]


# ---------------------------------------------------------------- 索引版本


def seed_index_version(
    store: Store, version_id: str, created_at: str, *, status: str = "ready", members: int = 1
) -> None:
    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(store.root / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    collection = client.get_or_create_collection(
        "idx-" + version_id, metadata={"hnsw:space": "cosine"}, embedding_function=None
    )
    collection.upsert(
        ids=[f"{version_id}-0"], embeddings=[[0.1] * 8], documents=["d"], metadatas=[{"view": "video"}]
    )
    snapshot = json.dumps({"path": f"clips/x/{version_id}.mp4", "thumbnail": f"thumbnails/{version_id}.jpg"})
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO index_versions(id,spec,status,created_at) VALUES(?,?,?,?)",
            (version_id, json.dumps({"space": {}}), status, created_at),
        )
        for i in range(members):
            db.execute(
                "INSERT OR REPLACE INTO index_members(index_id,segment_id,snapshot,complete) VALUES(?,?,?,1)",
                (version_id, f"seg-{version_id}-{i}", snapshot),
            )
            db.execute(
                "INSERT OR REPLACE INTO representations"
                "(id,index_id,segment_id,view,document,cache_key) VALUES(?,?,?,?,?,?)",
                (
                    f"rep-{version_id}-{i}",
                    version_id,
                    f"seg-{version_id}-{i}",
                    "video",
                    "d",
                    f"cache-{version_id}-{i}",
                ),
            )
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ("index_report." + version_id, "{}")
        )


def test_index_versions_retention(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    # active=最旧 v1；最近两个非活动 ready 是 v4/v3；v2 应删；v5 building（7 天内）应保留。
    seed_index_version(store, "v1", "2026-01-01 00:00:00")
    seed_index_version(store, "v2", "2026-01-02 00:00:00")
    seed_index_version(store, "v3", "2026-01-03 00:00:00")
    seed_index_version(store, "v4", "2026-01-04 00:00:00")
    seed_index_version(store, "v5", _recent(days=1), status="building")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active','v1')")
        db.execute(
            "INSERT OR REPLACE INTO search_releases(id,content_id,person_id) VALUES('rel2','v2','pv2')"
        )
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
        db.execute(
            "INSERT OR REPLACE INTO channel_data VALUES('s1','dialogue','p-now',"
            "'complete','{}',CURRENT_TIMESTAMP)"
        )
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
        db.execute("INSERT INTO runs(stage,status,finished_at) VALUES('detect','failed',datetime('now'))")
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
    (directory / "job.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "steps": {"recall": {"status": "complete", "attempt": attempt}},
                "execution": {"status": execution},
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_studio_attempts_keep_referenced_and_latest(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    job = studio_job(root, "j1")
    (job / "checkpoints.json").write_text(
        json.dumps(
            {
                "candidates/shot-01.json": {"path": "attempts/recall-a2/candidates/shot-01.json"},
            }
        ),
        encoding="utf-8",
    )
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
    from shared.locks import SharedFileLock

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


# ---------------------------------------------------------------- chroma 孤儿目录 / 超期 building /
# person_features / VACUUM


def _recent(days: int = 1) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _segment_dirs_of(root: Path, collection: str) -> list[str]:

    with sqlite3.connect(root / "chroma" / "chroma.sqlite3") as db:
        ids = [
            row[0]
            for row in db.execute(
                "SELECT s.id FROM segments s JOIN collections c ON s.collection=c.id WHERE c.name=?",
                (collection,),
            )
        ]
    # 每个集合有 VECTOR/METADATA 两个 segment，只有 VECTOR 落 HNSW 目录
    return [sid for sid in ids if (root / "chroma" / sid).is_dir()]


def test_chroma_orphan_segments_removed(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    seed_index_version(store, "v1", "2026-01-01 00:00:00")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active','v1')")
    # 模拟历史 delete_collection 只删行不删目录的残留：伪造一个不在 segments 表里的 UUID 目录
    orphan = root / "chroma" / "00000000-0000-4000-8000-000000000000"
    (orphan / "sub").mkdir(parents=True)
    (orphan / "sub" / "data_level0.bin").write_bytes(b"x" * 4096)
    live = _segment_dirs_of(root, "idx-v1")

    report = report_of(invoke(config_path))  # 预览不删
    names = categories(report)
    assert [item["path"] for item in names["chroma_orphan_segments"]["items"]] == [f"chroma/{orphan.name}"]
    assert names["chroma_orphan_segments"]["level"] == "A"
    assert names["chroma_orphan_segments"]["bytes"] == 4096
    assert orphan.exists()

    report_of(invoke(config_path, "--apply"))

    assert not orphan.exists(), "孤儿 HNSW 目录应被删除"
    for name in live:
        assert (root / "chroma" / name).is_dir(), "存活集合的 segment 目录不能动"


def test_stale_building_version_pruned_with_segment_dirs(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    seed_index_version(store, "v1", "2026-01-01 00:00:00")
    seed_index_version(store, "vstale", "2026-01-02 00:00:00", status="building")
    seed_index_version(store, "vnew", _recent(days=1), status="building")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active','v1')")
    stale_dirs = _segment_dirs_of(root, "idx-vstale")

    report = report_of(invoke(config_path, "--apply"))

    names = categories(report)
    paths = [item["path"] for item in names["index_versions"]["items"]]
    assert paths == ["vstale"], "超期 building 应删除，7 天内的应保留"
    assert set(names["index_versions"]["kept"]["versions"]) == {"v1", "vnew"}
    assert store.one("SELECT * FROM index_versions WHERE id='vstale'") is None
    for name in stale_dirs:
        assert not (root / "chroma" / name).exists(), "版本删除应连带回收 segment 目录"


def test_orphan_person_features_pruned(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO sources(id,name,path,media,layout_key,prepared) "
            "VALUES('s1','s1','sources/s1/a.mp4','{}','layout',1)"
        )
        db.executemany(
            "INSERT OR REPLACE INTO person_features(id,source_id,space,kind,data) VALUES(?,?,?,?,?)",
            [
                ("feat-alive", "s1", "{}", "shot", '"x"'),
                ("feat-orphan", "s-gone", "{}", "shot", '"x"'),
                ("feat-ref", None, "{}", "reference", '"x"'),
            ],
        )

    report = report_of(invoke(config_path))

    names = categories(report)
    assert [item["path"] for item in names["orphan_person_features"]["items"]] == [
        "person_features#feat-orphan"
    ]
    assert names["orphan_person_features"]["level"] == "A"

    report_of(invoke(config_path, "--apply"))

    ids = {row["id"] for row in store.rows("SELECT id FROM person_features")}
    assert ids == {"feat-alive", "feat-ref"}, "在源特征与 reference（source_id 为空）保留"


def test_vacuum_shrinks_metadata_database(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    with store.connect() as db:
        db.executemany(
            "INSERT OR REPLACE INTO person_features(id,source_id,space,kind,data) VALUES(?,?,?,?,?)",
            [(f"big-{i}", None, "{}", "reference", "x" * 200_000) for i in range(50)],
        )
        db.execute("DELETE FROM person_features")

    report = report_of(invoke(config_path, "--apply", "--vacuum"))

    entries = {entry["path"]: entry for entry in report["vacuum"]["databases"]}
    entry = entries["metadata.sqlite3"]
    assert entry["bytes_after"] < entry["bytes_before"], "VACUUM 应回收已删行的库文件空间"
    assert (root / "metadata.sqlite3").stat().st_size < entry["bytes_before"]


def test_vacuum_preview_reports_sizes_without_running(tmp_path):
    config_path = make_config(tmp_path)
    Store(tmp_path / "data")

    report = report_of(invoke(config_path, "--vacuum"))

    entries = {entry["path"]: entry for entry in report["vacuum"]["databases"]}
    assert "bytes_before" in entries["metadata.sqlite3"]
    assert "bytes_after" not in entries["metadata.sqlite3"]


def test_vacuum_busy_database_skips_without_failure(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    seed_index_version(store, "v1", "2026-01-01 00:00:00")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active','v1')")

    blocker = sqlite3.connect(root / "chroma" / "chroma.sqlite3", timeout=1)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        report = report_of(invoke(config_path, "--apply", "--vacuum"))
    finally:
        blocker.rollback()
        blocker.close()

    entries = {entry["path"]: entry for entry in report["vacuum"]["databases"]}
    assert "bytes_after" in entries["metadata.sqlite3"], "未被占用的库正常收缩"
    assert entries["chroma/chroma.sqlite3"].get("error"), "被占用的库应记 error 而非失败退出"


# ---------------------------------------------------------------- --days 阈值覆盖


def test_days_replaces_age_thresholds(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    fresh = write(root / "cache" / "shot-media" / "abc.mp4")  # mtime = 现在
    just_finished = (datetime.now(UTC) - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    with store.connect() as db:
        db.execute(
            "INSERT INTO runs(stage,status,finished_at) VALUES('detect','complete',?)", (just_finished,)
        )

    report = report_of(invoke(config_path, "--apply", "--days", "7"))

    assert fresh.exists(), "--days 7 保留 7 天内的新缓存"
    assert "shot_media_cache" not in categories(report)
    assert any(row["stage"] == "detect" for row in store.rows("SELECT stage FROM runs")), (
        "--days 7 不动刚完成的 runs 记录"
    )

    report = report_of(invoke(config_path, "--apply", "--days", "0"))

    assert not fresh.exists(), "--days 0 清空全部按龄过期项"
    assert categories(report)["shot_media_cache"]["result"] == {"deleted": 1}
    assert not [row for row in store.rows("SELECT stage FROM runs") if row["stage"] == "detect"], (
        "--days 0 连刚完成的 runs 记录也按龄清掉"
    )


def test_days_zero_keeps_hard_protections(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    store = Store(root)
    upload = write(root / "subtitles" / "s1" / "uploads" / "sha" / "subtitle.srt")
    with store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO channel_data VALUES('s1','dialogue','p-now',"
            "'complete','{}',CURRENT_TIMESTAMP)"
        )
    dialogue_now = age(write(root / "dialogue" / "s1" / "p-now" / "0.json"), 40)
    seed_index_version(store, "v1", "2026-01-01 00:00:00")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active','v1')")

    result = invoke(config_path, "--apply", "--days", "0")
    assert result.exit_code == 0, result.output

    assert upload.exists(), "uploads 用户原件永不清理"
    assert dialogue_now.exists(), "现行对白 profile 即使 --days 0 也保留"
    assert store.one("SELECT * FROM index_versions WHERE id='v1'") is not None, "活动索引不受影响"

    import chromadb
    from chromadb.config import Settings

    client = chromadb.PersistentClient(
        path=str(root / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    client.get_collection("idx-v1", embedding_function=None)


def test_days_preview_deletes_nothing(tmp_path):
    config_path = make_config(tmp_path)
    root = tmp_path / "data"
    fresh = write(root / "cache" / "shot-media" / "abc.mp4")

    report = report_of(invoke(config_path, "--days", "0"))

    assert fresh.exists()
    assert categories(report)["shot_media_cache"]["item_count"] == 1


@pytest.mark.parametrize(
    "rule_name,relative",
    [("context_cache", "context/scene.mp4"), ("shot_media_cache", "cache/shot-media/scene.mp4")],
)
def test_cache_cleanup_rechecks_lease_after_preview(tmp_path, rule_name, relative):

    from maintenance import clean

    root = tmp_path / "data"
    store = Store(root)
    path = age(write(root / relative), 40)
    scan = clean.Scan(Config(data_dir=root), store, datetime.now(UTC))
    action = getattr(clean, rule_name)(scan)
    assert action and action.items
    with FileLock(str(path) + ".lock"):
        result = action.execute()
    assert path.is_file() and result == {"deleted": 0, "skipped": 1}


@pytest.mark.parametrize(
    "rule_name,relative",
    [("context_cache", "context/scene.mp4"), ("shot_media_cache", "cache/shot-media/scene.mp4")],
)
def test_cache_cleanup_keeps_recently_accessed_old_media(tmp_path, rule_name, relative):
    from maintenance import clean

    root = tmp_path / "data"
    store = Store(root)
    path = age(write(root / relative), 40)
    scan = clean.Scan(Config(data_dir=root), store, datetime.now(UTC))
    action = getattr(clean, rule_name)(scan)
    assert action
    os.utime(path, (datetime.now(UTC).timestamp(), path.stat().st_mtime))
    assert action.execute() == {"deleted": 0, "skipped": 1}
    assert path.is_file()
    assert getattr(clean, rule_name)(scan) is None


# ---------------------------------------------------------------- 进度日志（progress 回调）


def test_progress_reports_scan_and_summary(tmp_path):
    """预览模式：入口/逐规则扫描/汇总消息齐全，且不出现执行段消息。"""
    config = Config(data_dir=tmp_path / "data")
    write(config.data_dir / "sub" / ".DS_Store")
    messages: list[str] = []

    report = clean_library(config, progress=messages.append)

    assert report["applied"] is False
    assert "开始清理" in messages[0] and str(config.data_dir) in messages[0]
    assert any("扫描 2/" in m and "ds_store" in m and "1 项" in m for m in messages)
    assert "清理完成" in messages[-1] and "预览" in messages[-1]
    assert not any(m.startswith("执行 ") for m in messages)


def test_progress_reports_apply_execution(tmp_path):
    """apply 模式：取锁与逐类别执行消息，含实际删除数量。"""
    config = Config(data_dir=tmp_path / "data")
    ds = write(config.data_dir / "sub" / ".DS_Store")
    messages: list[str] = []

    clean_library(config, apply=True, progress=messages.append)

    assert not ds.exists()
    assert any("管线" in m and "锁" in m for m in messages)
    assert any(m.startswith("执行 ds_store 完成") and "删除 1" in m for m in messages)


def test_progress_reports_vacuum_stages(tmp_path):
    """apply+vacuum：每个库输出开始（含当前大小）与完成（含节省）消息。"""
    config = Config(data_dir=tmp_path / "data")
    store = Store(config.data_dir)
    with store.connect() as db:
        db.executemany(
            "INSERT OR REPLACE INTO person_features(id,source_id,space,kind,data) VALUES(?,?,?,?,?)",
            [(f"big-{i}", None, "{}", "reference", "x" * 200_000) for i in range(20)],
        )
        db.execute("DELETE FROM person_features")
    messages: list[str] = []

    clean_library(config, apply=True, vacuum=True, progress=messages.append)

    assert any("VACUUM metadata.sqlite3 开始" in m for m in messages)
    assert any("VACUUM metadata.sqlite3 完成" in m and "节省" in m for m in messages)


def test_progress_reports_vacuum_busy_skip(tmp_path):
    """被占用的库：输出跳过原因，不抛错。"""

    config = Config(data_dir=tmp_path / "data")
    Store(config.data_dir)
    chroma = config.data_dir / "chroma"
    chroma.mkdir(parents=True, exist_ok=True)
    blocker = sqlite3.connect(chroma / "chroma.sqlite3", timeout=1)
    blocker.execute("BEGIN EXCLUSIVE")
    messages: list[str] = []
    try:
        clean_library(config, apply=True, vacuum=True, progress=messages.append)
    finally:
        blocker.rollback()
        blocker.close()

    assert any("VACUUM chroma/chroma.sqlite3" in m and "占用" in m for m in messages)


def test_progress_vacuum_heartbeat(monkeypatch):
    """VACUUM 心跳线程：超过间隔发「仍在 VACUUM」；with 退出后线程停止。"""
    from maintenance import clean as clean_module

    monkeypatch.setattr(clean_module, "VACUUM_HEARTBEAT_SECONDS", 0.05)
    messages: list[str] = []

    with clean_module._vacuum_heartbeat("metadata.sqlite3", messages.append):
        time.sleep(0.15)

    assert any("仍在 VACUUM metadata.sqlite3" in m for m in messages)
    emitted = len(messages)
    time.sleep(0.1)
    assert len(messages) == emitted, "with 块退出后心跳线程应停止"


def test_cli_progress_lines_precede_json(tmp_path):
    """CLI：stdout 先输出带时间戳的进度行，最后的 JSON 报告仍可整体解析。"""
    config_path = make_config(tmp_path)
    write(tmp_path / "data" / ".DS_Store")

    result = invoke(config_path, "--apply")

    assert result.exit_code == 0, result.output
    assert any(re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", line) for line in result.output.splitlines())
    assert categories(report_of(result))["ds_store"]["result"] == {"deleted": 1}
