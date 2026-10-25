"""素材覆盖：用选题的关键画面描述查询活动索引（与创作召回同一条检索路径），给出命中片段与缺口。

命中判定：首个结果的向量路由余弦相似度不低于 ``topics.coverage.min_similarity``；
嵌入不可用而降级为 BM25 时，有结果即记为命中并标注 ``degraded``。没有活动索引时覆盖率为 0 并给出原因，
不伪造向量或结果。
"""

from __future__ import annotations

from mindex.models.adapters import ModelError


def best_similarity(hit: dict) -> float | None:
    values = []
    for match in hit.get("matches") or []:
        if match.get("cosine_similarity") is not None:
            values.append(match["cosine_similarity"])
        for inner in match.get("rank_details") or []:
            if inner.get("cosine_similarity") is not None:
                values.append(inner["cosine_similarity"])
    return round(max(values), 4) if values else None


def clip_view(hit: dict) -> dict:
    return {"segment_id": hit["id"], "source_id": hit.get("source_id"), "source_name": hit.get("source_name"),
            "kind": hit.get("kind"), "start": hit.get("start"), "end": hit.get("end"),
            "score": round(float(hit.get("score") or 0), 6), "similarity": best_similarity(hit),
            "thumbnail_url": hit.get("thumbnail_url"), "media_url": hit.get("media_url")}


def empty(visuals: list[str], status: str, reason: str) -> dict:
    return {"status": status, "reason": reason, "index_id": None, "degraded": False,
            "visuals": [{"visual": v, "hit": False, "similarity": None, "clips": []} for v in visuals],
            "hits": 0, "total": len(visuals), "ratio": 0.0}


def report(config, store, visuals: list[str], *, embedder=None, engine=None) -> dict:
    """``engine`` 供测试注入（需有 ``text(TextSearch)``）；缺省按活动索引构造 ``SearchEngine``。"""
    from mindex.shared.types import TextSearch

    visuals = [v for v in visuals if v and v.strip()]
    if not visuals:
        return empty([], "ok", "没有关键画面")
    active = store.active_index()
    if engine is None:
        if active is None:
            return empty(visuals, "no_index", "没有活动索引：先在素材管理页处理并发布素材")
        from mindex.search.search import SearchEngine

        engine = SearchEngine(config, embedder, index_id=active["id"])
    limits = config.topics.coverage
    rows, degraded = [], False
    for visual in visuals:
        try:
            result = engine.text(TextSearch(text=visual[:400], granularity=config.studio.recall.granularity,
                                            limit=limits.limit))
        except ModelError as exc:
            return {**empty(visuals, "unavailable", f"检索不可用：{exc}"), "index_id": active and active["id"]}
        except ValueError as exc:
            rows.append({"visual": visual, "hit": False, "similarity": None, "clips": [], "error": str(exc)})
            continue
        clips = [clip_view(hit) for hit in result.get("results") or []]
        top = clips[0]["similarity"] if clips else None
        lowered = bool(result.get("degraded"))
        degraded = degraded or lowered
        hit = bool(clips) and (lowered or (top is not None and top >= limits.min_similarity))
        rows.append({"visual": visual, "hit": hit, "similarity": top, "clips": clips})
    hits = sum(1 for row in rows if row["hit"])
    return {"status": "ok", "reason": "嵌入不可用，按 BM25 文本检索判断" if degraded else None,
            "index_id": active["id"] if active else None, "degraded": degraded, "visuals": rows,
            "hits": hits, "total": len(rows), "ratio": round(hits / len(rows), 4)}


def gaps(coverage: dict) -> list[str]:
    return [row["visual"] for row in coverage.get("visuals") or [] if not row["hit"]]
