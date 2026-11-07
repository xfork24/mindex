"""SQL 内的 eligible 关系与 BM25 评分（plans/horizontal-scaling-plan.md §6.1）。

检索路径（format 2 且派生索引可用时）不再装载全量快照：

1. ``eligible_projection``：search_segments 小投影 + 隐藏（素材/自身/父镜头）与
   时长过滤下推 SQL（数值 OR 真值按方言 CASE 化），分析字段在 Python casefold；
2. ``sql_bm25``：eligible 片段放入**请求级临时表**（同一连接事务内创建并随事务
   消亡），N/mean/df 在该 eligible 上计算（缺文档长度 0 计入分母；mean 为 0 按
   1），按查询 token JOIN 倒排在 SQL 内累计 Okapi BM25（k1=1.5、b=0.75，与
   search.lexical_search 参考公式一致）；
3. SQL 只负责候选选取（score DESC、segment_id 确定序，保留 count+margin 的完整
   边界候选集合），再按参考公式对候选逐项精确重算与裁切——聚合浮点顺序不承诺
   逐位一致，验收容差 1e-10（plans §6.1）。

高频词与复杂人物筛选仍可能 O(N)；此处不做静默截断或改用全库统计。
"""

from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING

from shared.tokenizer import document_terms

if TYPE_CHECKING:
    from shared.storage import Store

log = logging.getLogger(__name__)

__all__ = ["eligible_projection", "sql_bm25"]

# 候选边界余量：SQL 浮点与参考公式的误差远小于典型分差，边界并列候选全部保留。
_CANDIDATE_MARGIN = 64
_CHUNK = 400

_HIDDEN = {
    "sqlite": "(COALESCE(c.hidden,0) OR COALESCE(sc.hidden,0) OR COALESCE(pc.hidden,0))",
    "pg": (
        "(CASE WHEN COALESCE(c.hidden,0)<>0 OR COALESCE(sc.hidden,0)<>0 OR COALESCE(pc.hidden,0)<>0"
        " THEN 1 ELSE 0 END)"
    ),
}


def _dialect(store: Store) -> str:
    return "pg" if store.pg else "sqlite"


def eligible_sql(store, index_id, granularity, filters, selected_sources):
    hidden = _HIDDEN[_dialect(store)]
    clauses = ["g.index_id=?", "g.kind=?", f"{hidden}=0", "g.end - g.start >= ?"]
    args = [index_id, granularity, filters.min_duration]
    if filters.max_duration is not None:
        clauses.append("g.end - g.start <= ?")
        args.append(filters.max_duration)
    if selected_sources is not None:
        sources = sorted(selected_sources)
        if not sources:
            clauses.append("1=0")
        else:
            clauses.append("g.source_id IN (" + ",".join("?" for _ in sources) + ")")
            args.extend(sources)
    return (
        "FROM search_segments g JOIN sources s ON s.id=g.source_id "
        "LEFT JOIN segment_curation c ON c.id=g.segment_id "
        "LEFT JOIN source_curation sc ON sc.id=g.source_id "
        "LEFT JOIN segment_curation pc ON pc.id=g.parent_id WHERE " + " AND ".join(clauses),
        args,
    )


def projected_row(row, filters):
    item = dict(row)
    if "annotation" not in item:
        item["annotation"] = json.loads(item["annotation_projection"])
        item["indexed_views"] = json.loads(item["indexed_views"])
    annotation = item["annotation"]
    if any(
        value and not any(value.casefold() in entry.casefold() for entry in annotation.get(field) or [])
        for field, value in [
            ("scene", filters.scene),
            ("emotions", filters.emotion),
            ("shot_size", filters.shot_size),
        ]
    ):
        return None
    return item


def eligible_projection(store, index_id, granularity, filters, selected_sources) -> list[dict]:
    """本地参考路径；集群使用相同定义的 EligibleSet 分页 SQL 关系。"""
    query, args = eligible_sql(store, index_id, granularity, filters, selected_sources)
    rows = store.rows(
        "SELECT g.segment_id AS id,g.source_id,g.parent_id,g.kind,g.start,g.end,"
        "g.annotation_projection,g.indexed_views " + query,
        args,
    )
    return [item for row in rows if (item := projected_row(row, filters)) is not None]


