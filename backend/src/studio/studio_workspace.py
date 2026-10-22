"""Versioned Studio workspace. Plans are mutable pointers; versions and build records are historical.

All mutations are made under the existing global Studio lock by studio_api. Legacy files are
read only during migration, and remain available through the artifact endpoints.
"""

from __future__ import annotations

import copy
import json
import math
import re
import shutil
import wave
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from mindex.models.adapters import make_speaker, make_storyboarder
from mindex.shared.config import StrictModel, fingerprint, portable_parameters
from mindex.shared.media import atomic_media, audio_info, probe
from mindex.shared.shot_media import ShotMedia
from mindex.shared.storage import Store
from mindex.shared.trace import current
from mindex.studio import studio, studio_select, studio_steps
from mindex.studio.studio_artifacts import digest, safe_path, sanitized, visibility_key
from mindex.studio.studio_logs import LABELS, ExecutionLog, active_log, diagnostic_event

STAGES = ("recall", "tts", "select", "render")


class PlanOptions(StrictModel):
    voice: str | None = Field(default=None, min_length=1, max_length=100)
    voice_mode: Literal["inherit", "custom"] = "inherit"
    recall_limit: int = Field(default=20, ge=1, le=50)
    audio_mode: str = Field(default="tts_only", pattern="^(tts_only|ducking)$")
    burn: bool = True
    atempo: float | None = Field(default=1.0, ge=0.75, le=1.25)
    alignment: str = Field(default="auto", pattern="^(auto|freeze|strict)$")
    duck_gain: float = Field(default=0.15, gt=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def explicit_voice(cls, value):
        # Older API callers that explicitly supply a voice still request an override.
        if isinstance(value, dict) and "voice_mode" not in value and value.get("voice"):
            return {**value, "voice_mode": "custom"}
        return value

    @model_validator(mode="after")
    def validate_voice(self):
        if self.voice_mode == "custom" and not self.voice:
            raise ValueError("指定音色模式需要填写音色")
        if self.voice_mode == "inherit":
            self.voice = None
        return self


def normalize_voice_options(p):
    if "voice_mode" not in p["options"]:
        # Old workspaces copied the default into every plan without recording intent.
        # Keep the old value for reference, but do not treat it as an explicit override.
        p.setdefault("legacy_voice", p["options"].get("voice"))
        p["options"] = {**p["options"], "voice": None, "voice_mode": "inherit"}


def effective_voice(config, p):
    if p["options"].get("voice_mode", "inherit") == "custom":
        voice = p["options"].get("voice")
        if not voice:
            raise studio.StudioError("指定音色模式需要填写音色")
        return voice
    return config.studio.tts.voice


def root(config, job_id):
    return studio.job_dir(config, job_id)


def path(config, job_id):
    return root(config, job_id) / "workspace.json"


def persist(config, job_id, workspace):
    workspace["updated_at"] = studio.now()
    studio.save(path(config, job_id), workspace)


def version(workspace, version_id):
    result = workspace["versions"].get(version_id)
    if result is None:
        raise studio.StudioError("找不到产物版本")
    return result


def plan(workspace, plan_id):
    result = workspace["plans"].get(plan_id)
    if result is None:
        raise studio.StudioError("找不到构建方案")
    return result


def revision(p):
    return fingerprint(p)


def require_revision(p, expected):
    if expected != revision(p):
        raise studio.StudioError("方案已变化，请刷新后重新预检或提交")


def new_plan(name, options):
    return {
        "id": uuid4().hex[:12],
        "name": name,
        "created_at": studio.now(),
        "board": None,
        "shots": {},
        "options": PlanOptions.model_validate(options).model_dump(),
        "ranges": {},
        "anchors": {},
        "final": None,
    }


def add_version(
    config,
    job_id,
    w,
    stage,
    shot_id=None,
    *,
    data=None,
    inputs=None,
    parameters=None,
    signature=None,
    files=None,
    status="complete",
    error=None,
    incomplete=False,
    version_id=None,
):
    vid = version_id or uuid4().hex
    item = {
        "id": vid,
        "stage": stage,
        "shot_id": shot_id,
        "created_at": studio.now(),
        "status": status,
        "inputs": inputs or {},
        "parameters": sanitized(parameters or {}),
        "signature": signature,
        "data": data or {},
        "files": files or {},
        "error": error,
        "incomplete": incomplete,
    }
    target = root(config, job_id) / "versions" / vid / "version.json"
    if target.exists():
        raise studio.StudioError("不可变版本已存在")
    studio.save(target, item)
    w["versions"][vid] = item
    return vid


def file_info(config, job_id, target):
    safe_path(root(config, job_id), target.relative_to(root(config, job_id)).as_posix())
    return {
        "path": target.relative_to(root(config, job_id)).as_posix(),
        "sha256": digest(target),
        "bytes": target.stat().st_size,
    }


def file_path(config, job_id, item, name):
    metadata = item["files"].get(name)
    if not metadata:
        raise studio.StudioError(f"版本 {item['id']} 缺少 {name}")
    target = safe_path(root(config, job_id), metadata["path"])
    if not target.is_file() or digest(target) != metadata["sha256"]:
        raise studio.StudioError(f"版本 {item['id']} 文件损坏或缺失")
    return target


def valid_files(config, job_id, item):
    try:
        for name in item["files"]:
            file_path(config, job_id, item, name)
        return True
    except (OSError, studio.StudioError):
        return False


def set_board(config, job_id, w, p, shots, *, origin=None):
    board = studio.Storyboard(job_id=job_id, shots=shots)
    if len(board.shots) > config.studio.max_shots:
        raise studio.StudioError("分镜数量超过上限")
    selected = {}
    content_ids = []
    for shot in board.shots:
        old = p["shots"].get(shot.id, {})
        content = w["versions"].get(old.get("content"))
        if not content or content["data"] != shot.model_dump():
            cid = add_version(
                config,
                job_id,
                w,
                "content",
                shot.id,
                data=shot.model_dump(),
                inputs={"split": origin} if origin else {},
            )
        else:
            cid = content["id"]
        selected[shot.id] = {**old, "content": cid}
        content_ids.append(cid)
    bid = add_version(
        config,
        job_id,
        w,
        "board",
        data={"order": [s.id for s in board.shots]},
        inputs={str(i): cid for i, cid in enumerate(content_ids)},
    )
    p.update(board=bid, shots=selected)
    # Ranges belonging to removed shots remain in historical plans/versions only.
    p["ranges"] = {sid: value for sid, value in p["ranges"].items() if sid in selected}
    p["anchors"] = {sid: value for sid, value in p.get("anchors", {}).items() if sid in selected}


def load(config, job_id):
    """Call under mutation lock for first migration; subsequent reads do not modify files."""
    studio.load_job(config, job_id)
    if path(config, job_id).is_file():
        w = json.loads(path(config, job_id).read_text())
        for p in w["plans"].values():
            normalize_voice_options(p)
            p.setdefault("anchors", {})
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
        "默认方案",
        PlanOptions(
            **state["options"],
            voice_mode=state.get("voice_mode", "inherit"),
            duck_gain=config.studio.audio.duck_gain,
        ).model_dump(),
    )
    w = {"schema": 2, "active_plan": p["id"], "plans": {p["id"]: p}, "versions": {}, "builds": []}
    try:
        board = studio.load_storyboard(config, job_id)
        set_board(config, job_id, w, p, board.shots)
    except studio.EntityNotFound:
        pass
    # Each verified legacy file is preserved as a version. Missing provenance stays explicit;
    # matching today's content to an old execution is never assumed.
    imported = {}
    manifests = sorted((directory / "attempts").glob("*/manifest.json"))
    registry = directory / "checkpoints.json"
    outputs = []
    legacy_documents = []
    for manifest in manifests:
        try:
            document = json.loads(manifest.read_text())
            legacy_documents.append(document)
            outputs.extend(document.get("outputs", {}).items())
        except (ValueError, OSError):
            continue
    if registry.is_file():
        outputs.extend(json.loads(registry.read_text()).items())
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
            data = audio_info(source) if stage == "tts" else json.loads(source.read_text())
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
            record = json.loads(attempt_file.read_text())
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
                    data = audio_info(copied) if stage == "tts" else json.loads(copied.read_text())
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
            document = json.loads(timeline_file.read_text())
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


