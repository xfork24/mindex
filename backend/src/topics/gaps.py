"""素材采集清单（``topic_gaps``）：覆盖报告里没有命中的关键画面，按档案汇总，指导素材积累方向。

- 合并：与本档案未完成缺口的描述相近（规范化后相同、文本向量余弦 ≥ ``MERGE_SIMILARITY``，或嵌入不可用时字符
  二元组 Jaccard ≥ ``MERGE_JACCARD``）时并入，``card_ids`` 记录涉及的选题，出现次数为其长度（同一选题重复打分不重复计数）。
- 批量：``Matcher`` 让一次打分运行只嵌入开放缺口一遍（新缺口即时并入比对集）；逐条登记的 ``record``
  是它的一次性包装。嵌入量是 O(开放缺口 + 新画面)，不随卡片数 × 历史缺口数相乘。
- 状态：open（待采集）/ done（已采集）。导出 CSV 带 UTF-8 BOM，Windows 上用 Excel 打开不乱码。
"""

from __future__ import annotations

import csv
import io
from typing import Literal

import numpy as np

from mindex.admin.curation import EntityNotFound
from mindex.shared.config import StrictModel
from mindex.topics import cluster, profiles
from mindex.topics.common import check_id, dumps, loads, new_id, now
from mindex.topics.items import normalize_title
from mindex.topics.scoring import jaccard_max

MERGE_SIMILARITY = 0.85
MERGE_JACCARD = 0.6


class GapChanges(StrictModel):
    status: Literal["open", "done"]


class Matcher:
    """打分循环共用的缺口匹配器：开放缺口向量每次运行只嵌入一次，新缺口即时并入比对集。

    合并判定与一次性 ``record`` 一致：规范化标题精确匹配 → 文本向量余弦最大且 ≥ ``MERGE_SIMILARITY``
    → 嵌入不可用时字符二元组 Jaccard 最大且 ≥ ``MERGE_JACCARD``。运行中嵌入一旦失败即整体退化为
    文字重合度合并（不重试），与旧行为同样不中断挖掘。
    """

    def __init__(self, store, profile_id: str, embedder=None):
        self.store, self.profile_id, self.embedder = store, profile_id, embedder
        self._rows: list[dict] = []
        self._vectors: list[np.ndarray] = []
        self._loaded, self._failed = False, False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._rows = self.store.rows("SELECT * FROM topic_gaps WHERE profile_id=? AND status='open'", (self.profile_id,))

    def _vectors_for(self, texts: list[str]) -> dict[str, np.ndarray]:
        """把尚无向量的缺口与本批画面合并成一次嵌入；失败时整体退化为文字比对。"""
        if self.embedder is None or self._failed:
            return {}
        pending = [g["visual"] for g in self._rows[len(self._vectors) :]]
        if not pending and not texts:
            return {}
        try:
            batch = list(cluster.embed(self.embedder, [*pending, *texts]))
            self._vectors.extend(batch[: len(pending)])
            return {text: batch[len(pending) + index] for index, text in enumerate(texts)}
        except Exception:  # noqa: BLE001 — 嵌入不可用时按文字重合度合并
            self._failed = True
            self._vectors = []
            return {}

    def _match(self, visual: str, vector) -> dict | None:
        key = normalize_title(visual)
        for gap in self._rows:
            if normalize_title(gap["visual"]) == key:
                return gap
        if not self._rows:
            return None
        if vector is not None and len(self._vectors) == len(self._rows):
            sims = np.asarray(self._vectors) @ vector
            best = int(np.argmax(sims))
            return self._rows[best] if float(sims[best]) >= MERGE_SIMILARITY else None
        best = max(self._rows, key=lambda g: jaccard_max(visual, [g["visual"]]))
        return best if jaccard_max(visual, [best["visual"]]) >= MERGE_JACCARD else None

    def _create(self, card_id: str, visual: str, vector) -> None:
        stamp = now()
        gap_id = new_id("tg")
        self.store.execute("INSERT INTO topic_gaps(id,profile_id,card_id,card_ids,visual,count,status,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?)",
                           (gap_id, self.profile_id, card_id, dumps([card_id]), visual, 1, "open", stamp, stamp))
        row = {"id": gap_id, "profile_id": self.profile_id, "card_id": card_id, "card_ids": dumps([card_id]),
               "visual": visual, "count": 1, "status": "open", "created_at": stamp, "updated_at": stamp}
        self._rows.append(row)
        if vector is not None and len(self._vectors) == len(self._rows) - 1:
            self._vectors.append(vector)

    def record(self, card_id: str, visuals: list[str]) -> int:
        """登记一张卡片的缺口；返回新建条数。同一 Matcher 的多次调用共享缺口快照与向量。"""
        self._load()
        cleaned = [visual for visual in (" ".join(raw.split())[:120] for raw in visuals) if visual]
        vectors = self._vectors_for(cleaned)
        created = 0
        for visual in cleaned:
            found = self._match(visual, vectors.get(visual))
            if found is not None:
                ids = loads(found["card_ids"], [])
                if card_id not in ids:
                    ids.append(card_id)
                self.store.execute("UPDATE topic_gaps SET card_id=?,card_ids=?,count=?,updated_at=? WHERE id=?",
                                   (card_id, dumps(ids), len(ids), now(), found["id"]))
                # 同步缓存行，避免后续调用以过期的 card_ids 把计数写回旧值。
                found.update(card_id=card_id, card_ids=dumps(ids), count=len(ids), updated_at=now())
                continue
            self._create(card_id, visual, vectors.get(visual))
            created += 1
        return created


def record(store, profile_id: str, card_id: str, visuals: list[str], *, embedder=None) -> int:
    """一次性登记一张卡片的缺口；打分循环应复用 ``Matcher`` 以共享缺口快照与向量。"""
    return Matcher(store, profile_id, embedder).record(card_id, visuals)


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
