"""素材采集清单（``topic_gaps``）：覆盖报告里没有命中的关键画面，按档案汇总，指导素材积累方向。

- 合并：与本档案未完成缺口的描述相近（规范化后相同、文本向量余弦 ≥ ``MERGE_SIMILARITY``，或嵌入不可用时字符
  二元组 Jaccard ≥ ``MERGE_JACCARD``）时并入，``card_ids`` 记录涉及的选题，出现次数为其长度（同一选题重复打分不重复计数）。
- 状态：open（待采集）/ done（已采集）。导出 CSV 带 UTF-8 BOM，Windows 上用 Excel 打开不乱码。
"""

from __future__ import annotations

import csv
import io
from typing import Literal

import numpy as np

from mindex.admin.curation import EntityNotFound
from mindex.shared.config import StrictModel
from mindex.topics import profiles
from mindex.topics.common import check_id, dumps, loads, new_id, now
from mindex.topics.items import normalize_title
from mindex.topics.scoring import jaccard_max

MERGE_SIMILARITY = 0.85
MERGE_JACCARD = 0.6


class GapChanges(StrictModel):
    status: Literal["open", "done"]


def _similar(visual: str, candidates: list[dict], embedder) -> dict | None:
    key = normalize_title(visual)
    for gap in candidates:
        if normalize_title(gap["visual"]) == key:
            return gap
    if not candidates:
        return None
    if embedder is not None:
        try:
            from mindex.topics.cluster import embed

            vectors = embed(embedder, [visual, *[g["visual"] for g in candidates]])
            sims = vectors[1:] @ vectors[0]
            best = int(np.argmax(sims))
            return candidates[best] if float(sims[best]) >= MERGE_SIMILARITY else None
        except Exception:  # noqa: BLE001 — 嵌入不可用时按文字重合度合并
            pass
    best = max(candidates, key=lambda g: jaccard_max(visual, [g["visual"]]))
    return best if jaccard_max(visual, [best["visual"]]) >= MERGE_JACCARD else None


def record(store, profile_id: str, card_id: str, visuals: list[str], *, embedder=None) -> int:
    """登记一张卡片的缺口；返回新建条数。"""
    created = 0
    for visual in visuals:
        visual = " ".join(visual.split())[:120]
        if not visual:
            continue
        open_gaps = store.rows("SELECT * FROM topic_gaps WHERE profile_id=? AND status='open'", (profile_id,))
        found = _similar(visual, open_gaps, embedder)
        if found is not None:
            ids = loads(found["card_ids"], [])
            if card_id not in ids:
                ids.append(card_id)
            store.execute("UPDATE topic_gaps SET card_id=?,card_ids=?,count=?,updated_at=? WHERE id=?",
                          (card_id, dumps(ids), len(ids), now(), found["id"]))
            continue
        stamp = now()
        store.execute("INSERT INTO topic_gaps(id,profile_id,card_id,card_ids,visual,count,status,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (new_id("tg"), profile_id, card_id, dumps([card_id]), visual, 1, "open", stamp, stamp))
        created += 1
    return created


def _view(row: dict, titles: dict[str, str]) -> dict:
    ids = loads(row["card_ids"], [])
    return {"id": row["id"], "profile_id": row["profile_id"], "visual": row["visual"], "count": row["count"],
            "status": row["status"], "card_ids": ids, "cards": [{"id": i, "title": titles.get(i)} for i in ids],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_gaps(store, profile_id: str, *, status: str | None = "open", limit: int = 200) -> list[dict]:
    profiles.get_profile(store, profile_id)
    if status in (None, "all"):
        rows = store.rows("SELECT * FROM topic_gaps WHERE profile_id=? ORDER BY status DESC, count DESC, created_at "
                          "LIMIT ?", (profile_id, limit))
    else:
        if status not in ("open", "done"):
            raise ValueError(f"未知的缺口状态：{status}")
        rows = store.rows("SELECT * FROM topic_gaps WHERE profile_id=? AND status=? ORDER BY count DESC, created_at "
                          "LIMIT ?", (profile_id, status, limit))
    titles = {r["id"]: r["title"] for r in store.rows("SELECT id,title FROM topic_cards WHERE profile_id=?",
                                                     (profile_id,))}
    return [_view(row, titles) for row in rows]


def update_gap(store, gap_id: str, changes: GapChanges) -> dict:
    check_id(gap_id, "tg", "素材缺口")
    row = store.one("SELECT * FROM topic_gaps WHERE id=?", (gap_id,))
    if row is None:
        raise EntityNotFound("找不到素材缺口")
    store.execute("UPDATE topic_gaps SET status=?,updated_at=? WHERE id=?", (changes.status, now(), gap_id))
    row = store.one("SELECT * FROM topic_gaps WHERE id=?", (gap_id,))
    titles = {r["id"]: r["title"] for r in store.rows("SELECT id,title FROM topic_cards WHERE profile_id=?",
                                                     (row["profile_id"],))}
    return _view(row, titles)


def cell(value) -> str:
    """防止表格软件把以 = + - @ 开头的文字当作公式执行。"""
    text = str(value if value is not None else "")
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(store, profile_id: str, *, status: str | None = "all") -> bytes:
    rows = list_gaps(store, profile_id, status=status, limit=10000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["画面", "出现次数", "状态", "相关选题", "更新时间"])
    for gap in rows:
        titles = "；".join(c["title"] or c["id"] for c in gap["cards"])
        writer.writerow([cell(gap["visual"]), gap["count"], "已采集" if gap["status"] == "done" else "待采集",
                         cell(titles), gap["updated_at"]])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")
