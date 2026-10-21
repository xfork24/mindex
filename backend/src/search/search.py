from __future__ import annotations

import math
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter

import jieba

from mindex.admin.curation import SOURCE_SQL, Library, source_public
from mindex.models.adapters import ModelError, MultimodalEmbedder, make_embedder
from mindex.processing.indexing import VectorIndex
from mindex.shared.config import Config
from mindex.shared.images import image_png
from mindex.shared.media import cut, probe, windows
from mindex.shared.storage import Store
from mindex.shared.types import Filters, TextSearch

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
        if key not in {"path", "thumbnail", "source_path", "views", "generation", "representation_ids"}
    } | {
        "media_url": f"/api/media/{segment['id']}",
        "thumbnail_url": f"/api/thumbnails/{segment['id']}",
        "context_url": f"/api/context/{segment['id']}",
    }


class SearchEngine:
    def __init__(
        self, config: Config, embedder: MultimodalEmbedder | None = None, *, index_id: str | None = None,
        release_id: str | None = None,
    ):
        self.config = config
        self.store = Store(config.data_dir)
        self._embedder = embedder
        self.index_id = index_id
        self.release_id = release_id

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = make_embedder(self.config)
        return self._embedder

    def _validate_space(self, active):
        identity = self.embedder.identity()
        if hasattr(self.embedder, "require_gate"):
            self.embedder.require_gate()
        if identity != active["spec"]["space"]:
            raise ModelError("当前模型与已发布索引的向量空间不同，请切回对应模型或建立新索引")
        if self.config.video.model_dump() != active["spec"]["preprocessing"]:
            raise ModelError("视频预处理配置与索引不一致，请使用对应配置")

    def _context(self, granularity: str, filters: Filters, *, text=False, person=False):
        active = self.store.index_version(self.index_id) if self.index_id else self.store.active_index()
        if active is None:
            raise ValueError("尚未发布索引，请先完成离线处理并执行 index --activate")
        if active["status"] != "ready":
            raise ModelError("当前索引未就绪")
        if not text or (active["spec"].get("format", 1) == 1 and not person):
            self._validate_space(active)
        snapshots = []
        library = Library(self.store)
        sources, segments = library.overlays()
        selected_sources = filters.selected_sources()
        for segment in self.store.snapshots(active["id"]):
            segment = library.decorate(segment, sources, segments)
            if segment["effective_hidden"]:
                continue
            if segment["kind"] != granularity:
                continue
            duration = segment["end"] - segment["start"]
            if (filters.source_id or filters.source_ids) and segment["source_id"] not in selected_sources:
                continue
            if duration < filters.min_duration or (
                filters.max_duration is not None and duration > filters.max_duration
            ):
                continue
            annotation = segment.get("annotation") or {}
            if any(
                value and not any(value.casefold() in item.casefold() for item in annotation.get(field) or [])
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
        from mindex.persons.persons import Persons, overlaps_for, resolve_names

        started = perf_counter()
        catalogue = Persons(self.store)
        preliminary_ids, _, _, _ = resolve_names(query, catalogue.entities())
        active, snapshots, vectors = self._context(
            query.granularity, query.filters, text=True,
            person=bool(preliminary_ids and query.person_mode != 'mentioned'))
        release, published = catalogue.published(active['id'], pinned=self.index_id is not None, release_id=self.release_id or active.get('search_release_id'))
        # Newly registered names are also strict even before the first person publication.
        entities = list({e['id']: e for e in [*published['entities'], *catalogue.entities()]}.values())
        ids, recognized, remaining, variant_groups = resolve_names(query, entities)
        mode = 'onscreen' if query.person_mode == 'auto' and ids else query.person_mode
        meta = {'person_mode': mode, 'person_ids': ids,
                'recognized_people': [p for p in recognized if p.get('usage') != 'reference'],
                'release_id': release['id'] if release else None,
                'person_version': release['person_id'] if release else None}
        references = [p for p in recognized if p.get('usage') == 'reference']
        if references:
            meta['referenced_people'] = references
        if mode == 'onscreen' and not ids:
            raise ValueError('人物出镜模式需要选择人物或输入已登记姓名')
        if mode == 'onscreen':
            from mindex.persons.person_processing import EvidenceReferences
            from mindex.persons.persons import coverage_missing
            reference_images = EvidenceReferences(self.config, self.store)
            meta['person_coverage_missing'] = coverage_missing(snapshots, published['coverage'])
            eligible = []
            names = {e['id']: e['name'] for e in entities}
            for segment in snapshots:
                evidence = overlaps_for(segment, published['evidence'], variant_groups, query.person_relation)
                if evidence:
                    enriched = []
                    for item in evidence:
                        enriched.append({**item, 'data': {k:v for k,v in item['data'].items() if k not in {'body_feature','space'}}, 'actor_name': names.get(item['actor_id']),
                            'role_name': names.get(item['role_id']),
                            'image_url': '/api/persons/evidence/' + item['id'] + '/image' if item['data'].get('image_path') else None,
                            'reference_url': reference_images.url(item),
                            'start': max(item['start'], segment['start']),
                            'end': min(item['end'], segment['end'])})
                    eligible.append({**segment, 'person_evidence': enriched})
            snapshots = eligible
            if not remaining:
                def score(segment):
                    intervals = sorted((e['start'], e['end']) for e in segment['person_evidence'])
                    total, end = 0, -1
                    for a, b in intervals:
                        total += max(0, b - max(a, end))
                        end = max(end, b)
                    quality = max(e['data'].get('score', 0) for e in segment['person_evidence'])
                    return total + quality * 0.001
                ranked = [{'id': s['id'], 'score': score(s), 'matches': [{'route': 'person', 'rank': i+1}]}
                          for i, s in enumerate(sorted(snapshots, key=lambda s: (-score(s), s['id'])))]
                result = self._result(active, snapshots, {}, query.limit, started, ranked=ranked)
                result['score_type'] = 'person_coverage_quality'
                return {**result, **meta}
            query = query.model_copy(update={'text': remaining})
        elif mode == 'mentioned':
            # Mention mode is literal dialogue evidence, with no visual/analysis bypass.
            from mindex.persons.persons import _occurrences

            names = {e['id']: [e['name'], *e.get('aliases', [])] for e in entities}
            if ids:
                def mentions(s):
                    text = s.get('views', {}).get('dialogue', '')
                    checks = [any(_occurrences(n, text) for i in group for n in names[i]) for group in variant_groups]
                    return any(checks) if query.person_relation == 'any' else all(checks)
                snapshots = [s for s in snapshots if mentions(s)]
            query = query.model_copy(update={'channels': ['dialogue'], 'text': query.text or ' '.join(names[i][0] for i in ids)})
        result = self._text_query(query, active, snapshots, vectors, started)
        return {**result, **meta}

    def _text_query(self, query, active, snapshots, vectors, started):
        if not snapshots:
            return self._result(active, snapshots, {}, query.limit, started)
        if active["spec"].get("format", 1) >= 2:
            return self._channel_text(query, active, snapshots, vectors, started)
        self._validate_space(active)
        vector = self.embedder.text([query.text], role="query")[0]
        ids = [s["id"] for s in snapshots]
        count = self.config.search.candidates
        available = {"video", *(view for s in snapshots for view in s["views"])}
        channels = set(query.channels or ("visual", "dialogue", "analysis"))
        selected_views = set()
        for channel, views in {
            "visual": {"video"},
            "dialogue": {"dialogue"},
            "analysis": {"description", "style"},
        }.items():
            if channel in channels:
                selected_views.update(views)
        routes = {
            view: vectors.query(vector, view, ids, count)
            for view in ("video", "description", "style", "dialogue")
            if self.config.search.weights[view] > 0 and view in available and view in selected_views
        }
        if self.config.search.weights["lexical"] > 0:
            routes["lexical"] = lexical_search(
                query.text,
                snapshots,
                count,
                set(self.config.search.lexical_exclude)
                | ({"description", "style", "dialogue"} - selected_views),
            )
        return self._result(active, snapshots, routes, query.limit, started)

    def _channel_text(self, query, active, snapshots, vectors, started):
        selected = set(query.channels or ("visual", "dialogue", "analysis"))
        selected = {c for c in selected if self.config.search.channel_weights[c] > 0}
        count, k = self.config.search.candidates, self.config.search.rrf_k
        available = {v for s in snapshots for v in s.get("indexed_views", [])}
        selected = {c for c in selected if ("video" if c == "visual" else c) in available}
        if not selected:
            result = self._result(active, snapshots, {}, query.limit, started)
            return {**result, "channels": [], "degraded": False, "warnings": []}
        ids = [s["id"] for s in snapshots]
        lexical = {}
        for channel in selected - {"visual"}:
            documents = [
                {**s, "views": {channel: s["views"][channel]} if channel in s["views"] else {}}
                for s in snapshots
            ]
            lexical[channel] = lexical_search(query.text, documents, count)
        vector, warning = None, None
        try:
            self._validate_space(active)
            vector = self.embedder.text([query.text], role="query")[0]
        except ModelError as exc:
            warning = str(exc)
            if not lexical:
                raise ModelError("查询嵌入不可用，且没有可用词法召回渠道：" + warning) from exc
        routes, details = {}, {}
        for channel in sorted(selected):
            view = "video" if channel == "visual" else channel
            components = {}
            if vector is not None:
                components["semantic"] = vectors.query(vector, view, ids, count)
            if channel in lexical:
                components["bm25"] = lexical[channel]
            inner = fuse(components, {key: 1 for key in components}, k)
            routes[channel] = inner
            details[channel] = {h["id"]: h["matches"] for h in inner}
        ranked = fuse(routes, self.config.search.channel_weights, k)
        lookup = {s["id"]: s for s in snapshots}
        query_terms = set(tokens(query.text))
        for hit in ranked:
            segment = lookup[hit["id"]]
            for match in hit["matches"]:
                channel = match["route"]
                match.update(
                    channel=channel,
                    rank_details=details[channel][hit["id"]],
                    weight=self.config.search.channel_weights[channel],
                )
                if channel == "dialogue":
                    utterances = segment.get("dialogue", [])
                    ordered = sorted(utterances, key=lambda u: -len(query_terms & set(tokens(u["text"]))))
                    match["evidence"] = [
                        {
                            "text": u["text"],
                            "start": u.get("evidence_start", segment["start"] + u["start"]),
                            "end": u.get("evidence_end", segment["start"] + u["end"]),
                        }
                        for u in ordered[:3]
                    ]
                    match["matched_text"] = (
                        ordered[0]["text"] if ordered else segment["views"].get("dialogue", "")
                    )
                elif channel == "analysis":
                    match["matched_text"] = segment["views"].get("analysis", "")
                    match["evidence"] = [{"start": segment["start"], "end": segment["end"]}]
                else:
                    match["route"] = "video"
                    match["matched_text"] = None
                    match["evidence"] = [{"start": segment["start"], "end": segment["end"]}]
        result = self._result(active, snapshots, {}, query.limit, started, ranked=ranked)
        return {
            **result,
            "channels": sorted(selected),
            "degraded": warning is not None,
            "warnings": ["嵌入不可用，已降级为所选文本渠道的 BM25 检索：" + warning] if warning else [],
        }

    def video(self, path: Path, granularity="moment", limit=20, filters: Filters | None = None) -> dict:
        started = perf_counter()
        info = probe(path)
        if info["duration"] > self.config.search.max_query_seconds + 0.05:
            raise ValueError(f"查询视频不能超过 {self.config.search.max_query_seconds:g} 秒")
        active, snapshots, vectors = self._context(granularity, filters or Filters())
        if active["spec"].get("format", 1) >= 2:
            snapshots = [s for s in snapshots if "video" in s.get("indexed_views", [])]
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

    def image(self, path: Path, granularity="moment", limit=20, filters: Filters | None = None) -> dict:
        started = perf_counter()
        png = image_png(path, self.config.video.max_pixels)
        active, snapshots, vectors = self._context(granularity, filters or Filters())
        if active["spec"].get("format", 1) >= 2:
            snapshots = [s for s in snapshots if "video" in s.get("indexed_views", [])]
        if not snapshots:
            return self._result(active, snapshots, {}, limit, started)
        if not hasattr(self.embedder, "image"):
            raise ModelError("当前嵌入适配器尚不支持图片输入，请切换支持图片的模型服务")
        with tempfile.TemporaryDirectory(prefix="query-image-", dir=self.store.root) as directory:
            normalized = Path(directory) / "query.png"
            normalized.write_bytes(png)
            embedding = self.embedder.image(normalized)
        hits = vectors.query(embedding, "video", [s["id"] for s in snapshots], self.config.search.candidates)
        # Image queries compare only with video vectors, independent of text-fusion weights.
        ranked = fuse({"video": hits}, {"video": 1}, self.config.search.rrf_k)
        return self._result(active, snapshots, {}, limit, started, ranked=ranked)


def status(
    config: Config, *, health=False, summary=False,
    embedder: MultimodalEmbedder | None = None, index_id: str | None = None
) -> dict:
    store = Store(config.data_dir)
    active = store.index_version(index_id) if index_id else store.active_index()
    result = {
        "active_index": active,
        "sources": [
            source_public(s)
            for s in store.rows(SOURCE_SQL + " WHERE COALESCE(c.hidden,0)=0 ORDER BY s.name,s.id")
        ],
        "segment_counts": store.rows("SELECT kind,count(*) AS count FROM segments GROUP BY kind"),
        "runs": store.rows("SELECT * FROM runs ORDER BY id DESC LIMIT 15"),
        "detector": config.detector.backend,
        "runtime_overrides": config.runtime_overrides,
        "embedding": {
            "model": config.embedding.model,
            "dimensions": config.embedding.dimensions,
            "quantization": config.embedding.quantization,
        },
        "model_health": {"checked": False},
        "channel_coverage": channel_coverage(store, active) if not summary else {},
        "person_coverage": __import__('mindex.persons.persons', fromlist=['Persons']).Persons(store).published(
            active['id'], pinned=index_id is not None)[1]['coverage'] if active and not summary else [],
    }
    if health:
        try:
            client = embedder or make_embedder(config)
            identity = client.identity()
            if hasattr(client, "require_gate"):
                client.require_gate()
            result["model_health"] = {
                "checked": True,
                "ready": True,
                "space": identity,
                "matches_index": active is None or active["spec"]["space"] == identity,
            }
        except ModelError as exc:
            result["model_health"] = {"checked": True, "ready": False, "error": str(exc)}
    return result


def channel_coverage(store, active=None, source_id=None, snapshots=None):
    active = active or store.active_index()
    if snapshots is None:
        snapshots = store.snapshots(active["id"], source_id) if active else []
    snapshots = [s for s in snapshots if source_id is None or s["source_id"] == source_id]
    totals = {"visual": 0, "dialogue": 0, "analysis": 0}
    for s in snapshots:
        views = set(s.get("indexed_views", ["video", *s.get("views", {})]))
        totals["visual"] += int("video" in views)
        totals["dialogue"] += int("dialogue" in views)
        totals["analysis"] += int(bool(views & {"analysis", "description", "style"}))
    return {
        c: {
            "indexed_segments": n,
            "total_segments": len(snapshots),
            "coverage": n / len(snapshots) if snapshots else 0,
        }
        for c, n in totals.items()
    }
