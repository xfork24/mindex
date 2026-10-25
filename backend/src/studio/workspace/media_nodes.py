"""媒体节点：素材区间校验、选片对齐（素材区间与上传片段）、分镜渲染（含静态图片推拉与 AI 动画录制）与成片合成。"""

from __future__ import annotations

import copy
import math
import re
import wave
from fractions import Fraction

from mindex.shared.media import atomic_media, probe
from mindex.shared.shot_media import ShotMedia
from mindex.shared.storage import Store
from mindex.studio import studio, studio_select, studio_steps

from .plans import canvas_of, canvas_render, needs_recall
from .sources import content_source, plan_source, source_file, video_file
from .versions import file_info, file_path, ordered_shots, version


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


def clip_alignment(config, job_id, w, p, sid):
    """上传片段（clip）选片：从片段的人工起点（缺省 0）起取与配音等长的窗口。

    片段从起点起短于配音时冻结末帧补足（strict 对齐拒绝）；变速沿用方案 atempo（自动=不变速）。"""
    shot = version(w, p["shots"][sid]["content"])["data"]
    source = content_source(shot)
    path = source_file(config, job_id, source)
    data = _file_alignment(config, job_id, w, p, sid, path, {"input": path.name}, float(source.get("start") or 0),
                           "片段", "请调整起点、换用更长的片段或变速")
    data["clip"] = {"media_id": source["media_id"], **data.pop("window")}
    return data


def generated_alignment(config, job_id, w, p, sid):
    """AIGC 选片：在 generate 版本的 generated.mp4 内从 0 起取与配音等长的窗口；片段偏短时冻结末帧补足。"""
    vid = p["shots"][sid].get("generate")
    generated = w["versions"].get(vid)
    if not generated or generated["status"] != "complete":
        raise studio.StudioError("缺少成功的生成画面，请先执行生成画面")
    path = file_path(config, job_id, generated, "generated")
    data = _file_alignment(config, job_id, w, p, sid, path, {"version": vid, "file": "generated"}, 0.0,
                           "生成画面", "请改用更长的允许时长或变速")
    data["generated"] = {"version": vid, **data.pop("window")}
    return data


def _file_alignment(config, job_id, w, p, sid, path, ref, start, noun, advice):
    """文件区间选片的公共部分：从 ``start`` 起取与配音等长的窗口（start/end 为文件内时间）。"""
    from mindex.shared.media import MediaError

    selected = p["shots"][sid]
    speech = version(w, selected.get("tts"))
    if speech["status"] != "complete":
        raise studio.StudioError("缺少成功配音")
    file_path(config, job_id, speech, "audio")
    shot = version(w, selected["content"])["data"]
    try:
        info = probe(path)
    except MediaError as exc:
        raise studio.StudioError(f"{noun}无法解码：{exc}") from exc
    frame = 1 / config.studio.render.fps
    length = info["duration"]
    if start > length - frame:
        raise studio.StudioError(f"{noun}起点 {start:g} 秒超出{noun}时长 {length:.2f} 秒，请调整起点")
    target = speech["data"]["duration"]
    speed = p["options"]["atempo"] or 1.0
    render_duration = target / speed
    end = min(length, start + render_duration)
    pad = render_duration - (end - start)
    if p["options"]["alignment"] == "strict" and pad > frame:
        raise studio.StudioError(f"严格对齐要求{noun}从起点起覆盖配音时长，{advice}")
    label = "padded" if pad > frame else ("trimmed" if length - start > render_duration + frame else "nearest")
    warning = (
        f"分镜 {sid} 的{noun}从起点起比配音短 {pad:.2f} 秒，成片将冻结末帧补足" if pad > frame else None
    )
    event = studio.TimelineEvent(
        shot_id=sid,
        order=1,
        audio=studio_select.studio_audio(sid, target, speed, render_duration),
        video={
            "kind": "file",
            "ref": ref,
            "start": round(start, 3),
            "end": max(round(end, 3), round(start, 3) + 0.001),
            "alignment": label,
        },
        subtitle={"start": 0.0, "end": round(render_duration, 3), "text": shot["text"]},
        warning=warning,
    )
    event.audio.file = speech["files"]["audio"]["path"]
    return {
        "event": event.model_dump(),
        "range": None,
        "window": {"duration": length, "start": start, "end": end},
        "trim_seconds": max(0, length - end),
        "freeze_seconds": max(0, pad),
        "atempo": speed,
        "manual": False,
    }