def ordered_shots(w, p):
    if not p["board"]:
        return []
    return version(w, p["board"])["data"]["order"]


@dataclass
class DetailContext:
    """Reuse reads within one detail response; never cache across edits or requests."""

    store: Store | None = None
    settings: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)

    def database(self, config):
        if self.store is None:
            self.store = Store(config.data_dir)
        return self.store


def settings(config, p, stage, *, context=None):
    if context is not None:
        if stage not in context.settings:
            context.settings[stage] = portable_parameters(config, _settings(
                config, p, stage, store=context.database(config) if stage == "recall" else None,
            ))
        return context.settings[stage]
    return portable_parameters(config, _settings(config, p, stage))


def _settings(config, p, stage, *, store=None):
    o = p["options"]
    if stage == "split":
        return {
            "generator": sanitized(config.studio.generator.model_dump()),
            "split_version": studio.SPLIT_VERSION,
        }
    if stage == "recall":
        store = store or Store(config.data_dir)
        active = store.active_index()
        # Text recall is implicitly affected by the active person release; cover it in the key.
        return {
            "index_id": active["id"] if active else None,
            "person_release": active.get("search_release_id") if active else None,
            "limit": o["recall_limit"],
            "recall": {**config.studio.recall.model_dump(), "limit": o["recall_limit"]},
            "search": config.search.model_dump(),
            "embedding": sanitized(config.embedding.model_dump()),
            "visibility": visibility_key(store),
        }
    if stage == "tts":
        voice = effective_voice(config, p)
        return {
            "voice": voice,
            "tts": sanitized({**config.studio.tts.model_dump(), "voice": voice}),
        }
    if stage == "select":
        return {
            "selection": config.studio.selection.model_dump(),
            "atempo": o["atempo"],
            "alignment": o["alignment"],
        }
    if stage == "render":
        return {
            "render": config.studio.render.model_dump(),
            "audio": {
                **config.studio.audio.model_dump(),
                "mode": o["audio_mode"],
                "duck_gain": o["duck_gain"],
            },
            "audio_mode": o["audio_mode"],
            "duck_gain": o["duck_gain"],
        }
    return {
        "burn": o["burn"],
        "subtitle": {**config.studio.subtitle.model_dump(), "burn": o["burn"]},
        "render": config.studio.render.model_dump(),
    }


def needs_recall(p, sid):
    return sid not in p["ranges"] or bool(p["ranges"][sid].get("anchor_ref"))


def recall_signature(recall):
    """规范化分镜召回设置；全默认返回 None，payload 与旧版逐字节一致以复用历史版本。"""
    if not recall:
        return None
    value = studio.ShotRecall.model_validate(recall) if isinstance(recall, dict) else recall
    filters = {k: v for k, v in value.filters.model_dump().items() if k != "source_ids" and v not in (None, 0)}
    sources = sorted(value.filters.selected_sources())
    if len(sources) == 1:
        filters["source_id"] = sources[0]
    elif sources:
        filters.pop("source_id", None)
        filters["source_ids"] = sources
    signature = {
        "mode": value.mode,
        "person_ids": sorted(value.person_ids),
        "person_mode": value.person_mode,
        "person_relation": value.person_relation,
        "filters": filters,
    }
    if value.media_id:
        signature["media"] = {"id": value.media_id, "kind": value.media_kind}
    if (
        value.mode == "text"
        and not value.person_ids
        and value.person_mode == "auto"
        and value.person_relation == "all"
        and not filters
    ):
        return None
    return signature


def query_media(config, job_id, recall):
    """按内容寻址定位查询文件并复验 sha256；防止 inputs 被清理或篡改后用错文件。"""
    for path in sorted((root(config, job_id) / "inputs").glob(recall.media_id + ".*")):
        if path.suffix != ".partial" and digest(path) == recall.media_id:
            return path
    raise studio.StudioError("查询文件缺失或已损坏，请重新上传")


