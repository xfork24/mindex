from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

import chromadb
from chromadb.config import Settings

from mindex.models.adapters import ModelError, MultimodalEmbedder, make_embedder
from mindex.processing.pipeline import Pipeline
from mindex.shared.config import Config, fingerprint
from mindex.shared.media import windows
from mindex.shared.storage import Store, dumps

log = logging.getLogger(__name__)

# chroma HNSW 段的默认落盘阈值：累计这么多元素才把 id_map 与索引写盘。
CHROMA_HNSW_SYNC_THRESHOLD = 1000

# 检索的「无过滤多取 + 内存后过滤」参数：首次取 max(count*10, 1000) 条，不够按 3 倍放大，
# 超过上限仍不够则回退到 chroma where 过滤（极小的 eligible 子集才会触发）。
POSTFILTER_OVERFETCH = 10
POSTFILTER_MIN_CANDIDATES = 1000
POSTFILTER_GROWTH = 3
POSTFILTER_MAX_CANDIDATES = 20000
# 无过滤查询只取距离（约比同时取元数据快 5 倍），向量 id → (segment_id, view) 走进程内映射。
REPRESENTATION_MAP_ENTRIES = 2
_REPRESENTATION_MAPS: OrderedDict = OrderedDict()
_REPRESENTATION_LOCK = threading.Lock()
RAW_MEMO_ENTRIES = 8


def retain_representation_maps(root: Path, keep: set[str]) -> int:
    """淘汰该数据目录下不在 keep 里的向量映射缓存。返回淘汰条数。

    只影响本模块的映射缓存；Chroma 自身已加载的 HNSW 在 1.5.x 本地实现里无法由进程内释放。
    """
    resolved = str(Path(root).resolve())
    with _REPRESENTATION_LOCK:
        stale = [key for key in _REPRESENTATION_MAPS if key[0] == resolved and key[1] not in keep]
        for key in stale:
            del _REPRESENTATION_MAPS[key]
    return len(stale)


