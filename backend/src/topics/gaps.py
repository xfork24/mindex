"""素材采集清单（``topic_gaps``）：覆盖报告里没有命中的关键画面，按档案汇总，指导素材积累方向。

- 合并：与本档案未完成缺口的描述相近（规范化后相同、文本向量余弦 ≥ ``MERGE_SIMILARITY``，或嵌入不可用时字符
  二元组 Jaccard ≥ ``MERGE_JACCARD``）时并入，``card_ids`` 记录涉及的选题，
出现次数为其长度（同一选题重复打分不重复计数）。
- 批量：``Matcher`` 让缺口向量持久化在 ``topic_gaps``（``embedding``/``embedding_space`` 列，按嵌入空间
  身份指纹失效），每次打分只嵌入没有可用向量的缺口；逐条登记的 ``record`` 是它的一次性包装。
  嵌入量是 O(新增缺口 + 新画面)，不随卡片数 × 历史缺口数相乘。
- 状态：open（待采集）/ done（已采集）。导出 CSV 带 UTF-8 BOM，Windows 上用 Excel 打开不乱码。
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Literal

import numpy as np

from admin.curation import EntityNotFound
from shared.config import StrictModel, fingerprint
from topics import cluster, profiles
from topics.common import check_id, dumps, loads, new_id, now
from topics.items import normalize_title
from topics.scoring import jaccard_max

log = logging.getLogger(__name__)

MERGE_SIMILARITY = 0.85
MERGE_JACCARD = 0.6


class GapChanges(StrictModel):
    status: Literal["open", "done"]


class Matcher:
    """打分循环共用的缺口匹配器：缺口向量持久化在库里，未命中缓存才嵌入。

    合并判定与一次性 ``record`` 一致：规范化标题精确匹配 → 文本向量余弦最大且 ≥ ``MERGE_SIMILARITY``
    → 嵌入不可用时字符二元组 Jaccard 最大且 ≥ ``MERGE_JACCARD``。向量按嵌入空间身份指纹
    （``embedding_space``）缓存与失效，切换嵌入模型后指纹不符的行自动重嵌，绝不跨空间比较。
    运行中嵌入一旦失败即整体退化为文字重合度合并（不重试），与旧行为同样不中断挖掘。
    """

    def __init__(self, store, profile_id: str, embedder=None):
        self.store, self.profile_id, self.embedder = store, profile_id, embedder
        self._rows: list[dict] = []
        self._vectors: dict[str, np.ndarray] = {}
        self._matrix: np.ndarray | None = None
        self._loaded = self._failed = self._identity_ready = False
        self._space: str | None = None

    def _identity(self) -> str | None:
        """嵌入空间身份指纹；取不到（无 identity 的嵌入器或服务未就绪）时返回 None，只做会话内缓存。"""
        if not self._identity_ready:
            self._identity_ready = True
            fn = getattr(self.embedder, "identity", None)
            try:
                self._space = fingerprint(fn()) if fn is not None else None
            except Exception:  # noqa: BLE001 — 身份不可得时不持久化，成败交给嵌入本身
                self._space = None
        return self._space

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._rows = self.store.rows(
            "SELECT * FROM topic_gaps WHERE profile_id=? AND status='open'", (self.profile_id,)
        )
        space = self._identity()
        if not space:
            return
        for gap in self._rows:
            if gap["embedding_space"] == space and gap["embedding"] is not None:
                self._vectors[gap["id"]] = np.frombuffer(gap["embedding"], dtype=np.float32)

    def _matrix_of(self) -> np.ndarray | None:
        """全部缺口都有向量时返回按 ``_rows`` 顺序堆叠的矩阵，否则 None（余弦退化为不可用）。"""
        if self._matrix is None and self._rows and all(g["id"] in self._vectors for g in self._rows):
            self._matrix = np.stack([self._vectors[g["id"]] for g in self._rows])
        return self._matrix

    def _vectors_for(self, texts: list[str]) -> dict[str, np.ndarray]:
        """把缓存未命中的缺口与本批画面合并成一次嵌入并回写数据库；失败时整体退化为文字比对。"""
        if self.embedder is None or self._failed:
            return {}
        missing = [g for g in self._rows if g["id"] not in self._vectors]
        if not missing and not texts:
            return {}
        try:
            batch = list(cluster.embed(self.embedder, [*[g["visual"] for g in missing], *texts]))
        except Exception:  # 嵌入不可用时按文字重合度合并
            log.warning("缺口合并嵌入不可用，按文字重合度合并", exc_info=True)
            self._failed = True
            return {}
        space = self._identity()
        for gap, vector in zip(missing, batch[: len(missing)], strict=True):
            self._vectors[gap["id"]] = vector
            self._matrix = None
            if space:
                self.store.execute(
                    "UPDATE topic_gaps SET embedding=?,embedding_space=? WHERE id=?",
                    (np.asarray(vector, dtype=np.float32).tobytes(), space, gap["id"]),
                )
        return {text: batch[len(missing) + index] for index, text in enumerate(texts)}

    def _match(self, visual: str, vector) -> dict | None:
        key = normalize_title(visual)
        for gap in self._rows:
            if normalize_title(gap["visual"]) == key:
                return gap
        if not self._rows:
            return None
        matrix = self._matrix_of() if vector is not None else None
        if matrix is not None:
            sims = matrix @ vector
            best = int(np.argmax(sims))
            return self._rows[best] if float(sims[best]) >= MERGE_SIMILARITY else None
        best = max(self._rows, key=lambda g: jaccard_max(visual, [g["visual"]]))
        return best if jaccard_max(visual, [best["visual"]]) >= MERGE_JACCARD else None

    def _create(self, card_id: str, visual: str, vector) -> None:
        stamp = now()
        gap_id = new_id("tg")
        space = self._identity() if vector is not None else None
        blob = np.asarray(vector, dtype=np.float32).tobytes() if (vector is not None and space) else None
        self.store.execute(
            "INSERT INTO topic_gaps(id,profile_id,card_id,card_ids,visual,count,status,"
            "embedding,embedding_space,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                gap_id,
                self.profile_id,
                card_id,
                dumps([card_id]),
                visual,
                1,
                "open",
                blob,
                space if blob else None,
                stamp,
                stamp,
            ),
        )
        row = {
            "id": gap_id,
            "profile_id": self.profile_id,
            "card_id": card_id,
            "card_ids": dumps([card_id]),
            "visual": visual,
            "count": 1,
            "status": "open",
            "embedding": blob,
            "embedding_space": space if blob else None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        self._rows.append(row)
        self._matrix = None  # 追加了行，矩阵按 _rows 顺序缓存必须失效
        if vector is not None:
            self._vectors[gap_id] = vector

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
                self.store.execute(
                    "UPDATE topic_gaps SET card_id=?,card_ids=?,count=?,updated_at=? WHERE id=?",
                    (card_id, dumps(ids), len(ids), now(), found["id"]),
                )
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
    return {
        "id": row["id"],
        "profile_id": row["profile_id"],
        "visual": row["visual"],
        "count": row["count"],
        "status": row["status"],
        "card_ids": ids,
        "cards": [{"id": i, "title": titles.get(i)} for i in ids],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_gaps(store, profile_id: str, *, status: str | None = "open", limit: int = 200) -> list[dict]:
    profiles.get_profile(store, profile_id)
    if status in (None, "all"):
        rows = store.rows(
            "SELECT * FROM topic_gaps WHERE profile_id=? ORDER BY status DESC, count DESC, created_at "
            "LIMIT ?",
            (profile_id, limit),
        )
    else:
        if status not in ("open", "done"):
            raise ValueError(f"未知的缺口状态：{status}")
        rows = store.rows(
            "SELECT * FROM topic_gaps WHERE profile_id=? AND status=? ORDER BY count DESC, created_at "
            "LIMIT ?",
            (profile_id, status, limit),
        )
    titles = {
        r["id"]: r["title"]
        for r in store.rows("SELECT id,title FROM topic_cards WHERE profile_id=?", (profile_id,))
    }
    return [_view(row, titles) for row in rows]


def update_gap(store, gap_id: str, changes: GapChanges) -> dict:
    check_id(gap_id, "tg", "素材缺口")
    row = store.one("SELECT * FROM topic_gaps WHERE id=?", (gap_id,))
    if row is None:
        raise EntityNotFound("找不到素材缺口")
    store.execute("UPDATE topic_gaps SET status=?,updated_at=? WHERE id=?", (changes.status, now(), gap_id))
    row = store.one("SELECT * FROM topic_gaps WHERE id=?", (gap_id,))
    titles = {
        r["id"]: r["title"]
        for r in store.rows("SELECT id,title FROM topic_cards WHERE profile_id=?", (row["profile_id"],))
    }
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
        writer.writerow(
            [
                cell(gap["visual"]),
                gap["count"],
                "已采集" if gap["status"] == "done" else "待采集",
                cell(titles),
                gap["updated_at"],
            ]
        )
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")
