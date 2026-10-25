"""Brave Search（web 或 news 接口）：按档案关键词与子方向搜索最近的内容。

- 请求 ``{topics.brave.base_url}/res/v1/{news|web}/search``，请求头 ``X-Subscription-Token``（数据源密钥）。
- 查询词：配置的 ``queries``；没有时用档案关键词，再没有时用"领域 + 子方向"。
- ``freshness``（pd/pw/pm/py）、``country``、``search_lang`` 取自配置，缺省用档案的地区与语言。
- 计费保护：同一次挖掘里所有 Brave 来源合计不超过 ``topics.brave.max_queries`` 次查询。
注意：查询词（档案关键词）会发送给 Brave。
"""

from __future__ import annotations

import json
from typing import ClassVar, Literal

from pydantic import Field, field_validator

from mindex.shared.config import StrictModel
from mindex.topics.common import parse_time
from mindex.topics.sources.base import FetchContext, Item, SourceError, http_get, item, register

MAX_QUERIES_PER_SOURCE = 20


class BraveConfig(StrictModel):
    endpoint: Literal["news", "web"] = Field(default="news", description="news=新闻搜索；web=网页搜索")
    freshness: Literal["pd", "pw", "pm", "py"] = Field(default="pd", description="pd=24 小时，pw=7 天，pm=31 天，py=一年")
    queries: list[str] = Field(default_factory=list, max_length=MAX_QUERIES_PER_SOURCE,
                               description="查询词；留空时用档案关键词或领域与子方向")
    count: int = Field(default=10, ge=1, le=20, description="每个查询取几条结果")
    country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}$", description="国家代码；缺省用档案地区")
    search_lang: str | None = Field(default=None, pattern=r"^[A-Za-z-]{2,16}$", description="搜索语言；缺省用档案语言")

    @field_validator("queries")
    @classmethod
    def clean(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(" ".join(v.split())[:200] for v in values if v and v.strip()))
        return cleaned


def queries_for(cfg: BraveConfig, profile) -> list[str]:
    if cfg.queries:
        return cfg.queries
    if profile.keywords:
        return profile.keywords[:MAX_QUERIES_PER_SOURCE]
    return [f"{profile.domain} {topic}" for topic in profile.subtopics[:MAX_QUERIES_PER_SOURCE]] or [profile.domain]


def message(error: SourceError) -> str:
    if error.status in (401, 403):
        return f"Brave Search 密钥无效或没有权限（HTTP {error.status}），请检查数据源的 API Key"
    if error.status == 429:
        return "Brave Search 请求过于频繁或额度已用完（HTTP 429），请稍后再试或检查订阅"
    if error.status == 422:
        return "Brave Search 拒绝了请求参数（HTTP 422），请检查国家代码与搜索语言"
    return str(error)


def parse(data: bytes, endpoint: str) -> list[Item]:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise SourceError("Brave Search 返回的不是有效的 JSON") from exc
    rows = payload.get("results") if endpoint == "news" else (payload.get("web") or {}).get("results")
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = row.get("url") if isinstance(row.get("url"), str) and row["url"].startswith(("http://", "https://")) else None
        published = parse_time(row.get("page_age"))
        found = item(row.get("title") or "", row.get("description") or "", url=url,
                     published_at=published.isoformat(timespec="seconds") if published else None)
        if found:
            items.append(found)
    return items


class BraveSource:
    kind: ClassVar[str] = "brave"
    label: ClassVar[str] = "Brave 搜索"
    config_model: ClassVar[type] = BraveConfig
    third_party: ClassVar[bool] = True
    secret: ClassVar[str | None] = "Brave Search API Key"

    def fetch(self, ctx: FetchContext, cfg: BraveConfig) -> list[Item]:
        if not ctx.secret:
            raise SourceError("Brave 搜索需要 API Key")
        settings = ctx.config.topics.brave
        url = f"{settings.base_url.rstrip('/')}/res/v1/{cfg.endpoint}/search"
        headers = {"X-Subscription-Token": ctx.secret, "Accept": "application/json"}
        items, skipped = [], 0
        for query in queries_for(cfg, ctx.profile):
            used = ctx.budget.get("brave", 0)
            if used >= settings.max_queries:
                skipped += 1
                continue
            ctx.budget["brave"] = used + 1
            params = {"q": query, "count": cfg.count, "freshness": cfg.freshness,
                      "country": (cfg.country or ctx.profile.region).upper(),
                      "search_lang": cfg.search_lang or ctx.profile.language}
            try:
                _, _, body = http_get(ctx.config, url, params=params, headers=headers, label="Brave Search")
            except SourceError as exc:
                raise SourceError(message(exc), exc.status) from exc
            items.extend(parse(body, cfg.endpoint))
        if skipped and not items:
            raise SourceError(f"已达到本次挖掘的 Brave 查询上限（topics.brave.max_queries={settings.max_queries}）")
        return items[: ctx.config.topics.fetch.max_items_per_source]


register(BraveSource())