def specification(config, job_id, w, p, stage, sid=None, *, context=None, parameters=None):
    params = settings(config, p, stage, context=context)
    if stage == "render":
        params = {**params, "media_version": "shot-media-v1"}
    if stage == "split":
        payload = {"text": studio.load_job(config, job_id)["input_text"]}
        inputs = {}
    elif stage == "compose":
        inputs = {"board": p["board"], **{sid: p["shots"][sid].get("render") for sid in ordered_shots(w, p)}}
        # Query/estimate-only board edits do not change an already rendered film.
        # Provenance still records the exact board used when this version was produced.
        payload = {"renders": [[sid, inputs[sid]] for sid in ordered_shots(w, p)]}
    else:
        selected = p["shots"].get(sid)
        if selected is None:
            raise studio.StudioError("分镜不存在于当前方案")
        content = version(w, selected["content"])["data"]
        inputs = {"content": selected["content"]}
        if stage == "recall":
            payload = {"query": content["query"]}
            # 分镜级覆盖仅在实际设置时进签名，保证未覆盖分镜与历史版本签名逐字节一致。
            if content.get("granularity"):
                payload["granularity"] = content["granularity"]
            if content.get("recall_limit"):
                payload["limit"] = content["recall_limit"]
            # 只在有限制时加键，保证不限任务的签名与历史版本逐字节一致。
            if scope := studio.material_scope(studio.load_job(config, job_id)):
                params = {**params, "sources": sorted(scope)}
            recall = recall_signature(content.get("recall"))
            if recall is not None:
                payload["recall"] = recall
            from mindex.persons.persons import AUTO_PERSON_QUERY_VERSION, has_document_reference

            value = content.get("recall") or {}
            if (
                value.get("mode", "text") == "text"
                and value.get("person_mode", "auto") == "auto"
                and has_document_reference(content["query"])
            ):
                payload["auto_person_query_version"] = AUTO_PERSON_QUERY_VERSION
        elif stage == "tts":
            payload = {"text": content["text"]}
        elif stage == "select":
            inputs["tts"] = selected.get("tts")
            if needs_recall(p, sid):
                inputs["recall"] = selected.get("recall")
            payload = {
                "text": content["text"],
                "tts": inputs["tts"],
                "recall": inputs.get("recall"),
                "range": p["ranges"].get(sid),
                "default_window_version": 2,
            }
            if p.get("anchors", {}).get(sid, {}).get("mode") == "manual":
                payload["anchor"] = p["anchors"][sid]
        else:
            inputs.update(select=selected.get("select"), tts=selected.get("tts"))
            event = w["versions"].get(inputs["select"], {}).get("data", {}).get("event")
            source = None
            if event:
                store = context.database(config) if context is not None else Store(config.data_dir)
                row = store.one(
                    "SELECT path FROM sources WHERE id=?", (event["video"]["source_id"],)
                )
                if row:
                    source = ShotMedia(store).identity(event["video"]["source_id"],
                                                       event["video"]["start"], event["video"]["end"])
            payload = {"select": inputs["select"], "tts": inputs["tts"], "source": source}
    if parameters is not None:
        params = parameters
    return {
        "inputs": inputs,
        "parameters": params,
        "signature": fingerprint({"stage": stage, "payload": payload, "parameters": params}),
    }


def signature_matches(config, job_id, w, p, stage, sid, item, expected, *, context=None):
    if item["signature"] == expected["signature"]:
        return True
    if portable_parameters(config, item["parameters"], expected["parameters"]) != expected["parameters"]:
        return False
    # Reconstruct the current payload with the historical path spelling. This
    # verifies the original hash without rewriting immutable version records.
    legacy = specification(config, job_id, w, p, stage, sid, context=context,
                           parameters=item["parameters"])
    return item["signature"] == legacy["signature"]


def conflicts(config, job_id, w, p, stage, sid, vid, *, context=None):
    key = (stage, sid, vid)
    if context is not None and key in context.conflicts:
        return context.conflicts[key]
    item = version(w, vid)
    expected = specification(config, job_id, w, p, stage, sid, context=context)
    messages = []
    if stage == "select":
        from mindex.studio.studio_window import anchor_conflict

        if error := anchor_conflict(w, p, sid):
            messages.append(error)
        choice = p.get("anchors", {}).get(sid, {})
        original = item["data"].get("anchor_ref")
        if original and choice.get("mode") == "manual" and original["candidate_id"] != choice["candidate_id"]:
            messages.append(f"参考候选：原版本 {original['candidate_id']}，当前 {choice['candidate_id']}")
    if item["incomplete"]:
        messages.append("历史记录不完整，无法验证依赖与参数")
    if not signature_matches(config, job_id, w, p, stage, sid, item, expected, context=context):
        if stage == "recall":
            messages.append("查询、召回方式或召回设置已变化")
        elif stage == "tts":
            old_content = w["versions"].get(item["inputs"].get("content"), {}).get("data", {})
            new_content = version(w, expected["inputs"]["content"])["data"]
            if "text" in old_content and old_content["text"] != new_content["text"]:
                messages.append("旁白已变化")
            before = item["parameters"].get("tts", {})
            after = expected["parameters"]["tts"]
            for field, label in (("backend", "配音服务"), ("model", "配音模型"), ("voice", "方案音色")):
                if before.get(field) != after.get(field):
                    messages.append(f"{label}已变化：{before.get(field) or '未记录'} → {after.get(field)}")
            if item["parameters"] != expected["parameters"] and not messages:
                messages.append("配音服务参数已变化")
        for key, value in expected["inputs"].items():
            if key != "content" and item["inputs"].get(key) != value:
                messages.append(
                    f"依赖 {key}：原版本 {item['inputs'].get(key) or '缺失'}，当前 {value or '缺失'}"
                )
        if stage != "tts" and portable_parameters(config, item["parameters"], expected["parameters"]) != expected["parameters"]:
            messages.append("阶段参数与当前方案不一致")
        if not messages:
            messages.append("实际输入内容或素材文件已变化")
    for dependency in ("recall", "tts", "select"):
        dep_id = expected["inputs"].get(dependency)
        if dep_id and dep_id in w["versions"]:
            upstream = conflicts(config, job_id, w, p, dependency, sid, dep_id, context=context)
            if upstream:
                messages.append(f"上游 {dependency} 不兼容：{'；'.join(upstream)}")
    if context is not None:
        context.conflicts[key] = messages
    return messages


def match(config, job_id, w, p, stage, sid=None, *, verify=True):
    if stage == "split":
        return None  # Re-splitting is an explicit structural operation, never a pointer switch.
    spec = specification(config, job_id, w, p, stage, sid)
    selected = p.get("final") if stage == "compose" else p["shots"].get(sid, {}).get(stage)
    items = list(reversed(list(w["versions"].values())))
    if selected:
        items.insert(0, version(w, selected))
    for item in items:
        if (
            item["stage"] == stage
            and item["shot_id"] == sid
            and item["status"] == "complete"
            and signature_matches(config, job_id, w, p, stage, sid, item, spec)
            and not item["incomplete"]
            and (not verify or valid_files(config, job_id, item))
        ):
            return item["id"]
    return None


