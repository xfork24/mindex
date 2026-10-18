"""读取四项工作的真实结果；最近任务与已有结果各自独立。"""

from __future__ import annotations

import json
from collections import defaultdict
from types import SimpleNamespace

from .config import fingerprint
from .dialogue import READY, current_dialogue
from .source_tasks import person_setup, public_task


def covered_shots(shots, rows):
    intervals = defaultdict(list)
    for row in rows:
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
        intervals[data.get("shot_id")].append((data["start"], data["end"]))
    count = 0
    for shot in shots:
        cursor = shot["start"]
        for start, end in sorted(intervals[shot["id"]]):
            if start > cursor + 1e-6:
                break
            cursor = max(cursor, end)
        count += cursor >= shot["end"] - 1e-6
    return count


def source_work(store, source):
    from .channel_index import normalize_snapshot, snapshots_for
    from .persons import Persons

    sid = source["id"]
    segments = store.segments(sid)
    shots = [s for s in segments if s["kind"] == "shot"]
    annotations = {
        r["segment_id"]
        for r in store.rows(
            "SELECT DISTINCT a.segment_id FROM annotations a JOIN segments s ON s.id=a.segment_id WHERE s.source_id=?",
            (sid,),
        )
    }
    dialogue = current_dialogue(store, sid) or {}
    latest_dialogue = store.one(
        "SELECT data FROM channel_data WHERE source_id=? AND channel='dialogue'", (sid,)
    )
    if not dialogue and latest_dialogue:
        dialogue = json.loads(latest_dialogue["data"])
    has_scene_plan = bool(store.one("SELECT 1 FROM scene_plans WHERE source_id=? LIMIT 1", (sid,)))
    catalogue = Persons(store)
    coverage = store.rows("SELECT * FROM person_coverage WHERE source_id=?", (sid,))
    covered = covered_shots(shots, coverage)
    counts = catalogue.review_summary().get(sid, {"accepted": 0, "candidate": 0, "rejected": 0})
    linked = store.rows("SELECT DISTINCT actor_id,role_id FROM person_evidence WHERE source_id=?", (sid,))
    people = {r[k] for r in linked for k in ("actor_id", "role_id") if r[k]}
    active = store.active_index()
    published = (
        [normalize_snapshot(s) for s in store.snapshots(active["id"]) if s["source_id"] == sid]
        if active
        else []
    )
    kinds = sorted({s["kind"] for s in published}) or ["moment"]
    fresh = snapshots_for(SimpleNamespace(store=store), sid, kinds)
    old = {s["id"]: s for s in published}
    work = {
        "shots": {
            "status": "complete"
            if source["prepared"] and not source["missing_artifacts"]
            else "partial"
            if shots
            else "not_processed",
            "result": {
                "shots": len(shots),
                "moments": source["moment_count"],
                "missing_artifacts": source["missing_artifacts"],
            },
        },
        "dialogue": {
            "status": dialogue.get("status", "not_processed"),
            "result": {
                **source["channels"]["dialogue"],
                **{
                    k: dialogue.get(k)
                    for k in ("source", "selected_track", "model_identity", "fallback_reason")
                },
                "utterances": len(dialogue.get("utterances", [])),
            },
        },
        "analysis": {
            "status": "complete"
            if segments and len(annotations) == len(segments) and has_scene_plan
            else "partial"
            if annotations
            else "not_processed",
            "result": {
                "analyzed": len(annotations),
                "total": len(segments),
                "scenes": sum(s["kind"] == "scene" for s in segments),
            },
        },
        "persons": {
            "status": "complete"
            if shots and covered == len(shots)
            else "partial"
            if coverage
            else "not_processed",
            "result": {"covered_shots": covered, "total_shots": len(shots), "people": len(people), **counts},
            "setup": person_setup(store, sid),
        },
    }
    for name, channel in (("shots", "visual"), ("dialogue", "dialogue"), ("analysis", "analysis")):
        expected = [s for s in fresh if channel in s["channel_versions"]]
        current = bool(active and expected) and all(
            s["id"] in old and old[s["id"]]["channel_versions"].get(channel) == s["channel_versions"][channel]
            for s in expected
        )
        # 旧索引没有版本字段时，按冻结片段和文本验证覆盖，不修改历史快照。
        if published and all(not s["channel_versions"] for s in published):
            current = bool(expected) and all(
                s["id"] in old
                and (
                    channel == "visual"
                    or s["views"].get(channel, "") == old[s["id"]]["views"].get(channel, "")
                )
                for s in expected
            )
        work[name]["search"] = {
            **source["published_channels"][channel],
            "current": current,
            "expected_segments": sum(channel == "visual" or bool(s["views"].get(channel)) for s in expected),
            "index_id": active["id"] if active else None,
        }
    release, person_snapshot = catalogue.published(active["id"]) if active else (None, {})
    evidence = store.rows(
        "SELECT * FROM person_evidence WHERE source_id=? AND status='accepted' ORDER BY id", (sid,)
    )
    for row in evidence:
        row["data"] = json.loads(row["data"])
        row["role_id"] = row.pop("stage_id", None) or row["role_id"]
    published_evidence = [e for e in person_snapshot.get("evidence", []) if e["source_id"] == sid]
    published_coverage = [e for e in person_snapshot.get("coverage", []) if e["source_id"] == sid]
    work["persons"]["search"] = {
        "current": bool(release)
        and fingerprint(evidence) == fingerprint(published_evidence)
        and fingerprint(
            sorted([{**c, "data": json.loads(c["data"])} for c in coverage], key=lambda c: c["cache_id"])
        )
        == fingerprint(sorted(published_coverage, key=lambda c: c["cache_id"])),
        "indexed_segments": covered_shots(shots, published_coverage),
        "expected_segments": len(shots),
        "release_id": release["id"] if release else None,
    }
    # 只读验收也支持尚未运行新版本迁移的旧库。
    has_source_tasks = bool(
        store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_tasks'")
    )
    for name, value in work.items():
        table = {"dialogue": "dialogue_tasks", "persons": "person_tasks"}.get(name, "source_tasks")
        row = (
            store.one(
                f"SELECT * FROM {table} WHERE source_id=?"
                + (" AND work=?" if table == "source_tasks" else "")
                + " ORDER BY rowid DESC LIMIT 1",
                (sid, name) if table == "source_tasks" else (sid,),
            )
            if table != "source_tasks" or has_source_tasks
            else None
        )
        value["task"] = public_task(row) if row else None
        publication_task = (
            store.one(
                "SELECT * FROM source_tasks WHERE source_id=? AND work='publish' "
                "AND json_extract(options,'$.work')=? ORDER BY rowid DESC LIMIT 1",
                (sid, name),
            )
            if has_source_tasks
            else None
        )
        value["publish_task"] = public_task(publication_task) if publication_task else None
        latest = sorted(
            [t for t in (value["task"], value["publish_task"]) if t],
            key=lambda t: (t["updated_at"], t["created_at"]),
        )
        value["publication"] = latest[-1].get("publication") if latest else None
        ready = name in {"shots", "dialogue"} or bool(source["prepared"])
        value["can_start"] = ready
        value["start_reason"] = None if ready else "请先完成分镜处理"
        value["can_publish"] = bool(source["prepared"]) and value["status"] in READY | {"partial"}
    return work
