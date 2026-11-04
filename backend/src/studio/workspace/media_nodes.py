"""媒体节点：素材区间校验、选片对齐（素材区间与上传片段）、分镜渲染（含静态图片推拉与 AI 动画录制）。

成片合成见 ``post``。"""

from __future__ import annotations

import logging
import math
import wave
from fractions import Fraction

from mindex.shared.media import atomic_media, probe
from mindex.shared.shot_media import ShotMedia
from mindex.shared.storage import Store
from mindex.studio import studio, studio_select, studio_steps

from . import transitions
from .plans import canvas_render, needs_recall
from .sources import content_source, plan_source, source_file, video_file
from .versions import file_info, file_path, version

log = logging.getLogger(__name__)


def source_shots(config, source_id, *, overlays=None, store=None):
    """源内全部镜头及可见性；overlays 可由调用方预先取好，避免逐镜头全表扫描。"""
    from mindex.admin.curation import Library

    store = store if store is not None else Store.open(config)
    if overlays is None:
        overlays = Library(store).overlays()
    # 可见性一次性预查后逐行内存比对，避免每个镜头一次区间查询。
    source_hidden = bool(overlays[0].get(source_id, {}).get("hidden"))
    rows = store.rows("SELECT id,source_id,parent_id,kind,start,end FROM segments WHERE source_id=? ORDER BY start",
                      (source_id,))
    hidden = studio_steps.hidden_intervals(store, source_id, overlays=overlays, rows=rows)
    return [
        {
            **{key: row[key] for key in ("id", "source_id", "start", "end")},
            "visible": not source_hidden
            and studio_steps.range_visible(hidden, row["start"], row["end"]),
        }
        for row in rows if row["kind"] == "shot"
    ]


def validate_range(config, selected_range, *, overlays=None):
    from mindex.admin.curation import Library

    source_id = selected_range["source_id"]
    start, end = selected_range["start"], selected_range["end"]
    if not all(math.isfinite(v) for v in (start, end)) or end - start < 0.04:
        raise studio.StudioError("所选区间不足一帧或边界无效")
    store = Store.open(config)
    if overlays is None:
        overlays = Library(store).overlays()
    rows = source_shots(config, source_id, overlays=overlays, store=store)
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
    store = Store.open(config)
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


def tool_alignment(config, job_id, w, p, sid):
    """第三方工具选片：在 invoke 版本的视频产物内从 0 起取与配音等长的窗口；产物偏短时冻结末帧补足。"""
    vid = p["shots"][sid].get("invoke")
    made = w["versions"].get(vid)
    if not made or made["status"] != "complete":
        raise studio.StudioError("缺少成功的第三方工具产物，请先执行调用第三方工具")
    path = file_path(config, job_id, made, "artifact")
    data = _file_alignment(config, job_id, w, p, sid, path, {"version": vid, "file": "artifact"}, 0.0,
                           "工具产物", "请让工具生成更长的视频或调整变速")
    data["tool"] = {"version": vid, **data.pop("window")}
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


def html_layers(config, job_id, w, p, sid) -> list[dict]:
    """分镜的 HTML 文字层：叠加层源码与相对镜头的起止（None = 从头 / 到尾）；没有时为空。"""
    from .stages import html_overlays

    content = version(w, p["shots"][sid]["content"])["data"]
    found = html_overlays(content)
    if not found:
        return []
    made = w["versions"].get(p["shots"][sid].get("overlay"))
    if not made or made["status"] != "complete":
        raise studio.StudioError("缺少成功的叠加层源码，请先执行生成叠加层")
    return [{"index": index, "start": layer.get("start"), "end": layer.get("end"),
             "html": file_path(config, job_id, made, f"overlay-{index}").read_text(encoding="utf-8")}
            for index, layer in found]


