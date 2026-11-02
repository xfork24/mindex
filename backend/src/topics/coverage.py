"""素材覆盖：用选题的关键画面描述查询活动索引（与创作召回同一条检索路径），给出命中片段与缺口。

相似度与 BM25 仅召回候选；可用时长、镜头画面分析和模型相关性核验均通过后才计为覆盖。
核验失败或证据不足时保守不计命中，保留候选和原因。
"""

from __future__ import annotations

from mindex.models.adapters import ModelError
from mindex.topics import coverage_verify
from mindex.topics.llm import make_llm, profile_of


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
    return {"verification_version": coverage_verify.VERSION,
            "status": status, "reason": reason, "index_id": None, "degraded": False,
            "visuals": [{"visual": v, "hit": False, "similarity": None, "clips": []} for v in visuals],
            "hits": 0, "total": len(visuals), "ratio": 0.0}


def report(config, store, visuals: list[str], *, embedder=None, engine=None, filters=None,
           llm=None, title: str = "", angle: str = "", limits=None) -> dict:
    """``engine`` 供测试注入（需有 ``text(TextSearch)``）；缺省按活动索引构造 ``SearchEngine``。"""
    from mindex.shared.types import Filters, TextSearch

    filters = filters or Filters()
    scope = filters.effective_sources(store)
    visuals = [v for v in visuals if v and v.strip()]
    if scope == set():
        return empty(visuals, "ok", "所选素材范围为空")
    if not visuals:
        return empty([], "ok", "没有关键画面")
    active = store.active_index()
    if engine is None:
        if active is None:
            return empty(visuals, "no_index", "没有活动索引：先在素材管理页处理并发布素材")
        from mindex.search.search import SearchEngine

        engine = SearchEngine(config, embedder, index_id=active["id"])
    limits = limits or config.topics.coverage
    rows, degraded, pending, targets = [], False, [], {}
    for visual in visuals:
        try:
            result = engine.text(TextSearch(text=visual[:400], granularity=config.studio.recall.granularity,
                                            limit=limits.limit, filters=filters.model_copy(update={
                                                "min_duration": max(filters.min_duration, limits.min_duration)})))
        except ModelError as exc:
            return {**empty(visuals, "unavailable", f"检索不可用：{exc}"), "index_id": active and active["id"]}
        except ValueError as exc:
            rows.append({"visual": visual, "hit": False, "similarity": None, "clips": [], "error": str(exc)})
            continue
        candidates = []
        lowered = bool(result.get("degraded"))
        degraded = degraded or lowered
        for candidate in result.get("results") or []:
            if scope is not None and candidate.get("source_id") not in scope:
                continue
            clip = clip_view(candidate)
            candidates.append(clip)
            duration = float(clip.get("end") or 0) - float(clip.get("start") or 0)
            evidence = coverage_verify.evidence_of(candidate)
            if duration < limits.min_duration:
                decision = {"verdict": "rejected", "reason": "片段时长不足，不能作为可用画面", "quote": ""}
            elif not lowered and (clip["similarity"] is None or clip["similarity"] < limits.min_similarity):
                decision = {"verdict": "rejected", "reason": "相似度低于候选粗筛阈值", "quote": ""}
            elif not evidence:
                decision = {"verdict": "uncertain", "reason": "缺少镜头画面分析，无法确认内容", "quote": ""}
            else:
                identifier = len(pending) + 1
                pending.append({"candidate": identifier, "visual": visual, "evidence": evidence,
                                "duration": round(duration, 3)})
                targets[identifier] = clip
                decision = None
            clip["verification"] = decision
        rows.append({"visual": visual, "hit": False, "similarity": None, "clips": [], "candidates": candidates})
    if pending:
        decisions = coverage_verify.verify(llm or make_llm(config), pending, title=title, angle=angle,
                                           retries=profile_of(config).split_retries)
        for identifier, decision in decisions.items():
            targets[identifier]["verification"] = decision
    failed = any(row.get("error") for row in rows)
    for row in rows:
        candidates = row.get("candidates", [])
        row["clips"] = [c for c in candidates if c["verification"]["verdict"] == "matched"]
        row["hit"] = bool(row["clips"])
        row["similarity"] = row["clips"][0]["similarity"] if row["clips"] else None
        failed = failed or any(c["verification"]["verdict"] == "error" for c in candidates)
    hits = sum(1 for row in rows if row["hit"])
    reasons = []
    if degraded:
        reasons.append("嵌入不可用，BM25 仅用于召回候选，覆盖仍须通过相关性核验")
    if failed:
        reasons.append("部分候选核验未完成，未计入覆盖，请重新打分")
    return {"verification_version": coverage_verify.VERSION,
            "status": "unverified" if failed else "ok", "reason": "；".join(reasons) or None,
            "index_id": active["id"] if active else None, "degraded": degraded, "visuals": rows,
            "hits": hits, "total": len(rows), "ratio": round(hits / len(rows), 4)}


def gaps(coverage: dict) -> list[str]:
    return [row["visual"] for row in coverage.get("visuals") or [] if not row["hit"]]
