"""RSS 2.0、RSS 1.0（RDF）与 Atom 订阅。拒绝含 DOCTYPE 或实体声明的文档（防 XXE 与实体膨胀）。"""

from __future__ import annotations

import re
from email.utils import parsedate_to_datetime
from typing import ClassVar
from xml.etree import ElementTree

from pydantic import Field

from shared.config import StrictModel
from topics.common import parse_time
from topics.sources.base import FetchContext, Item, SourceError, http_get, item, register

_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class RssConfig(StrictModel):
    url: str = Field(pattern=r"^https?://", max_length=2000, description="RSS 或 Atom 订阅地址")


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def child(node, name: str):
    return next((c for c in node if local(c.tag) == name), None)


def text_of(node, *names: str) -> str:
    for name in names:
        found = child(node, name)
        if found is not None:
            value = "".join(found.itertext()).strip()
            if value:
                return value
    return ""


def time_of(node, *names: str) -> str | None:
    raw = text_of(node, *names)
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)  # RFC 822（RSS）
    except (TypeError, ValueError):
        value = parse_time(raw)  # ISO 8601（Atom、dc:date）
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.astimezone()
    return value.isoformat(timespec="seconds")


def atom_link(entry) -> str | None:
    links = [c for c in entry if local(c.tag) == "link"]
    preferred = next(
        (c for c in links if c.get("rel", "alternate") == "alternate"), links[0] if links else None
    )
    return preferred.get("href") if preferred is not None else None


def parse_feed(data: bytes, limit: int = 100) -> list[Item]:
    if _DTD.search(data[:4096]) or b"<!ENTITY" in data:
        raise SourceError("订阅内容含 DOCTYPE 或实体声明，已拒绝解析")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise SourceError(f"订阅内容不是有效的 XML：{exc}") from exc
    kind = local(root.tag)
    if kind == "feed":  # Atom
        entries = [e for e in root if local(e.tag) == "entry"]
    elif kind in ("rss", "RDF"):
        channel = child(root, "channel")
        entries = [
            e for e in (channel if channel is not None and kind == "rss" else root) if local(e.tag) == "item"
        ]
    else:
        raise SourceError("不是 RSS 或 Atom 订阅")
    items = []
    for entry in entries:
        if kind == "feed":
            link = atom_link(entry)
            summary = text_of(entry, "summary", "content")
            published = time_of(entry, "published", "updated")
        else:
            link = text_of(entry, "link") or None
            summary = text_of(entry, "description", "encoded", "summary")
            published = time_of(entry, "pubDate", "date", "published", "updated")
        found = item(text_of(entry, "title"), summary, url=link, published_at=published)
        if found:
            items.append(found)
        if len(items) >= limit:
            break
    return items


class RssSource:
    kind: ClassVar[str] = "rss"
    label: ClassVar[str] = "RSS / Atom 订阅"
    config_model: ClassVar[type] = RssConfig
    third_party: ClassVar[bool] = True
    secret: ClassVar[str | None] = None

    def fetch(self, ctx: FetchContext, cfg: RssConfig) -> list[Item]:
        _, _, body = http_get(ctx.config, cfg.url, label="订阅")
        return parse_feed(body, ctx.config.topics.fetch.max_items_per_source)


register(RssSource())