def composite_overlays(config, p, layers, source, output, frames, directory, capture=None) -> dict:
    """透明录制每个 HTML 文字层（时长 = 该层时间窗），按起点叠加到分镜画面上；帧数不变。"""
    from mindex.studio import animation_capture

    from .handlers.overlay import layer_window

    render, _ = canvas_render(config, p)
    fps = Fraction(str(render.fps))
    seconds = float(frames / fps)
    recorder = capture or animation_capture.capture
    args, graph, current, stats = ["-i", str(source)], [], "0:v", []
    for position, layer in enumerate(layers, 1):
        start, end = layer_window(layer, seconds)
        count = max(1, round((end - start) * float(fps)))
        target = directory / f"overlay-{layer['index']}.mov"
        stats.append(recorder(layer["html"], seconds=count / float(fps), width=render.width, height=render.height,
                              fps=render.fps, target=target, max_seconds=config.studio.animator.max_capture_seconds,
                              frames=count, transparent=True) or {})
        args += ["-i", str(target)]
        graph.append(f"[{position}:v]format=rgba,setpts=PTS-STARTPTS+{start:.6f}/TB[l{position}]")
        graph.append(f"[{current}][l{position}]overlay=0:0:eof_action=pass:format=auto[v{position}]")
        current = f"v{position}"
    graph.append(f"[{current}]format=yuv420p[out]")
    atomic_media([*args, "-filter_complex", ";".join(graph), "-map", "[out]", "-an", "-frames:v", str(frames),
                  "-c:v", "libx264", "-preset", render.preset, "-crf", str(render.crf), "-pix_fmt", "yuv420p",
                  "-movflags", "+faststart"], output, timeout=max(600, seconds * 10))
    for layer in layers:
        (directory / f"overlay-{layer['index']}.mov").unlink(missing_ok=True)
    return {"layers": len(layers), "frames": [item.get("frames") for item in stats]}


def finish_render(config, job_id, p, directory, speech, event, ambient, picture, absolute_event=None, *,
                  transition=None, overlays=None, capture=None, handle_seconds=None):
    """公共收尾：处理配音（可叠加原声）→ 按采样数定帧 → 生成画面（含转场余量）→ 镜内转场 → 合成带声预览。

    ``transition`` 为 ``transitions.effective`` 的结果：``next`` 让画面多出尾部余量帧（``handle_frames``，
    供成片重叠转场消耗），``enter``/``leave`` 在画面上叠加镜内转场；``overlays`` 为 ``html_layers`` 的结果，
    透明录制后叠加在画面上（先于镜内转场，文字随画面一起淡入淡出）。都没有时产物与旧版完全相同。
    ``handle_seconds`` 为尾部余量（重叠转场与节拍对齐取较大值，见 ``render_handle``）；None 时按 ``next`` 时长。"""
    from mindex.studio.studio_render import apply_edges, process_shot_audio

    from .transitions import clamp_edges

    transition = transition or {}
    cfg = config.studio.audio.model_copy(update={"duck_gain": p["options"]["duck_gain"]})
    processed = directory / "processed.wav"
    process_shot_audio(file_path(config, job_id, speech, "audio"), ambient, event, cfg, processed)
    samples, frames, fps = shot_frames(config, processed)
    if handle_seconds is None:
        handle_seconds = transition["next"]["duration"] if transition.get("next") else 0
    handle = round(Fraction(str(handle_seconds)) * fps) if handle_seconds else 0
    enter, leave, notes = clamp_edges(transition, float(frames / fps))
    clip = directory / "video.mp4"
    steps = [name for name, wanted in (("overlay", overlays), ("edges", enter or leave)) if wanted]
    current = directory / "video-raw.mp4" if steps else clip
    picture(frames + handle, current)
    overlay_stats = None
    for step in steps:
        target = clip if step == steps[-1] else directory / f"video-{step}.mp4"
        if step == "overlay":
            overlay_stats = composite_overlays(config, p, overlays, current, target, frames + handle, directory,
                                               capture)
        else:
            apply_edges(current, target, canvas_render(config, p)[0], frames=frames + handle, enter=enter,
                        leave=leave)
        if current != clip:
            current.unlink(missing_ok=True)
        current = target
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
            *(["-t", f"{float(frames / fps):.6f}"] if handle else []),
        ],
        preview,
    )
    recorded = absolute_event or event
    data = {"event": recorded.model_dump(), "frames": frames, "samples": samples, "fps": str(fps)}
    if handle:  # 只在有尾部余量（重叠转场或节拍对齐）时加键：旧版本数据形态不变
        data["handle_frames"] = handle
    if transition.get("ignored") or notes:
        data["transition_notes"] = [*transition.get("ignored", []), *notes]
    if overlay_stats:
        data["overlays"] = overlay_stats
    return data, {
        "video": file_info(config, job_id, clip),
        "audio": file_info(config, job_id, processed),
        "preview": file_info(config, job_id, preview),
    }


