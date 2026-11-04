from __future__ import annotations

import hashlib
import json
import math
import tempfile
import threading
from collections import Counter, OrderedDict, defaultdict
from contextlib import nullcontext
from functools import wraps
from pathlib import Path
from time import perf_counter

import jieba

from mindex.admin.curation import EntityNotFound, Library, source_public, source_sql
from mindex.models.adapters import ModelError, MultimodalEmbedder, make_embedder
from mindex.processing.indexing import VectorIndex
from mindex.shared.config import Config, fingerprint
from mindex.shared.images import image_png
from mindex.shared.jsonio import save
from mindex.shared.media import content_hash, cut, probe, windows
from mindex.shared.storage import Store
from mindex.shared.tokenizer import document_terms, tokens
from mindex.shared.trace import record
from mindex.shared.types import Filters, TextSearch

jieba.setLogLevel(30)

# 查询向量缓存条目上限（每条 4096 维 float，约 100 KB）。
QUERY_VECTOR_CACHE_SIZE = 128
_QUERY_VECTORS: OrderedDict = OrderedDict()
_QUERY_VECTOR_LOCK = threading.Lock()


def recall_scope(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        scope = getattr(self.embedder, "recall_scope", None)
        with scope() if scope else nullcontext():
            return method(self, *args, **kwargs)
    return wrapped


class SimilarUnavailable(Exception):
    """种子片段的画面向量在向量库中缺失（库内不一致），找相似无法进行；接口映射为 409。

    刻意不继承 ValueError，以免被通用的 422 处理器吞掉。
    """


def lexical_search(text: str, snapshots: list[dict], count: int, exclude=()) -> list[dict]:
    terms = Counter(tokens(text))
    parsed = [
        document_terms("\n".join(v for k, v in s["views"].items() if k not in exclude)) for s in snapshots
    ]
    documents = [document for document, _ in parsed]
    lengths = [length for _, length in parsed]
    mean_length = sum(lengths) / max(1, len(lengths)) or 1
    # 只需查询词的文档频率；等价于对全部词建 Counter，但不必遍历每篇文档的全部词。
    frequency = {term: sum(1 for document in documents if document.get(term)) for term in terms}
    results = []
    for segment, document, length in zip(snapshots, documents, lengths):
        score = 0.0
        for term in terms:
            tf = document.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / mean_length))
        if score > 0:
            results.append({"id": segment["id"], "score": score})
    return sorted(results, key=lambda v: (-v["score"], v["id"]))[:count]


def fuse(routes: dict[str, list[dict]], weights: dict[str, float], k: int) -> list[dict]:
    scores, reasons = defaultdict(float), defaultdict(list)
    # Normalize against the theoretical best RRF score, never the observed maximum.
    # Multiplying all weights by a constant cannot change the score or ordering.
    scale = max((weights.get(route, 0) for route, hits in routes.items() if hits), default=0) or 1
    relative = {route: weight / scale for route, weight in weights.items()}
    ceiling = sum(relative.get(route, 0) for route, hits in routes.items() if hits and relative.get(route, 0) > 0) / (k + 1)
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
            scores[hit["id"]] += relative[route] / (k + rank)
            reason = {"route": route, "rank": rank}
            if "distance" in hit:
                reason["cosine_similarity"] = 1 - hit["distance"]
            reasons[hit["id"]].append(reason)
    return [
        {"id": identifier, "score": min(1.0, max(0.0, score / ceiling)), "matches": reasons[identifier]}
        for identifier, score in sorted(scores.items(), key=lambda v: (-v[1], v[0]))
    ]



