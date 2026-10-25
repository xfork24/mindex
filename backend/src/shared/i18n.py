"""Stable UI message metadata. Original diagnostics and content remain untouched."""
from __future__ import annotations

import json
import re
from functools import lru_cache

from mindex.shared.web_assets import frontend_root


@lru_cache(maxsize=1)
def message_patterns():
    catalog = json.loads((frontend_root() / "shared" / "messages.json").read_text(encoding="utf-8"))
    patterns = []
    for key, entry in catalog.items():
        if key.startswith("system.") or not re.search(r"[一-鿿]", entry["zh"]):
            continue
        names = []
        pieces = []
        for part in re.split(r"(\{\w+\})", entry["zh"]):
            if re.fullmatch(r"\{\w+\}", part):
                names.append(part[1:-1])
                pieces.append(r"([\s\S]*?)")
            else:
                pieces.append(re.escape(part))
        patterns.append((len(entry["zh"]), key, re.compile("^" + "".join(pieces) + "$"), names))
    return sorted(patterns, key=lambda item: (not item[3], item[0]), reverse=True)


@lru_cache(maxsize=2048)
def _message_metadata(text):
    for _, key, pattern, names in message_patterns():
        found = pattern.fullmatch(text)
        if found:
            return {"message_key": key, "message_params": dict(zip(names, found.groups()))}
    return {"message_key": "system.diagnostic", "message_params": {"diagnostic": text}}


def message_metadata(message):
    meta = _message_metadata(str(message))
    # Callers may augment response metadata; do not expose the cached dictionaries.
    return {"message_key": meta["message_key"], "message_params": dict(meta["message_params"])}


STAGE_MESSAGES = {
    "detect": "检测分镜", "split": "生成切片", "repair": "修复文件", "subtitle": "提取字幕",
    "asr": "ASR 转写", "analyze": "片段分析", "scene_grouping": "场景分组", "scenes": "场景分析",
    "matching": "人物匹配", "extracting": "提取人物特征", "publish": "发布检索",
    "embedding": "嵌入片段", "index_check": "检查索引", "published": "检索已更新", "indexed": "索引已完成",
    "index_wait": "等待检索更新", "index_prepare": "准备检索数据", "person_index": "更新人物检索",
}

def add_message_metadata(value):
    """Annotate system fields only, never name/text/annotation/script/subtitle fields.

    Older stored events acquire metadata when read without rewriting history.
    Each additional field leaves the original JSON shape and values intact.
    """
    if isinstance(value, list):
        return [add_message_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    content_fields = {"annotation", "annotations", "input_text", "outline", "script", "text", "query", "notes", "name", "message_key", "message_params"}
    result = {
        key: item if key.endswith("_message") or key in content_fields else add_message_metadata(item)
        for key, item in value.items()
    }
    for field in ("message", "detail", "error", "reason", "start_reason", "fallback_reason"):
        text = value.get(field)
        if isinstance(text, str) and text:
            meta = message_metadata(text)
            if field in ("message", "detail"):
                result.update(meta)
            else:
                result[field + "_message"] = {"message": text, **meta}
    if "message" not in value and value.get("stage") in STAGE_MESSAGES and (
        len(value) == 1 or any(key.startswith(("completed", "total")) for key in value)
    ):
        result.update(message_metadata(STAGE_MESSAGES[value["stage"]]))
    for field in ("warnings", "missing"):
        if isinstance(value.get(field), list) and all(isinstance(item, str) for item in value[field]):
            result[field + "_messages"] = [{"message": item, **message_metadata(item)} for item in value[field]]
    return result
