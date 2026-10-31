"""Format 2: independent channels, immutable incremental snapshots and vector reuse."""

from __future__ import annotations

import json
import logging
from time import perf_counter

from mindex.models.adapters import ModelError
from mindex.processing.dialogue import CHANNELS, current_dialogue, indexed_dialogue, relative_dialogue
from mindex.processing.indexing import VectorIndex, check_index
from mindex.shared.config import fingerprint
from mindex.shared.scene_plans import current_segments
from mindex.shared.storage import dumps
from mindex.shared.types import Annotation

log = logging.getLogger(__name__)


def normalize_snapshot(snapshot):
    s = dict(snapshot)
    views = s.get("views", {})
    if "indexed_views" not in s:
        analysis = "\n".join(views[k] for k in ("description", "style") if views.get(k, "").strip())
        s["views"] = {
            k: v
            for k, v in {"analysis": analysis, "dialogue": views.get("dialogue", "")}.items()
            if v.strip()
        }
        s["indexed_views"] = ["video", *s["views"]]
    s.setdefault("channel_versions", {})
    s.setdefault("channel_metadata", {})
    return s


def snapshots_for(builder, source_id, kinds, *, status_only=False, segments=None, analysis_rows=None):
    store, result = builder.store, []
    sources = [s for s in store.sources() if source_id is None or s["id"] == source_id]
    if source_id and not sources:
        raise ValueError("找不到素材 ID")
    for source in sources:
        transcript = indexed_dialogue(current_dialogue(store, source["id"]))
        latest = {}
        records = analysis_rows if analysis_rows is not None else store.rows(
            "SELECT a.segment_id,a.data,a.profile FROM annotations a JOIN segments s ON s.id=a.segment_id "
            "WHERE s.source_id=? ORDER BY a.rowid DESC", (source['id'],))
        for record in records:
            latest.setdefault(record['segment_id'], record)
        for segment in current_segments(store, source["id"], segments):
            if segment["kind"] not in kinds:
                continue
            row = latest.get(segment["id"])
            value = json.loads(row["data"]) if row else None
            skipped = bool(value and value.get("status") == "skipped")
            annotation = (
                Annotation.model_validate(value["annotation"])
                if value and not skipped
                else Annotation(description="尚未进行语义描述，仅索引视频画面")
            )
            analysis = "\n".join(annotation.views("").values()) if value and not skipped else ""
            dialogue = relative_dialogue(transcript, segment["start"], segment["end"]) if transcript else []
            # Old annotations can contain dialogue unavailable in the transcripts table.
            if not transcript and value:
                dialogue = value.get("dialogue", [])
            text = "\n".join(u["text"] for u in dialogue)
            views = {k: v for k, v in {"analysis": analysis, "dialogue": text}.items() if v.strip()}
            versions = {"visual": fingerprint([source["id"], segment["start"], segment["end"]])}
            if value:
                versions["analysis"] = fingerprint({k: v for k, v in value.items() if k != "reused_from_profile"})
            if transcript:
                versions["dialogue"] = transcript["content_version"]
            elif dialogue:
                versions["dialogue"] = fingerprint(dialogue)
            if status_only:
                result.append({"id": segment['id'], "kind": segment['kind'],
                               "views": views, "channel_versions": versions})
                continue
            metadata = {
                "visual": {
                    "status": "complete",
                    "content_version": versions["visual"],
                    "input_fingerprint": fingerprint([source["id"], segment["generation"]]),
                    "model_identity": {"processor": "ffmpeg", "layout_key": source["layout_key"]},
                },
            }
            if transcript or dialogue:
                metadata["dialogue"] = {
                    "status": transcript["status"] if transcript else "complete",
                    "content_version": versions["dialogue"],
                    "input_fingerprint": transcript.get("input_fingerprint", transcript.get("profile"))
                    if transcript
                    else fingerprint(dialogue),
                    "model_identity": transcript.get("model_identity", {"profile": transcript.get("profile")})
                    if transcript
                    else {"source": "legacy_annotation"},
                }
            if value:
                metadata["analysis"] = {
                    "status": "skipped" if skipped else "complete",
                    "content_version": versions["analysis"],
                    "input_fingerprint": value.get("input_fingerprint", value.get("reused_from_profile", row["profile"])),
                    "model_identity": value.get("model_identity", {"profile": value.get("reused_from_profile", row["profile"])}),
                    "dialogue_input_version": value.get("dialogue_input_version"),
                }
            result.append(
                {
                    **segment,
                    "source_name": source["name"],
                    "source_path": source["path"],
                    "source_duration": json.loads(source["media"])["duration"],
                    "annotation": None if skipped else annotation.model_dump(),
                    "annotation_status": "skipped" if skipped else "complete" if value else "not_analyzed",
                    "dialogue": dialogue,
                    "transcript_status": transcript["status"] if transcript else "not_processed",
                    "dialogue_source": transcript.get("source") if transcript else None,
                    "scene_ids": [],
                    "views": views,
                    "indexed_views": ["video", *views],
                    "channel_versions": versions,
                    "channel_metadata": metadata,
                }
            )
    return result