def render_handle(w, p, sid) -> float:
    """分镜画面的尾部余量（秒）：重叠转场时长与节拍对齐容差取较大值；都没有时为 0。"""
    from . import beat_align

    return max(transitions.handle_seconds(w, p, sid), beat_align.handle_seconds(w, p, sid))


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


def image_render(config, job_id, w, p, sid, directory, source, capture=None):
    from mindex.studio.studio_render import still_shot

    speech = version(w, p["shots"][sid]["tts"])
    if speech["status"] != "complete":
        raise studio.StudioError("缺少成功配音")
    if source["kind"] == "tool":
        # 第三方工具的图片产物：取 invoke 版本的 artifact.png，之后与静态图片完全相同。
        made = w["versions"].get(p["shots"][sid].get("invoke"))
        if not made or made["status"] != "complete":
            raise studio.StudioError("缺少成功的第三方工具产物，请先执行调用第三方工具")
        path = file_path(config, job_id, made, "artifact")
        event = still_event(config, w, p, sid, {"version": made["id"], "file": "artifact"})
    elif source["kind"] == "aigc_image":
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
    return finish_render(config, job_id, p, directory, speech, event, None, picture,
                         transition=transitions.effective(w, p, sid),
                         overlays=html_layers(config, job_id, w, p, sid), capture=capture,
                         handle_seconds=render_handle(w, p, sid))


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

    data, files = finish_render(config, job_id, p, directory, speech, event, None, picture,
                                transition=transitions.effective(w, p, sid),
                                overlays=html_layers(config, job_id, w, p, sid), capture=capture,
                                handle_seconds=render_handle(w, p, sid))
    files["animation"] = file_info(config, job_id, raw)
    data["capture"] = {"frames": stats.get("frames"), "blocked_requests": len(stats.get("blocked_requests") or []),
                       "wall_seconds": stats.get("wall_seconds")}
    return data, files


def render_node(config, job_id, w, p, sid, directory, *, capture=None):
    from mindex.studio.studio_render import cut_shot, extract_ambient

    source = plan_source(w, p, sid)
    if source and (source["kind"] in ("image", "aigc_image")
                   or (source["kind"] == "tool" and source.get("output") == "image")):
        return image_render(config, job_id, w, p, sid, directory, source, capture)
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
    transition = transitions.effective(w, p, sid)
    extra = render_handle(w, p, sid)
    window = event.model_copy(deep=True)  # 画面取景窗口：有重叠转场时尽量从源继续多取余量
    if event.video.kind == "file":
        # 文件区间（上传片段）：start/end 已是文件内时间，原声直接取自片段本身。
        media = video_file(config, job_id, w, event.video)
        picture_media = media
        absolute_event = None
        if extra:
            window.video.end = min(probe(media)["duration"], event.video.end + extra)
        if ducking and probe(media)["audio"]:
            ambient = directory / "ambient.wav"
            extract_ambient(media, event, cfg, ambient)
    else:
        validate_range(config, selection["data"]["range"])
        store = Store.open(config)
        absolute_event = event.model_copy(deep=True)
        media = ShotMedia(store).materialize(event.video.source_id, event.video.start, event.video.end)
        picture_media = media
        if extra:
            try:  # 余量越过素材末尾时取不到，退回冻结末帧补足
                picture_media = ShotMedia(store).materialize(
                    event.video.source_id, event.video.start, event.video.end + extra)
                window.video.end = event.video.end + extra
            except Exception as exc:  # noqa: BLE001 — 任何越界都按冻结末帧处理
                log.debug("选片余量延长失败，按冻结末帧处理：%s", exc)
                picture_media = media
                window.video.end = event.video.end
        window.video.end -= event.video.start
        window.video.start = 0
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
        cut_shot(picture_media, window, render, output, frames=frames, fit=fit)

    return finish_render(config, job_id, p, directory, speech, event, ambient, picture, absolute_event,
                         transition=transition, overlays=html_layers(config, job_id, w, p, sid), capture=capture,
                         handle_seconds=extra)
