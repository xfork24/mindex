"""选片与时长对齐纯算法：候选打分、同源相邻延长、音频 atempo 钳制。

不直接访问数据库：片段相邻关系与原片时长通过回调注入，便于无 I/O 单测。
时长对齐阶梯：候选池按时长匹配打分 → 相邻 shot 延长（区间可落在 shot 中间，
渲染时从原片任意时间点精确切割）→ atempo ±limit 吸收残差 → 冻结帧/裁剪兜底。
"""

from __future__ import annotations

from collections.abc import Callable

from .config import SelectionConfig
from .studio import TimelineEvent

_EPS = 1e-6
_PAD_WARNING = 0.02


class SelectionError(ValueError):
    """选片无法进行（候选缺失等），消息直接面向用户。"""


def _extend_forward(
    source_id: str, start: float, end: float, want: float, selection: SelectionConfig,
    extend_next: Callable[[str, float], dict | None],
) -> float:
    while end - start < want - _EPS:
        nxt = extend_next(source_id, end)
        if not nxt or nxt["start"] > end + selection.gap_epsilon:
            break
        end = min(nxt["end"], start + want)
        if end <= start + _EPS:
            break
    return end


def _extend_backward(
    source_id: str, start: float, end: float, want: float, selection: SelectionConfig,
    extend_prev: Callable[[str, float], dict | None],
) -> float:
    while end - start < want - _EPS:
        prv = extend_prev(source_id, start)
        if not prv or prv["end"] < start - selection.gap_epsilon:
            break
        start = max(prv["start"], end - want)
        if start <= _EPS:
            break
    return start


def _alignment_label(extended: bool, atempo: float, pad: float) -> str:
    if pad > _PAD_WARNING:
        return "extended+padded" if extended else "padded"
    if pad < -_PAD_WARNING:
        return "extended+trimmed" if extended else "trimmed"
    if abs(atempo - 1) > 1e-3:
        return "extended+atempo" if extended else "atempo"
    return "extended" if extended else "nearest"


def _overlaps(interval: tuple[float, float], others: list[tuple[float, float]]) -> bool:
    start, end = interval
    return any(other[0] < end - _EPS and other[1] > start + _EPS for other in others)


def plan_anchor(
    anchor: dict,
    target: float,
    selection: SelectionConfig,
    *,
    source_duration: Callable[[str], float | None],
    extend_next: Callable[[str, float], dict | None],
    extend_prev: Callable[[str, float], dict | None],
) -> dict | None:
    """单个候选的时长对齐规划：延长 → atempo 钳制 → pad/trim 残差。不可用返回 None。"""
    if anchor["end"] - anchor["start"] < 0.04:
        return None  # 退化候选
    if not source_duration(anchor["source_id"]):
        return None
    end = _extend_forward(anchor["source_id"], anchor["start"], anchor["end"], target, selection, extend_next)
    start = _extend_backward(anchor["source_id"], anchor["start"], end, target, selection, extend_prev)
    return _plan_interval(anchor["source_id"], anchor, start, end, target, selection)


def plan_explicit(
    source_id: str, start: float, end: float, target: float, selection: SelectionConfig, *, source_duration: float
) -> dict:
    """人工指定区间的规划：clamp 到原片范围，同一套 atempo/残差公式。"""
    start = max(0.0, min(start, source_duration))
    end = max(start + 0.04, min(end, source_duration))
    return _plan_interval(source_id, {"source_id": source_id, "start": start, "end": end}, start, end, target, selection)


def _plan_interval(source_id: str, anchor: dict, start: float, end: float, target: float, selection: SelectionConfig) -> dict | None:
    vlen = end - start
    if vlen <= _EPS:
        return None
    atempo = min(1 + selection.atempo_limit, max(1 - selection.atempo_limit, target / vlen))
    render_duration = target / atempo
    pad = render_duration - vlen
    return {
        "anchor": anchor,
        "source_id": source_id,
        "start": start,
        "end": end,
        "vlen": vlen,
        "atempo": atempo,
        "render_duration": render_duration,
        "pad": pad,
    }


