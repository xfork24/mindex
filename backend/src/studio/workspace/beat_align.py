"""节拍对齐（方案参数 ``beat_align``）：成片合成时把背景音乐段落内的切点推迟到容差内最近的节拍。

- 分镜渲染：开启后每个非末镜多渲染 ``tolerance`` 秒尾部余量（与重叠转场共用余量，取两者较大值）；
  有出场转场（leave）的分镜不留余量，其后的切点不推迟（淡出在切点结束）；
- 成片合成：按分镜顺序逐个切点处理——切点前后两镜属于同一背景音乐段落、且本镜没有重叠转场时，在
  ``[切点 - 半帧, 切点 + tolerance]`` 内找段落音乐的下一个节拍，把切点推迟 δ 帧（δ ≤ 本镜余量）：
  画面多取 δ 帧余量，配音段末尾补 δ 帧静音，之后的字幕、背景音乐与边界整体后移；
- 节拍来自段落音乐文件的分析（``studio/beats.py``），段落内循环播放时节拍按曲长周期延伸；
- 关闭时（``beat_align`` 为 None）render 与 compose 的签名、切点都与旧版一致。
"""

from __future__ import annotations

import bisect
from fractions import Fraction

from studio import beats
from studio.workspace.versions import ordered_shots

# 改动对齐规则时递增：进入开启节拍对齐的 compose 签名。
BEAT_ALIGN_VERSION = 1


def tolerance(p) -> float | None:
    value = (p.get("options") or {}).get("beat_align")
    return float(value.get("tolerance", 0.25)) if isinstance(value, dict) else None


def handle_seconds(w, p, sid) -> float:
    """节拍对齐需要的尾部余量（秒）：开启时非末镜为 tolerance，否则 0。"""
    found = tolerance(p)
    if found is None:
        return 0.0
    order = ordered_shots(w, p)
    if sid not in order or sid == order[-1]:
        return 0.0
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from studio.workspace import transitions

    if transitions.effective(w, p, sid).get("leave"):
        return 0.0  # 出场转场在切点结束：这个切点不推迟，也不需要余量
    return found


def render_payload(w, p, sid) -> float | None:
    """进入 render 签名的余量；不需要时为 None（签名与旧版一致）。"""
    return handle_seconds(w, p, sid) or None


def compose_payload(p) -> dict | None:

    found = tolerance(p)
    if found is None:
        return None
    return {"tolerance": found, "version": BEAT_ALIGN_VERSION, "analysis_version": beats.ANALYSIS_VERSION}


def beat_grid(beats: list[float], period: float, start: float, until: float) -> list[float]:
    """段落从 ``start`` 秒起循环播放一首节拍为 ``beats``、长 ``period`` 秒的音乐，``until``
    之前的全部节拍时间。
    """
    if not beats or period <= 0:
        return []
    found, offset = [], start
    while offset < until:
        found.extend(offset + beat for beat in beats if offset + beat < until)
        offset += period
    return found


def delays(
    frames: list[int],
    handles: list[int],
    sids: list[str],
    segments: list[dict],
    analyses: dict,
    fps: Fraction,
    tolerance_seconds: float,
    skip: set[str],
) -> list[int]:
    """逐个切点计算推迟帧数（与 ``frames`` 等长，末镜恒为 0）。

    ``segments`` 为 ``music.segments`` 的结果，``analyses[cue] = (beats, duration)``；
    ``skip`` 为有重叠转场的分镜。"""
    owner = {}
    for segment in segments:
        for sid in segment["shots"]:
            owner[sid] = segment["cue"]
    result = [0] * len(frames)
    starts, cursor = {}, 0
    tolerance_frames = Fraction(str(tolerance_seconds)) * fps
    for index, sid in enumerate(sids):
        if sid in owner and owner[sid] == sid:
            starts[sid] = cursor  # 段落从提示点分镜的开头（含前面切点的推迟）开始
        cursor += frames[index]
        if index == len(sids) - 1 or sid in skip or not handles[index]:
            continue
        cue = owner.get(sid)
        if cue is None or owner.get(sids[index + 1]) != cue or cue not in analyses:
            continue
        beats, period = analyses[cue]
        cut = Fraction(cursor) / fps
        start = Fraction(starts[cue]) / fps
        limit = cut + Fraction(str(tolerance_seconds))
        grid = beat_grid(beats, period, float(start), float(limit) + 1e-9)
        position = bisect.bisect_left(grid, float(cut - Fraction(1, 2) / fps))
        if position >= len(grid):
            continue
        delta = max(0, round((Fraction(str(grid[position])) - cut) * fps))
        delta = min(delta, handles[index], int(tolerance_frames))
        if delta:
            result[index] = delta
            cursor += delta
    return result
