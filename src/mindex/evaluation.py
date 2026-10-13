from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .config import Config, fingerprint
from .media import content_hash
from .search import SearchEngine
from .storage import Store
from .types import TextSearch


def relevance(result: dict, targets: list[dict]) -> float:
    grade = 0.0
    for target in targets:
        if "segment_id" in target:
            matched = result["id"] == target["segment_id"]
        else:
            intersection = max(0, min(result["end"], target["end"]) - max(result["start"], target["start"]))
            shorter = min(result["end"] - result["start"], target["end"] - target["start"])
            matched = (
                result["source_id"] == target["source_id"]
                and shorter > 0
                and intersection / shorter >= target.get("min_overlap", 0.5)
            )
        if matched:
            grade = max(grade, target.get("grade", 3))
    return grade


def metrics(results: list[dict], targets: list[dict]) -> dict:
    grades = [relevance(result, targets) for result in results]
    # Interval targets are relevance groups: repeated windows matching one group
    # do not receive multiple rewards. This also bounds the ideal ranking.
    used, unique_grades = set(), []
    for result in results[:10]:
        candidates = [
            (index, relevance(result, [target])) for index, target in enumerate(targets) if index not in used
        ]
        best = max(candidates, key=lambda v: v[1], default=(None, 0))
        if best[1] > 0:
            used.add(best[0])
        unique_grades.append(best[1])
    dcg = sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(unique_grades))
    ideal = sorted([t.get("grade", 3) for t in targets], reverse=True)[:10]
    idcg = sum((2**grade - 1) / math.log2(rank + 2) for rank, grade in enumerate(ideal))
    return {
        "hit_at_1": float(any(g > 0 for g in grades[:1])),
        "hit_at_5": float(any(g > 0 for g in grades[:5])),
        "mrr_at_10": next((1 / (i + 1) for i, g in enumerate(grades[:10]) if g > 0), 0),
        "ndcg_at_10": dcg / idcg if idcg else 0,
    }


def evaluate(
    config: Config, path: Path, split: str, output: Path | None = None, *, index_id: str | None = None
) -> dict:
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    entries = [entry for entry in entries if entry.get("split") == split]
    if any(entry.get("label_status", "confirmed") != "confirmed" for entry in entries):
        raise ValueError("标注草稿不能计质量分数；请先人工复核，或使用 preview 查看排名")
    if not entries or any(not entry.get("relevant") and not entry.get("no_answer") for entry in entries):
        raise ValueError("所选分组必须包含已标注的查询，relevant 不能为空")
    profiles = {
        "video_only": {"video": 1},
        "video_asr": {key: config.search.weights[key] for key in ("video", "dialogue", "lexical")},
        "description_only": {"description": 1},
        "fusion": config.search.weights,
        "no_dialogue": {**config.search.weights, "dialogue": 0},
        "no_style": {**config.search.weights, "style": 0},
    }
    store = Store(config.data_dir)
    active = store.index_version(index_id) if index_id else store.active_index()
    if not active:
        raise ValueError("请先发布待评测索引")
    snapshots = store.snapshots(active["id"])
    candidate_layout = sorted(
        [{key: row[key] for key in ("id", "source_id", "kind", "start", "end")} for row in snapshots],
        key=lambda row: row["id"],
    )
    available = {
        "video",
        "lexical",
        *(view for row in snapshots for view in row["views"]),
    }
    curation = {
        table: store.rows(f"SELECT * FROM {table} ORDER BY 1")
        for table in ("source_curation", "segment_curation")
    }
    candidate_id = fingerprint([active["id"], curation])
    reports = {}
    for name, weights in profiles.items():
        if not any(weight > 0 and key in available for key, weight in weights.items()):
            reports[name] = {"queries": 0, "status": "unavailable_views", "details": []}
            continue
        current = config.model_copy(deep=True)
        current.search.weights = {key: weights.get(key, 0) for key in config.search.weights}
        excluded = {
            "video_asr": ["description", "style"],
            "no_dialogue": ["dialogue"],
            "no_style": ["style"],
        }.get(name, [])
        current.search.lexical_exclude = list(dict.fromkeys([*config.search.lexical_exclude, *excluded]))
        engine = SearchEngine(current, index_id=active["id"])
        rows = []
        for entry in entries:
            if "text" in entry:
                result = engine.text(
                    TextSearch(text=entry["text"], granularity=entry.get("granularity", "moment"), limit=20)
                )
            elif "video" in entry:
                if name != "video_only":
                    continue
                result = engine.video(path.parent / entry["video"], limit=20)
            elif "image" in entry:
                if name != "video_only":
                    continue
                result = engine.image(path.parent / entry["image"], limit=20)
            else:
                raise ValueError("每条评测查询必须有 text、video 或 image")
            rows.append(
                {
                    "id": entry["id"],
                    **(metrics(result["results"], entry["relevant"]) if not entry.get("no_answer") else {}),
                    "diagnostic_no_answer": bool(entry.get("no_answer")),
                    "modality": "text" if "text" in entry else "image" if "image" in entry else "video",
                    "index_id": result["index_id"],
                    "eligible_segments": result["eligible_segments"],
                    "elapsed_ms": result["elapsed_ms"],
                    "results": result["results"],
                }
            )
        scored = [r for r in rows if not r["diagnostic_no_answer"]]
        reports[name] = {
            "weights": current.search.weights,
            "lexical_exclude": current.search.lexical_exclude,
            "queries": len(rows),
            "scored_queries": len(scored),
            **{
                key: sum(r[key] for r in scored) / len(scored) if scored else None
                for key in ("hit_at_1", "hit_at_5", "mrr_at_10", "ndcg_at_10")
            },
            "latency_ms": {
                "first": rows[0]["elapsed_ms"] if rows else None,
                "warm_p50": float(np.percentile([r["elapsed_ms"] for r in rows[1:]], 50))
                if len(rows) > 1
                else None,
                "warm_p95": float(np.percentile([r["elapsed_ms"] for r in rows[1:]], 95))
                if len(rows) > 1
                else None,
            },
            "details": rows,
        }
        if (not index_id and store.active_index()["id"] != active["id"]) or candidate_id != fingerprint(
            [active["id"], {table: store.rows(f"SELECT * FROM {table} ORDER BY 1") for table in curation}]
        ):
            raise ValueError("评测过程中索引或整理状态发生变化，拒绝比较不同候选集")
    report = {
        "split": split,
        "profiles": reports,
        "embedding": config.embedding.model_dump(),
        "space": active["spec"]["space"],
        "index_id": active["id"],
        "candidate_id": candidate_id,
        "candidate_layout": candidate_layout,
        "candidate_layout_hash": fingerprint(candidate_layout),
        "curation_hash": fingerprint(curation),
        "queries_sha256": content_hash(path),
        "config": config.model_dump(mode="json"),
        "quality_target": {"hit_at_5": 0.85, "minimum_test_queries": 40},
        "note": "时间区间按相关组计分；仅已标注目标视为相关，正式评测前应补全候选池标注。",
        "acceptance": "smoke_only" if len(entries) < 40 else "requires_multi_source_and_manual_review",
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report