def select_events(
    shots: list[dict],
    candidates: dict[str, list[dict]],
    audio: dict[str, float],
    selection: SelectionConfig,
    *,
    source_duration: Callable[[str], float | None],
    extend_next: Callable[[str, float], dict | None],
    extend_prev: Callable[[str, float], dict | None],
    covered_shots: Callable[[str, float, float], list[str]],
) -> tuple[list[TimelineEvent], list[str]]:
    """顺序处理每个分镜，返回时间线事件与全局警告列表。

    候选打分 = 语义分数 + 时长匹配 - 延长占比惩罚 - 来源重复惩罚 - 同源重叠惩罚；
    目标时长一律用 TTS 实际时长（estimated_seconds 仅作 UI 参照）。
    """
    missing = [shot["id"] for shot in shots if not candidates.get(shot["id"])]
    if missing:
        raise SelectionError(f"分镜 {'、'.join(missing)} 无召回候选，请修改查询后重跑召回，或编辑分镜")
    weights = selection.weights
    usage: dict[str, list[tuple[float, float]]] = {}
    events: list[TimelineEvent] = []
    warnings: list[str] = []
    for order, shot in enumerate(shots, 1):
        target = audio[shot["id"]]
        best: dict | None = None
        for anchor in candidates[shot["id"]]:
            plan = plan_anchor(
                anchor, target, selection,
                source_duration=source_duration, extend_next=extend_next, extend_prev=extend_prev,
            )
            if plan is None:
                continue
            semantic = 1.0 / anchor["rank"]
            residual = abs(plan["pad"]) / target
            fit = 1.0 - min(1.0, residual / (2 * selection.atempo_limit))
            extension = max(0.0, plan["vlen"] - (anchor["end"] - anchor["start"])) / target
            repeat = len(usage.get(anchor["source_id"], []))
            overlap = 1.0 if _overlaps((plan["start"], plan["end"]), usage.get(anchor["source_id"], [])) else 0.0
            total = (
                weights["semantic"] * semantic
                + weights["duration"] * fit
                - weights["extension"] * extension
                - weights["repeat"] * repeat
                - weights["overlap"] * overlap
            )
            if best is None or total > best["total"]:
                best = {**plan, "total": total}
        if best is None:
            raise SelectionError(f"分镜 {shot['id']} 的候选均无法用于选片，请重跑召回")
        events.append(build_event(shot, order, best, target, selection, covered_shots, warnings))
        usage.setdefault(best["anchor"]["source_id"], []).append((best["start"], best["end"]))
    return events, warnings


def build_event(
    shot: dict, order: int, plan: dict, target: float, selection: SelectionConfig,
    covered_shots: Callable[[str, float, float], list[str]], warnings: list[str],
    alignment_override: str | None = None,
) -> TimelineEvent:
    anchor = plan["anchor"]
    extended = plan["vlen"] > (anchor["end"] - anchor["start"]) + _EPS
    alignment = alignment_override or _alignment_label(extended, plan["atempo"], plan["pad"])
    warning = (
        f"分镜 {shot['id']} 音视频时长差超过 ±{selection.atempo_limit:.0%}，成片将以冻结末帧或裁剪对齐"
        if abs(plan["pad"]) > _PAD_WARNING
        else None
    )
    if warning:
        warnings.append(warning)
    return TimelineEvent(
        shot_id=shot["id"],
        order=order,
        audio=studio_audio(shot["id"], target, plan["atempo"], plan["render_duration"]),
        video=studio_video(anchor, plan, covered_shots, alignment),
        subtitle={"start": 0.0, "end": round(plan["render_duration"], 3), "text": shot["text"]},
        warning=warning,
    )


def studio_audio(shot_id: str, duration: float, atempo: float, render_duration: float) -> dict:
    return {
        "file": f"audio/{shot_id}.wav",
        "duration": round(duration, 3),
        "atempo": round(atempo, 4),
        "render_duration": round(render_duration, 3),
    }


def studio_video(anchor: dict, best: dict, covered_shots: Callable, alignment: str) -> dict:
    return {
        "source_id": anchor["source_id"],
        "source_name": anchor.get("source_name", ""),
        "segment_id": anchor["segment_id"],
        "segment_ids": covered_shots(anchor["source_id"], best["start"], best["end"]),
        "start": round(best["start"], 3),
        "end": round(best["end"], 3),
        "score": anchor.get("score", 0.0),
        "rank": anchor.get("rank", 0),
        "alignment": alignment,
    }
