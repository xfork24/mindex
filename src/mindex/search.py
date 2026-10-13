from __future__ import annotations

import math
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter

import jieba

from .adapters import ModelError, MultimodalEmbedder, make_embedder
from .config import Config
from .curation import SOURCE_SQL, Library, source_public
from .indexing import VectorIndex
from .media import cut, probe, windows
from .storage import Store
from .types import Filters, TextSearch

jieba.setLogLevel(30)


def tokens(text: str) -> list[str]:
    return [t.lower() for t in jieba.cut_for_search(text) if re.search(r"[\w\u4e00-\u9fff]", t)]


def lexical_search(text: str, snapshots: list[dict], count: int, exclude=()) -> list[dict]:
    terms = Counter(tokens(text))
    documents = [
        Counter(tokens("\n".join(v for k, v in s["views"].items() if k not in exclude))) for s in snapshots
    ]
    lengths = [sum(doc.values()) for doc in documents]
    mean_length = sum(lengths) / max(1, len(lengths)) or 1
    frequency = Counter(term for document in documents for term in document)
    results = []
    for segment, document, length in zip(snapshots, documents, lengths):
        score = 0.0
        for term in terms:
            tf = document[term]
            if not tf:
                continue
            idf = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / mean_length))
        if score > 0:
            results.append({"id": segment["id"], "score": score})
    return sorted(results, key=lambda v: (-v["score"], v["id"]))[:count]


def fuse(routes: dict[str, list[dict]], weights: dict[str, float], k: int) -> list[dict]:
    scores, reasons = defaultdict(float), defaultdict(list)
    for route, hits in routes.items():
        if weights.get(route, 0) == 0:
            continue
        seen = set()
        position = 0
        rank = 0
        previous_value = None
        for hit in hits:
            if hit["id"] in seen:
                continue
            seen.add(hit["id"])
            position += 1
            value = hit.get("distance", hit.get("score"))
            if (
                value is None
                or previous_value is None
                or not math.isclose(value, previous_value, rel_tol=1e-8, abs_tol=1e-8)
            ):
                rank = position
            previous_value = value
            scores[hit["id"]] += weights[route] / (k + rank)
            reason = {"route": route, "rank": rank}
            if "distance" in hit:
                reason["cosine_similarity"] = 1 - hit["distance"]
            reasons[hit["id"]].append(reason)
    return [
        {"id": identifier, "score": score, "matches": reasons[identifier]}
        for identifier, score in sorted(scores.items(), key=lambda v: (-v[1], v[0]))
    ]


def time_iou(left: dict, right: dict) -> float:
    if left["source_id"] != right["source_id"]:
        return 0
    intersection = max(0, min(left["end"], right["end"]) - max(left["start"], right["start"]))
    union = max(left["end"], right["end"]) - min(left["start"], right["start"])
    return intersection / union if union else 0


def public_segment(segment: dict) -> dict:
    return {
        key: value
        for key, value in segment.items()
        if key not in {"path", "thumbnail", "source_path", "views", "generation"}
    } | {
        "media_url": f"/api/media/{segment['id']}",
        "thumbnail_url": f"/api/thumbnails/{segment['id']}",
        "context_url": f"/api/context/{segment['id']}",
    }


