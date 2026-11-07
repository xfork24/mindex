"""分镜与版本 ID 解析。

界面显示的是截断 ID（``shot-`` + 8 位十六进制，版本取前 8 位），用户会照抄短 ID。
接受：完整 id；短前缀（可带或不带 ``shot-``）；``#N``（方案内第 N 个分镜，从 1 开始）。
有歧义时返回候选，由智能助手向用户澄清，绝不猜测。
"""

from __future__ import annotations

import re


def short(value: str | None) -> str | None:
    if not value:
        return value
    if value.startswith("shot-") and len(value) > 13:
        return value[:13]
    if re.fullmatch(r"[0-9a-f]{9,64}", value):
        return value[:8]
    return value


def resolve_shot(order: list[str], ref: str) -> dict:
    token = str(ref).strip()
    if not token:
        return {"ref": ref, "status": "missing", "candidates": []}
    match = re.fullmatch(r"#?(\d{1,4})", token) if token.startswith("#") else None
    if match:
        index = int(match.group(1))
        if 1 <= index <= len(order):
            return {"ref": ref, "status": "ok", "shot_id": order[index - 1], "index": index}
        return {"ref": ref, "status": "missing", "candidates": [], "reason": f"方案只有 {len(order)} 个分镜"}
    if token in order:
        return {"ref": ref, "status": "ok", "shot_id": token, "index": order.index(token) + 1}
    bare = token[5:] if token.lower().startswith("shot-") else token
    bare = bare.lower()
    hits = [
        sid
        for sid in order
        if sid.lower().startswith(token.lower())
        or (sid.startswith("shot-") and sid[5:].lower().startswith(bare))
    ]
    if len(hits) == 1:
        return {"ref": ref, "status": "ok", "shot_id": hits[0], "index": order.index(hits[0]) + 1}
    if hits:
        return {
            "ref": ref,
            "status": "ambiguous",
            "candidates": [{"shot_id": sid, "index": order.index(sid) + 1} for sid in hits],
        }
    return {"ref": ref, "status": "missing", "candidates": []}


def resolve_version(
    versions: dict, ref: str, *, stage: str | None = None, shot_id: str | None = None
) -> dict:
    token = str(ref).strip().lower()
    hits = [
        vid
        for vid, item in versions.items()
        if vid.startswith(token)
        and (stage is None or item.get("stage") == stage)
        and (shot_id is None or item.get("shot_id") == shot_id)
    ]
    if len(hits) == 1:
        return {"ref": ref, "status": "ok", "version_id": hits[0]}
    if hits:
        return {"ref": ref, "status": "ambiguous", "candidates": hits[:10]}
    return {"ref": ref, "status": "missing", "candidates": []}
