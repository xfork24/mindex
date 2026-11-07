"""Candidate references and fixed-duration, single-source refinement windows."""

from __future__ import annotations

import json
import math
from fractions import Fraction

from filelock import FileLock

from admin.curation import Library
from shared.config import fingerprint
from shared.media import atomic_media
from shared.shot_media import ShotMedia
from shared.storage import Store
from shared.work_budget import configured
from studio import select as studio_select
from studio import steps as studio_steps
from studio import studio
from studio.artifacts import visibility_key


def candidate_id(candidate):
    return candidate.get("id") or candidate["segment_id"]


def anchor_conflict(w, p, sid):
    choice = p.get("anchors", {}).get(sid, {"mode": "auto"})
    if choice["mode"] == "auto":
        return None
    current = p["shots"].get(sid, {}).get("recall")
    if choice["recall_version"] != current:
        return (
            f"参考锚点来自召回版本 {choice['recall_version']}，当前召回版本为 {current or '缺失'}；"
            "请重新选择候选或恢复自动选择"
        )
    recall = w["versions"].get(current, {})
    if recall.get("stage") != "recall" or recall.get("shot_id") != sid or recall.get("status") != "complete":
        return "参考锚点没有属于此分镜的成功召回版本"
    if not any(candidate_id(c) == choice["candidate_id"] for c in recall["data"].get("results", [])):
        return "参考锚点候选已不存在，请重新选择候选"
    return None


def set_anchor(w, p, sid, choice):
    if sid not in p["shots"]:
        raise studio.StudioError("分镜不存在于当前方案")
    draft = {**p, "anchors": {**p.get("anchors", {}), sid: choice}}
    if error := anchor_conflict(w, draft, sid):
        raise studio.StudioError(error)
    if p.get("anchors", {}).get(sid) != choice:
        p["ranges"].pop(sid, None)
    p.setdefault("anchors", {})[sid] = choice


def candidates(config, w, p, sid):

    if error := anchor_conflict(w, p, sid):
        raise studio.StudioError(error)
    recall = w["versions"].get(p["shots"][sid].get("recall"))
    if (
        not recall
        or recall["stage"] != "recall"
        or recall["shot_id"] != sid
        or recall["status"] != "complete"
    ):
        raise studio.StudioError("缺少属于此分镜的成功召回")
    store = Store.open(config)
    overlays = Library(store).overlays()
    result = [
        c
        for c in recall["data"].get("results", [])
        if studio_steps.interval_visible(store, c["source_id"], c["start"], c["end"], overlays=overlays)
    ]
    choice = p.get("anchors", {}).get(sid, {"mode": "auto"})
    if choice["mode"] == "manual":
        result = [c for c in result if candidate_id(c) == choice["candidate_id"]]
    if not result:
        raise studio.StudioError("参考候选不存在或已隐藏，请更换候选")
    return result


def resolve_anchor(config, w, p, sid, target):
    pool = candidates(config, w, p, sid)
    if p.get("anchors", {}).get(sid, {}).get("mode") == "manual":
        return pool[0]
    if target is None:
        return None  # Automatic scoring needs the actual voice duration.
    duration, nxt, prev, covered = studio_steps._selection_callbacks(Store.open(config))
    shot = w["versions"][p["shots"][sid]["content"]]["data"]
    events, _ = studio_select.select_events(
        [shot],
        {sid: pool},
        {sid: target},
        config.studio.selection,
        source_duration=duration,
        extend_next=nxt,
        extend_prev=prev,
        covered_shots=covered,
    )
    return next(c for c in pool if c["segment_id"] == events[0].video.segment_id)


def neighborhood(rows, anchor, length):
    """Grow context to twice the voice duration, balancing the original side durations."""
    start, end = anchor["start"], anchor["end"]
    indices = [i for i, r in enumerate(rows) if r["start"] < end - 1e-6 and r["end"] > start + 1e-6]
    if not indices:
        raise studio.StudioError("候选范围没有可定位的素材镜头")
    lo, hi = indices[0], indices[-1]
    cursor = start
    for r in rows[lo : hi + 1]:
        if not r["visible"] or r["start"] > cursor + 1e-6:
            raise studio.StudioError("候选锚点包含隐藏区间或不连续镜头")
        cursor = max(cursor, r["end"])
    if start < rows[lo]["start"] - 1e-6 or end > cursor + 1e-6:
        raise studio.StudioError("候选范围超出已知镜头边界")

    def can_prev():
        return lo > 0 and rows[lo - 1]["visible"] and abs(rows[lo - 1]["end"] - rows[lo]["start"]) < 1e-6

    def can_next():
        return (
            hi + 1 < len(rows)
            and rows[hi + 1]["visible"]
            and abs(rows[hi]["end"] - rows[hi + 1]["start"]) < 1e-6
        )

    for _ in range(2):
        if can_prev():
            lo -= 1
    for _ in range(2):
        if can_next():
            hi += 1
    default = {"start": rows[lo]["start"], "end": rows[hi]["end"]}
    anchor_start, anchor_end = rows[indices[0]]["start"], rows[indices[-1]]["end"]
    before = anchor_start - default["start"]
    after = default["end"] - anchor_end
    extra_before = extra_after = 0
    while length and rows[hi]["end"] - rows[lo]["start"] < 2 * length - 1e-9:
        prev, nxt = can_prev(), can_next()
        if not prev and not nxt:
            break
        # Balance growth in seconds relative to each side's original duration.
        # A blocked side hands its remaining share to the other side.
        grow_before = prev and (
            not nxt or (anchor_start - rows[lo]["start"]) / before <= (rows[hi]["end"] - anchor_end) / after
        )
        if grow_before:
            lo -= 1
            extra_before += 1
        else:
            hi += 1
            extra_after += 1
    return rows[lo : hi + 1], default, {"before": extra_before, "after": extra_after}