def preflight(config, job_id, w, p, *, force=False):
    draft = copy.deepcopy(p)
    nodes = []
    if not p["board"]:
        nodes.append(
            {
                "key": "split",
                "stage": "split",
                "shot_id": None,
                "action": "rebuild",
                "reason": "自动拆分镜后依次执行召回、配音、选片对齐、分镜渲染和成片合成",
            }
        )
    for stage in STAGES:
        for sid in ordered_shots(w, p):
            key = f"{stage}:{sid}"
            found = None if force else match(config, job_id, w, draft, stage, sid)
            spec = specification(config, job_id, w, draft, stage, sid)
            action = "reuse" if found else "rebuild"
            reason = "匹配成功版本" if found else "缺少匹配的成功版本"
            if stage == "recall" and spec["parameters"]["index_id"] is None and needs_recall(p, sid):
                action, reason = "blocked", "尚未发布索引"
            # Explicit ranges do not need recall and can be built without an index.
            if stage == "recall" and not needs_recall(p, sid) and not found:
                action, reason = "skip", "人工选片不依赖召回"
            if stage == "select":
                from mindex.studio.studio_window import anchor_conflict

                anchor_error = anchor_conflict(w, p, sid)
                if anchor_error:
                    action, reason = "blocked", anchor_error
                if sid in p["ranges"]:
                    try:
                        validate_range(config, p["ranges"][sid])
                        if p["ranges"][sid].get("anchor_ref"):
                            alignment(config, job_id, w, p, sid, p["ranges"][sid])
                    except studio.StudioError as exc:
                        action, reason = "blocked", str(exc)
                else:
                    recall = w["versions"].get(spec["inputs"].get("recall"))
                    if recall and not recall["data"].get("results"):
                        action, reason = "blocked", "召回版本没有候选，请修改查询或人工选片"
            nodes.append(
                {
                    "key": key,
                    "stage": stage,
                    "shot_id": sid,
                    "action": action,
                    "version": found,
                    "reason": reason,
                }
            )
            draft["shots"][sid][stage] = found or f"@{key}"
    found = None if force or not p["board"] else match(config, job_id, w, draft, "compose")
    nodes.append(
        {
            "key": "compose",
            "stage": "compose",
            "shot_id": None,
            "action": "reuse" if found else "rebuild",
            "version": found,
            "reason": "全部分镜就绪后合成",
        }
    )
    # Predict blockers transitively; the worker still rechecks every dependency.
    blocked = {n["key"] for n in nodes if n["action"] == "blocked"}
    for n in nodes:
        sid, stage = n["shot_id"], n["stage"]
        deps = (
            ([f"tts:{sid}"] + ([f"recall:{sid}"] if needs_recall(p, sid) else []))
            if stage == "select"
            else (
                [f"select:{sid}", f"tts:{sid}"]
                if stage == "render"
                else ([f"render:{s}" for s in ordered_shots(w, p)] if stage == "compose" else [])
            )
        )
        if any(d in blocked for d in deps):
            n.update(action="blocked", reason="必需依赖阻塞")
            blocked.add(n["key"])
    result = {"plan_id": p["id"], "revision": revision(p), "force": force, "nodes": nodes}
    result["token"] = fingerprint(result)
    return result


def choose(w, p, stage, sid, vid):
    item = version(w, vid)
    if item["status"] != "complete" or item["stage"] != stage or item["shot_id"] != sid:
        raise studio.StudioError("只能选用属于此阶段和分镜的成功版本")
    if stage == "compose":
        p["final"] = vid
    else:
        if sid not in p["shots"]:
            raise studio.StudioError("当前方案没有此分镜")
        if stage == "recall" and p["shots"][sid].get("recall") not in (None, vid):
            # A new candidate pool starts a new automatic selection, bound to
            # this recall rather than a previous anchor or explicit window.
            p.setdefault("anchors", {})[sid] = {"mode": "auto"}
            p["ranges"].pop(sid, None)
        p["shots"][sid][stage] = vid
        if stage == "select":
            if item["data"].get("manual"):
                p["ranges"][sid] = item["data"]["range"]
                if item["data"].get("anchor_choice"):
                    p.setdefault("anchors", {})[sid] = item["data"]["anchor_choice"]
            else:
                p["ranges"].pop(sid, None)


def restore(w, vid, name):
    item = version(w, vid)
    snapshot = item["data"].get("plan")
    if item["stage"] != "compose" or not snapshot:
        raise studio.StudioError("此成片记录不完整，无法还原构建方案；原文件仍可下载")
    p = copy.deepcopy(snapshot)
    normalize_voice_options(p)
    p.setdefault("anchors", {})
    p.update(id=uuid4().hex[:12], name=name, created_at=studio.now(), final=vid)
    w["plans"][p["id"]] = p
    w["active_plan"] = p["id"]
    return p


def source_shots(config, source_id, *, overlays=None):
    """源内全部镜头及可见性；overlays 可由调用方预先取好，避免逐镜头全表扫描。"""
    from mindex.admin.curation import Library

    store = Store(config.data_dir)
    if overlays is None:
        overlays = Library(store).overlays()
    # 可见性一次性预查后逐行内存比对，避免每个镜头一次区间查询。
    source_hidden = bool(overlays[0].get(source_id, {}).get("hidden"))
    hidden = studio_steps.hidden_intervals(store, source_id, overlays=overlays)
    return [
        {
            **row,
            "visible": not source_hidden
            and studio_steps.range_visible(hidden, row["start"], row["end"]),
        }
        for row in store.rows(
            "SELECT id,source_id,start,end FROM segments WHERE source_id=? AND kind='shot' ORDER BY start",
            (source_id,),
        )
    ]


def validate_range(config, selected_range, *, overlays=None):
    from mindex.admin.curation import Library

    source_id = selected_range["source_id"]
    start, end = selected_range["start"], selected_range["end"]
    if not all(math.isfinite(v) for v in (start, end)) or end - start < 0.04:
        raise studio.StudioError("所选区间不足一帧或边界无效")
    store = Store(config.data_dir)
    if overlays is None:
        overlays = Library(store).overlays()
    rows = source_shots(config, source_id, overlays=overlays)
    ids = selected_range.get("segment_ids")
    if ids:
        chosen = [row for row in rows if row["id"] in ids]
        if len(chosen) != len(set(ids)) or len(ids) != len(set(ids)):
            raise studio.StudioError("镜头必须全部来自同一素材，且不能重复")
        positions = [rows.index(row) for row in chosen]
        if positions != list(range(min(positions), max(positions) + 1)):
            raise studio.StudioError("只能选择连续镜头")
        if start < chosen[0]["start"] - 1e-6 or end > chosen[-1]["end"] + 1e-6:
            raise studio.StudioError("微调边界必须位于已选镜头范围内")
    covered = [r for r in rows if r["start"] < end - 1e-6 and r["end"] > start + 1e-6]
    if not covered or start < covered[0]["start"] - 1e-6 or end > covered[-1]["end"] + 1e-6:
        raise studio.StudioError("区间超出已知素材镜头范围")
    cursor = start
    for row in covered:
        if row["start"] > cursor + 1e-6:
            raise studio.StudioError("区间包含不连续镜头")
        cursor = max(cursor, row["end"])
    if not studio_steps.interval_visible(store, source_id, start, end, overlays=overlays):
        raise studio.StudioError("所选区间包含隐藏素材")
    return {**selected_range, "segment_ids": [r["id"] for r in covered]}