def shot_frames(config, processed):
    """处理后配音的采样数与画面帧数：保证任意合成偏移下画面都容得下全部采样。"""
    with wave.open(str(processed), "rb") as audio:
        samples = audio.getnframes()
        if audio.getframerate() != 48000 or samples <= 0:
            raise studio.StudioError("处理后配音采样率无效")
    fps = Fraction(str(config.studio.render.fps))
    # Guarantee enough samples at *every* possible composition offset (round may round down).
    frames = math.ceil(Fraction(samples, 48000) * fps)
    while math.floor(Fraction(frames, 1) / fps * 48000) < samples:
        frames += 1
    return samples, frames, fps


def finish_render(config, job_id, p, directory, speech, event, ambient, picture, absolute_event=None):
    """公共收尾：处理配音（可叠加原声）→ 按采样数定帧 → 生成画面 → 合成带声预览。"""
    from mindex.studio.studio_render import process_shot_audio

    cfg = config.studio.audio.model_copy(update={"duck_gain": p["options"]["duck_gain"]})
    processed = directory / "processed.wav"
    process_shot_audio(file_path(config, job_id, speech, "audio"), ambient, event, cfg, processed)
    samples, frames, fps = shot_frames(config, processed)
    clip = directory / "video.mp4"
    picture(frames, clip)
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
    recorded = absolute_event or event
    return {"event": recorded.model_dump(), "frames": frames, "samples": samples, "fps": str(fps)}, {
        "video": file_info(config, job_id, clip),
        "audio": file_info(config, job_id, processed),
        "preview": file_info(config, job_id, preview),
    }


def image_event(config, w, p, sid, path):
    """静态图片分镜的时间线事件：画面时长 = 配音时长 / 方案变速（自动 = 不变速）。"""
    return still_event(config, w, p, sid, {"input": path.name})


def still_event(config, w, p, sid, ref):
    """没有选片阶段的文件画面（静态图片、AI 动画）的时间线事件：从 0 起、与配音等长。"""
    selected = p["shots"][sid]
    speech = version(w, selected.get("tts"))
    shot = version(w, selected["content"])["data"]
    target = speech["data"]["duration"]
    speed = p["options"]["atempo"] or 1.0
    render_duration = target / speed
    event = studio.TimelineEvent(
        shot_id=sid,
        order=1,
        audio=studio_select.studio_audio(sid, target, speed, render_duration),
        video={
            "kind": "file",
            "ref": ref,
            "start": 0.0,
            "end": max(round(render_duration, 3), 0.001),
            "alignment": "nearest",
        },
        subtitle={"start": 0.0, "end": round(render_duration, 3), "text": shot["text"]},
    )
    event.audio.file = speech["files"]["audio"]["path"]
    return event


def image_render(config, job_id, w, p, sid, directory, source):
    from mindex.studio.studio_render import still_shot

    speech = version(w, p["shots"][sid]["tts"])
    if speech["status"] != "complete":
        raise studio.StudioError("缺少成功配音")
    if source["kind"] == "aigc_image":
        # AI 生成图片：取 draw 版本的 image.png，之后与静态图片完全相同。
        drawn = w["versions"].get(p["shots"][sid].get("draw"))
        if not drawn or drawn["status"] != "complete":
            raise studio.StudioError("缺少成功的生成图片，请先执行生成图片")
        path = file_path(config, job_id, drawn, "image")
        event = still_event(config, w, p, sid, {"version": drawn["id"], "file": "image"})
    else:
        path = source_file(config, job_id, source)
        event = image_event(config, w, p, sid, path)
    render, fit = canvas_render(config, p)

    def picture(frames, output):
        still_shot(
            path, render, output, frames=frames, motion=source.get("motion", "zoom-in"),
            strength=source.get("strength", 0.3), fit=fit,
        )

    # 图片没有原声：ducking 方案下同样只有配音。
    return finish_render(config, job_id, p, directory, speech, event, None, picture)