def context(config, job_id, w, p, sid):
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from studio import workspace as ws

    if sid not in p["shots"]:
        raise studio.StudioError("分镜不存在于当前方案")
    old = w["versions"].get(p["shots"][sid].get("select"), {}).get("data", {})
    if old.get("manual") and not old.get("anchor_ref") and sid not in p.get("anchors", {}):
        raise studio.StudioError("此历史人工选片未记录候选引用；重新调整前请明确选择参考候选")
    speech = w["versions"].get(p["shots"][sid].get("tts"))
    target = None
    if speech and speech["stage"] == "tts" and speech["shot_id"] == sid and speech["status"] == "complete":
        ws.file_path(config, job_id, speech, "audio")
        target = speech["data"]["duration"]
    speed = p["options"].get("atempo") or 1.0
    # Audio processing uses four decimal places; bind the exact effective speed.
    speed = round(speed, 4)
    length = target / speed if target else None
    anchor = resolve_anchor(config, w, p, sid, target)
    result = {
        "plan_id": p["id"],
        "shot_id": sid,
        "revision": ws.revision(p),
        "tts_version": speech["id"] if target else None,
        "window_duration": length,
        "atempo": speed,
        "anchor": anchor,
        "ready": False,
        "reason": "等待成功配音后启用定长窗口",
    }
    if anchor:
        store = Store.open(config)
        source = store.one("SELECT * FROM sources WHERE id=?", (anchor["source_id"],))
        if not source:
            raise studio.StudioError("候选素材不存在")

        service = ShotMedia(store)
        info = json.loads(source["media"])
        timeline = [
            {**r, "end": min(r["end"], info["duration"])}
            for r in ws.source_shots(config, anchor["source_id"], store=store)
            if r["start"] < info["duration"]
        ]
        rows, default, extension = neighborhood(timeline, anchor, length)
        lo, hi = rows[0]["start"], min(rows[-1]["end"], info["duration"])
        ws.validate_range(config, {"source_id": anchor["source_id"], "start": lo, "end": hi})
        available = length is not None and hi - lo >= length - 1e-9
        ref = {"recall_version": p["shots"][sid]["recall"], "candidate_id": candidate_id(anchor)}
        identity = {
            "source_id": source["id"],
            "media": service.identity(source["id"], lo, hi),
            "layout_key": source["layout_key"],
            "visibility": visibility_key(store),
            "start": lo,
            "end": hi,
        }
        result.update(
            anchor_ref=ref,
            source_id=source["id"],
            shots=rows,
            context_range={"start": lo, "end": hi},
            default_range=default,
            extension=extension,
            legal_start={"min": lo, "max": max(lo, hi - (length or 0))},
            initial_start=min(max(anchor["start"], lo), max(lo, hi - (length or 0))),
            fps=info["fps"],
            frame_step=float(1 / Fraction(str(info["fps"]))),
            resource_key=fingerprint(identity),
            source_identity=identity,
            ready=available,
            reason=""
            if available
            else ("连续可用素材不足配音时长，请更换候选" if length else result["reason"]),
        )
    result["context_token"] = fingerprint(result)
    return result