def alignment(config, job_id, w, p, sid, selected_range=None):
    from mindex.studio import studio_window

    if error := studio_window.anchor_conflict(w, p, sid):
        raise studio.StudioError(error)
    if selected_range and selected_range.get("anchor_ref"):
        return studio_window.alignment(config, job_id, w, p, sid, selected_range)
    if selected_range is None:
        ctx = studio_window.context(config, job_id, w, p, sid)
        if ctx["ready"]:
            default, _ = studio_window.selected_range(
                config, job_id, w, p, sid, ctx["context_token"], ctx["initial_start"],
                ref=ctx["anchor_ref"], tts_version=ctx["tts_version"], _context=ctx,
            )
            data = studio_window.alignment(config, job_id, w, p, sid, default, _context=ctx)
            data["manual"] = False
            return data
    selected = p["shots"][sid]
    speech = version(w, selected.get("tts"))
    if speech["status"] != "complete":
        raise studio.StudioError("缺少成功配音")
    file_path(config, job_id, speech, "audio")
    shot = version(w, selected["content"])["data"]
    store = Store(config.data_dir)
    duration, nxt, prev, covered = studio_steps._selection_callbacks(store)
    if selected_range:
        selected_range = validate_range(config, selected_range)
        anchor = {
            **selected_range,
            "segment_id": selected_range["segment_ids"][0],
            "rank": 0,
            "source_name": store.source_names()[selected_range["source_id"]],
        }
        plan_data = studio_select.plan_explicit(
            anchor["source_id"],
            anchor["start"],
            anchor["end"],
            speech["data"]["duration"],
            config.studio.selection,
            source_duration=duration(anchor["source_id"]),
        )
        plan_data["anchor"] = anchor
    else:
        candidates = studio_window.candidates(config, w, p, sid)
        events, _ = studio_select.select_events(
            [shot],
            {sid: candidates},
            {sid: speech["data"]["duration"]},
            config.studio.selection,
            source_duration=duration,
            extend_next=nxt,
            extend_prev=prev,
            covered_shots=covered,
        )
        anchor = next(c for c in candidates if c["segment_id"] == events[0].video.segment_id)
        plan_data = studio_select.plan_anchor(
            anchor,
            speech["data"]["duration"],
            config.studio.selection,
            source_duration=duration,
            extend_next=nxt,
            extend_prev=prev,
        )
        # plan_anchor's vlen records the full extended range before trimming.
        selected_range = {
            "source_id": anchor["source_id"],
            "start": plan_data["start"],
            "end": plan_data["start"] + plan_data["vlen"],
        }
        selected_range = validate_range(config, selected_range)
    target = speech["data"]["duration"]
    start, end = selected_range["start"], selected_range["end"]
    speed = p["options"]["atempo"]
    if speed is None:
        speed = 1.0 if p["options"]["alignment"] == "freeze" else plan_data["atempo"]
    render_duration = target / speed
    if (
        p["options"]["alignment"] == "strict"
        and abs(render_duration - (end - start)) > 1 / config.studio.render.fps
    ):
        raise studio.StudioError("严格对齐要求素材范围与配音时长一致，请微调边界或变速")
    actual_end = min(end, start + render_duration)
    plan_data.update(
        start=start,
        end=actual_end,
        vlen=end - start,
        atempo=speed,
        render_duration=render_duration,
        pad=render_duration - (end - start),
    )
    event = studio_select.build_event(
        shot,
        1,
        plan_data,
        target,
        config.studio.selection,
        covered,
        [],
        alignment_override="manual" if sid in p["ranges"] else None,
    )
    event.audio.file = speech["files"]["audio"]["path"]
    return {
        "event": event.model_dump(),
        "range": selected_range,
        "anchor": anchor,
        "anchor_ref": (
            {"recall_version": selected.get("recall"), "candidate_id": studio_window.candidate_id(anchor)}
            if needs_recall(p, sid) else None
        ),
        "extension": {"before": max(0, anchor["start"] - start), "after": max(0, end - anchor["end"])},
        "actual_range": {"source_id": anchor["source_id"], "start": start, "end": actual_end},
        "trim_seconds": max(0, end - actual_end),
        "freeze_seconds": max(0, render_duration - (end - start)),
        "atempo": speed,
        "manual": sid in p["ranges"],
    }


def render_node(config, job_id, w, p, sid, directory):
    from mindex.studio.studio_render import cut_shot, extract_ambient, process_shot_audio

    selection = version(w, p["shots"][sid]["select"])
    speech = version(w, p["shots"][sid]["tts"])
    if selection["inputs"].get("tts") != speech["id"]:
        raise studio.StudioError("选片绑定的配音与当前配音不同，请先重新对齐")
    event = studio.TimelineEvent.model_validate(selection["data"]["event"])
    validate_range(config, selection["data"]["range"])
    store = Store(config.data_dir)
    absolute_event = event.model_copy(deep=True)
    source = ShotMedia(store).materialize(event.video.source_id, event.video.start, event.video.end)
    event.video.end -= event.video.start
    event.video.start = 0
    cfg = config.studio.audio.model_copy(update={"duck_gain": p["options"]["duck_gain"]})
    ambient = None
    if p["options"]["audio_mode"] == "ducking" and probe(source)["audio"]:
        ambient = directory / "ambient.wav"
        raw_ambient = directory / "shot-audio.wav"
        ShotMedia(store).audio(absolute_event.video.source_id, raw_ambient, absolute_event.video.start,
                               absolute_event.video.end - absolute_event.video.start)
        extract_ambient(raw_ambient, event, cfg, ambient)
    processed = directory / "processed.wav"
    process_shot_audio(file_path(config, job_id, speech, "audio"), ambient, event, cfg, processed)
    with wave.open(str(processed), "rb") as audio:
        samples = audio.getnframes()
        if audio.getframerate() != 48000 or samples <= 0:
            raise studio.StudioError("处理后配音采样率无效")
    fps = Fraction(str(config.studio.render.fps))
    # Guarantee enough samples at *every* possible composition offset (round may round down).
    frames = math.ceil(Fraction(samples, 48000) * fps)
    while math.floor(Fraction(frames, 1) / fps * 48000) < samples:
        frames += 1
    clip = directory / "video.mp4"
    cut_shot(source, event, config.studio.render, clip, frames=frames)
    preview = directory / "preview.mp4"
    atomic_media(
        [
            "-i",
            str(clip),
            "-i",
            str(processed),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-af",
            f"apad,atrim=duration={float(frames / fps):.9f}",
            "-c:a",
            "aac",
        ],
        preview,
    )
    return {"event": absolute_event.model_dump(), "frames": frames, "samples": samples, "fps": str(fps)}, {
        "video": file_info(config, job_id, clip),
        "audio": file_info(config, job_id, processed),
        "preview": file_info(config, job_id, preview),
    }