def duration_rank(ranked: list[dict], segments: dict[str, dict], target: float | None, settings) -> list[dict]:
    """Bounded blend of relevance and usable duration; missing targets leave ordinary search intact."""
    if target is None or settings.duration_weight == 0:
        return ranked
    results = []
    for position, hit in enumerate(ranked, 1):
        segment = segments[hit["id"]]
        ratio = min(1.0, max(0.0, segment["end"] - segment["start"]) / target)
        duration = ratio ** settings.duration_exponent
        relevance = hit["score"]
        if any(match.get("route") == "person" for match in hit["matches"]):
            relevance = (settings.rrf_k + 1) / (settings.rrf_k + position)
        score = (1 - settings.duration_weight) * relevance + settings.duration_weight * duration
        results.append({**hit, "score": score, "relevance_score": relevance,
                        "duration_score": duration, "target_duration": target})
    return sorted(results, key=lambda item: (-item["score"], item["id"]))


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
        force_embedding: bool = False,
    ):
        self.config = config
        self.store = Store.open(config)
        self._embedder = embedder
        self.index_id = index_id
        self.release_id = release_id
        self.force_embedding = force_embedding

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = make_embedder(self.config)
        return self._embedder

    def _vectors(self, index_id: str, *, create: bool = False) -> VectorIndex:
        """按配置选择向量客户端：缺省本地嵌入；chroma_server 形态走共享 HttpClient（plans §6.2）。"""
        from mindex.processing.indexing import vector_client

        return VectorIndex(self.store.root, index_id, create,
                           client=vector_client(self.config, library_id=self.store.library_id))

    def _validate_space(self, active):
        self._space_id = None
        identity = self.embedder.identity()
        if hasattr(self.embedder, "require_gate"):
            self.embedder.require_gate()
        self._space_id = fingerprint(identity)
        if identity != active["spec"]["space"]:
            raise ModelError("当前模型与已发布索引的向量空间不同，请切回对应模型或建立新索引")
        if self.config.video.model_dump() != active["spec"]["preprocessing"]:
            raise ModelError("视频预处理配置与索引不一致，请使用对应配置")

    def _query_vector(self, text: str) -> list[float]:
        """查询文字的嵌入向量，按「向量空间 + 文字」缓存。

        只在 _validate_space 已通过（部署身份与兼容性门槛刚核验）之后调用；空间标识进入缓存键，
        所以换模型/部署后不会命中旧向量。命中时省掉一次 HTTP 推理及其前后两轮部署核验。
        """
        space_id = getattr(self, "_space_id", None)
        if space_id is None:
            return self.embedder.text([text], role="query")[0]
        return self._cached_vector("text", text, lambda: self.embedder.text([text], role="query")[0])

    def _cached_vector(self, kind, content, compute):
        """按空间与查询内容持久缓存；每次读取前仍须核验当前身份。"""
        key = (self.store.library_id, self._space_id, kind, content)
        identity = fingerprint(key)
        path = self.store.root / "cache" / "query-vectors" / (identity + ".json")
        if not self.force_embedding:
            with _QUERY_VECTOR_LOCK:
                cached = _QUERY_VECTORS.get(key)
                if cached is not None:
                    _QUERY_VECTORS.move_to_end(key)
                    record("query-vector-cache", {"hit": True, "kind": kind})
                    return list(cached)
            try:
                saved = json.loads(path.read_text())
                vector = saved["vector"]
                if (saved["key"] == identity and saved["checksum"] == fingerprint(vector)
                    and len(vector) == self.config.embedding.dimensions
                    and all(type(v) in (int, float) and math.isfinite(v) for v in vector)
                    and sum(v * v for v in vector) > 1e-12):
                    self._remember_vector(key, vector)
                    record("query-vector-cache", {"hit": True, "kind": kind})
                    return vector
            except (OSError, ValueError, KeyError, TypeError):
                pass
        record("query-vector-cache", {"hit": False, "kind": kind})
        vector = compute()
        try:
            save(path, {"key": identity, "vector": vector, "checksum": fingerprint(vector)})
        except OSError:
            pass  # 缓存写入失败不影响本次有效召回。
        self._remember_vector(key, vector)
        return vector

    @staticmethod
    def _remember_vector(key, vector):
        with _QUERY_VECTOR_LOCK:
            _QUERY_VECTORS[key] = tuple(vector)
            _QUERY_VECTORS.move_to_end(key)
            while len(_QUERY_VECTORS) > QUERY_VECTOR_CACHE_SIZE:
                _QUERY_VECTORS.popitem(last=False)

    def _active(self) -> dict:
        """当前检索使用的索引版本：固定索引优先，否则为活动索引；必须已就绪。"""
        active = self.store.index_version(self.index_id) if self.index_id else self.store.active_index()
        if active is None:
            raise ValueError("尚未发布索引，请先完成离线处理并执行 index --activate")
        if active["status"] != "ready":
            raise ModelError("当前索引未就绪")
        return active

    def _context(self, granularity: str, filters: Filters, *, text=False, person=False):
        active = self._active()
        if not text or (active["spec"].get("format", 1) == 1 and not person):
            self._validate_space(active)
        snapshots = self._snapshots(active, granularity, filters)
        return active, snapshots, self._vectors(active["id"])

    def _snapshots(self, active: dict, granularity: str, filters: Filters) -> list[dict]:
        """按隐藏、粒度、素材、时长与分析字段过滤后的候选快照；不做向量空间校验。"""
        snapshots = []
        library = Library(self.store)
        sources, segments = library.overlays()
        selected_sources = filters.effective_sources(self.store)
        for segment in self.store.snapshots_shared(active["id"]):
            segment = library.decorate(segment, sources, segments)
            if segment["effective_hidden"]:
                continue
            if segment["kind"] != granularity:
                continue
            duration = segment["end"] - segment["start"]
            if selected_sources is not None and segment["source_id"] not in selected_sources:
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
        return snapshots

    def _result(
        self,
        active: dict,
        snapshots: list[dict],
        routes: dict,
        limit: int,
        started: float,
        ranked: list[dict] | None = None,
        target_duration: float | None = None,
        *,
        lookup: dict[str, dict] | None = None,
        eligible_count: int | None = None,
        projected: bool = False,
    ):
        if ranked is None:
            ranked = fuse(routes, self.config.search.weights, self.config.search.rrf_k)
        lookup = lookup if lookup is not None else {s["id"]: s for s in snapshots}
        ranked = duration_rank(ranked, lookup, target_duration, self.config.search)
        selected, chosen = [], []
        for hit in ranked:
            segment = lookup[hit["id"]]
            if any(time_iou(segment, prior) > 0.5 for prior in selected):
                continue
            selected.append(segment)
            chosen.append(hit)
            if len(chosen) >= limit:
                break
        if projected:
            hydrated = self._hydrate(active, [hit["id"] for hit in chosen])
            lookup = {key: {**value, **({"person_evidence": lookup[key]["person_evidence"]}
                                       if "person_evidence" in lookup[key] else {})}
                      for key, value in hydrated.items()}
        results = [{**public_segment(lookup[hit["id"]]), **{key: hit[key] for key in
                    ("score", "matches", "relevance_score", "duration_score", "target_duration") if key in hit}}
                   for hit in chosen]
        return {
            "index_id": active["id"],
            "index_mode": active["spec"].get("mode", "enriched"),
            "results": results,
            "elapsed_ms": round((perf_counter() - started) * 1000),
            "score_type": "duration_blend" if target_duration and self.config.search.duration_weight else "weighted_rrf",
            "eligible_segments": len(snapshots) if eligible_count is None else eligible_count,
        }

    def _hydrate(self, active: dict, ids: list[str]) -> dict[str, dict]:
        """按命中批量读取完整快照并 decorate；投影路径只在这一步接触全快照（plans §6.1）。"""
        if not ids:
            return {}
        library = Library(self.store)
        snapshots = []
        for offset in range(0, len(ids), 400):
            batch = ids[offset:offset + 400]
            placeholders = ",".join("?" * len(batch))
            snapshots.extend(json.loads(row["snapshot"]) for row in self.store.rows(
                "SELECT snapshot FROM index_members WHERE index_id=? "
                f"AND segment_id IN ({placeholders})", [active["id"], *batch]))
        sources, segments = library.overlays(snapshots=snapshots)
        lookup = {}
        for snapshot in snapshots:
            lookup[snapshot["id"]] = library.decorate(snapshot, sources, segments)
        return lookup

    def _text_projection(self, query, active: dict, started: float):
        """format 2 且派生词法索引可用的文字检索快路径；不可用时返回 None 回退。"""
        from mindex.search.lexical_query import eligible_projection
        from mindex.shared.lexical_store import lexical_ready

        if active["spec"].get("format", 1) < 2 or not lexical_ready(self.store, active["id"]):
            return None
        selected_sources = query.filters.effective_sources(self.store)
        rows = eligible_projection(
            self.store, active["id"], query.granularity, query.filters, selected_sources)
        vectors = self._vectors(active["id"])
        return self._channel_text(query, active, rows, vectors, started, projection=True)

    @recall_scope
    def text(self, query: TextSearch) -> dict:
        from mindex.persons.person_names import overlaps_for, resolve_names
        from mindex.persons.persons import Persons

        started = perf_counter()
        catalogue = Persons(self.store)
        preliminary_ids, _, _, _ = resolve_names(query, catalogue.entities())
        person = bool(preliminary_ids and query.person_mode != 'mentioned')
        if not person and query.person_mode != 'mentioned':
            # 无人物条件的文字检索走投影 + SQL 词法路径（format 2 且派生索引可用；
            # 否则返回 None 回退全快照参考路径，行为与旧实现一致）。
            active = self._active()
            fast = self._text_projection(query, active, started)
            if fast is not None:
                release = catalogue.release_for(
                    active['id'], pinned=self.index_id is not None,
                    release_id=self.release_id or active.get('search_release_id'))
                meta = {'person_mode': query.person_mode, 'person_ids': [],
                        'recognized_people': [],
                        'release_id': release['id'] if release else None,
                        'person_version': release['person_id'] if release else None}
                return {**fast, **meta}
        active = self._active()
        if active["spec"].get("format", 1) == 1 and not person:
            self._validate_space(active)
        snapshots, projected = self._eligible_rows(active, query.granularity, query.filters)
        vectors = self._vectors(active["id"])
        release = catalogue.release_for(active['id'], pinned=self.index_id is not None, release_id=self.release_id or active.get('search_release_id'))
        # 只取实体（几 KB）；数十 MB 的证据与覆盖区间只在出镜检索时才加载。
        published = catalogue.published_entities(release)
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
            from mindex.persons.person_names import coverage_missing
            from mindex.persons.person_processing import EvidenceReferences
            reference_images = EvidenceReferences(self.config, self.store)
            if published.get('format', 1) >= 2:
                # Actor searches expand through the immutable release cast, not live edits.
                cast_roles = {}
                for entity in published['entities']:
                    if entity.get('kind') == 'role':
                        for actor in entity.get('actor_ids', []):
                            cast_roles.setdefault(actor, []).append(entity['id'])
                variant_groups = [list(dict.fromkeys(
                    role for identifier in group for role in cast_roles.get(identifier, [identifier])
                )) for group in variant_groups]
            published = {**published, **catalogue.evidence_for(
                release, sources={s['source_id'] for s in snapshots},
                identifiers={identifier for group in variant_groups for identifier in group})}
            meta['person_coverage_missing'] = coverage_missing(snapshots, published['coverage'])
            eligible = []
            names = {e['id']: e['name'] for e in entities}
            # reference_url 要读特征缓存、较贵，只为最终返回的少数结果计算（见 finish_references）。
            originals = {}
            index = published.get('evidence_index') or published['evidence']
            for segment in snapshots:
                evidence = overlaps_for(segment, index, variant_groups, query.person_relation)
                if evidence:
                    enriched = []
                    for item in evidence:
                        originals[(segment['id'], item['id'])] = item
                        enriched.append({**item, 'data': {k:v for k,v in item['data'].items() if k not in {'body_feature','space'}}, 'actor_name': names.get(item['actor_id']),
                            'role_name': names.get(item['role_id']),
                            'image_url': '/api/persons/evidence/' + item['id'] + '/image' if item['data'].get('image_path') else None,
                            'reference_url': None,
                            'start': max(item['start'], segment['start']),
                            'end': min(item['end'], segment['end'])})
                    eligible.append({**segment, 'person_evidence': enriched})
            snapshots = eligible

            def finish_references(result):
                for hit in result['results']:
                    for item in hit.get('person_evidence', []):
                        item['reference_url'] = reference_images.url(originals[(hit['id'], item['id'])])
                return result
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
                result = finish_references(self._result(active, snapshots, {}, query.limit, started, ranked=ranked,
                                                       target_duration=query.target_duration, projected=projected))
                result['score_type'] = 'duration_blend' if query.target_duration and self.config.search.duration_weight else 'person_coverage_quality'
                return {**result, **meta}
            query = query.model_copy(update={'text': remaining})
        elif mode == 'mentioned':
            # Mention mode is literal dialogue evidence, with no visual/analysis bypass.
            from mindex.persons.person_names import _occurrences

            names = {e['id']: [e['name'], *e.get('aliases', [])] for e in entities}
            if ids:
                if projected:
                    from mindex.search.lexical_query import dialogue_projection

                    dialogue = dialogue_projection(self.store, active['id'], [s['id'] for s in snapshots])
                    snapshots = [{**s, 'views': {'dialogue': dialogue.get(s['id'], '')}} for s in snapshots]
                def mentions(s):
                    text = s.get('views', {}).get('dialogue', '')
                    checks = [any(_occurrences(n, text) for i in group for n in names[i]) for group in variant_groups]
                    return any(checks) if query.person_relation == 'any' else all(checks)
                snapshots = [s for s in snapshots if mentions(s)]
            query = query.model_copy(update={'channels': ['dialogue'], 'text': query.text or ' '.join(names[i][0] for i in ids)})
        result = self._text_query(query, active, snapshots, vectors, started, projection=projected)
        if mode == 'onscreen':
            finish_references(result)
        return {**result, **meta}

    def _text_query(self, query, active, snapshots, vectors, started, *, projection=False):
        if not snapshots:
            return self._result(active, snapshots, {}, query.limit, started, target_duration=query.target_duration)
        if active["spec"].get("format", 1) >= 2:
            return self._channel_text(query, active, snapshots, vectors, started, projection=projection)
        self._validate_space(active)
        vector = self._query_vector(query.text)
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
        return self._result(active, snapshots, routes, query.limit, started, target_duration=query.target_duration)

    def _channel_text(self, query, active, snapshots, vectors, started, *, projection=False):
        """format 2 文字检索。``projection=True`` 时 ``snapshots`` 是 eligible 投影行
        （无 views/dialogue 全文）：词法走 SQL 倒排，融合后按命中批量水合快照。"""
        from mindex.search.lexical_query import sql_bm25

        selected = set(query.channels or ("visual", "dialogue", "analysis"))
        selected = {c for c in selected if self.config.search.channel_weights[c] > 0}
        count, k = self.config.search.candidates, self.config.search.rrf_k
        available = {v for s in snapshots for v in s.get("indexed_views", [])}
        selected = {c for c in selected if ("video" if c == "visual" else c) in available}
        if not selected:
            result = self._result(active, snapshots, {}, query.limit, started, target_duration=query.target_duration)
            return {**result, "channels": [], "degraded": False, "warnings": []}
        ids = [s["id"] for s in snapshots]
        lexical = {}
        for channel in selected - {"visual"}:
            if projection:
                # SQL 倒排：N/df/mean 都在该 eligible 上统计，参考公式精确重排（plans §6.1）
                lexical[channel] = sql_bm25(self.store, active["id"], channel, query.text, ids, count)
            else:
                documents = [
                    {**s, "views": {channel: s["views"][channel]} if channel in s["views"] else {}}
                    for s in snapshots
                ]
                lexical[channel] = lexical_search(query.text, documents, count)
        vector, warning = None, None
        try:
            self._validate_space(active)
            vector = self._query_vector(query.text)
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
        if projection:
            # 只对融合后的候选命中读取完整快照（时长排序/IoU 去重/证据展示需要）。
            lookup = self._hydrate(active, [hit["id"] for hit in ranked])
            evidence = {s["id"]: s["person_evidence"] for s in snapshots if "person_evidence" in s}
            for identifier in evidence.keys() & lookup.keys():
                lookup[identifier] = {**lookup[identifier], "person_evidence": evidence[identifier]}
        else:
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
        result = self._result(active, snapshots, {}, query.limit, started, ranked=ranked,
                              target_duration=query.target_duration, lookup=lookup,
                              eligible_count=len(snapshots))
        return {
            **result,
            "channels": sorted(selected),
            "degraded": warning is not None,
            "warnings": ["嵌入不可用，已降级为所选文本渠道的 BM25 检索：" + warning] if warning else [],
        }

    def _eligible_rows(self, active: dict, granularity: str, filters: Filters):
        """向量检索的候选行：优先投影（format 2 且派生索引可用），否则全快照过滤。"""
        projection = None
        if active["spec"].get("format", 1) >= 2:
            from mindex.shared.lexical_store import lexical_ready

            if lexical_ready(self.store, active["id"]):
                from mindex.search.lexical_query import eligible_projection

                projection = eligible_projection(
                    self.store, active["id"], granularity, filters, filters.effective_sources(self.store))
        if projection is not None:
            return projection, True
        return self._snapshots(active, granularity, filters), False

    @recall_scope
    def video(self, path: Path, granularity="moment", limit=20, filters: Filters | None = None, *, target_duration: float | None = None) -> dict:
        started = perf_counter()
        info = probe(path)
        if info["duration"] > self.config.search.max_query_seconds + 0.05:
            raise ValueError(f"查询视频不能超过 {self.config.search.max_query_seconds:g} 秒")
        active = self._active()
        self._validate_space(active)
        snapshots, projected = self._eligible_rows(active, granularity, filters or Filters())
        vectors = self._vectors(active["id"])
        if active["spec"].get("format", 1) >= 2:
            snapshots = [s for s in snapshots if "video" in s.get("indexed_views", [])]
        if not snapshots:
            return self._result(active, snapshots, {}, limit, started, target_duration=target_duration)
        v = self.config.video
        pieces = windows(0, info["duration"], v.window_seconds, v.stride_seconds)
        query_hash = content_hash(path)
        routes = {}
        for index, (start, end) in enumerate(pieces):
            if len(pieces) == 1:
                embedding = self._cached_vector("video", query_hash, lambda: self.embedder.video(path))
            else:
                def compute(start=start, end=end):
                    with tempfile.TemporaryDirectory(prefix="query-window-", dir=self.store.root) as directory:
                        window = Path(directory) / "window.mp4"
                        cut(path, start, end, window)
                        return self.embedder.video(window)

                embedding = self._cached_vector("video", fingerprint({
                    "sha256": query_hash, "start": start, "end": end}), compute)
            routes[f"video_{index}"] = vectors.query(
                embedding, "video", [s["id"] for s in snapshots], self.config.search.candidates
            )
        # Query windows are equal votes; the number of windows must not change scale.
        weights = {key: 1 / len(routes) for key in routes}
        fused = fuse(routes, weights, self.config.search.rrf_k)
        for hit in fused:
            for match in hit["matches"]:
                match["route"] = "video"
        lookup = self._hydrate(active, [hit["id"] for hit in fused]) if projected else None
        return self._result(active, snapshots, {}, limit, started, ranked=fused,
                            target_duration=target_duration, lookup=lookup, eligible_count=len(snapshots))

    @recall_scope
    def image(self, path: Path, granularity="moment", limit=20, filters: Filters | None = None, *, target_duration: float | None = None) -> dict:
        started = perf_counter()
        png = image_png(path, self.config.video.max_pixels)
        active = self._active()
        self._validate_space(active)
        snapshots, projected = self._eligible_rows(active, granularity, filters or Filters())
        vectors = self._vectors(active["id"])
        if active["spec"].get("format", 1) >= 2:
            snapshots = [s for s in snapshots if "video" in s.get("indexed_views", [])]
        if not snapshots:
            return self._result(active, snapshots, {}, limit, started, target_duration=target_duration)
        if not hasattr(self.embedder, "image"):
            raise ModelError("当前嵌入适配器尚不支持图片输入，请切换支持图片的模型服务")
        with tempfile.TemporaryDirectory(prefix="query-image-", dir=self.store.root) as directory:
            normalized = Path(directory) / "query.png"
            normalized.write_bytes(png)
            embedding = self._cached_vector("image", hashlib.sha256(png).hexdigest(),
                                            lambda: self.embedder.image(normalized))
        hits = vectors.query(embedding, "video", [s["id"] for s in snapshots], self.config.search.candidates)
        # Image queries compare only with video vectors, independent from text-fusion weights.
        ranked = fuse({"video": hits}, {"video": 1}, self.config.search.rrf_k)
        lookup = self._hydrate(active, [hit["id"] for hit in ranked]) if projected else None
        return self._result(active, snapshots, {}, limit, started, ranked=ranked,
                            target_duration=target_duration, lookup=lookup, eligible_count=len(snapshots))

    def similar(
        self, segment_id: str, granularity: str | None = None, limit: int = 20, filters: Filters | None = None
    ) -> dict:
        """以库内片段的画面向量找相似片段。

        只读同一索引里已存的 video 向量，不调用查询嵌入服务，因此无需空间校验、离线时也可用。
        种子有多个窗口向量时，每个窗口一路近邻检索，按等权 RRF 融合（与视频查询一致）。
        """
        started = perf_counter()
        active = self._active()
        row = self.store.one(
            "SELECT snapshot FROM index_members WHERE index_id=? AND segment_id=? AND complete=1",
            (active["id"], segment_id),
        )
        if row is None:
            raise EntityNotFound("片段不在当前索引中或尚未完成")
        seed = json.loads(row["snapshot"])
        visual_only = active["spec"].get("format", 1) >= 2
        if visual_only and "video" not in seed.get("indexed_views", []):
            raise ValueError("该片段没有画面表示，无法找相似")
        ids = [
            r["id"]
            for r in self.store.rows(
                "SELECT id FROM representations WHERE index_id=? AND segment_id=? AND view='video' ORDER BY id",
                (active["id"], segment_id),
            )
        ]
        vectors = self._vectors(active["id"])
        found = vectors.vectors(ids) if ids else {}
        embeddings = [found[i] for i in ids if i in found]
        if not embeddings:
            raise SimilarUnavailable("该片段的画面向量缺失，请重建索引后再试")
        candidates, projected = self._eligible_rows(active, granularity or seed["kind"], filters or Filters())
        snapshots = [
            s
            for s in candidates
            if (not visual_only or "video" in s.get("indexed_views", []))
            and s["id"] != seed["id"]
            and not (
                s["source_id"] == seed["source_id"]
                and min(s["end"], seed["end"]) - max(s["start"], seed["start"]) > 0
            )
        ]
        if not snapshots:
            result = self._result(active, snapshots, {}, limit, started)
            return {**result, "seed_segment_id": segment_id}
        eligible = [s["id"] for s in snapshots]
        routes = {
            f"video_{index}": vectors.query(embedding, "video", eligible, self.config.search.candidates)
            for index, embedding in enumerate(embeddings)
        }
        weights = {key: 1 / len(routes) for key in routes}
        fused = fuse(routes, weights, self.config.search.rrf_k)
        for hit in fused:
            for match in hit["matches"]:
                match["route"] = "video"
        lookup = self._hydrate(active, [hit["id"] for hit in fused]) if projected else None
        result = self._result(active, snapshots, {}, limit, started, ranked=fused,
                              lookup=lookup, eligible_count=len(snapshots))
        return {**result, "seed_segment_id": segment_id}


def status(
    config: Config, *, health=False, summary=False,
    embedder: MultimodalEmbedder | None = None, index_id: str | None = None
) -> dict:
    store = Store.open(config)
    active = store.index_version(index_id) if index_id else store.active_index()
    result = {
        "active_index": active,
        "sources": [
            source_public(s)
            for s in store.rows(source_sql(store) + " WHERE COALESCE(c.hidden,0)=0 ORDER BY s.name,s.id")
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
