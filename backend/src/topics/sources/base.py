"""数据源协议、注册表与共用的网络请求。

- 每种来源实现 ``TopicSource``：``kind``、``label``、``config_model``（严格模型）与 ``fetch(ctx, cfg)``。
- 网络来源一律经 ``http_get``：只允许 http/https、单次超时与响应大小上限（``topics.fetch``），
  失败抛出带中文原因的 ``SourceError``。不内置爬虫：只请求用户配置的地址。
- 测试替换模块属性 ``TRANSPORT``（``httpx.MockTransport``）拦截全部请求。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

TRANSPORT: httpx.BaseTransport | None = None
USER_AGENT = "Mindex-Topics/1.0 (+https://github.com/mindex)"
TITLE_CHARS = 200
SUMMARY_CHARS = 600


class SourceError(ValueError):
    """来源抓取失败（中文原因）；``status`` 为 HTTP 状态码（若有）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Item:
    title: str
    summary: str = ""
    url: str | None = None
    published_at: str | None = None  # ISO 8601
    event_at: str | None = None  # 日历事件时间（ISO 8601）
    hot: float | None = None
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"title": self.title, "summary": self.summary, "url": self.url, "published_at": self.published_at,
                "event_at": self.event_at, "hot": self.hot}


@dataclass
class FetchContext:
    config: Any  # mindex.shared.config.Config
    profile: Any  # mindex.topics.models.ProfileBody
    store: Any
    secret: str | None = None
    source_id: str | None = None
    # 本次挖掘里各来源共享的预算（例如 Brave 的查询次数上限）。
    budget: dict = field(default_factory=dict)


class TopicSource(Protocol):
    kind: ClassVar[str]
    label: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    third_party: ClassVar[bool]  # 请求会发往第三方服务（页面需提示）
    secret: ClassVar[str | None]  # 需要的密钥说明；None 表示不需要

    def fetch(self, ctx: FetchContext, cfg: BaseModel) -> list[Item]: ...


REGISTRY: dict[str, TopicSource] = {}


def register(source: TopicSource) -> TopicSource:
    REGISTRY[source.kind] = source
    return source


def get(kind: str) -> TopicSource:
    from mindex.topics import sources  # noqa: F401 — 导入即注册全部来源

    try:
        return REGISTRY[kind]
    except KeyError as exc:
        raise ValueError(f"未知的数据源类型：{kind}") from exc


def kinds() -> list[dict]:
    from mindex.topics import sources  # noqa: F401

    return [{"kind": s.kind, "label": s.label, "third_party": s.third_party, "secret": s.secret,
             "schema": s.config_model.model_json_schema()} for s in REGISTRY.values()]


def check_url(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SourceError("地址必须以 http:// 或 https:// 开头")
    return url


def http_get(config, url: str, *, params: dict | None = None, headers: dict | None = None,
             label: str = "数据源") -> tuple[int, dict, bytes]:
    """GET ``url``，返回（状态码，响应头，正文）。HTTP 错误状态、超时、超限与协议不符都抛出 ``SourceError``。"""
    limits = config.topics.fetch
    check_url(url)
    try:
        with httpx.Client(timeout=limits.timeout, follow_redirects=True, transport=TRANSPORT,
                          headers={"User-Agent": USER_AGENT}) as client:
            with client.stream("GET", url, params=params, headers=headers) as response:
                if response.url.scheme not in ("http", "https"):
                    raise SourceError("重定向到了非 http/https 地址")
                if response.status_code >= 400:
                    raise SourceError(f"{label}返回 HTTP {response.status_code}", response.status_code)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limits.max_bytes:
                        raise SourceError(f"{label}响应超过 {limits.max_bytes // 1024} KB 上限")
                    chunks.append(chunk)
                return response.status_code, dict(response.headers), b"".join(chunks)
    except httpx.TimeoutException as exc:
        raise SourceError(f"{label}请求超时（{limits.timeout:g} 秒）") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"{label}请求失败：{exc}") from exc


_BLOCK = re.compile(r"<\s*/?\s*(p|br|div|li|ul|ol|h[1-6]|tr|td|th|table|blockquote|section|article)\b[^>]*>",
                    re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def plain(text: str | None, limit: int) -> str:
    """去掉 HTML 标签与实体（块级标签换成空格，行内标签直接去掉）、压缩空白并截断。"""
    value = html.unescape(_TAG.sub("", _BLOCK.sub(" ", text or "")))
    value = _SPACE.sub(" ", html.unescape(_TAG.sub("", _BLOCK.sub(" ", value)))).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def item(title, summary="", **kwargs) -> Item | None:
    title = plain(title, TITLE_CHARS)
    if not title:
        return None
    return Item(title=title, summary=plain(summary, SUMMARY_CHARS), **kwargs)
