"""候选条目：去重键、入库与过期清理（``topic_items``）。

去重键按档案隔离：有链接时用规范化链接（去掉片段、末尾斜杠，主机小写），否则用规范化标题；
来源可在 ``Item.extra["dedup"]`` 给出自己的键（例如本地素材）。已存在的条目只刷新热度与抓取时间，
保留首次见到的发布时间。
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from mindex.topics.common import new_id, now, parse_time
from mindex.topics.sources.base import Item

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_title(title: str) -> str:
    return _PUNCT.sub("", unicodedata.normalize("NFKC", title or "").lower())


def dedup_key(item: Item) -> str:
    if item.extra.get("dedup"):
        return str(item.extra["dedup"])[:300]
    if item.url:
        parts = urlsplit(item.url.strip())
        if parts.scheme in ("http", "https") and parts.netloc:
            path = parts.path.rstrip("/") or "/"
            return "url:" + urlunsplit((parts.scheme, parts.netloc.lower(), path, parts.query, ""))[:300]
    return "title:" + normalize_title(item.title)[:200]


def store_items(store, profile_id: str, source_id: str, items: list[Item]) -> int:
    """写入条目，返回新增条数。"""
    stamp = now()
    added = 0
    with store.connect() as db:
        for found in items:
            key = dedup_key(found)
            exists = db.execute("SELECT 1 FROM topic_items WHERE profile_id=? AND dedup_key=?",
                                (profile_id, key)).fetchone()
            if exists:
                db.execute("UPDATE topic_items SET hot=COALESCE(?,hot),fetched_at=?,source_id=? "
                           "WHERE profile_id=? AND dedup_key=?", (found.hot, stamp, source_id, profile_id, key))
                continue
            db.execute(
                "INSERT INTO topic_items(id,profile_id,source_id,dedup_key,title,summary,url,published_at,event_at,"
                "hot,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("ti"), profile_id, source_id, key, found.title, found.summary, found.url, found.published_at,
                 found.event_at, found.hot, stamp),
            )
            added += 1
    return added


def purge(store, profile_id: str, days: int) -> int:
    """删除抓取时间早于 ``days`` 天、且未被任何卡片引用的条目；返回删除条数。"""
    cutoff = datetime.now().astimezone() - timedelta(days=days)
    referenced = set()
    for row in store.rows("SELECT body FROM topic_cards WHERE profile_id=?", (profile_id,)):
        referenced.update(json.loads(row["body"]).get("item_ids") or [])
    stale = [row["id"] for row in store.rows("SELECT id,fetched_at FROM topic_items WHERE profile_id=?", (profile_id,))
             if (parse_time(row["fetched_at"]) or cutoff) < cutoff and row["id"] not in referenced]
    with store.connect() as db:
        for item_id in stale:
            db.execute("DELETE FROM topic_items WHERE id=?", (item_id,))
    return len(stale)


def list_items(store, profile_id: str, *, limit: int = 50, source_id: str | None = None,
               since: str | None = None) -> list[dict]:
    clauses, args = ["profile_id=?"], [profile_id]
    if source_id:
        clauses.append("source_id=?")
        args.append(source_id)
    if since:
        clauses.append("fetched_at>=?")
        args.append(since)
    rows = store.rows(f"SELECT * FROM topic_items WHERE {' AND '.join(clauses)} "
                      "ORDER BY COALESCE(published_at, event_at, fetched_at) DESC, id LIMIT ?", (*args, limit))
    return [{k: row[k] for k in ("id", "source_id", "title", "summary", "url", "published_at", "event_at", "hot",
                                 "fetched_at")} for row in rows]


def items_by_id(store, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = {row["id"]: row for row in store.rows(f"SELECT * FROM topic_items WHERE id IN ({marks})", ids)}
    return [rows[i] for i in ids if i in rows]