def animation_render(config, job_id, w, p, sid, directory, capture=None):
    """AI 动画：在沙箱里按渲染帧数逐帧录制动画源码（animation.mp4），再按画幅规范化为 video.mp4。

    ``capture`` 缺省为 ``studio.animation_capture.capture``（测试可注入确定性替身）；动画没有原声。"""
    from mindex.studio import animation_capture
    from mindex.studio.studio_render import cut_shot

    selected = p["shots"][sid]
    speech = version(w, selected.get("tts"))
    if speech["status"] != "complete":
        raise studio.StudioError("缺少成功配音")
    animated = w["versions"].get(selected.get("animate"))
    if not animated or animated["status"] != "complete":
        raise studio.StudioError("缺少成功的动画源码，请先执行生成动画")
    html = file_path(config, job_id, animated, "animation").read_text(encoding="utf-8")
    event = still_event(config, w, p, sid, {"version": animated["id"], "file": "animation"})
    render, fit = canvas_render(config, p)
    fps = Fraction(str(render.fps))
    raw = directory / "animation.mp4"
    stats = {}

    def picture(frames, output):
        recorder = capture or animation_capture.capture
        stats.update(recorder(
            html, seconds=float(frames / fps), width=render.width, height=render.height, fps=render.fps,
            target=raw, max_seconds=config.studio.animator.max_capture_seconds, frames=frames,
        ) or {})
        window = event.model_copy(deep=True)
        window.video.start, window.video.end = 0.0, float(frames / fps)
        cut_shot(raw, window, render, output, frames=frames, fit=fit)

    data, files = finish_render(config, job_id, p, directory, speech, event, None, picture)
    files["animation"] = file_info(config, job_id, raw)
    data["capture"] = {"frames": stats.get("frames"), "blocked_requests": len(stats.get("blocked_requests") or []),
                       "wall_seconds": stats.get("wall_seconds")}
    return data, files


def render_node(config, job_id, w, p, sid, directory, *, capture=None):
    from mindex.studio.studio_render import cut_shot, extract_ambient

    source = plan_source(w, p, sid)
    if source and source["kind"] in ("image", "aigc_image"):
        return image_render(config, job_id, w, p, sid, directory, source)
    if source and source["kind"] == "animation":
        return animation_render(config, job_id, w, p, sid, directory, capture)
    selection = version(w, p["shots"][sid]["select"])
    speech = version(w, p["shots"][sid]["tts"])
    if selection["inputs"].get("tts") != speech["id"]:
        raise studio.StudioError("选片绑定的配音与当前配音不同，请先重新对齐")
    event = studio.TimelineEvent.model_validate(selection["data"]["event"])
    cfg = config.studio.audio.model_copy(update={"duck_gain": p["options"]["duck_gain"]})
    ducking = p["options"]["audio_mode"] == "ducking"
    ambient = None
    if event.video.kind == "file":
        # 文件区间（上传片段）：start/end 已是文件内时间，原声直接取自片段本身。
        media = video_file(config, job_id, w, event.video)
        absolute_event = None
        if ducking and probe(media)["audio"]:
            ambient = directory / "ambient.wav"
            extract_ambient(media, event, cfg, ambient)
    else:
        validate_range(config, selection["data"]["range"])
        store = Store(config.data_dir)
        absolute_event = event.model_copy(deep=True)
        media = ShotMedia(store).materialize(event.video.source_id, event.video.start, event.video.end)
        event.video.end -= event.video.start
        event.video.start = 0
        if ducking and probe(media)["audio"]:
            ambient = directory / "ambient.wav"
            raw_ambient = directory / "shot-audio.wav"
            ShotMedia(store).audio(absolute_event.video.source_id, raw_ambient, absolute_event.video.start,
                                   absolute_event.video.end - absolute_event.video.start)
            extract_ambient(raw_ambient, event, cfg, ambient)
    render, fit = canvas_render(config, p)

    def picture(frames, output):
        cut_shot(media, event, render, output, frames=frames, fit=fit)

    return finish_render(config, job_id, p, directory, speech, event, ambient, picture, absolute_event)


