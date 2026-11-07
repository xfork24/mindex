"""账号档案的写作约束快照：快照哈希与文案提示词正文。

快照只取影响写作的字段（领域、子方向、受众、定位、禁区）。由 ``topics/profiles.py`` 生成快照，
创作 script 阶段（``studio/workspace/handlers/script.py``）复验哈希并把正文交给 writer；放在 shared，
studio 因此不依赖 topics 包。
"""

from __future__ import annotations

from shared.config import fingerprint

# 进入写作约束与快照哈希的字段。
WRITING_KEYS = ("domain", "subtopics", "audience", "positioning", "taboos")


def profile_hash(snapshot: dict) -> str:
    return fingerprint({key: snapshot.get(key) for key in WRITING_KEYS})


def writer_block(snapshot: dict) -> str:
    """文案提示词里的「账号档案｜写作约束」正文；没有可用约束时返回空串。"""
    lines = []
    domain = snapshot.get("domain") or ""
    subtopics = "、".join(snapshot.get("subtopics") or [])
    if domain or subtopics:
        lines.append(f"领域：{domain}" + (f"（子方向：{subtopics}）" if subtopics else ""))
    if snapshot.get("audience"):
        lines.append(f"目标受众：{snapshot['audience']}")
    if snapshot.get("positioning"):
        lines.append(f"账号定位与表达风格：{snapshot['positioning']}")
    taboos = snapshot.get("taboos") or []
    if taboos:
        lines.append("禁区（不得出现）：" + "；".join(taboos))
    return "\n".join(lines)