class VectorIndex:
    def __init__(self, root: Path, index_id: str, create=False):
        self.client = chromadb.PersistentClient(
            path=str(root / "chroma"), settings=Settings(anonymized_telemetry=False)
        )
        self.root, self.index_id = Path(root), index_id
        self._raw_memo: OrderedDict = OrderedDict()
        name = "idx-" + index_id
        if create:
            self.collection = self.client.get_or_create_collection(
                name, metadata={"hnsw:space": "cosine"}, embedding_function=None
            )
        else:
            self.collection = self.client.get_collection(name, embedding_function=None)

    def upsert(self, identifier: str, vector: list[float], segment: dict, view: str, document: str):
        self.collection.upsert(
            ids=[identifier],
            embeddings=[vector],
            documents=[document or view],
            metadatas=[
                {
                    "segment_id": segment["id"],
                    "source_id": segment["source_id"],
                    "granularity": segment["kind"],
                    "view": view,
                }
            ],
        )

    def vector(self, identifier: str) -> list[float] | None:
        try:
            raw = self.collection.get(ids=[identifier], include=["embeddings"])
        except Exception as exc:  # 仅降级「元数据有、HNSW 无」的库内不一致，其余照常抛出
            if "Error finding id" not in str(exc):
                raise
            log.warning("向量 %s 在 HNSW 段缺失（元数据仍在）：视为不可复用，将重新嵌入", identifier)
            return None
        return raw["embeddings"][0].tolist() if raw["ids"] else None

    def vectors(self, identifiers: list[str]) -> dict[str, list[float]]:
        """Read reusable vectors together; isolate damaged legacy entries on failure."""
        try:
            raw = self.collection.get(ids=list(dict.fromkeys(identifiers)), include=["embeddings"])
        except Exception as exc:
            if "Error finding id" not in str(exc):
                raise
            return {identifier: vector for identifier in identifiers
                    if (vector := self.vector(identifier)) is not None}
        return {identifier: vector.tolist() for identifier, vector in zip(raw["ids"], raw["embeddings"])}

    def upsert_many(self, entries):
        self.collection.upsert(
            ids=[entry[0] for entry in entries],
            embeddings=[entry[1] for entry in entries],
            documents=[entry[4] or entry[3] for entry in entries],
            metadatas=[{"segment_id": s["id"], "source_id": s["source_id"],
                        "granularity": s["kind"], "view": view}
                       for _, _, s, view, _ in entries],
        )

    def flush(self) -> None:
        """幂等重写既有向量，冲过 chroma HNSW 落盘阈值，确保内存态全部写盘。

        chroma 元数据段每笔提交，HNSW 段仅在累计 sync_threshold（默认 1000）个
        元素时才持久化 id_map 与索引；构建收尾不冲阈值时尾部向量只留在内存与
        WAL，重启或跨进程访问后可能丢失（复用时报 Error finding id）。
        重写内容与既有记录一致，不改变集合可见状态。
        """
        if self.collection.count() == 0:
            return
        try:
            batch = self.collection.get(
                limit=CHROMA_HNSW_SYNC_THRESHOLD,
                include=["embeddings", "documents", "metadatas"],
            )
        except Exception as exc:  # noqa: BLE001 — 落盘尽力而为，缺 id 由 vector() 自愈兜底
            log.warning("索引落盘预读失败，跳过强制持久化：%s", exc)
            return
        ids = list(batch["ids"])
        embeddings = list(batch["embeddings"]) if batch["embeddings"] is not None else []
        if not ids or len(embeddings) != len(ids):
            return
        documents = list(batch["documents"]) if batch["documents"] is not None else [None] * len(ids)
        metadatas = list(batch["metadatas"]) if batch["metadatas"] is not None else [None] * len(ids)
        rounds = -(-CHROMA_HNSW_SYNC_THRESHOLD // len(ids))
        for _ in range(rounds):
            self.collection.upsert(
                ids=ids,
                embeddings=[embedding.tolist() for embedding in embeddings],
                documents=documents,
                metadatas=metadatas,
            )

    def delete(self, identifiers: list[str]) -> None:
        if identifiers:
            self.collection.delete(ids=identifiers)

    def ids(self) -> set[str]:
        result = set()
        offset = 0
        while True:
            batch = self.collection.get(limit=1000, offset=offset, include=[])["ids"]
            result.update(batch)
            if len(batch) < 1000:
                return result
            offset += 1000

    def query(self, vector: list[float], view: str, eligible: list[str], count: int) -> list[dict]:
        total = self.collection.count() if eligible else 0
        if total == 0:
            return []
        # Chroma 的元数据 where 过滤要扫 sqlite（线上 1.2 万向量约 1.5 秒/次），而无过滤的
        # HNSW 查询只要毫秒级。先无过滤多取候选再在内存里按 view/eligible 过滤；
        # 取不够就放大候选数，放大到上限仍不够再回退到原来的 where 过滤。
        hits = self._query_postfiltered(vector, view, set(eligible), count, total)
        if hits is None:
            hits = self._query_filtered(vector, view, eligible, count, total)
        return self._best_per_segment(hits, count)

    def _representation_map(self) -> dict[str, tuple[str, str]]:
        """向量 id → (segment_id, view)。representations 行按内容寻址、写入后不可变，可进程内缓存。

        读不到（例如没有元数据库的独立向量集合）时返回空映射，调用方改用 chroma 自带的元数据。
        """
        key = (str(self.root.resolve()), self.index_id)
        with _REPRESENTATION_LOCK:
            cached = _REPRESENTATION_MAPS.get(key)
            if cached is not None:
                _REPRESENTATION_MAPS.move_to_end(key)
                return cached
        mapping: dict[str, tuple[str, str]] = {}
        database = self.root / "metadata.sqlite3"
        if database.is_file():
            try:
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
                    mapping = {
                        row[0]: (row[1], row[2])
                        for row in db.execute(
                            "SELECT id,segment_id,view FROM representations WHERE index_id=?", (self.index_id,)
                        )
                    }
            except sqlite3.Error as exc:
                log.warning("读取向量映射失败，改用 chroma 元数据：%s", exc)
        if mapping:
            with _REPRESENTATION_LOCK:
                _REPRESENTATION_MAPS[key] = mapping
                _REPRESENTATION_MAPS.move_to_end(key)
                while len(_REPRESENTATION_MAPS) > REPRESENTATION_MAP_ENTRIES:
                    _REPRESENTATION_MAPS.popitem(last=False)
        return mapping

    def _candidates(self, vector, size: int, mapping: dict) -> tuple[list[str], list[float], dict]:
        """无过滤的最近邻候选 (ids, distances, id→(segment_id, view))。

        同一向量同一大小的结果在本实例内复用：文字检索的三个渠道共用一个查询向量，各自放大候选数时
        会反复请求相同的大小。
        """
        key = (tuple(vector), size, bool(mapping))
        if key in self._raw_memo:
            self._raw_memo.move_to_end(key)
            return self._raw_memo[key]
        raw = self.collection.query(
            query_embeddings=[vector], n_results=size,
            include=["distances"] if mapping else ["metadatas", "distances"],
        )
        ids, distances = raw["ids"][0], raw["distances"][0]
        if mapping:
            lookup = {i: mapping[i] for i in ids if i in mapping}
            missing = [i for i in ids if i not in lookup]
            for offset in range(0, len(missing), 1000):  # 映射缺项（索引在映射缓存后被追加）逐批补取
                got = self.collection.get(ids=missing[offset : offset + 1000], include=["metadatas"])
                lookup.update(
                    (i, (m["segment_id"], m.get("view"))) for i, m in zip(got["ids"], got["metadatas"])
                )
        else:
            lookup = {i: (m["segment_id"], m.get("view")) for i, m in zip(ids, raw["metadatas"][0])}
        value = (ids, distances, lookup)
        self._raw_memo[key] = value
        while len(self._raw_memo) > RAW_MEMO_ENTRIES:
            self._raw_memo.popitem(last=False)
        return value

    def _query_postfiltered(self, vector, view, allowed: set[str], count: int, total: int) -> list[dict] | None:
        size = min(total, max(count * POSTFILTER_OVERFETCH, POSTFILTER_MIN_CANDIDATES))
        mapping = self._representation_map()
        while size <= POSTFILTER_MAX_CANDIDATES:
            try:
                ids, distances, lookup = self._candidates(vector, size, mapping)
            except Exception as exc:  # noqa: BLE001 — 任何后过滤路径失败都交给原过滤路径处理/报错
                log.warning("无过滤向量查询失败，回退到 where 过滤：%s", exc)
                return None
            hits = []
            for identifier, distance in zip(ids, distances):
                segment_id, found_view = lookup.get(identifier, (None, None))
                if found_view == view and segment_id in allowed:
                    hits.append({"id": segment_id, "distance": distance})
            exhausted = size >= total or len(ids) < size
            if exhausted or len({hit["id"] for hit in hits}) >= count:
                return hits
            size = min(size * POSTFILTER_GROWTH, total)
        return None

    def _query_filtered(self, vector, view, eligible: list[str], count: int, total: int) -> list[dict]:
        # Chunk eligibility lists to bound SQLite/Chroma filter size, then globally rank.
        hits = []
        for offset in range(0, len(eligible), 500):
            where = {"$and": [{"view": view}, {"segment_id": {"$in": eligible[offset : offset + 500]}}]}
            size = min(count, total)
            while True:
                raw = self.collection.query(
                    query_embeddings=[vector], n_results=size, where=where, include=["metadatas", "distances"]
                )
                unique = {m["segment_id"] for m in raw["metadatas"][0]}
                if len(unique) >= count or len(raw["ids"][0]) < size or size == total:
                    break
                size = min(size * 2, total)
            hits.extend(
                {"id": metadata["segment_id"], "distance": distance}
                for metadata, distance in zip(raw["metadatas"][0], raw["distances"][0])
            )
        return hits

    @staticmethod
    def _best_per_segment(hits: list[dict], count: int) -> list[dict]:
        # Many windows from one segment only contribute once to a route.
        best = {}
        for hit in sorted(hits, key=lambda v: (v["distance"], v["id"])):
            best.setdefault(hit["id"], hit)
        return list(best.values())[:count]


class IndexBuilder:
    def __init__(self, config: Config, embedder: MultimodalEmbedder | None = None):
        self.config = config
        self.store = Store(config.data_dir)
        self._embedder = embedder

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = make_embedder(self.config)
        return self._embedder

    def build(self, *, activate=False, mode="auto", source_id=None, kinds=None, channels=None, progress=None):
        from mindex.shared.processing_lock import index_write_lock

        if progress:
            progress({"stage": "index_wait", "phase": "publish"})
        check_wait = (lambda: progress({"stage": "index_wait", "phase": "publish"})) if progress else None
        with index_write_lock(self.store.root, check=check_wait):
            if progress:
                progress({"stage": "index_prepare", "phase": "publish"})
            if mode == "enriched":
                if channels is not None:
                    raise ValueError("enriched 兼容模式不支持 channels，请使用 auto")
                return self._legacy_build(activate=activate, mode=mode, source_id=source_id, kinds=kinds)
            from mindex.processing.channel_index import build_channels

            return build_channels(
                self, activate=activate, mode=mode, source_id=source_id, kinds=kinds, channels=channels,
                progress=progress,
            )

    def _legacy_build(
        self,
        *,
        activate: bool = False,
        mode: str = "enriched",
        source_id: str | None = None,
        kinds: tuple[str, ...] | None = None,
    ) -> str:
        if mode not in {"visual", "enriched"}:
            raise ValueError("索引模式必须是 visual 或 enriched")
        if kinds is not None and (not kinds or set(kinds) - {"shot", "moment", "scene"}):
            raise ValueError("kind 必须是 shot、moment 或 scene")
        pipeline = Pipeline(self.config)
        if mode == "visual":
            if kinds and "scene" in kinds:
                raise ValueError("visual 模式不生成语义场景，请选择 shot 或 moment")
            snapshots = pipeline.visual_snapshots(source_id, kinds or ("moment", "shot"))
        else:
            snapshots = pipeline.snapshots(source_id)
            if kinds:
                snapshots = [s for s in snapshots if s["kind"] in kinds]
        if not snapshots:
            raise ValueError("没有可建索引的素材，请先 prepare；enriched 模式还需 transcribe 和 analyze")
        identity = self.embedder.identity()
        if hasattr(self.embedder, "require_gate"):
            self.embedder.require_gate()
        spec = {
            "space": identity,
            "preprocessing": self.config.video.model_dump(),
            "analysis_key": self.config.analysis_key if mode == "enriched" else None,
            "mode": mode,
            # An immutable membership/annotation manifest prevents changes under an active index.
            "manifest": fingerprint(snapshots),
            "format": 1,
        }
        index_id = fingerprint(spec)
        self.store.execute(
            "INSERT OR IGNORE INTO index_versions(id,spec,status) VALUES(?,?,'building')",
            (index_id, dumps(spec)),
        )
        vectors = VectorIndex(self.store.root, index_id, create=True)
        existing = vectors.ids()
        with self.store.run("index"):
            for number, segment in enumerate(snapshots, 1):
                log.info(
                    "Index %s %d/%d %s %.3f–%.3f",
                    mode,
                    number,
                    len(snapshots),
                    segment["kind"],
                    segment["start"],
                    segment["end"],
                )
                self.store.execute(
                    "INSERT OR IGNORE INTO index_members(index_id,segment_id,snapshot) VALUES(?,?,?)",
                    (index_id, segment["id"], dumps(segment)),
                )
                representations = self._representations(segment)
                for rep_id, view, document, bounds in representations:
                    rep_id = fingerprint([index_id, rep_id])
                    if rep_id not in existing:
                        if view == "video":
                            from mindex.shared.shot_media import ShotMedia

                            a, b = bounds or (0, segment["end"] - segment["start"])
                            with ShotMedia(self.store, cache_probes=False).inference_video(
                                segment["source_id"], segment["start"] + a, segment["start"] + b
                            ) as path:
                                vector = self.embedder.video(path)
                        else:
                            vector = self.embedder.text([document])[0]
                        vectors.upsert(rep_id, vector, segment, view, document)
                    # Chroma is written first. A crash before SQLite is repaired on resume.
                    self.store.execute(
                        "INSERT OR REPLACE INTO representations(id,index_id,segment_id,view,document) "
                        "VALUES(?,?,?,?,?)",
                        (rep_id, index_id, segment["id"], view, document),
                    )
                self.store.execute(
                    "UPDATE index_members SET complete=1 WHERE index_id=? AND segment_id=?",
                    (index_id, segment["id"]),
                )
            # 冲过 chroma HNSW 落盘阈值，避免尾部向量只存于内存与 WAL（详见 VectorIndex.flush）。
            vectors.flush()
            issues = check_index(self.store, index_id)
            if issues:
                raise ModelError("索引一致性检查失败：" + "; ".join(issues[:5]))
            if self.embedder.identity() != identity:
                raise ModelError("索引完成前模型空间发生变化，拒绝发布")
            if hasattr(self.embedder, "require_gate"):
                self.embedder.require_gate()
            self.store.execute("UPDATE index_versions SET status='ready' WHERE id=?", (index_id,))
            if activate:
                self.store.activate(index_id)
        return index_id

    def _representations(self, segment: dict) -> list[tuple]:
        duration = segment["end"] - segment["start"]
        video = self.config.video
        # Bound all physical units, including long shots and scenes, at inference time.
        pieces = (
            [(0, duration)]
            if duration <= video.short_shot_seconds
            else windows(0, duration, video.window_seconds, video.stride_seconds)
        )
        result = []
        for start, end in pieces:
            bounds = None if start == 0 and end == duration else (start, end)
            identifier = fingerprint([segment["id"], "video", start, end])
            result.append((identifier, "video", "", bounds))
        for view, document in segment["views"].items():
            identifier = fingerprint([segment["id"], view, document])
            result.append((identifier, view, document, None))
        return result


def check_index(store: Store, index_id: str) -> list[str]:
    issues = []
    row = store.one("SELECT * FROM index_versions WHERE id=?", (index_id,))
    if row is None:
        return ["找不到索引版本"]
    from collections import defaultdict

    representations = store.rows('SELECT id,segment_id,view FROM representations WHERE index_id=?', (index_id,))
    expected = {r['id'] for r in representations}
    member_views, member_ids = defaultdict(set), defaultdict(set)
    for representation in representations:
        member_views[representation['segment_id']].add(representation['view'])
        member_ids[representation['segment_id']].add(representation['id'])
    actual = VectorIndex(store.root, index_id).ids()
    if expected != actual:
        issues.append(f"向量与元数据不一致：缺少 {len(expected - actual)}，多出 {len(actual - expected)}")
    members = store.rows("SELECT * FROM index_members WHERE index_id=?", (index_id,))
    from mindex.shared.shot_media import ShotMedia
    media = ShotMedia(store, cache_probes=False, reuse_checks=True)
    archives = {}
    references = {r['path']: r for r in store.rows(
        "SELECT path,source_id,start,end FROM media_references WHERE source_id IN "
        "(SELECT DISTINCT json_extract(snapshot,'$.source_id') FROM index_members WHERE index_id=?)", (index_id,))}
    for member in members:
        if not member["complete"]:
            issues.append(f"片段未完成：{member['segment_id']}")
        snapshot = json.loads(member["snapshot"])
        # Validate references against physical shots without materializing media.
        reference = references.get(snapshot['path'])
        thumbnail = (store.root / snapshot["thumbnail"]).resolve()
        if not thumbnail.is_relative_to(store.root) or not thumbnail.is_file():
            issues.append(f"文件缺失：{snapshot['thumbnail']}")
        if reference and (reference['source_id'] != snapshot['source_id'] or
                          abs(reference['start'] - snapshot['start']) > 1e-6 or
                          abs(reference['end'] - snapshot['end']) > 1e-6):
            issues.append(f"媒体引用区间不一致：{member['segment_id']}")
        from mindex.processing.subtitle_archive import load_archive
        try:
            media.references(snapshot["source_id"], snapshot["start"], snapshot["end"])
            sid = snapshot['source_id']
            if sid not in archives:
                try:
                    load_archive(store, sid)
                    archives[sid] = None
                except (ValueError, OSError) as exc:
                    archives[sid] = str(exc)
            if archives[sid] is not None:
                raise ValueError(archives[sid])
        except (ValueError, OSError) as exc:
            issues.append(f"Shot/subtitle dependency failure: {exc}")
        views = member_views[member['segment_id']]
        if "representation_ids" in snapshot:
            manifest_ids = {fingerprint([index_id, rep]) for rep in snapshot["representation_ids"]}
            stored_ids = member_ids[member['segment_id']]
            if manifest_ids != stored_ids:
                issues.append(f"片段表示清单不一致：{member['segment_id']}")
        expected_views = set(snapshot.get("indexed_views", {"video", *snapshot["views"]}))
        if not expected_views or views != expected_views:
            issues.append(f"片段表示缺失：{member['segment_id']}")
    if not members:
        issues.append("索引没有片段")
    return issues
