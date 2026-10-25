"""选题模块共用：ID、时间、JSON 列与异常。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from uuid import uuid4

from mindex.admin.curation import EntityNotFound

# 各实体 ID：前缀 + 12 位十六进制，便于在日志与对话中区分（tp 档案、ts 数据源、ti 条目、tc 卡片、tg 缺口）。
ID_PATTERN = r"^t[psicg]_[a-f0-9]{12}$"
_ID = re.compile(ID_PATTERN)
# 挖掘任务沿用后台任务的 32 位十六进制 ID。
RUN_ID_PATTERN = r"^[a-f0-9]{32}$"


class TopicConflict(ValueError):
    """与当前状态冲突（API 返回 409），例如已有挖掘任务在运行、卡片已采纳。"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def check_id(value: str, prefix: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.match(value) or not value.startswith(prefix + "_"):
        raise EntityNotFound(f"找不到{label}")
    return value


def now() -> str:
    """本地时区、带偏移的 ISO 8601（秒）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    """解析带或不带偏移的 ISO 时间；不带偏移的按本地时区；无法解析返回 None。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value, default=None):
    if value in (None, ""):
        return default
    return json.loads(value)
