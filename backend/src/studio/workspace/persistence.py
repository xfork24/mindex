"""工作台读取与迁移：load() 读取 workspace.json，旧任务（无 workspace.json）在首次读取时迁移。

迁移只读旧文件：每个校验通过的旧产物保存为不可变版本，缺失的来源保持显式未知。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mindex.shared.media import audio_info, probe
from mindex.studio import studio
from mindex.studio.studio_artifacts import digest, safe_path

from .plans import PlanOptions, new_plan, normalize_voice_options, set_board
from .versions import add_version, file_info, path, persist, root


def _legacy_script(config, job_id, w):
    """旧任务的 input_text 就是文案：确定性地导入为一个 legacy 文案版本。

    版本号由任务与原文派生，只读请求在内存里得到同一份迁移结果（方案 revision 稳定）；
    version.json 是不可变文件，内容确定，缺失时补写不会与并发读冲突。workspace.json
    只在后续持锁修改时随 persist 落盘。
    """
    state = studio.load_job(config, job_id)
    text = (state.get("input_text") or "").strip()
    if not text:
        return None
    import hashlib

    vid = hashlib.sha256(f"legacy-script:{job_id}:{text}".encode()).hexdigest()[:32]
    if vid not in w["versions"]:
        item = {
            "id": vid,
            "stage": "script",
            "shot_id": None,
            "created_at": state["created_at"],
            "status": "complete",
            "inputs": {},
            "parameters": {},
            "signature": None,
            "data": {"text": text, "origin": "legacy"},
            "files": {},
            "error": None,
            "incomplete": False,
        }
        target = root(config, job_id) / "versions" / vid / "version.json"
        if not target.exists():
            studio.save(target, item)
        w["versions"][vid] = item
    return vid


def normalize_script(config, job_id, w):
    """方案缺少文案字段时迁移：旧任务导入 legacy 文案，且视为当前分镜的来源。"""
    pending = [p for p in w["plans"].values() if "script" not in p]
    if not pending:
        return
    legacy = _legacy_script(config, job_id, w)
    for p in pending:
        p["script"] = legacy
        p["board_script"] = legacy if p.get("board") else None


def load(config, job_id):
    """Call under mutation lock for first migration; subsequent reads do not modify files."""
    studio.load_job(config, job_id)
    if config.storage.backend == "s3":
        from mindex.shared import files
        from mindex.shared.storage import Store
        store = Store.open(config)
        files.read_bytes(store, path(config, job_id))
        for row in files.entries(store, f"studio/{job_id}/"):
            if row["path"].endswith(".json") or "/inputs/" in row["path"]:
                files.hydrate(store, store.root / row["path"])
    if path(config, job_id).is_file():
        w = json.loads(path(config, job_id).read_text(encoding="utf-8"))
        for p in w["plans"].values():
            normalize_voice_options(p)
            p.setdefault("anchors", {})
        normalize_script(config, job_id, w)
        return w
    directory = root(config, job_id)
    snapshot = directory / "migration-v2"
    snapshot.mkdir(exist_ok=True)
    for name in ("job.json", "storyboard.json", "timeline.json", "audio.json", "checkpoints.json"):
        source = directory / name
        if source.is_file() and not (snapshot / name).exists():
            shutil.copy2(source, snapshot / name)
    state = studio.load_job(config, job_id)
    p = new_plan(
        "Default Plan" if state.get("language") == "en" else "默认方案",
        PlanOptions(
            **state["options"],
            voice_mode=state.get("voice_mode", "inherit"),
            duck_gain=config.studio.audio.duck_gain,
        ).model_dump(),
    )
    w = {"schema": 2, "job_language": state.get("language", "zh"), "active_plan": p["id"],
         "plans": {p["id"]: p}, "versions": {}, "builds": []}
    del p["script"], p["board_script"]  # 由 normalize_script 统一迁移
    try:
        board = studio.load_storyboard(config, job_id)
        set_board(config, job_id, w, p, board.shots)
    except studio.EntityNotFound:
        pass
    normalize_script(config, job_id, w)
    # Each verified legacy file is preserved as a version. Missing provenance stays explicit;
    # matching today's content to an old execution is never assumed.
    imported = {}
    manifests = sorted((directory / "attempts").glob("*/manifest.json"))
    registry = directory / "checkpoints.json"
    outputs = []
    legacy_documents = []
    for manifest in manifests:
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
            legacy_documents.append(document)
            outputs.extend(document.get("outputs", {}).items())
        except (ValueError, OSError):
            continue
    if registry.is_file():
        outputs.extend(json.loads(registry.read_text(encoding="utf-8")).items())
    published = state.get("published_render", {})
    for logical, relative in published.get("files", {}).items():
        source = safe_path(directory, relative)
        if source.is_file():
            outputs.append((logical, {"path": relative, "sha256": digest(source)}))
    final_groups = {}
    for logical, info in outputs:
        try:
            source = safe_path(directory, info["path"])
            if not source.is_file() or digest(source) != info.get("sha256"):
                continue
            if logical in {"render/final.mp4", "render/final.srt", "render/final.vtt"}:
                final_groups.setdefault(str(source.parent), {})[source.suffix[1:]] = source
                continue
            stage = (
                "tts"
                if logical.startswith("audio/") and logical.endswith(".wav")
                else ("recall" if logical.startswith("candidates/") else None)
            )
            if stage is None or info["path"] in imported:
                continue
            sid = Path(logical).stem
            data = audio_info(source) if stage == "tts" else json.loads(source.read_text(encoding="utf-8"))
            name = "audio" if stage == "tts" else "candidates"
            vid = add_version(
                config,
                job_id,
                w,
                stage,
                sid,
                data=data,
                files={name: file_info(config, job_id, source)},
                incomplete=True,
            )
            imported[info["path"]] = vid
            if sid in p["shots"]:
                p["shots"][sid][stage] = vid
        except (OSError, ValueError):
            continue
    # Recover current pointers from verified aliases, never from attempt-name ordering.
    for sid, selected in p["shots"].items():
        for stage, logical, name in (
            ("tts", f"audio/{sid}.wav", "audio"),
            ("recall", f"candidates/{sid}.json", "candidates"),
        ):
            alias = directory / logical
            if alias.is_file():
                checksum = digest(alias)
                for item in w["versions"].values():
                    if (
                        item["stage"] == stage
                        and item["shot_id"] == sid
                        and item["files"].get(name, {}).get("sha256") == checksum
                    ):
                        selected[stage] = item["id"]
    # A render attempt explicitly pairs its clip with processed audio. It does not prove
    # which content or original TTS version was used, so those edges remain unknown.
    seen_clips = set()
    for document in legacy_documents:
        out = document.get("outputs", {})
        for logical, metadata in out.items():
            if not logical.startswith("render/clips/") or metadata["path"] in seen_clips:
                continue
            try:
                sid = Path(logical).stem
                clip = safe_path(directory, metadata["path"])
                if not clip.is_file() or digest(clip) != metadata.get("sha256"):
                    continue
                info = probe(clip)
                files = {"video": file_info(config, job_id, clip)}
                speech = out.get(f"render/voice/{sid}.wav")
                if speech:
                    audio = safe_path(directory, speech["path"])
                    if audio.is_file() and digest(audio) == speech.get("sha256"):
                        files["audio"] = file_info(config, job_id, audio)
                vid = add_version(
                    config,
                    job_id,
                    w,
                    "render",
                    sid,
                    data={"fps": str(info["fps"]), "duration": info["duration"]},
                    files=files,
                    incomplete=True,
                )
                seen_clips.add(metadata["path"])
                alias = directory / logical
                if sid in p["shots"] and alias.is_file() and digest(alias) == metadata["sha256"]:
                    p["shots"][sid]["render"] = vid
            except (OSError, ValueError):
                continue
    for attempt_file in sorted((directory / "attempts").glob("*.json")):
        try:
            record = json.loads(attempt_file.read_text(encoding="utf-8"))
            if record.get("status") == "failed":
                add_version(
                    config,
                    job_id,
                    w,
                    record["step"],
                    status="failed",
                    error=record.get("error"),
                    data={"legacy_attempt": record},
                    files={"record": file_info(config, job_id, attempt_file)},
                    incomplete=True,
                )
        except (OSError, ValueError, KeyError):
            continue
    # Aliases may be the only surviving legacy artifact. Keep them in immutable copies.
    for stage, subdir, suffix, name in (
        ("tts", "audio", "wav", "audio"),
        ("recall", "candidates", "json", "candidates"),
    ):
        for sid, selection in p["shots"].items():
            source = directory / subdir / f"{sid}.{suffix}"
            if stage not in selection and source.is_file():
                copied = snapshot / subdir / source.name
                copied.parent.mkdir(exist_ok=True)
                if not copied.exists():
                    shutil.copy2(source, copied)
                try:
                    data = audio_info(copied) if stage == "tts" else json.loads(copied.read_text(encoding="utf-8"))
                    selection[stage] = add_version(
                        config,
                        job_id,
                        w,
                        stage,
                        sid,
                        data=data,
                        files={name: file_info(config, job_id, copied)},
                        incomplete=True,
                    )
                except ValueError:
                    pass
    legacy_timeline = snapshot / "timeline.json"
    timeline_files = set((directory / "attempts").glob("edit-timeline-*/timeline.json"))
    for logical, metadata in outputs:
        if logical == "timeline.json":
            candidate = safe_path(directory, metadata["path"])
            if candidate.is_file() and digest(candidate) == metadata.get("sha256"):
                timeline_files.add(candidate)
    for timeline_file in [*sorted(timeline_files), legacy_timeline]:
        if not timeline_file.is_file():
            continue
        try:
            document = json.loads(timeline_file.read_text(encoding="utf-8"))
            timeline = studio.Timeline.model_validate(
                {k: v for k, v in document.items() if k not in {"schema", "updated_at"}}
            )
        except (ValueError, OSError):
            continue
        for event_model in timeline.events:
            event = event_model.model_dump()
            sid = event["shot_id"]
            selected_range = {
                "source_id": event["video"]["source_id"],
                "start": event["video"]["start"],
                "end": event["video"]["end"],
            }
            manual = event["video"]["alignment"] == "manual"
            vid = add_version(
                config,
                job_id,
                w,
                "select",
                sid,
                data={
                    "event": event,
                    "range": selected_range,
                    "manual": manual,
                    "range_origin": "legacy_used_range",
                },
                files={"timeline": file_info(config, job_id, timeline_file)},
                incomplete=True,
            )
            if timeline_file == legacy_timeline and sid in p["shots"]:
                p["shots"][sid]["select"] = vid
                if manual:
                    p["ranges"][sid] = selected_range
    if not final_groups and (directory / "render/final.mp4").is_file():
        final_groups["alias"] = {}
        for ext in ("mp4", "srt", "vtt"):
            source = directory / f"render/final.{ext}"
            if source.is_file():
                copied = snapshot / source.name
                if not copied.exists():
                    shutil.copy2(source, copied)
                final_groups["alias"][ext] = copied
    for group in final_groups.values():
        if "mp4" in group:
            p["final"] = add_version(
                config,
                job_id,
                w,
                "compose",
                files={ext: file_info(config, job_id, source) for ext, source in group.items()},
                incomplete=True,
            )
    # Select the explicitly published final, not a lexicographically last historical file.
    published_path = published.get("files", {}).get("render/final.mp4")
    for item in w["versions"].values():
        if item["stage"] == "compose" and item["files"].get("mp4", {}).get("path") == published_path:
            p["final"] = item["id"]
    persist(config, job_id, w)
    return w