def sql_bm25(
    store: Store, index_id: str, channel: str, text: str, eligible_ids: list[str], count: int
) -> list[dict]:
    """该渠道 eligible 语料的 BM25 候选（参考公式精确重算后裁切到 count）。"""
    terms = list(document_terms(text)[0].keys()) if text else []
    if not terms or not eligible_ids:
        return []
    with store.connect() as db:
        relation = getattr(eligible_ids, "relation", None)
        table = relation.table if relation is not None else "_eligible"
        if relation is None:
            _populate_eligible(db, store, eligible_ids)

        def execute(sql, args=()):
            return db.execute(sql.replace("_eligible", table), args)

        stats = execute(
            "SELECT count(*) AS n, CASE WHEN COALESCE(avg(d.doc_length),0)>0 "
            "THEN avg(d.doc_length) ELSE 1 END AS mean_length "
            "FROM lexical_docs d JOIN _eligible e ON e.segment_id=d.segment_id "
            "WHERE d.index_id=? AND d.channel=?",
            (index_id, channel),
        ).fetchone()
        total, mean_length = int(stats["n"]), float(stats["mean_length"])
        if total == 0:
            return []
        # SQL 内累计同一 Okapi BM25（k1=1.5、b=0.75、+1 内 IDF）用于候选选取；
        # N/mean/df 全部来自同一 eligible 临时表，与参考统计口径一致。
        query_terms_sql = ",".join("?" * len(terms))
        candidates = execute(
            "WITH dfs AS ("
            " SELECT tx.term AS term, count(*) AS df FROM lexical_terms tx"
            " JOIN _eligible ex ON ex.segment_id=tx.segment_id"
            f" WHERE tx.index_id=? AND tx.channel=? AND tx.term IN ({query_terms_sql})"
            " GROUP BY tx.term),"
            " scored AS ("
            " SELECT t.segment_id AS id, SUM("
            f"  ln(1 + ({total} - dfs.df + 0.5)/(dfs.df + 0.5))"
            f"  * t.tf * 2.5 / (t.tf + 1.5*(0.25 + 0.75*d.doc_length/{mean_length!r}))"
            " ) AS score"
            " FROM lexical_terms t"
            " JOIN lexical_docs d ON d.index_id=t.index_id AND d.segment_id=t.segment_id AND "
            "d.channel=t.channel"
            " JOIN _eligible e ON e.segment_id=t.segment_id"
            " JOIN dfs ON dfs.term=t.term"
            " WHERE t.index_id=? AND t.channel=?"
            " GROUP BY t.segment_id)"
            " SELECT id, score FROM scored WHERE score > 0"
            " ORDER BY score DESC, id LIMIT ?",
            (index_id, channel, *terms, index_id, channel, count + _CANDIDATE_MARGIN),
        ).fetchall()
        if not candidates:
            return []
        df_rows = execute(
            "SELECT t.term, count(*) AS df FROM lexical_terms t "
            "JOIN _eligible e ON e.segment_id=t.segment_id "
            f"WHERE t.index_id=? AND t.channel=? AND t.term IN ({query_terms_sql}) GROUP BY t.term",
            (index_id, channel, *terms),
        ).fetchall()
        frequency = {row["term"]: int(row["df"]) for row in df_rows}
        # 边界候选集合按参考公式逐项精确重算（与 lexical_search 的累计顺序一致）。
        candidate_ids = [row["id"] for row in candidates]
        id_sql = ",".join("?" * len(candidate_ids))
        postings: dict[str, dict] = {}
        for row in execute(
            "SELECT t.segment_id AS id, t.term, t.tf, d.doc_length FROM lexical_terms t "
            "JOIN lexical_docs d ON d.index_id=t.index_id AND d.segment_id=t.segment_id AND "
            "d.channel=t.channel "
            f"WHERE t.index_id=? AND t.channel=? AND t.term IN ({query_terms_sql}) "
            f"AND t.segment_id IN ({id_sql})",
            (index_id, channel, *terms, *candidate_ids),
        ).fetchall():
            entry = postings.setdefault(row["id"], {"length": int(row["doc_length"]), "tf": {}})
            entry["tf"][row["term"]] = int(row["tf"])
        results = []
        for segment_id, entry in postings.items():
            score = 0.0
            for term in terms:  # 与参考实现相同的词序累计
                tf = entry["tf"].get(term, 0)
                if not tf or term not in frequency:
                    continue
                idf = math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * entry["length"] / mean_length))
            if score > 0:
                results.append({"id": segment_id, "score": score})
        results.sort(key=lambda item: (-item["score"], item["id"]))
        return results[:count]


def dialogue_projection(store: Store, index_id: str, identifiers: list[str]) -> dict[str, str]:
    """姓名提及匹配只读对白文本，保持 Unicode/词边界的参考语义。"""
    result = {}
    for offset in range(0, len(identifiers), _CHUNK):
        batch = identifiers[offset : offset + _CHUNK]
        marks = ",".join("?" * len(batch))
        for row in store.rows(
            "SELECT segment_id,json_extract(snapshot,'$.views.dialogue') AS dialogue "
            f"FROM index_members WHERE index_id=? AND segment_id IN ({marks})",
            [index_id, *batch],
        ):
            result[row["segment_id"]] = row["dialogue"] or ""
    return result


def _populate_eligible(db, store: Store, eligible_ids: list[str]) -> None:
    """请求级临时表：同一事务内建、灌、查、消亡（PG 用 ON COMMIT DROP 归还池连接）。"""
    db.execute("DROP TABLE IF EXISTS _eligible")
    db.execute(
        "CREATE TEMP TABLE _eligible(segment_id TEXT PRIMARY KEY)" + (" ON COMMIT DROP" if store.pg else "")
    )
    for offset in range(0, len(eligible_ids), _CHUNK):
        db.executemany(
            "INSERT OR IGNORE INTO _eligible(segment_id) VALUES(?)",
            [(identifier,) for identifier in eligible_ids[offset : offset + _CHUNK]],
        )