def representation_key(builder, identity, segment, view, document, bounds):
    if view == "video":
        a, b = bounds if bounds is not None else (0, segment["end"] - segment["start"])
        media = [
            segment["source_id"],
            round(segment["start"] + a, 6),
            round(segment["start"] + b, 6),
        ]
        return fingerprint(["visual-v1", identity, builder.config.video.model_dump(), media])
    return fingerprint(["text-document-v1", identity, view, document])


def representations(builder, segment):
    result = builder._representations(segment)
    return [r for r in result if r[1] in segment["indexed_views"]]


def reusable_vectors(builder, identity):
    store, cache = builder.store, {}
    for row in store.rows(
        "SELECT id,spec FROM index_versions WHERE status IN ('ready','building') ORDER BY created_at DESC"
    ):
        spec = json.loads(row["spec"])
        if spec["space"] != identity or spec["preprocessing"] != builder.config.video.model_dump():
            continue
        records = store.rows("SELECT * FROM representations WHERE index_id=?", (row["id"],))
        record_ids = {r["id"] for r in records if not r["cache_key"]}
        for r in records:
            if r["cache_key"]:
                cache.setdefault(r["cache_key"], (row["id"], r["id"]))
        # Reconstruct interval keys for legacy and format 2 indexes.
        if not record_ids:
            continue
        for segment in store.snapshots(row["id"]):
            for rep, view, document, bounds in builder._representations(segment):
                rep_id = fingerprint([row["id"], rep])
                if rep_id in record_ids:
                    key = representation_key(builder, identity, segment, view, document, bounds)
                    cache.setdefault(key, (row["id"], rep_id))
    return cache


