from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import chromadb
from chromadb.config import Settings

from .adapters import ModelError, MultimodalEmbedder, make_embedder
from .config import Config, fingerprint
from .media import cut, windows
from .pipeline import Pipeline
from .storage import Store, dumps

log = logging.getLogger(__name__)


class VectorIndex:
    def __init__(self, root: Path, index_id: str, create=False):
        self.client = chromadb.PersistentClient(
            path=str(root / "chroma"), settings=Settings(anonymized_telemetry=False)
        )
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
        raw = self.collection.get(ids=[identifier], include=["embeddings"])
        return raw["embeddings"][0].tolist() if raw["ids"] else None

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
        if not eligible or self.collection.count() == 0:
            return []
        # Chunk eligibility lists to bound SQLite/Chroma filter size, then globally rank.
        hits = []
        for offset in range(0, len(eligible), 500):
            where = {"$and": [{"view": view}, {"segment_id": {"$in": eligible[offset : offset + 500]}}]}
            size = min(count, self.collection.count())
            while True:
                raw = self.collection.query(
                    query_embeddings=[vector], n_results=size, where=where, include=["metadatas", "distances"]
                )
                unique = {m["segment_id"] for m in raw["metadatas"][0]}
                if len(unique) >= count or len(raw["ids"][0]) < size or size == self.collection.count():
                    break
                size = min(size * 2, self.collection.count())
            hits.extend(
                {"id": metadata["segment_id"], "distance": distance}
                for metadata, distance in zip(raw["metadatas"][0], raw["distances"][0])
            )
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
        from .processing_lock import index_write_lock

        if progress:
            progress({"stage": "index_wait", "phase": "publish"})
        with index_write_lock(self.store.root):
            if progress:
                progress({"stage": "index_prepare", "phase": "publish"})
            if mode == "enriched":
                if channels is not None:
                    raise ValueError("enriched 兼容模式不支持 channels，请使用 auto")
                return self._legacy_build(activate=activate, mode=mode, source_id=source_id, kinds=kinds)
            from .channel_index import build_channels

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
            snapshots = pipeline.visual_snapshots(source_id, kinds or ("moment",))
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
                            path = self.store.resolve_media(segment["path"])
                            if bounds is None:
                                vector = self.embedder.video(path)
                            else:
                                with tempfile.TemporaryDirectory(
                                    prefix="embed-", dir=self.store.root
                                ) as directory:
                                    window = Path(directory) / "window.mp4"
                                    cut(path, *bounds, window)
                                    vector = self.embedder.video(window)
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
    expected = {r["id"] for r in store.rows("SELECT id FROM representations WHERE index_id=?", (index_id,))}
    actual = VectorIndex(store.root, index_id).ids()
    if expected != actual:
        issues.append(f"向量与元数据不一致：缺少 {len(expected - actual)}，多出 {len(actual - expected)}")
    members = store.rows("SELECT * FROM index_members WHERE index_id=?", (index_id,))
    for member in members:
        if not member["complete"]:
            issues.append(f"片段未完成：{member['segment_id']}")
        snapshot = json.loads(member["snapshot"])
        for field in ("path", "thumbnail", "source_path"):
            try:
                store.resolve_media(snapshot[field])
            except FileNotFoundError:
                issues.append(f"文件缺失：{snapshot[field]}")
        views = {
            r["view"]
            for r in store.rows(
                "SELECT view FROM representations WHERE index_id=? AND segment_id=?",
                (index_id, member["segment_id"]),
            )
        }
        if "representation_ids" in snapshot:
            manifest_ids = {fingerprint([index_id, rep]) for rep in snapshot["representation_ids"]}
            stored_ids = {
                r["id"]
                for r in store.rows(
                    "SELECT id FROM representations WHERE index_id=? AND segment_id=?",
                    (index_id, member["segment_id"]),
                )
            }
            if manifest_ids != stored_ids:
                issues.append(f"片段表示清单不一致：{member['segment_id']}")
        expected_views = set(snapshot.get("indexed_views", {"video", *snapshot["views"]}))
        if not expected_views or views != expected_views:
            issues.append(f"片段表示缺失：{member['segment_id']}")
    if not members:
        issues.append("索引没有片段")
    return issues