MUSIC_FADE_IN = 0.5
MUSIC_FADE_OUT = 1.5


def mix_music(config, job_id, w, p, directory, voice, boundaries, total_samples):
    """背景音乐段落 → 与配音等长的背景音轨（段内循环/截断、淡入淡出、按音量缩放，段外静音），
    再以配音为侧链自动压低后与配音混合。没有背景音乐段落时返回 None（成片音轨与旧版完全相同）。"""
    from . import music

    segments = music.segments(w, p)
    if not segments:
        return None
    spans = {item["shot_id"]: (item["start_sample"], item["end_sample"]) for item in boundaries}
    pieces, cursor = [], 0

    def silence(samples, index):
        target = directory / f"bgm-gap-{index}.wav"
        atomic_media(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-af", f"atrim=end_sample={samples}",
                      "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"], target)
        return target

    for index, segment in enumerate(segments, 1):
        start = spans[segment["shots"][0]][0]
        end = spans[segment["shots"][-1]][1]
        if start > cursor:
            pieces.append(silence(start - cursor, index))
        samples = end - start
        seconds = samples / 48000
        item = version(w, p["shots"][segment["cue"]]["music"])
        if item["status"] != "complete":
            raise studio.StudioError("背景音乐尚未成功生成，请先执行背景音乐")
        source = file_path(config, job_id, item, "music")
        volume = float(segment["music"].get("volume", 0.25))
        fade_out = min(MUSIC_FADE_OUT, seconds / 3)
        target = directory / f"bgm-{index}.wav"
        atomic_media(
            ["-stream_loop", "-1", "-i", str(source), "-vn", "-af",
             f"aresample=48000,volume={volume:.4f},afade=t=in:d={min(MUSIC_FADE_IN, seconds / 3):.3f},"
             f"apad=whole_len={samples},atrim=end_sample={samples},"
             f"afade=t=out:st={max(0.0, seconds - fade_out):.3f}:d={fade_out:.3f},asetpts=PTS-STARTPTS",
             "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"],
            target,
        )
        pieces.append(target)
        cursor = end
    if total_samples > cursor:
        pieces.append(silence(total_samples - cursor, len(segments) + 1))
    from mindex.studio.studio_render import concat_media

    bed = directory / "bgm.wav"
    concat_media(pieces, bed)
    mixed = directory / "audio-mixed.wav"
    atomic_media(
        ["-i", str(voice), "-i", str(bed), "-filter_complex",
         "[0:a]asplit=2[voice][key];"
         "[1:a][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[bed];"
         "[voice][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
         f"atrim=end_sample={total_samples}",
         "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"],
        mixed,
    )
    for piece in pieces:
        piece.unlink(missing_ok=True)
    return mixed


def compose(config, job_id, w, p, directory):
    from mindex.studio.studio_render import (
        build_srt,
        canvas_subtitle_style,
        check_font,
        concat_media,
        mux,
        validate_final,
    )

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
    bgm = mix_music(config, job_id, w, p, directory, audio, boundaries, total_samples)
    if bgm is not None:
        audio = bgm
    content = build_srt(timeline, boundaries)
    srt, vtt, final = directory / "final.srt", directory / "final.vtt", directory / "final.mp4"
    studio.save_bytes(srt, content.encode())
    studio.save_bytes(
        vtt, ("WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)).encode()
    )
    if timeline.burn:
        check_font(srt, config.studio.subtitle)
    render, _ = canvas_render(config, p)
    style = canvas_subtitle_style(canvas_of(p)[0], render.width, render.height, config.studio.subtitle)
    mux(video, audio, srt, final, config, burn=timeline.burn, style=style)
    seconds = float(total_frames / fps)
    validate_final(final, seconds, timeline.burn, config.studio.render.fps, size=(render.width, render.height))
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