@configured
def resources(config, job_id, ctx):
    """Shared low-resolution continuous video and a bounded thumbnail strip, cached by source identity."""
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from studio import workspace as ws

    if not ctx.get("source_id"):
        return {}
    directory = ws.root(config, job_id) / "previews" / f"context-{ctx['resource_key']}"
    directory.mkdir(parents=True, exist_ok=True)
    lo, hi = ctx["context_range"]["start"], ctx["context_range"]["end"]

    source = str(ShotMedia(Store.open(config)).materialize(ctx["source_id"], lo, hi))
    output = directory / "context.mp4"
    count = min(80, max(5, math.ceil((hi - lo) / 2)))
    with FileLock(str(directory / "resources.lock"), timeout=0):
        if not output.is_file():
            atomic_media(
                [
                    "-ss",
                    "0",
                    "-i",
                    source,
                    "-t",
                    str(hi - lo),
                    "-an",
                    "-vf",
                    "scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "30",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                ],
                output,
            )
        strip = directory / "thumbnails.jpg"
        if not strip.is_file():
            atomic_media(
                [
                    "-ss",
                    "0",
                    "-i",
                    source,
                    "-t",
                    str(hi - lo),
                    "-vf",
                    (
                        f"fps={count / (hi - lo):.9f},scale=160:90:force_original_aspect_ratio=decrease,"
                        f"pad=160:90:(ow-iw)/2:(oh-ih)/2,tile={count}x1"
                    ),
                    "-frames:v",
                    "1",
                ],
                strip,
            )

    def url(file):
        return f"/api/studio/jobs/{job_id}/artifacts/{file.relative_to(ws.root(config, job_id)).as_posix()}"

    return {"video_url": url(output), "thumbnails_url": url(strip), "thumbnail_count": count}


def selected_range(config, job_id, w, p, sid, token, start, *, ref, tts_version, _context=None):
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from studio import workspace as ws

    ctx = _context if _context is not None else context(config, job_id, w, p, sid)
    if token != ctx["context_token"] or ref != ctx.get("anchor_ref") or tts_version != ctx["tts_version"]:
        raise studio.StudioError("选片上下文、候选或配音已变化，请重新打开窗口并预览")
    if not ctx["ready"]:
        raise studio.StudioError(ctx["reason"])
    if (
        not math.isfinite(start)
        or not ctx["legal_start"]["min"] - 1e-9 <= start <= ctx["legal_start"]["max"] + 1e-9
    ):
        raise studio.StudioError("定长窗口超出合法范围")
    start = min(max(start, ctx["legal_start"]["min"]), ctx["legal_start"]["max"])
    return ws.validate_range(
        config,
        {
            "source_id": ctx["source_id"],
            "start": start,
            "end": start + ctx["window_duration"],
            "anchor_ref": ctx["anchor_ref"],
            "context_range": ctx["context_range"],
            "resource_key": ctx["resource_key"],
            "tts_version": ctx["tts_version"],
            "atempo": ctx["atempo"],
        },
    ), ctx


def alignment(config, job_id, w, p, sid, selected, *, _context=None):
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from studio import workspace as ws

    ctx = _context if _context is not None else context(config, job_id, w, p, sid)
    selected, _ = selected_range(
        config,
        job_id,
        w,
        p,
        sid,
        ctx["context_token"],
        selected["start"],
        ref=selected["anchor_ref"],
        tts_version=selected["tts_version"],
        _context=ctx,
    )
    stored = p["ranges"].get(sid, {})
    for field in ("end", "resource_key", "atempo", "context_range"):
        if stored and stored.get(field) != selected[field]:
            raise studio.StudioError("配音、速度或素材上下文已变化，请重新调整定长窗口")
    speech = ws.version(w, ctx["tts_version"])
    shot = ws.version(w, p["shots"][sid]["content"])["data"]
    _, _, _, covered = studio_steps._selection_callbacks(Store.open(config))
    plan = {
        "anchor": ctx["anchor"],
        "start": selected["start"],
        "end": selected["end"],
        "vlen": ctx["window_duration"],
        "atempo": ctx["atempo"],
        "render_duration": ctx["window_duration"],
        "pad": 0,
    }
    event = studio_select.build_event(
        shot,
        1,
        plan,
        speech["data"]["duration"],
        config.studio.selection,
        covered,
        [],
        alignment_override="manual",
    )
    # Keep exact duration/bounds; the renderer alone quantizes the final output frame count.
    event.video.start, event.video.end = selected["start"], selected["end"]
    event.audio.atempo, event.audio.render_duration = ctx["atempo"], ctx["window_duration"]
    # build_event rounds the subtitle and audio to milliseconds together. Once the
    # fixed window restores the exact audio duration, restore its subtitle too:
    # rounding only the subtitle up would make a valid window fail revalidation.
    event.subtitle.end = ctx["window_duration"]
    event.audio.file = speech["files"]["audio"]["path"]
    event = studio.TimelineEvent.model_validate(event.model_dump())
    return {
        "event": event.model_dump(),
        "range": selected,
        "actual_range": {
            "source_id": selected["source_id"],
            "start": selected["start"],
            "end": selected["end"],
        },
        "anchor": ctx["anchor"],
        "anchor_ref": ctx["anchor_ref"],
        "anchor_choice": p.get("anchors", {}).get(sid, {"mode": "auto"}),
        "context_range": ctx["context_range"],
        "tts_version": ctx["tts_version"],
        "atempo": ctx["atempo"],
        "trim_seconds": 0,
        "freeze_seconds": 0,
        "manual": True,
    }
