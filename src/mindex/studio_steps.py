"""创作步骤编排：每步产物落盘、可断点续跑（同 pipeline 阶段模式）。

run_step 是统一入口：状态迁移全部委托 studio 状态机，
步骤体只负责产物生成；任何异常都会记录到 attempt 并置 failed。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import studio
from .adapters import ModelError, MultimodalEmbedder, Speaker, Storyboarder, make_storyboarder
from .config import Config, fingerprint, portable_parameters
from .media import MediaError, audio_info
from .shot_media import ShotMedia
from .studio_artifacts import ArtifactSession, current, digest, visibility_key
from .types import Filters, ShotDraft, TextSearch

coverage_warning = studio.coverage_warning


def run_split(config: Config, job_id: str, state: dict, storyboarder: Storyboarder) -> dict:
    session = current.get()
    split_key = fingerprint(
        {
            "text": state["input_text"],
            "generator": portable_parameters(config, config.studio.generator.model_dump()),
            "split_version": studio.SPLIT_VERSION,
        }
    )
    cached = session.reuse("storyboard.json", split_key) if session else None
    if cached:
        board = studio.load_storyboard(config, job_id)
        return {
            "shots": len(board.shots),
            "coverage_warning": board.coverage_warning,
            "estimated_seconds": sum(s.estimated_seconds for s in board.shots),
        }
    drafts: list[ShotDraft] = storyboarder.split(state["input_text"])
    if not drafts:
        raise ModelError("拆分镜结果为空，请检查文案后重试")
    if len(drafts) > config.studio.max_shots:
        raise ModelError(f"分镜数量 {len(drafts)} 超过上限 {config.studio.max_shots}，请精简文案")
    shots = [
        studio.Shot(
            id=f"shot-{index:02d}",
            text=draft.text.strip(),
            query=draft.query.strip(),
            estimated_seconds=draft.estimated_seconds,
        )
        for index, draft in enumerate(drafts, 1)
    ]
    storyboard = studio.Storyboard(
        job_id=job_id,
        shots=shots,
        coverage_warning=coverage_warning(state["input_text"], [shot.text for shot in shots]),
    )
    if storyboard.coverage_warning:
        raise ModelError(storyboard.coverage_warning)
    if session:
        session.json(
            "storyboard.json", {"schema": 1, **storyboard.model_dump(), "updated_at": studio.now()}, split_key
        )
    else:
        studio.write_storyboard(config, job_id, storyboard)
    return {
        "shots": len(shots),
        "estimated_seconds": round(sum(shot.estimated_seconds for shot in shots), 2),
        "coverage_warning": storyboard.coverage_warning,
    }


def run_recall(config: Config, job_id: str, state: dict, embedder: MultimodalEmbedder | None) -> dict:
    """逐镜语义召回：固定步骤开始时的活动索引，逐镜检查点，空候选显式标记不判失败。"""
    from .search import SearchEngine
    from .storage import Store

    storyboard = studio.load_storyboard(config, job_id)
    store = Store(config.data_dir)
    active = store.active_index()
    if active is None:
        raise studio.StudioError("尚未发布索引，请先完成离线处理并执行 index --activate")
    engine = SearchEngine(config, embedder, index_id=active["id"])
    directory = studio.candidates_dir(config, job_id)
    empty: list[str] = []
    session = current.get()
    visibility = visibility_key(store)
    scope = studio.material_scope(state)
    for shot in storyboard.shots:
        # 分镜级覆盖优先，未设置时跟随方案数量与服务配置粒度。
        limit = shot.recall_limit or state["options"]["recall_limit"]
        granularity = shot.granularity or config.studio.recall.granularity
        target = directory / f"{shot.id}.json"
        logical = f"candidates/{shot.id}.json"
        signature = {
            "query": shot.query,
            "index_id": active["id"],
            "limit": limit,
            "granularity": granularity,
            "search": config.search.model_dump(),
            "embedding": portable_parameters(config, config.embedding.model_dump()),
            "visibility": visibility,
        }
        # 只在有限制时加键，保证不限任务的检查点与历史产物逐字节一致。
        if scope:
            signature["sources"] = sorted(scope)
        key = fingerprint(signature)
        cached_path = session.reuse(logical, key) if session else None
        if cached_path:
            cached = json.loads(cached_path.read_text())
            if cached.get("status") == "empty":
                empty.append(shot.id)
            continue
        result = engine.text(
            TextSearch(
                text=shot.query,
                granularity=granularity,
                limit=limit,
                filters=studio.scoped_filters(Filters(), scope),
            )
        )
        entries = [
            {
                "segment_id": item["id"],
                "source_id": item["source_id"],
                "source_name": item.get("source_name", ""),
                "kind": item.get("kind", "shot"),
                "start": item["start"],
                "end": item["end"],
                "duration": round(item["end"] - item["start"], 3),
                "score": item["score"],
                "rank": rank,
                "media_url": f"/api/admin/segments/{item['id']}/media",
                "thumbnail_url": f"/api/admin/segments/{item['id']}/thumbnail",
            }
            for rank, item in enumerate(result["results"], 1)
        ]
        document = {
            "schema": 1,
            "shot_id": shot.id,
            "query": shot.query,
            "index_id": active["id"],
            "retrieved_at": studio.now(),
            "status": "ok" if entries else "empty",
            "results": entries,
            "elapsed_ms": result["elapsed_ms"],
        }
        if session:
            session.json(logical, document, key)
        else:
            studio.save(target, document)
        if not entries:
            empty.append(shot.id)
    return {
        "shots": len(storyboard.shots),
        "recalled": len(storyboard.shots) - len(empty),
        "empty": empty,
        "index_id": active["id"],
    }


def run_tts(config: Config, job_id: str, state: dict, speaker: Speaker) -> dict:
    """按文本、声音和模型指纹复用配音，历史音频留存，清单只包含当前分镜。"""
    storyboard = studio.load_storyboard(config, job_id)
    directory = studio.audio_dir(config, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    session = current.get()
    voice = state["options"]["voice"]
    manifest = {}
    for shot in storyboard.shots:
        logical = f"audio/{shot.id}.wav"
        key = fingerprint({"text": shot.text, "voice": voice, "tts": portable_parameters(config, config.studio.tts.model_dump())})
        target = session.reuse(logical, key) if session else None
        if target:
            try:
                info = audio_info(target)
            except MediaError:
                target = None
        if target is None:
            target = session.target(logical) if session else directory / f"{shot.id}.wav"
            payload = speaker.speak(shot.text, voice)
            if not payload:
                raise ModelError(f"分镜 {shot.id} 配音返回空音频，请检查 TTS 服务")
            studio.save_bytes(target, payload)
            info = audio_info(target)
            if session:
                session.publish(logical, target, key, duration=info["duration"])
        manifest[shot.id] = {
            "file": logical,
            "duration": info["duration"],
            "bytes": target.stat().st_size,
            "sha256": digest(target),
            "key": key,
        }
    document = {"schema": 1, "voice": voice, "shots": manifest}
    if session:
        session.json("audio.json", document)
    else:
        studio.save(studio.job_dir(config, job_id) / "audio.json", document)
    return {
        "shots": len(manifest),
        "total_seconds": round(sum(item["duration"] for item in manifest.values()), 2),
    }


def _selection_callbacks(store):
    """选片所需的原片信息回调：相邻 shot 查询与原片时长（segments_timeline 索引）。"""

    from .curation import Library

    overlays = Library(store).overlays()

    def source_duration(source_id: str) -> float | None:
        if store.one("SELECT id FROM source_curation WHERE id=? AND hidden=1", (source_id,)):
            return None
        row = store.one("SELECT media FROM sources WHERE id=?", (source_id,))
        return float(json.loads(row["media"])["duration"]) if row else None

    def extend_next(source_id: str, end: float) -> dict | None:
        # 结束时间在 end 之后、且与 end 连续（或覆盖 end）的最早 shot
        row = store.one(
            "SELECT id,start,end FROM segments WHERE source_id=? AND kind='shot' AND end > ? + 1e-6 "
            "ORDER BY end ASC LIMIT 1",
            (source_id, end),
        )
        return (
            row
            if row and interval_visible(store, source_id, row["start"], row["end"], overlays=overlays)
            else None
        )

    def extend_prev(source_id: str, start: float) -> dict | None:
        # 开始时间在 start 之前、且与 start 连续（或覆盖 start）的最晚 shot
        row = store.one(
            "SELECT id,start,end FROM segments WHERE source_id=? AND kind='shot' AND start < ? - 1e-6 "
            "ORDER BY start DESC LIMIT 1",
            (source_id, start),
        )
        return (
            row
            if row and interval_visible(store, source_id, row["start"], row["end"], overlays=overlays)
            else None
        )

    def covered_shots(source_id: str, start: float, end: float) -> list[str]:
        return [
            row["id"]
            for row in store.rows(
                "SELECT id FROM segments WHERE source_id=? AND kind='shot' "
                "AND start < ? - 1e-6 AND end > ? + 1e-6 ORDER BY start",
                (source_id, end, start),
            )
        ]

    return source_duration, extend_next, extend_prev, covered_shots


def load_audio_manifest(config: Config, job_id: str, storyboard) -> dict:
    """读取配音时长清单并校验覆盖全部分镜。"""
    manifest_file = studio.job_dir(config, job_id) / "audio.json"
    if not manifest_file.is_file():
        raise studio.StudioError("尚未生成配音时长清单，请先执行生成配音")
    manifest = json.loads(manifest_file.read_text())["shots"]
    missing = [shot.id for shot in storyboard.shots if shot.id not in manifest]
    if missing:
        raise studio.StudioError(f"分镜 {'、'.join(missing)} 缺少配音，请重跑生成配音")
    state = studio.load_job(config, job_id)
    for shot in storyboard.shots:
        item = manifest[shot.id]
        expected = fingerprint(
            {"text": shot.text, "voice": state["options"]["voice"], "tts": portable_parameters(config, config.studio.tts.model_dump())}
        )
        path = studio.audio_dir(config, job_id) / f"{shot.id}.wav"
        if item.get("key") != expected or not path.is_file() or digest(path) != item.get("sha256"):
            raise studio.StudioError(f"分镜 {shot.id} 的配音已失效，请重跑生成配音")
    return manifest


def run_select(config: Config, job_id: str, state: dict) -> dict:
    """在已知音频时长的约束下选片：候选打分 + 同源相邻延长 + atempo 钳制。"""
    from . import studio_select
    from .storage import Store

    storyboard = studio.load_storyboard(config, job_id)
    manifest = load_audio_manifest(config, job_id, storyboard)
    missing_candidates = [
        shot.id
        for shot in storyboard.shots
        if not (studio.candidates_dir(config, job_id) / f"{shot.id}.json").is_file()
    ]
    if missing_candidates:
        raise studio.StudioError(f"分镜 {'、'.join(missing_candidates)} 缺少召回候选，请重跑语义召回")

    store = Store(config.data_dir)
    from .curation import Library

    overlays = Library(store).overlays()
    source_duration, extend_next, extend_prev, covered_shots = _selection_callbacks(store)
    candidates = {}
    for shot in storyboard.shots:
        document = json.loads((studio.candidates_dir(config, job_id) / f"{shot.id}.json").read_text())
        candidates[shot.id] = [
            item
            for item in document.get("results", [])
            if interval_visible(store, item["source_id"], item["start"], item["end"], overlays=overlays)
        ]
    audio = {shot_id: item["duration"] for shot_id, item in manifest.items()}
    events, warnings = studio_select.select_events(
        [{"id": shot.id, "text": shot.text} for shot in storyboard.shots],
        candidates,
        audio,
        config.studio.selection,
        source_duration=source_duration,
        extend_next=extend_next,
        extend_prev=extend_prev,
        covered_shots=covered_shots,
    )
    timeline = studio.Timeline(
        job_id=job_id,
        mode="auto",
        audio_mode=state["options"]["audio_mode"],
        burn=state["options"]["burn"],
        events=events,
    )
    session = current.get()
    if session:
        session.json(
            "timeline.json",
            {"schema": 1, **timeline.model_dump(), "updated_at": studio.now()},
            fingerprint(
                {
                    "storyboard": storyboard.model_dump(),
                    "candidates": candidates,
                    "audio": manifest,
                    "selection": config.studio.selection.model_dump(),
                    "visibility": visibility_key(Store(config.data_dir)),
                }
            ),
        )
    else:
        studio.write_timeline(config, job_id, timeline)
    return {
        "events": len(events),
        "total_seconds": round(timeline.total_seconds, 2),
        "warnings": warnings,
    }


def apply_timeline_patch(
    config: Config,
    job_id: str,
    patches: list[dict],
    *,
    audio_mode: str | None = None,
    burn: bool | None = None,
) -> studio.Timeline:
    """人工换选/微调：换选限候选池内，显式区间 clamp 后同一公式重算 atempo 与警告。"""
    from . import studio_select
    from .storage import Store

    storyboard = studio.load_storyboard(config, job_id)
    manifest = load_audio_manifest(config, job_id, storyboard)
    current_timeline = studio.load_timeline(config, job_id)
    patch_ids = sorted(patch["shot_id"] for patch in patches)
    if patch_ids != sorted(shot.id for shot in storyboard.shots):
        raise studio.StudioError("换选必须覆盖全部分镜，每个分镜恰好一次")

    source_duration, extend_next, extend_prev, covered_shots = _selection_callbacks(Store(config.data_dir))
    selection = config.studio.selection
    text_by_id = {shot.id: shot.text for shot in storyboard.shots}
    events: list[studio.TimelineEvent] = []
    warnings: list[str] = []
    for order, patch in enumerate(patches, 1):
        shot_id = patch["shot_id"]
        target = manifest[shot_id]["duration"]
        video = patch.get("video") or {}
        prior = next(e for e in current_timeline.events if e.shot_id == shot_id)
        if video.get("segment_id") == prior.video.segment_id:
            if not interval_visible(
                Store(config.data_dir), prior.video.source_id, prior.video.start, prior.video.end
            ):
                raise studio.StudioError("所选区间包含隐藏素材，请重新选片")
            events.append(prior.model_copy(update={"order": order}))
            continue
        if video.get("segment_id"):
            document = json.loads((studio.candidates_dir(config, job_id) / f"{shot_id}.json").read_text())
            anchor = next(
                (item for item in document.get("results", []) if item["segment_id"] == video["segment_id"]),
                None,
            )
            if anchor is None:
                raise studio.StudioError(f"分镜 {shot_id} 的候选池中没有片段 {video['segment_id']}")
            plan = studio_select.plan_anchor(
                anchor,
                target,
                selection,
                source_duration=source_duration,
                extend_next=extend_next,
                extend_prev=extend_prev,
            )
            if plan is None:
                raise studio.StudioError(f"分镜 {shot_id} 换选的候选无法对齐，请选择其他候选")
            override = None
        else:
            source_id, start, end = video.get("source_id"), video.get("start"), video.get("end")
            if not source_id or start is None or end is None:
                raise studio.StudioError(f"分镜 {shot_id} 的人工区间需要 source_id、start、end")
            duration = source_duration(source_id)
            if not duration:
                raise studio.StudioError(f"素材 {source_id} 已不在库中，请重跑召回与选片")
            plan = studio_select.plan_explicit(
                source_id, float(start), float(end), target, selection, source_duration=duration
            )
            plan["anchor"] = {
                **plan["anchor"],
                "segment_id": f"{source_id}@{start:.3f}-{end:.3f}",
                "source_name": Store(config.data_dir).source_names()[source_id],
                "score": 0.0,
                "rank": 0,
            }
            override = "manual"
        if not interval_visible(Store(config.data_dir), plan["source_id"], plan["start"], plan["end"]):
            raise studio.StudioError("所选区间包含隐藏素材，请选择其他区间")
        events.append(
            studio_select.build_event(
                {"id": shot_id, "text": text_by_id[shot_id]},
                order,
                plan,
                target,
                selection,
                covered_shots,
                warnings,
                alignment_override=override,
            )
        )
    timeline = studio.Timeline(
        job_id=job_id,
        mode="manual",
        audio_mode=audio_mode or current_timeline.audio_mode,
        burn=current_timeline.burn if burn is None else burn,
        events=events,
    )
    studio.save_timeline(config, job_id, timeline)  # 保持 select 完成态，仅作废渲染
    return timeline


def run_render(config: Config, job_id: str, state: dict) -> dict:
    """三轨合成：timeline 是纯函数输入，重渲染不依赖任何模型服务。"""
    from . import studio_render
    from .storage import Store

    timeline = studio.load_timeline(config, job_id)
    store = Store(config.data_dir)

    def voice_lookup(shot_id: str) -> Path:
        path = studio.job_dir(config, job_id) / "audio" / f"{shot_id}.wav"
        if not path.is_file():
            raise studio.StudioError(f"分镜 {shot_id} 缺少配音文件，请重跑生成配音")
        return path

    def source_lookup(source_id: str) -> Path:
        row = store.one("SELECT path FROM sources WHERE id=?", (source_id,))
        if not row:
            raise studio.StudioError(f"素材 {source_id} 已不在库中，请重跑召回与选片")
        return ShotMedia(store).materialize(source_id)

    for event in timeline.events:
        if not interval_visible(store, event.video.source_id, event.video.start, event.video.end):
            raise studio.StudioError(f"分镜 {event.shot_id} 区间包含隐藏素材，请重新选片")
    rendered = studio_render.render_timeline(
        config,
        timeline,
        voice_lookup=voice_lookup,
        source_lookup=source_lookup,
        workdir=studio.render_dir(config, job_id),
        interval_lookup=ShotMedia(store).materialize,
        audio_lookup=lambda sid, start, end, output: ShotMedia(store).audio(sid, output, start, end - start),
    )
    return {
        "events": len(timeline.events),
        "total_seconds": rendered["total_seconds"],
        "version": state["steps"]["render"]["attempt"],
        "final": "render/final.mp4",
        "srt": "render/final.srt",
        "vtt": "render/final.vtt",
    }


def run_step(
    config: Config,
    job_id: str,
    step: str,
    *,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
    embedder: MultimodalEmbedder | None = None,
    mode: str = "resume",
    shot_ids: list[str] | None = None,
    started: bool = False,
) -> dict:
    state = studio.load_job(config, job_id) if started else studio.begin_step(config, job_id, step)
    session = None
    token = None
    try:
        session = ArtifactSession(config, job_id, step, state, mode=mode, shot_ids=shot_ids)
        token = current.set(session)
        if step == "split":
            result = run_split(config, job_id, state, storyboarder or make_storyboarder(config))
        elif step == "recall":
            result = run_recall(config, job_id, state, embedder)
        elif step == "tts":
            from .adapters import make_speaker

            result = run_tts(config, job_id, state, speaker or make_speaker(config))
        elif step == "select":
            result = run_select(config, job_id, state)
        elif step == "render":
            result = run_render(config, job_id, state)
        else:
            raise studio.StudioError(f"步骤 {studio.STEP_LABELS.get(step, step)} 尚未实现")
        session.finish()
        if step == "render":
            fresh = studio.load_job(config, job_id)
            fresh["published_render"] = {
                "version": state["steps"][step]["attempt"],
                "files": {
                    k: v["path"]
                    for k, v in session.outputs.items()
                    if k in {"render/final.mp4", "render/final.srt", "render/final.vtt"}
                },
            }
            studio.save(studio.job_path(config, job_id), fresh)
        studio.complete_step(config, job_id, step, result)
        return result
    except Exception as exc:
        try:
            if session:
                session.record("error", {"type": type(exc).__name__, "message": str(exc)})
                session.finish()
        finally:
            if studio.load_job(config, job_id)["steps"][step]["status"] == "running":
                studio.fail_step(config, job_id, step, exc)
        raise
    finally:
        if token is not None:
            current.reset(token)


def interval_visible(store, source_id, start, end, *, overlays=None):
    from .curation import Library

    sources, segments = overlays if overlays is not None else Library(store).overlays()
    if sources.get(source_id, {}).get("hidden"):
        return False
    for row in store.rows(
        "SELECT id,parent_id FROM segments WHERE source_id=? AND start < ? AND end > ?",
        (source_id, end - 1e-6, start + 1e-6),
    ):
        item = segments.get(row["id"], {})
        parent = segments.get(row["parent_id"], {})
        if item.get("hidden") or parent.get("hidden"):
            return False
    return True


def hidden_intervals(store, source_id, *, overlays=None):
    """该源内全部不可见区间的列表，判定规则与 interval_visible 逐点一致。

    segment 自身或其 parent 被隐藏即整段不可见；parent 可能属于别的源，隐藏状态
    取自全库 curation。预查一次供逐行内存比对，避免每个区间都查一次库。
    """
    from .curation import Library

    _, segments = overlays if overlays is not None else Library(store).overlays()
    return [
        (row["start"], row["end"])
        for row in store.rows("SELECT id,parent_id,start,end FROM segments WHERE source_id=?", (source_id,))
        if segments.get(row["id"], {}).get("hidden") or segments.get(row["parent_id"], {}).get("hidden")
    ]


def range_visible(hidden, start, end):
    """区间是否避开全部不可见区间；容差与 interval_visible 的重叠判定完全一致。"""
    return not any(low < end - 1e-6 and high > start + 1e-6 for low, high in hidden)
