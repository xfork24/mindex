"""热榜 JSON 接口：通用字段映射，或 DailyHotApi 预设（自建或第三方部署的聚合服务，见 docs/topics.md）。

- 通用：``url`` 返回 JSON，``items_path`` 指向条目数组（点分路径，如 ``data`` 或 ``data.list``），
  ``fields`` 把条目字段映射到标题、摘要、链接、热度与时间。
- DailyHotApi：``base_url`` + 榜单名（``weibo``、``zhihu``、``36kr``、``history`` 等），逐个请求 ``{base_url}/{榜单}``，
  响应为 ``{code, title, data: [{title, desc, url, hot, timestamp}]}``。
时间接受秒或毫秒时间戳与 ISO 字符串；热度接受数字或"123万"这类文字。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import ClassVar, Literal

from pydantic import Field, model_validator

from mindex.shared.config import StrictModel
from mindex.topics.common import parse_time
from mindex.topics.sources.base import FetchContext, Item, SourceError, http_get, item, register

BOARD = r"^[a-z0-9][a-z0-9-]{0,40}$"
_NUMBER = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(万|亿|w|k|m)?", re.IGNORECASE)
_SCALE = {"万": 1e4, "亿": 1e8, "w": 1e4, "k": 1e3, "m": 1e6}


class HotFields(StrictModel):
    title: str = Field(default="title", min_length=1, max_length=60)
    desc: str | None = Field(default="desc", max_length=60)
    url: str | None = Field(default="url", max_length=60)
    hot: str | None = Field(default="hot", max_length=60)
    time: str | None = Field(default="timestamp", max_length=60)


class HotlistConfig(StrictModel):
    preset: Literal["custom", "dailyhot"] = Field(default="custom", description="custom=通用 JSON；dailyhot=DailyHotApi")
    url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000, description="通用接口地址")
    base_url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000,
                                 description="DailyHotApi 服务地址，如 http://127.0.0.1:6688")
    boards: list[str] = Field(default_factory=list, max_length=20, description="DailyHotApi 榜单名，如 weibo、zhihu、36kr")
    items_path: str = Field(default="data", max_length=120, description="条目数组的点分路径")
    fields: HotFields = Field(default_factory=HotFields)
    limit: int = Field(default=30, ge=1, le=100, description="每个榜单最多取几条")

    @model_validator(mode="after")
    def target(self):
        if self.preset == "custom" and not self.url:
            raise ValueError("通用热榜需要 url")
        if self.preset == "dailyhot":
            if not self.base_url:
                raise ValueError("DailyHotApi 需要 base_url")
            if not self.boards:
                raise ValueError("DailyHotApi 至少选择一个榜单")
            bad = [b for b in self.boards if not re.match(BOARD, b)]
            if bad:
                raise ValueError(f"榜单名不合法：{'、'.join(bad)}")
        return self


def pick(value, path: str | None):
    if not path:
        return None
    current = value
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit() and int(key) < len(current):
            current = current[int(key)]
        else:
            return None
    return current


def hot_value(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    found = _NUMBER.match(str(value).replace(",", ""))
    if not found:
        return None
    return float(found.group(1)) * _SCALE.get((found.group(2) or "").lower(), 1)


def time_value(value) -> str | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 946684800000 else value
        try:
            return datetime.fromtimestamp(seconds, UTC).astimezone().isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return time_value(int(text))
    parsed = parse_time(text)
    return parsed.isoformat(timespec="seconds") if parsed else None


def parse(data: bytes, cfg: HotlistConfig, label: str, limit: int) -> list[Item]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise SourceError(f"{label}返回的不是有效的 JSON") from exc
    rows = pick(payload, cfg.items_path)
    if not isinstance(rows, list):
        raise SourceError(f"{label}的 {cfg.items_path} 不是条目数组，请检查 items_path")
    items = []
    fields = cfg.fields
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = pick(row, fields.url)
        found = item(pick(row, fields.title) or "", str(pick(row, fields.desc) or ""),
                     url=url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
                     published_at=time_value(pick(row, fields.time)), hot=hot_value(pick(row, fields.hot)))
        if found:
            items.append(found)
        if len(items) >= limit:
            break
    if rows and not items:
        raise SourceError(f"{label}的条目里没有找到标题字段 {fields.title}，请检查字段映射")
    return items


class HotlistSource:
    kind: ClassVar[str] = "hotlist"
    label: ClassVar[str] = "热榜接口"
    config_model: ClassVar[type] = HotlistConfig
    third_party: ClassVar[bool] = True
    secret: ClassVar[str | None] = None

    def fetch(self, ctx: FetchContext, cfg: HotlistConfig) -> list[Item]:
        limit = min(cfg.limit, ctx.config.topics.fetch.max_items_per_source)
        headers = {"Accept": "application/json"}
        if ctx.secret:
            headers["Authorization"] = f"Bearer {ctx.secret}"
        if cfg.preset == "custom":
            _, _, body = http_get(ctx.config, cfg.url, headers=headers, label="热榜接口")
            return parse(body, cfg, "热榜接口", limit)
        items, errors = [], []
        base = cfg.base_url.rstrip("/")
        preset = cfg.model_copy(update={"items_path": "data", "fields": HotFields()})
        for board in cfg.boards:
            try:
                _, _, body = http_get(ctx.config, f"{base}/{board}", params={"limit": limit}, headers=headers,
                                      label=f"榜单 {board}")
                items.extend(parse(body, preset, f"榜单 {board}", limit))
            except SourceError as exc:
                errors.append(str(exc))
        if errors and not items:
            raise SourceError("；".join(errors))
        return items[: ctx.config.topics.fetch.max_items_per_source]


register(HotlistSource())
