"""创作步骤编排：每步产物落盘、可断点续跑（同 pipeline 阶段模式）。

run_step 是统一入口：状态迁移全部委托 studio 状态机，
步骤体只负责产物生成；任何异常都会记录到 attempt 并置 failed。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import studio
from .adapters import ModelError, MultimodalEmbedder, Speaker, Storyboarder, make_storyboarder
from .config import Config
from .media import MediaError, audio_info
from .types import ShotDraft, TextSearch

_PUNCTUATION = re.compile(r"[\s，。！？；、：．.,!?;:()（）\[\]【】《》<>“”‘’\"'—…·-]+")


def coverage_warning(original: str, shot_texts: list[str]) -> str | None:
    """软校验：分镜拼接与原文重叠度过低时提示人工核对，不判失败。"""

    def normalize(value: str) -> str:
        return _PUNCTUATION.sub("", value)

    source, combined = normalize(original), normalize("".join(shot_texts))
    if not combined:
        return "分镜为空，请人工核对原文覆盖"
    ratio = sum(1 for char in combined if char in source) / len(combined)
    if ratio < 0.6:
        return f"分镜文本与原文重叠度约 {ratio:.0%}，请人工核对是否遗漏或改写内容"
    return None


def run_split(config: Config, job_id: str, state: dict, storyboarder: Storyboarder) -> dict:
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
    limit = state["options"]["recall_limit"]
    directory = studio.candidates_dir(config, job_id)
    empty: list[str] = []
    for shot in storyboard.shots:
        target = directory / f"{shot.id}.json"
        if target.is_file():
            cached = json.loads(target.read_text())
            # 检查点：查询文本与索引未变才可复用
            if cached.get("query") == shot.query and cached.get("index_id") == active["id"]:
                if cached.get("status") == "empty":
                    empty.append(shot.id)
                continue
        result = engine.text(
            TextSearch(text=shot.query, granularity=config.studio.recall.granularity, limit=limit)
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
                "media_url": item["media_url"],
                "thumbnail_url": item["thumbnail_url"],
            }
            for rank, item in enumerate(result["results"], 1)
        ]
        studio.save(
            target,
            {
                "schema": 1,
                "shot_id": shot.id,
                "query": shot.query,
                "index_id": active["id"],
                "retrieved_at": studio.now(),
                "status": "ok" if entries else "empty",
                "results": entries,
                "elapsed_ms": result["elapsed_ms"],
            },
        )
        if not entries:
            empty.append(shot.id)
    return {
        "shots": len(storyboard.shots),
        "recalled": len(storyboard.shots) - len(empty),
        "empty": empty,
        "index_id": active["id"],
    }


def run_tts(config: Config, job_id: str, state: dict, speaker: Speaker) -> dict:
    """逐镜配音：wav 存在且可探测时长即跳过（检查点），孤儿音频清理，audio.json 全量重建。"""
    storyboard = studio.load_storyboard(config, job_id)
    directory = studio.audio_dir(config, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    valid = {shot.id for shot in storyboard.shots}
    for existing in directory.glob("*.wav"):  # 人工删镜后的残留清理
        if existing.stem not in valid:
            existing.unlink()
    voice = state["options"]["voice"]
    manifest = {}
    for shot in storyboard.shots:
        target = directory / f"{shot.id}.wav"
        if target.is_file():
            try:
                info = audio_info(target)
            except MediaError:
                target.unlink(missing_ok=True)  # 损坏文件重新生成
            else:
                manifest[shot.id] = {
                    "file": f"audio/{shot.id}.wav",
                    "duration": round(info["duration"], 3),
                    "bytes": target.stat().st_size,
                }
                continue
        payload = speaker.speak(shot.text, voice)
        if not payload:
            raise ModelError(f"分镜 {shot.id} 配音返回空音频，请检查 TTS 服务")
        studio.save_bytes(target, payload)
        info = audio_info(target)
        manifest[shot.id] = {
            "file": f"audio/{shot.id}.wav",
            "duration": round(info["duration"], 3),
            "bytes": target.stat().st_size,
        }
    studio.save(
        studio.job_dir(config, job_id) / "audio.json",
        {"schema": 1, "voice": voice, "shots": manifest},
    )
    return {
        "shots": len(manifest),
        "total_seconds": round(sum(item["duration"] for item in manifest.values()), 2),
    }


def _selection_callbacks(store):
    """选片所需的原片信息回调：相邻 shot 查询与原片时长（segments_timeline 索引）。"""

    def source_duration(source_id: str) -> float | None:
        row = store.one("SELECT media FROM sources WHERE id=?", (source_id,))
        return float(json.loads(row["media"])["duration"]) if row else None

    def extend_next(source_id: str, end: float) -> dict | None:
        # 结束时间在 end 之后、且与 end 连续（或覆盖 end）的最早 shot
        return store.one(
            "SELECT id,start,end FROM segments WHERE source_id=? AND kind='shot' AND end > ? + 1e-6 "
            "ORDER BY end ASC LIMIT 1",
            (source_id, end),
        )

    def extend_prev(source_id: str, start: float) -> dict | None:
        # 开始时间在 start 之前、且与 start 连续（或覆盖 start）的最晚 shot
        return store.one(
            "SELECT id,start,end FROM segments WHERE source_id=? AND kind='shot' AND start < ? - 1e-6 "
            "ORDER BY start DESC LIMIT 1",
            (source_id, start),
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

    source_duration, extend_next, extend_prev, covered_shots = _selection_callbacks(Store(config.data_dir))
    candidates = {}
    for shot in storyboard.shots:
        document = json.loads((studio.candidates_dir(config, job_id) / f"{shot.id}.json").read_text())
        candidates[shot.id] = document.get("results", [])
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
    current = studio.load_timeline(config, job_id)
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
                "source_name": "",
                "score": 0.0,
                "rank": 0,
            }
            override = "manual"
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
        audio_mode=audio_mode or current.audio_mode,
        burn=current.burn if burn is None else burn,
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
        return store.resolve_media(row["path"])

    studio_render.render_timeline(
        config,
        timeline,
        voice_lookup=voice_lookup,
        source_lookup=source_lookup,
        workdir=studio.render_dir(config, job_id),
    )
    return {
        "events": len(timeline.events),
        "total_seconds": round(timeline.total_seconds, 2),
        "final": "render/final.mp4",
        "srt": "render/final.srt",
    }


def run_step(
    config: Config,
    job_id: str,
    step: str,
    *,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
    embedder: MultimodalEmbedder | None = None,
) -> dict:
    state = studio.begin_step(config, job_id, step)
    try:
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
    except Exception as exc:
        studio.fail_step(config, job_id, step, exc)
        raise
    studio.complete_step(config, job_id, step, result)
    return result
