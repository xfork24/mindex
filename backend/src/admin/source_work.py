"""读取四项工作的真实结果；最近任务与已有结果各自独立。"""

from __future__ import annotations

import json
from collections import defaultdict
from types import SimpleNamespace

from admin.source_tasks import person_setup
from persons.persons import Persons
from processing.channel_index import normalize_snapshot, snapshots_for
from processing.dialogue import READY, current_dialogue
from processing.embedding_coverage import embedding_coverage
from shared.background_tasks import public_task
from shared.config import fingerprint
from shared.scene_plans import current_segments, select_scenes


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


def same_json(left, right):
    """Compare canonical JSON content without serializing large evidence payloads."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(v, right[k]) for k, v in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        return repr(left) == repr(right)
    return left == right


def source_work(store, source, *, segments=None, analysis_rows=None, active=None, published=None):

    sid = source["id"]
    segments = current_segments(store, sid, segments)
    shots = [s for s in segments if s["kind"] == "shot"]
    segment_ids = {s["id"] for s in segments}
    scene_plan = store.one(
        "SELECT profile,data FROM scene_plans WHERE source_id=? ORDER BY rowid DESC LIMIT 1", (sid,)
    )
    # 场景按需生成媒体；已经排好的场景即使尚未落入 segments 也属于待分析工作。
    planned_ids = set()
    if scene_plan:
        plan = json.loads(scene_plan["data"])
        if isinstance(plan, list):
            scenes = select_scenes(segments, plan, scene_plan["profile"])
            for start, end in plan:
                # Match Pipeline._segment's existing-record lookup. Historical IDs
                # need not equal the ID a current fingerprint would generate.
                existing = next(
                    (s for s in scenes if abs(s["start"] - start) < 1e-6 and abs(s["end"] - end) < 1e-6), None
                )
                planned_ids.add(
                    existing["id"]
                    if existing
                    else fingerprint([sid, "scene", round(start, 6), round(end, 6), scene_plan["profile"]])
                )
    expected_ids = segment_ids | planned_ids
    annotations = {
        r["segment_id"]
        for r in store.rows(
            "SELECT DISTINCT a.segment_id FROM annotations a JOIN segments s ON s.id=a.segment_id WHERE "
            "s.source_id=?",
            (sid,),
        )
    } & expected_ids
    skipped_rows = store.rows(
        "SELECT a.segment_id,a.data FROM annotations a JOIN segments s ON s.id=a.segment_id "
        "WHERE s.source_id=? AND json_extract(a.data,'$.status')='skipped' "
        "AND a.rowid=(SELECT MAX(b.rowid) FROM annotations b WHERE b.segment_id=a.segment_id)",
        (sid,),
    )
    skipped = {r["segment_id"] for r in skipped_rows} & annotations
    skipped_reasons = defaultdict(int)
    for row in skipped_rows:
        if row["segment_id"] in skipped:
            value = json.loads(row["data"])
            skipped_reasons[value.get("reason") or value.get("message") or ""] += 1
    dialogue = current_dialogue(store, sid) or {}
    latest_dialogue = store.one(
        "SELECT data FROM channel_data WHERE source_id=? AND channel='dialogue'", (sid,)
    )
    if not dialogue and latest_dialogue:
        dialogue = json.loads(latest_dialogue["data"])
    pending = len(expected_ids - annotations)
    catalogue = Persons(store)
    coverage = store.rows("SELECT * FROM person_coverage WHERE source_id=?", (sid,))
    covered = covered_shots(shots, coverage)
    counts = catalogue.review_summary().get(sid, {"accepted": 0, "candidate": 0, "rejected": 0})
    linked = store.rows("SELECT DISTINCT actor_id,role_id FROM person_evidence WHERE source_id=?", (sid,))
    people = {r[k] for r in linked for k in ("actor_id", "role_id") if r[k]}
    active = active or store.active_index()
    published = (
        [normalize_snapshot(s) for s in published]
        if published is not None
        else (
            [normalize_snapshot(s) for s in store.snapshots(active["id"], sid) if s["source_id"] == sid]
            if active
            else []
        )
    )
    kinds = ("moment", "shot", "scene")
    fresh = snapshots_for(
        SimpleNamespace(store=store),
        sid,
        kinds,
        status_only=True,
        segments=segments,
        analysis_rows=analysis_rows,
    )
    work = {
        "shots": {
            "status": "complete"
            if source["prepared"] and not source["missing_artifacts"]
            else "partial"
            if shots
            else "not_processed",
            "result": {
                "shots": len(shots),
                "completed_shots": source.get(
                    "completed_shots", len(shots) if not source["missing_artifacts"] else 0
                ),
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
                    for k in (
                        "source",
                        "selected_track",
                        "subtitle_file",
                        "model_identity",
                        "fallback_reason",
                    )
                },
                "utterances": len(dialogue.get("utterances", [])),
            },
        },
        "analysis": {
            "status": "complete"
            if segments and not pending and scene_plan
            else "partial"
            if annotations
            else "not_processed",
            "result": {
                "analyzed": len(annotations - skipped),
                "skipped": len(skipped),
                "skipped_reasons": [
                    {"reason": reason, "count": count} for reason, count in sorted(skipped_reasons.items())
                ],
                "pending": pending,
                "total": len(expected_ids),
                "scenes": sum(s["kind"] == "scene" for s in segments) + len(planned_ids - segment_ids),
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

    for name, coverage_fields in embedding_coverage(store, sid, active, fresh, segments, published).items():
        work[name].update(coverage_fields)
    release, person_snapshot = catalogue.published(active["id"], source_id=sid) if active else (None, {})
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
        and bool(shots)
        and covered_shots(shots, published_coverage) == len(shots)
        and same_json(evidence, published_evidence)
        and same_json(
            sorted([{**c, "data": json.loads(c["data"])} for c in coverage], key=lambda c: c["cache_id"]),
            sorted(published_coverage, key=lambda c: c["cache_id"]),
        ),
        "indexed_segments": covered_shots(shots, published_coverage),
        "expected_segments": len(shots),
        "release_id": release["id"] if release else None,
    }
    return attach_tasks(store, source, work)


def attach_tasks(store, source, work):
    """Read persisted task progress without recomputing coverage."""
    sid = source["id"]
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
                "AND json_extract(options,'$.work')=? "
                "AND json_extract(options,'$.scope')='shot_moment' ORDER BY rowid DESC LIMIT 1",
                (sid, name),
            )
            if has_source_tasks
            else None
        )
        value["publish_task"] = public_task(publication_task) if publication_task else None
        scene_task = (
            store.one(
                "SELECT * FROM source_tasks WHERE source_id=? AND work='publish' "
                "AND json_extract(options,'$.work')=? AND json_extract(options,'$.scope')='scene' "
                "ORDER BY rowid DESC LIMIT 1",
                (sid, name),
            )
            if has_source_tasks
            else None
        )
        value["scene_publish_task"] = public_task(scene_task) if scene_task else None
        latest = sorted(
            [t for t in (value["task"], value["publish_task"]) if t],
            key=lambda t: (t["updated_at"], t["created_at"]),
        )
        value["publication"] = latest[-1].get("publication") if latest else None
        ready = name in {"shots", "dialogue"} or bool(source["prepared"])
        value["can_start"] = ready
        value["start_reason"] = None if ready else "请先完成分镜处理"
        value["can_publish_scene"] = (
            name in {"shots", "dialogue", "analysis"}
            and bool(source["prepared"])
            and value.get("scene_search", {}).get("expected_segments", 0) > 0
        )
        value["can_publish"] = bool(source["prepared"]) and value["status"] in READY | {"partial"}
    return work