class SearchEngine:
    def __init__(self, config: Config, embedder: MultimodalEmbedder | None = None):
        self.config = config
        self.store = Store(config.data_dir)
        self.embedder = embedder or make_embedder(config)

    def _context(self, granularity: str, filters: Filters):
        active = self.store.active_index()
        if active is None:
            raise ValueError("尚未发布索引，请先完成离线处理并执行 index --activate")
        if active["status"] != "ready":
            raise ModelError("当前索引未就绪")
        identity = self.embedder.identity()
        if identity != active["spec"]["space"]:
            raise ModelError("当前模型与已发布索引的向量空间不同，请切回对应模型或建立新索引")
        if self.config.video.model_dump() != active["spec"]["preprocessing"]:
            raise ModelError("视频预处理配置与索引不一致，请使用对应配置")
        snapshots = []
        library = Library(self.store)
        sources, segments = library.overlays()
        for segment in self.store.snapshots(active["id"]):
            segment = library.decorate(segment, sources, segments)
            if segment["effective_hidden"]:
                continue
            if segment["kind"] != granularity:
                continue
            duration = segment["end"] - segment["start"]
            if filters.source_id and segment["source_id"] != filters.source_id:
                continue
            if duration < filters.min_duration or (
                filters.max_duration is not None and duration > filters.max_duration
            ):
                continue
            annotation = segment["annotation"]
            if any(
                value and not any(value.casefold() in item.casefold() for item in annotation[field])
                for field, value in [
                    ("scene", filters.scene),
                    ("emotions", filters.emotion),
                    ("shot_size", filters.shot_size),
                ]
            ):
                continue
            snapshots.append(segment)
        return active, snapshots, VectorIndex(self.store.root, active["id"])

    def _result(
        self,
        active: dict,
        snapshots: list[dict],
        routes: dict,
        limit: int,
        started: float,
        ranked: list[dict] | None = None,
    ):
        if ranked is None:
            ranked = fuse(routes, self.config.search.weights, self.config.search.rrf_k)
        lookup = {s["id"]: s for s in snapshots}
        selected, results = [], []
        for hit in ranked:
            segment = lookup[hit["id"]]
            if any(time_iou(segment, prior) > 0.5 for prior in selected):
                continue
            selected.append(segment)
            results.append({**public_segment(segment), "score": hit["score"], "matches": hit["matches"]})
            if len(results) >= limit:
                break
        return {
            "index_id": active["id"],
            "index_mode": active["spec"].get("mode", "enriched"),
            "results": results,
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "score_type": "weighted_rrf",
            "eligible_segments": len(snapshots),
        }

    def text(self, query: TextSearch) -> dict:
        started = perf_counter()
        active, snapshots, vectors = self._context(query.granularity, query.filters)
        if not snapshots:
            return self._result(active, snapshots, {}, query.limit, started)
        vector = self.embedder.text([query.text], role="query")[0]
        ids = [s["id"] for s in snapshots]
        count = self.config.search.candidates
        available = {"video", *(view for s in snapshots for view in s["views"])}
        routes = {
            view: vectors.query(vector, view, ids, count)
            for view in ("video", "description", "style", "dialogue")
            if self.config.search.weights[view] > 0 and view in available
        }
        if self.config.search.weights["lexical"] > 0:
            routes["lexical"] = lexical_search(
                query.text, snapshots, count, self.config.search.lexical_exclude
            )
        return self._result(active, snapshots, routes, query.limit, started)

    def video(self, path: Path, granularity="moment", limit=20, filters: Filters | None = None) -> dict:
        started = perf_counter()
        info = probe(path)
        if info["duration"] > self.config.search.max_query_seconds + 0.05:
            raise ValueError(f"查询视频不能超过 {self.config.search.max_query_seconds:g} 秒")
        active, snapshots, vectors = self._context(granularity, filters or Filters())
        if not snapshots:
            return self._result(active, snapshots, {}, limit, started)
        v = self.config.video
        pieces = windows(0, info["duration"], v.window_seconds, v.stride_seconds)
        routes = {}
        for index, (start, end) in enumerate(pieces):
            if len(pieces) == 1:
                embedding = self.embedder.video(path)
            else:
                with tempfile.TemporaryDirectory(prefix="query-window-", dir=self.store.root) as directory:
                    window = Path(directory) / "window.mp4"
                    cut(path, start, end, window)
                    embedding = self.embedder.video(window)
            routes[f"video_{index}"] = vectors.query(
                embedding, "video", [s["id"] for s in snapshots], self.config.search.candidates
            )
        # Query windows are equal votes; the number of windows must not change scale.
        weights = {key: 1 / len(routes) for key in routes}
        fused = fuse(routes, weights, self.config.search.rrf_k)
        for hit in fused:
            for match in hit["matches"]:
                match["route"] = "video"
        return self._result(active, snapshots, {}, limit, started, ranked=fused)


def status(config: Config, *, health=False, embedder: MultimodalEmbedder | None = None) -> dict:
    store = Store(config.data_dir)
    active = store.active_index()
    result = {
        "active_index": active,
        "sources": [
            source_public(s)
            for s in store.rows(SOURCE_SQL + " WHERE COALESCE(c.hidden,0)=0 ORDER BY s.name,s.id")
        ],
        "segment_counts": store.rows("SELECT kind,count(*) AS count FROM segments GROUP BY kind"),
        "runs": store.rows("SELECT * FROM runs ORDER BY id DESC LIMIT 15"),
        "detector": config.detector.backend,
        "embedding": {
            "model": config.embedding.model,
            "dimensions": config.embedding.dimensions,
            "quantization": config.embedding.quantization,
        },
        "model_health": {"checked": False},
    }
    if health:
        try:
            identity = (embedder or make_embedder(config)).identity()
            result["model_health"] = {
                "checked": True,
                "ready": True,
                "space": identity,
                "matches_index": active is None or active["spec"]["space"] == identity,
            }
        except ModelError as exc:
            result["model_health"] = {"checked": True, "ready": False, "error": str(exc)}
    return result