def build_channels(builder, *, activate=False, mode="auto", source_id=None, kinds=None, channels=None, progress=None):
    started = perf_counter()
    if mode not in {"auto", "visual"}:
        raise ValueError("索引模式必须是 auto、visual 或 enriched")
    selected = set(channels if channels is not None else (["visual"] if mode == "visual" else CHANNELS))
    if not selected or selected - set(CHANNELS):
        raise ValueError("channels 必须是 visual、dialogue、analysis")
    if kinds is not None and (not kinds or set(kinds) - {"moment", "shot", "scene"}):
        raise ValueError("kind 必须是 shot、moment 或 scene")
    if mode == "visual" and kinds and "scene" in kinds:
        raise ValueError("visual 模式不生成语义场景")
    store = builder.store
    log.info("索引准备开始 · 素材 %s · 渠道 %s · 粒度 %s", source_id or "全部", ",".join(sorted(selected)), ",".join(kinds or (["moment", "shot"] if mode == "visual" else ["moment", "shot", "scene"])))
    previous = store.active_index()
    old = {s["id"]: normalize_snapshot(s) for s in store.snapshots(previous["id"])} if previous else {}
    if kinds is None:
        kinds = ("moment", "shot") if mode == "visual" else ("moment", "shot", "scene")
    fresh = snapshots_for(builder, source_id, kinds)
    # Preserve other sources and channels, even when building one source/channel only.
    merged = dict(old)
    if "scene" in kinds:
        for source in store.sources():
            if source_id is not None and source['id'] != source_id:
                continue
            plan_row = store.one('SELECT data FROM scene_plans WHERE source_id=? ORDER BY rowid DESC LIMIT 1',
                                 (source['id'],))
            scene_ids = {s['id'] for s in fresh if s['source_id'] == source['id'] and s['kind'] == 'scene'}
            if plan_row and len(scene_ids) == len(json.loads(plan_row['data'])):
                # 只在当前计划完整落地后替换旧场景；历史发布快照仍不可变。
                merged = {sid: s for sid, s in merged.items() if s['source_id'] != source['id']
                          or s['kind'] != 'scene' or sid in scene_ids}
    for s in fresh:
        prior = old.get(s["id"])
        result = dict(prior or s)
        result["views"] = dict(prior["views"]) if prior else {}
        result["indexed_views"] = list(prior["indexed_views"]) if prior else []
        result["channel_versions"] = dict(prior.get("channel_versions", {})) if prior else {}
        result["channel_metadata"] = dict(prior.get("channel_metadata", {})) if prior else {}
        for channel in selected:
            view = "video" if channel == "visual" else channel
            # Missing/failed processing does not remove an already published channel.
            if channel != "visual" and channel not in s["channel_versions"]:
                continue
            if view in result["indexed_views"]:
                result["indexed_views"].remove(view)
            result["views"].pop(view, None)
            if view == "video" or s["views"].get(view):
                result["indexed_views"].append(view)
            if view in s["views"]:
                result["views"][view] = s["views"][view]
            result["channel_versions"][channel] = s["channel_versions"].get(channel)
            result["channel_metadata"][channel] = s["channel_metadata"][channel]
            if channel == "dialogue":
                for key in ("dialogue", "transcript_status", "dialogue_source"):
                    result[key] = s[key]
            if channel == "analysis":
                result["annotation"] = s["annotation"]
                result["annotation_status"] = s["annotation_status"]
        result["indexed_views"] = sorted(set(result["indexed_views"]))
        if result["indexed_views"]:
            merged[s["id"]] = result
        else:
            merged.pop(s["id"], None)
    snapshots = sorted(merged.values(), key=lambda s: s["id"])
    if not snapshots:
        raise ValueError("没有可建索引的已完成渠道数据")
    for s in snapshots:
        s["representation_ids"] = [r[0] for r in representations(builder, s)]
    # Validate physical dependencies before creating a candidate or invoking a model.
    from mindex.shared.shot_media import ShotMedia
    media = ShotMedia(store, cache_probes=False, reuse_checks=True)
    for s in snapshots:
        try:
            media.references(s["source_id"], s["start"], s["end"])
        except (ValueError, OSError) as exc:
            raise ModelError(f"素材 {s['source_id']} ({s['source_name']}) 媒体缺失：{exc}") from exc
    log.info("索引快照准备完成 · %d 个片段；开始核验嵌入模型身份", len(snapshots))
    identity = builder.embedder.identity()
    if hasattr(builder.embedder, "require_gate"):
        builder.embedder.require_gate()
    for s in snapshots:
        for view in s["indexed_views"]:
            channel = "visual" if view == "video" else view
            s["channel_metadata"].setdefault(channel, {})["embedding_model_identity"] = identity
    spec = {
        "format": 2,
        "mode": mode,
        "space": identity,
        "preprocessing": builder.config.video.model_dump(),
        "manifest": fingerprint(snapshots),
    }
    index_id = fingerprint(spec)
    selected_views = {"video" if c == "visual" else c for c in selected}
    targets = {
        s["id"]: {fingerprint([index_id, r[0]]) for r in representations(builder, s) if r[1] in selected_views}
        for s in snapshots
        if (source_id is None or s["source_id"] == source_id) and s["kind"] in kinds
    }
    targets = {sid: ids for sid, ids in targets.items() if ids}
    completed = set()

    def notify(stage):
        if progress:
            progress({
                "stage": stage, "completed": len(completed), "total": len(targets),
                "unit": "segments", "index_id": index_id,
            })

    log.info("索引 %s · 待更新 %d 个片段 · 保留及合并共 %d 个片段", index_id, len(targets), len(snapshots))
    existing_version = store.one("SELECT status FROM index_versions WHERE id=?", (index_id,))
    if existing_version and existing_version["status"] == "ready":
        completed.update(targets)
        notify("index_check")
        log.info("复用已就绪索引 %s；开始一致性校验", index_id)
        issues = check_index(store, index_id)
        if issues:
            raise ModelError("索引一致性检查失败：" + "; ".join(issues[:5]))
        if activate:
            notify("publish")
            log.info("开始激活索引 %s", index_id)
            store.activate(index_id)
        notify("published" if activate else "indexed")
        log.info("索引 %s %s · 用时 %.2f 秒", index_id, "已激活" if activate else "已就绪", perf_counter() - started)
        return index_id
    store.execute(
        "INSERT OR IGNORE INTO index_versions(id,spec,status) VALUES(?,?,'building')", (index_id, dumps(spec))
    )
    vectors = VectorIndex(store.root, index_id, create=True)
    existing = vectors.ids()
    # Count durable vectors and metadata, including checkpoints from an earlier
    # attempt. Other sources retained in the incremental index are not this task.
    recorded = {r["id"] for r in store.rows("SELECT id FROM representations WHERE index_id=?", (index_id,))}
    durable = existing & recorded
    completed.update(sid for sid, ids in targets.items() if ids <= durable)
    notify("embedding")
    log.info("扫描可复用嵌入缓存 · %d 个片段检查点已完成", len(completed))
    cache = reusable_vectors(builder, identity)
    log.info("嵌入缓存扫描完成 · %d 个可复用项", len(cache))
    opened = {index_id: vectors}
    reused = embedded = resumed = 0
    task_reused = task_embedded = 0
    coverage = {c: 0 for c in CHANNELS}
    with store.run("index"):
        # Copy checkpoints in batches before invoking the model. A new immutable
        # index still needs the vectors, but reuse requires no media/model work.
        from collections import defaultdict

        copies = defaultdict(list)
        for s in snapshots:
            for local_id, view, document, bounds in representations(builder, s):
                rep_id = fingerprint([index_id, local_id])
                key = representation_key(builder, identity, s, view, document, bounds)
                if rep_id not in existing and key in cache:
                    version, identifier = cache[key]
                    copies[version].append((identifier, rep_id, s, view, document))
        copied = set()
        copy_total = sum(len(entries) for entries in copies.values())
        log.info("开始复制可复用向量 · 共 %d 项", copy_total)
        for version, entries in copies.items():
            if version not in opened:
                opened[version] = VectorIndex(store.root, version)
            for offset in range(0, len(entries), 256):
                batch = entries[offset:offset + 256]
                saved = opened[version].vectors([entry[0] for entry in batch])
                writes = [(rep_id, saved[identifier], s, view, document)
                          for identifier, rep_id, s, view, document in batch if identifier in saved]
                if writes:
                    vectors.upsert_many(writes)
                    copied.update(entry[0] for entry in writes)
                log.info("向量缓存复制 %d/%d · 来源索引 %s", len(copied), copy_total, version)
        existing.update(copied)
        # Finish and report reusable segments first, even if missing segments
        # occur earlier in the source's ID ordering.
        snapshots.sort(key=lambda s: not all(
            fingerprint([index_id, r[0]]) in existing for r in representations(builder, s)
        ))
        for s in snapshots:
            if s["id"] in targets:
                log.debug("发布片段 %s [%s] %s %.2f–%.2fs [%s] · 完成 %d/%d",
                         s.get("source_name", ""), s["source_id"], s["kind"], s["start"], s["end"], s["id"], len(completed), len(targets))
            store.execute(
                "INSERT OR IGNORE INTO index_members(index_id,segment_id,snapshot) VALUES(?,?,?)",
                (index_id, s["id"], dumps(s)),
            )
            for local_id, view, document, bounds in representations(builder, s):
                rep_id = fingerprint([index_id, local_id])
                key = representation_key(builder, identity, s, view, document, bounds)
                target_rep = rep_id in targets.get(s["id"], set())
                if rep_id in existing:
                    if rep_id in copied:
                        reused += 1
                        task_reused += int(target_rep)
                    else:
                        resumed += 1
                else:
                    vector = None
                    if key in cache:
                        version, identifier = cache[key]
                        if version not in opened:
                            opened[version] = VectorIndex(store.root, version)
                        vector = opened[version].vector(identifier)
                    if vector is not None:
                        reused += 1
                        task_reused += int(target_rep)
                    else:
                        if not target_rep:
                            raise ModelError(
                                f"保留索引中的 {view} 向量缺失（{s['id']}）；请单独更新对应子任务"
                            )
                        log.info("生成嵌入开始 · %s [%s] %s %.2f–%.2fs · 渠道 %s · 向量 %s",
                                 s.get("source_name", ""), s["source_id"], s["kind"], s["start"], s["end"], view, rep_id)
                        if view == "video":
                            a, b = bounds or (0, s["end"] - s["start"])
                            with media.inference_video(
                                s["source_id"], s["start"] + a, s["start"] + b
                            ) as path:
                                vector = builder.embedder.video(path)
                        else:
                            vector = builder.embedder.text([document])[0]
                        embedded += 1
                        task_embedded += 1
                    vectors.upsert(rep_id, vector, s, view, document)
                    existing.add(rep_id)
                    cache[key] = (index_id, rep_id)
                store.execute(
                    "INSERT OR REPLACE INTO representations(id,index_id,segment_id,view,document,cache_key) VALUES(?,?,?,?,?,?)",
                    (rep_id, index_id, s["id"], view, document, key),
                )
            for view in s["indexed_views"]:
                coverage["visual" if view == "video" else view] += 1
            store.execute(
                "UPDATE index_members SET complete=1 WHERE index_id=? AND segment_id=?", (index_id, s["id"])
            )
            if s["id"] in targets and s["id"] not in completed:
                completed.add(s["id"])
                notify("embedding")
                log.info(
                    "发布进度 %s [%s] %d/%d · 缓存复用向量 %d · 模型新生成向量 %d",
                    ",".join(sorted(selected)), ",".join(sorted(kinds)),
                    len(completed), len(targets), task_reused, task_embedded,
                )
        # 冲过 chroma HNSW 落盘阈值，避免尾部向量只存于内存与 WAL（详见 VectorIndex.flush）。
        log.info("嵌入处理结束 · 新生成 %d · 复用 %d · 断点续传 %d；开始向量落盘", embedded, reused, resumed)
        vectors.flush()
        notify("index_check")
        log.info("向量落盘完成；开始索引一致性校验 %s", index_id)
        issues = check_index(store, index_id)
        if issues:
            raise ModelError("索引一致性检查失败：" + "; ".join(issues[:5]))
        log.info("索引一致性校验通过；重新核验嵌入模型身份")
        if builder.embedder.identity() != identity:
            raise ModelError("索引完成前模型空间发生变化，拒绝发布")
        if hasattr(builder.embedder, "require_gate"):
            builder.embedder.require_gate()
        store.execute("UPDATE index_versions SET status='ready' WHERE id=?", (index_id,))
        report = {
            "elapsed_seconds": round(perf_counter() - started, 3),
            "reused_vectors": reused,
            "embedded_vectors": embedded,
            "resumed_vectors": resumed,
            "channel_coverage": coverage,
            "segments": len(snapshots),
            "rollback_index": previous["id"] if previous else None,
        }
        store.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            ("index_report." + index_id, dumps(report)),
        )
        if activate:
            notify("publish")
            log.info("开始激活索引 %s", index_id)
            store.activate(index_id)
        notify("published" if activate else "indexed")
    log.info("索引 %s %s · 用时 %.2f 秒 · 新生成 %d · 复用 %d · 断点续传 %d", index_id,
             "已激活" if activate else "已就绪", perf_counter() - started, embedded, reused, resumed)
    return index_id