def compose(config, job_id, w, p, directory):
    from mindex.studio.studio_render import build_srt, check_font, concat_media, mux, validate_final

    fps = Fraction(str(config.studio.render.fps))
    clips, tracks, events, boundaries = [], [], [], []
    total_frames = total_samples = 0
    for index, sid in enumerate(ordered_shots(w, p), 1):
        item = version(w, p["shots"][sid]["render"])
        if item["data"]["fps"] != str(fps):
            raise studio.StudioError("分镜帧率与成片帧率不一致")
        event = studio.TimelineEvent.model_validate(item["data"]["event"])
        event.order = index
        frames = item["data"]["frames"]
        end_sample = round(Fraction(total_frames + frames, 1) / fps * 48000)
        samples = end_sample - total_samples
        if samples < item["data"]["samples"]:
            raise studio.StudioError("分镜帧数不足以容纳配音，请重新渲染")
        track = directory / f"track-{index}.wav"
        atomic_media(
            [
                "-i",
                str(file_path(config, job_id, item, "audio")),
                "-af",
                f"apad=whole_len={samples},atrim=end_sample={samples},asetpts=PTS-STARTPTS",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
            ],
            track,
        )
        clips.append(file_path(config, job_id, item, "video"))
        tracks.append(track)
        events.append(event)
        boundaries.append(
            {
                "shot_id": sid,
                "start": float(total_frames / fps),
                "end": float((total_frames + frames) / fps),
                "frames": frames,
                "start_sample": total_samples,
                "end_sample": end_sample,
            }
        )
        total_frames += frames
        total_samples = end_sample
    timeline = studio.Timeline(
        job_id=job_id,
        mode="manual",
        audio_mode=p["options"]["audio_mode"],
        burn=p["options"]["burn"],
        events=events,
    )
    video, audio = directory / "video.mp4", directory / "audio.wav"
    concat_media(clips, video)
    concat_media(tracks, audio)
    content = build_srt(timeline, boundaries)
    srt, vtt, final = directory / "final.srt", directory / "final.vtt", directory / "final.mp4"
    studio.save_bytes(srt, content.encode())
    studio.save_bytes(
        vtt, ("WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)).encode()
    )
    if timeline.burn:
        check_font(srt, config.studio.subtitle)
    mux(video, audio, srt, final, config, burn=timeline.burn)
    seconds = float(total_frames / fps)
    validate_final(final, seconds, timeline.burn, config.studio.render.fps)
    graph_ids = set()

    def collect(vid):
        if vid in graph_ids:
            return
        graph_ids.add(vid)
        for dep in version(w, vid)["inputs"].values():
            if dep:
                collect(dep)

    collect(p["board"])
    for sid in ordered_shots(w, p):
        collect(p["shots"][sid]["render"])
    data = {
        "plan": copy.deepcopy(p),
        "graph": {vid: copy.deepcopy(version(w, vid)) for vid in graph_ids},
        "total_frames": total_frames,
        "total_samples": total_samples,
        "total_seconds": seconds,
        "boundaries": boundaries,
        "burn": timeline.burn,
    }
    studio.save(directory / "timeline.json", data)
    return data, {
        name: file_info(config, job_id, target)
        for name, target in (
            ("mp4", final),
            ("srt", srt),
            ("vtt", vtt),
            ("timeline", directory / "timeline.json"),
        )
    }


class VersionDiagnostics:
    def __init__(self, directory):
        self.directory, self.sequence = directory, 0

    def record(self, kind, value):
        self.sequence += 1
        studio.save(
            self.directory / "debug" / f"{self.sequence:04d}-{kind}.json",
            {"at": studio.now(), "data": sanitized(value)},
        )
        diagnostic_event(kind, value)


def execute(config, job_id, w, p, stage, sid=None, *, speaker=None, storyboarder=None, embedder=None):
    if stage == "select" and sid in p["ranges"]:
        p["ranges"][sid] = validate_range(config, p["ranges"][sid])
    spec = specification(config, job_id, w, p, stage, sid)
    vid = uuid4().hex
    directory = root(config, job_id) / "versions" / vid
    directory.mkdir(parents=True)
    diagnostics = VersionDiagnostics(directory)
    diagnostics.record("inputs", {"stage": stage, "shot_id": sid, **spec})
    token = current.set(diagnostics)
    try:
        data, files = {}, {}
        if stage == "split":
            text = studio.load_job(config, job_id)["input_text"]
            drafts = (storyboarder or make_storyboarder(config)).split(text)
            if studio.coverage_warning(text, [s.text for s in drafts]):
                raise studio.StudioError("自动分镜未完整覆盖原文")
            # New identities avoid aliasing an old shot after a structural re-split.
            shots = [
                studio.Shot(
                    id=f"shot-{uuid4().hex[:10]}",
                    text=d.text,
                    query=d.query,
                    estimated_seconds=d.estimated_seconds,
                )
                for d in drafts
            ]
            set_board(config, job_id, w, p, shots, origin=vid)
            data = {"board": p["board"]}
        elif stage == "tts":
            content = version(w, p["shots"][sid]["content"])["data"]
            target = directory / "audio.wav"
            payload = (speaker or make_speaker(config)).speak(content["text"], spec["parameters"]["voice"])
            if not payload:
                raise studio.StudioError("配音返回空音频")
            studio.save_bytes(target, payload)
            data, files = audio_info(target), {"audio": file_info(config, job_id, target)}
        elif stage == "recall":
            from mindex.search.search import SearchEngine
            from mindex.shared.types import Filters, TextSearch

            params = spec["parameters"]
            if not params["index_id"]:
                raise studio.StudioError("尚未发布索引")
            content = version(w, p["shots"][sid]["content"])["data"]
            recall = (
                studio.ShotRecall.model_validate(content["recall"]) if content.get("recall") else None
            )
            engine = SearchEngine(
                config, embedder, index_id=params["index_id"], release_id=params["person_release"]
            )
            mode = recall.mode if recall else "text"
            meta = {}
            # 分镜级覆盖优先，未设置时跟随方案数量与服务配置粒度。
            limit = content.get("recall_limit") or params["limit"]
            granularity = content.get("granularity") or config.studio.recall.granularity
            # 任务素材范围是硬上限，回显的 recall 仍是用户原始设置。
            scope = studio.material_scope(studio.load_job(config, job_id))
            if mode == "text":
                result = engine.text(
                    TextSearch(
                        text=content["query"],
                        granularity=granularity,
                        limit=limit,
                        person_ids=recall.person_ids if recall else [],
                        person_mode=recall.person_mode if recall else "auto",
                        person_relation=recall.person_relation if recall else "all",
                        filters=studio.scoped_filters(recall.filters if recall else Filters(), scope),
                    )
                )
                meta = {
                    key: result[key]
                    for key in (
                        "person_mode",
                        "person_ids",
                        "recognized_people",
                        "referenced_people",
                        "person_coverage_missing",
                        "person_version",
                        "score_type",
                    )
                    if key in result
                }
            else:
                search = engine.image if mode == "image" else engine.video
                result = search(
                    query_media(config, job_id, recall),
                    granularity=granularity,
                    limit=limit,
                    filters=studio.scoped_filters(recall.filters, scope),
                )
            entries = [
                {
                    **item,
                    "segment_id": item["id"],
                    "rank": rank,
                    "media_url": f"/api/admin/segments/{item['id']}/media",
                    "thumbnail_url": f"/api/admin/segments/{item['id']}/thumbnail",
                }
                for rank, item in enumerate(result["results"], 1)
            ]
            data = {
                "query": content["query"],
                "mode": mode,
                "recall": recall.model_dump() if recall else None,
                "meta": meta,
                "results": entries,
                "elapsed_ms": result["elapsed_ms"],
            }
            target = directory / "candidates.json"
            studio.save(target, data)
            files = {"candidates": file_info(config, job_id, target)}
        elif stage == "select":
            data = alignment(config, job_id, w, p, sid, p["ranges"].get(sid))
        elif stage == "render":
            data, files = render_node(config, job_id, w, p, sid, directory)
        elif stage == "compose":
            data, files = compose(config, job_id, w, p, directory)
        else:
            raise studio.StudioError("未知阶段")
        add_version(config, job_id, w, stage, sid, data=data, files=files, version_id=vid, **spec)
        if stage != "split":
            choose(w, p, stage, sid, vid)
        persist(config, job_id, w)
        return vid
    except Exception as exc:
        # Preserve partial files and diagnostics, but never select a failed version.
        diagnostics.record("error", {"type": type(exc).__name__, "message": str(exc)})
        partial = {
            f.relative_to(directory).as_posix(): file_info(config, job_id, f)
            for f in directory.rglob("*")
            if f.is_file()
        }
        add_version(
            config,
            job_id,
            w,
            stage,
            sid,
            status="failed",
            error=str(exc)[:2000],
            files=partial,
            version_id=vid,
            **spec,
        )
        persist(config, job_id, w)
        raise
    finally:
        current.reset(token)


def column_nodes(config, job_id, w, p, stage, inputs_by_shot):
    if stage == "split":
        if inputs_by_shot:
            raise studio.StudioError("整份文案拆分不接受逐镜输入")
        return [{"key": "split", "stage": "split", "shot_id": None, "action": "rebuild"}]
    ids = ordered_shots(w, p)
    if not ids or set(inputs_by_shot) != set(ids):
        raise studio.StudioError("列重生成必须绑定当前方案全部分镜")
    nodes = []
    for sid in ids:
        spec = specification(config, job_id, w, p, stage, sid)
        if inputs_by_shot[sid] != spec["inputs"]:
            raise studio.StudioError(f"分镜 {sid} 输入版本已变化，请刷新后重试")
        reason = None
        for dependency, vid in spec["inputs"].items():
            item = w["versions"].get(vid)
            if not item or item["status"] != "complete" or not valid_files(config, job_id, item):
                reason = f"依赖 {dependency} 缺失、失败或文件损坏"
                break
        if stage == "render" and reason is None:
            selection = version(w, spec["inputs"]["select"])
            if selection["inputs"].get("tts") != spec["inputs"]["tts"]:
                reason = "选片绑定的配音与当前选择不同，请先重新对齐"
        nodes.append(
            {
                "key": f"{stage}:{sid}",
                "stage": stage,
                "shot_id": sid,
                "action": "blocked" if reason else "rebuild",
                "reason": reason,
            }
        )
    return nodes


def run_build(config, job_id, build_id, *, speaker=None, storyboarder=None, embedder=None):
    log = ExecutionLog(config, job_id, build_id)
    token = active_log.set(log)
    try:
        return _run_build(
            config, job_id, build_id, log, speaker=speaker, storyboarder=storyboarder, embedder=embedder
        )
    finally:
        active_log.reset(token)


def _run_build(config, job_id, build_id, log, *, speaker=None, storyboarder=None, embedder=None):
    w = load(config, job_id)
    build = next(b for b in w["builds"] if b["id"] == build_id)
    p = plan(w, build["plan_id"])
    failed = set()
    log.emit("开始执行")
    for node in build["nodes"]:
        stage, sid = node["stage"], node["shot_id"]
        log.stage, log.shot_id = stage, sid
        deps = (
            ([f"tts:{sid}"] + ([f"recall:{sid}"] if needs_recall(p, sid) else []))
            if stage == "select"
            else (
                [f"select:{sid}", f"tts:{sid}"]
                if stage == "render"
                else ([f"render:{s}" for s in ordered_shots(w, p)] if stage == "compose" else [])
            )
        )
        if node["action"] == "skip":
            node["status"] = "skipped"
        elif node["action"] == "blocked" or any(dep in failed for dep in deps):
            node["status"] = "blocked"
            node["error"] = node.get("reason") or "依赖失败"
            failed.add(node["key"])
        else:
            node.update(status="running", started_at=studio.now())
            persist(config, job_id, w)
            log.emit(f"开始{LABELS[stage]}")
            try:
                found = None if build["force"] else match(config, job_id, w, p, stage, sid)
                if found:
                    choose(w, p, stage, sid, found)
                    node.update(status="reused", version=found)
                else:
                    vid = execute(
                        config,
                        job_id,
                        w,
                        p,
                        stage,
                        sid,
                        speaker=speaker,
                        storyboarder=storyboarder,
                        embedder=embedder,
                    )
                    node.update(status="complete", version=vid)
            except Exception as exc:  # noqa: BLE001 — failed nodes must not stop independent nodes
                node.update(status="failed", error=str(exc)[:2000])
                attempted = next(
                    (
                        v["id"]
                        for v in reversed(list(w["versions"].values()))
                        if v["stage"] == stage
                        and v["shot_id"] == sid
                        and v["status"] == "failed"
                        and v["created_at"] >= node["started_at"]
                    ),
                    None,
                )
                if attempted:
                    node["version"] = attempted
                failed.add(node["key"])
        if build.get("auto_split") and stage == "split":
            if node["status"] in ("complete", "reused"):
                checked = preflight(config, job_id, w, p, force=build["force"])
                build["nodes"][1:] = [{**n, "status": "pending"} for n in checked["nodes"]]
            else:
                for pending in build["nodes"][1:]:
                    pending.update(action="blocked", reason="拆分镜失败")
        node["finished_at"] = studio.now()
        persist(config, job_id, w)
        labels = {
            "complete": "完成",
            "reused": "复用成功版本",
            "failed": "失败",
            "blocked": "阻塞",
            "skipped": "跳过",
        }
        log.emit(
            f"{LABELS[stage]}：{labels[node['status']]}"
            + (f" · {node['error']}" if node.get("error") else ""),
            "error" if node["status"] == "failed" else ("warning" if node["status"] == "blocked" else "info"),
        )
    successes = any(n["status"] in ("complete", "reused") for n in build["nodes"])
    build.update(
        status=("partial" if successes else "failed") if failed else "complete", finished_at=studio.now()
    )
    log.stage = log.shot_id = None
    log.emit(
        "执行结束：" + {"complete": "全部完成", "partial": "部分完成", "failed": "失败"}[build["status"]]
    )
    persist(config, job_id, w)


def recover(config, job_id, w):
    changed = False
    running = [b for b in w["builds"] if b["status"] == "running"]
    # A killed process cannot write a terminal version record. Recover its recorded
    # inputs and partial files without changing any plan selection.
    if running:
        for directory in (root(config, job_id) / "versions").glob("*"):
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or (directory / "version.json").exists():
                continue
            try:
                safe_path(root(config, job_id), directory.relative_to(root(config, job_id)).as_posix())
                record = json.loads((directory / "debug/0001-inputs.json").read_text())
                data = record["data"]
                node = next(
                    (
                        n
                        for b in running
                        for n in b["nodes"]
                        if n.get("status") == "running"
                        and n["stage"] == data["stage"]
                        and n["shot_id"] == data["shot_id"]
                        and record["at"] >= b["created_at"]
                    ),
                    None,
                )
                if node is None:
                    continue
                files = {
                    f.relative_to(directory).as_posix(): file_info(config, job_id, f)
                    for f in directory.rglob("*")
                    if f.is_file()
                }
                node["version"] = add_version(
                    config,
                    job_id,
                    w,
                    data["stage"],
                    data["shot_id"],
                    inputs=data["inputs"],
                    parameters=data["parameters"],
                    signature=data["signature"],
                    files=files,
                    status="failed",
                    error="服务重启中断执行",
                    version_id=directory.name,
                )
            except (OSError, ValueError, KeyError):
                continue
    for build in w["builds"]:
        if build["status"] == "running":
            for node in build["nodes"]:
                if node.get("status") in ("pending", "running"):
                    node.update(status="blocked", error="服务重启中断，请重新预检续建")
            build.update(status="partial", finished_at=studio.now())
            ExecutionLog(config, job_id, build["id"]).emit("服务中断，请重新提交执行", "error")
            changed = True
    if changed:
        persist(config, job_id, w)


def compact_version(item):
    """Keep editor/preview fields; diagnostics remain available on the version API."""
    data = item.get("data", {})
    def candidate(value):
        return {k: v for k, v in value.items() if k not in {"annotation", "matches", "channel_metadata"}}
    if item["stage"] == "compose":
        data = {k: v for k, v in data.items() if k != "graph"}
    elif item["stage"] == "recall":
        data = {**data, "results": [candidate(c) for c in data.get("results", [])]}
    elif item["stage"] == "select" and data.get("anchor"):
        data = {**data, "anchor": candidate(data["anchor"])}
    return {**item, "data": data, "compact": True}


def detail(config, job_id, w, *, store=None, compact=False):
    p = plan(w, w["active_plan"])
    context = DetailContext(store=store)
    view = {**w, "versions": {k: compact_version(v) for k, v in w["versions"].items()}} if compact else w
    result = copy.deepcopy(view)
    result["job"] = studio.load_job(config, job_id)
    result["revision"] = revision(p)
    result["tts_settings"] = {
        "backend": config.studio.tts.backend,
        "model": config.studio.tts.model,
        "default_voice": config.studio.tts.voice,
        "voice": effective_voice(config, p),
        "voice_mode": p["options"].get("voice_mode", "inherit"),
    }
    result["conflicts"] = {}
    # 分镜编辑器「跟随方案」粒度默认值展示；数量默认直接读方案 recall_limit。
    result["recall_granularity"] = config.studio.recall.granularity
    from mindex.studio.studio_window import anchor_conflict

    result["anchor_conflicts"] = {sid: error for sid in ordered_shots(w, p)
                                  if (error := anchor_conflict(w, p, sid))}
    for sid in ordered_shots(w, p):
        for stage in STAGES:
            vid = p["shots"][sid].get(stage)
            if vid:
                result["conflicts"][f"{stage}:{sid}"] = conflicts(
                    config, job_id, w, p, stage, sid, vid, context=context,
                )
    for item in result["versions"].values():
        for metadata in item["files"].values():
            from urllib.parse import quote

            metadata["url"] = f"/api/studio/jobs/{job_id}/artifacts/{quote(metadata['path'], safe='/')}"
    # Old recall artifacts gain matched-reference display metadata without rewriting versions.
    references = None
    for item in result["versions"].values():
        if item["stage"] != "recall":
            continue
        for candidate in item.get("data", {}).get("results", []):
            for evidence in candidate.get("person_evidence", []):
                if references is None:
                    from mindex.persons.person_processing import EvidenceReferences
                    references = EvidenceReferences(config, store or Store(config.data_dir))
                evidence["reference_url"] = references.url(evidence)
    return result
