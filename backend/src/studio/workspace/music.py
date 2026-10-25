"""背景音乐分段：按分镜顺序扫描提示点（Shot.music）——提示点 generate/cover 从该分镜起换成新的背景音乐，
none 从该分镜起停止，没有提示点的分镜顺延前一段；方案开头到第一个提示点之前没有背景音乐。"""

from __future__ import annotations

import math

from .stages import music_cue
from .versions import ordered_shots, version

CUE_FIELDS = ("kind", "prompt", "lyrics", "instrumental", "audio_id")


def shot_cue(w, p, sid) -> dict | None:
    """分镜 content 上的提示点（含 none）；没有返回 None。"""
    selected = p["shots"].get(sid) or {}
    item = w["versions"].get(selected.get("content"))
    value = (item or {}).get("data", {}).get("music")
    return value if isinstance(value, dict) else None


def segments(w, p) -> list[dict]:
    """有背景音乐的段落：``{"cue": 起始分镜, "shots": [...], "music": 提示点}``，按分镜顺序。"""
    result, current = [], None
    for sid in ordered_shots(w, p):
        cue = shot_cue(w, p, sid)
        if cue is not None:
            current = None
            if music_cue({"music": cue}):
                current = {"cue": sid, "shots": [], "music": cue}
                result.append(current)
        if current is not None:
            current["shots"].append(sid)
    return result


def shot_music(w, p) -> dict:
    """每个分镜的背景音乐归属（展示用）：``{sid: {"from": 起始分镜, "start": 是否提示点}}``；
    停止提示点为 ``{"stop": True}``，没有背景音乐的分镜不出现。"""
    found = {}
    for segment in segments(w, p):
        for sid in segment["shots"]:
            found[sid] = {"from": segment["cue"], "start": sid == segment["cue"]}
    for sid in ordered_shots(w, p):
        cue = shot_cue(w, p, sid)
        if cue is not None and cue.get("kind") == "none":
            found[sid] = {"stop": True}
    return found


def segment_seconds(w, p, sid) -> int:
    """以提示点 ``sid`` 起始的段落的预估时长（各分镜 estimated_seconds 之和，向上取整到 5 秒）。"""
    for segment in segments(w, p):
        if segment["cue"] == sid:
            total = sum(version(w, p["shots"][s]["content"])["data"]["estimated_seconds"] for s in segment["shots"])
            return max(10, int(math.ceil(total / 5) * 5))
    return 30


def cue_identity(cue: dict) -> dict:
    """提示点在 music 阶段签名中的身份（不含混音音量：音量只影响成片合成）。"""
    return {key: cue.get(key) for key in CUE_FIELDS}
