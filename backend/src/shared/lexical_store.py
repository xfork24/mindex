"""派生词法索引（plans/horizontal-scaling-plan.md §6.1）。

以**冻结快照**构建三张派生表（``search_segments``/``lexical_docs``/``lexical_terms``），
SQL 内完成 eligible 统计与 BM25 评分；可随时删除重建，不改变已发布内容快照。

- 构建复用唯一分词实现（shared/tokenizer.py），并把分词器版本写入
  ``lexical_index_meta``；版本不一致的节点拒绝构建/查询新派生索引。
- 每个 segment 为 dialogue/analysis 各建一行文档（缺文档长度 0，计入分母）；
  term 为精确逐字节相等，不做模糊或不区分大小写 collation。
- 旧索引（本阶段之前构建）没有派生表，检索回退 Python 参考路径并给出升级指引。

双方言：表 DDL 在 storage._metadata_v12 与 schema_pg.py 各自维护；本模块的
INSERT/DELETE/SELECT 均为可移植语句（占位符由适配层处理）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from shared.storage import dumps
from shared.tokenizer import document_terms, tokenizer_version

if TYPE_CHECKING:
    from shared.storage import Store

log = logging.getLogger(__name__)

__all__ = ["LEXICAL_CHANNELS", "build_lexical_index", "drop_lexical_index", "lexical_ready"]

# format 2 的词法渠道：与检索路径的视图键一致（video 是向量视图，不入词法）。
LEXICAL_CHANNELS = ("dialogue", "analysis")
_ANNOTATION_FIELDS = ("scene", "emotions", "shot_size")
_BATCH = 500


def lexical_ready(store: Store, index_id: str) -> bool:
    """派生索引是否可用于该索引版本：已构建且分词器版本与本节点一致。"""
    meta = store.one("SELECT tokenizer_version FROM lexical_index_meta WHERE index_id=?", (index_id,))
    if meta is None:
        return False
    if meta["tokenizer_version"] != tokenizer_version():
        log.warning("索引 %s 的派生词法索引分词器版本不一致，回退 Python 参考路径", index_id)
        return False
    return True


def build_lexical_index(store: Store, index_id: str, *, replace: bool = False) -> dict:
    """以冻结快照（index_members, complete=1）构建派生表；返回统计。

    必须在 index-write 临界区内调用（发布流程或回填命令）；幂等：已存在且版本
    一致时跳过，``replace=True`` 强制重建。
    """
    version = tokenizer_version()
    meta = store.one(
        "SELECT tokenizer_version,segments FROM lexical_index_meta WHERE index_id=?", (index_id,)
    )
    if meta and not replace:
        if meta["tokenizer_version"] != version:
            raise ValueError("派生词法索引分词器版本与当前节点不一致；需 replace=True 强制重建")
        return {"index_id": index_id, "skipped": True, "segments": meta["segments"]}

    counts = {"segments": 0, "docs": 0, "terms": 0}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for table in ("lexical_terms", "lexical_docs", "search_segments", "lexical_index_meta"):
            db.execute(f"DELETE FROM {table} WHERE index_id=?", (index_id,))
        cursor = None
        while True:
            boundary, args = ("", ()) if cursor is None else (" AND segment_id>?", (cursor,))
            rows = db.execute(
                "SELECT segment_id,snapshot FROM index_members WHERE index_id=? AND complete=1"
                + boundary
                + " ORDER BY segment_id LIMIT ?",
                (index_id, *args, _BATCH),
            ).fetchall()
            if not rows:
                break
            segments, docs = [], []
            terms: list[tuple[str, str, str, str, int]] = []
            for row in rows:
                import json

                snapshot = json.loads(row["snapshot"])
                annotation = snapshot.get("annotation") or {}
                segments.append(
                    (
                        index_id,
                        snapshot["id"],
                        snapshot["source_id"],
                        snapshot.get("parent_id"),
                        snapshot["kind"],
                        snapshot["start"],
                        snapshot["end"],
                        dumps({field: list(annotation.get(field) or []) for field in _ANNOTATION_FIELDS}),
                        dumps(sorted(snapshot.get("indexed_views") or [])),
                    )
                )
                views = snapshot.get("views") or {}
                for channel in LEXICAL_CHANNELS:
                    counter, total = document_terms(
                        "\n".join(value for key, value in views.items() if key == channel)
                    )
                    docs.append((index_id, snapshot["id"], channel, total))
                    terms.extend(
                        (index_id, channel, term, snapshot["id"], tf) for term, tf in counter.items()
                    )
            db.executemany(
                "INSERT INTO search_segments(index_id,segment_id,source_id,parent_id,kind,"
                'start,"end",annotation_projection,indexed_views) VALUES(?,?,?,?,?,?,?,?,?)',
                segments,
            )
            db.executemany(
                "INSERT INTO lexical_docs(index_id,segment_id,channel,doc_length) VALUES(?,?,?,?)", docs
            )
            for offset in range(0, len(terms), _BATCH):
                db.executemany(
                    "INSERT INTO lexical_terms(index_id,channel,term,segment_id,tf) VALUES(?,?,?,?,?)",
                    terms[offset : offset + _BATCH],
                )
            for key, values in (("segments", segments), ("docs", docs), ("terms", terms)):
                counts[key] += len(values)
            cursor = rows[-1]["segment_id"]
        db.execute(
            "INSERT INTO lexical_index_meta(index_id,tokenizer_version,segments,docs,terms) "
            "VALUES(?,?,?,?,?)",
            (index_id, version, counts["segments"], counts["docs"], counts["terms"]),
        )
    log.info(
        "派生词法索引构建完成 · %s · %d segments · %d terms", index_id, counts["segments"], counts["terms"]
    )
    return {"index_id": index_id, **counts}


def drop_lexical_index(store: Store, index_id: str) -> None:
    """删除该索引的派生表（索引删除/回滚时调用；检索将回退 Python 路径）。"""
    with store.connect() as db:
        for table in ("lexical_terms", "lexical_docs", "search_segments", "lexical_index_meta"):
            db.execute(f"DELETE FROM {table} WHERE index_id=?", (index_id,))
