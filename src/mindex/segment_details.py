"""Read existing independent analysis outputs for an unpublished or published segment."""

from __future__ import annotations

import json

from filelock import FileLock

from .config import fingerprint
from .curation import Library, segment_public
from .dialogue import atomic_json, extract_subtitles, relative_dialogue, subtitle_tracks
from .media import MediaError


def person_section(store, segment):
    """Read processing coverage and overlapping evidence independently of publications."""
    from .persons import KIND_LABELS

    start, end = segment["start"], segment["end"]
    intervals = []
    for row in store.rows("SELECT data FROM person_coverage WHERE source_id=?", (segment["source_id"],)):
        data = json.loads(row["data"])
        a, b = max(start, data["start"]), min(end, data["end"])
        if b > a:
            intervals.append((a, b))
    covered = []
    for a, b in sorted(intervals):
        if covered and a <= covered[-1]["end"] + 1e-6:
            covered[-1]["end"] = max(b, covered[-1]["end"])
        else:
            covered.append({"start": a, "end": b})
    uncovered, cursor = [], start
    for interval in covered:
        if interval["start"] > cursor + 1e-6:
            uncovered.append({"start": cursor, "end": interval["start"]})
        cursor = interval["end"]
    if cursor < end - 1e-6:
        uncovered.append({"start": cursor, "end": end})
    status = "not_processed" if not covered else "partial" if uncovered else "processed"
    counts = dict.fromkeys(("accepted", "candidate", "rejected"), 0)
    entities = {r["id"]: json.loads(r["data"]) for r in store.rows("SELECT id,data FROM person_entities")}
    people = {}
    for row in store.rows(
        "SELECT status,actor_id,role_id,stage_id FROM person_evidence "
        "WHERE source_id=? AND start<? AND end>?",
        (segment["source_id"], end, start),
    ):
        counts[row["status"]] += 1
        for field in ("actor_id", "role_id", "stage_id"):
            identifier = row[field]
            if identifier in entities:
                entity = entities[identifier]
                people[identifier] = {
                    "id": identifier,
                    "name": entity["name"],
                    "kind": entity["kind"],
                    "kind_label": KIND_LABELS[entity["kind"]],
                }
    return {
        "status": status,
        "message": {"not_processed": "人物未处理", "partial": "人物部分处理", "processed": "人物处理完成"}[
            status
        ],
        "covered": covered,
        "uncovered": uncovered,
        "counts": counts,
        "people": sorted(people.values(), key=lambda p: (p["kind"], p["name"], p["id"])),
    }


def segment_details(store, segment_id):
    segment = Library(store).segment_row(segment_id)
    source = store.one("SELECT * FROM sources WHERE id=?", (segment["source_id"],))
    start, end = segment["start"], segment["end"]
    asr_candidates = []
    channel = store.one(
        "SELECT * FROM channel_data WHERE source_id=? AND channel='dialogue'", (source["id"],)
    )
    dialogue = json.loads(channel["data"]) if channel else {}
    if dialogue.get("source") in {"asr", "legacy_asr"}:
        asr_candidates.append({"profile": channel["profile"], **dialogue})
    for row in store.rows("SELECT * FROM transcripts WHERE source_id=? ORDER BY rowid DESC", (source["id"],)):
        data = json.loads(row["data"])
        if data.get("source", "asr") != "subtitle":
            asr_candidates.append({"profile": row["profile"], **data})
    asr = asr_candidates[0] if asr_candidates else None
    asr_result = {
        "status": asr.get("status", "complete") if asr else "not_processed",
        "message": "ASR 未处理",
        "utterances": [],
    }
    if asr:
        asr_result.update(
            utterances=relative_dialogue(asr, start, end),
            profile=asr.get("profile"),
            model_identity=asr.get("model_identity"),
            error=asr.get("error"),
        )
        if asr_result["utterances"]:
            asr_result["message"] = "ASR 分析结果"
        elif asr_result["status"] in {"complete", "empty"}:
            asr_result["message"] = "此镜头无 ASR 对白"
        else:
            asr_result["message"] = {
                "pending_alignment": "ASR 等待时间对齐",
                "no_audio": "原片无音轨",
                "disabled": "ASR 未启用",
                "partial": "ASR 部分处理，此镜头暂无结果",
                "failed": "ASR 处理失败",
                "processing": "ASR 处理中",
            }.get(asr_result["status"], "ASR 未处理")
    subtitle_result = {"status": "complete", "tracks": [], "message": "字幕轨道结果"}
    try:
        path = store.resolve_media(source["path"])
        # Subtitle metadata/text cache belongs to source bytes, not embedding versions.
        stat = path.stat()
        key = fingerprint(["segment-subtitles-v1", source["id"], stat.st_size, stat.st_mtime_ns])
        cache_path = store.root / "subtitles" / source["id"] / (key + ".json")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(cache_path) + ".lock", timeout=180):
            if cache_path.exists():
                cached = json.loads(cache_path.read_text())
            else:
                tracks = subtitle_tracks(path)
                cached = []
                for track in tracks:
                    item = {
                        **track,
                        "utterances": [],
                        "status": "bitmap" if not track["text"] else "complete",
                    }
                    if track["text"]:
                        try:
                            item["utterances"] = extract_subtitles(
                                path, track, json.loads(source["media"])["duration"]
                            )
                        except MediaError as exc:
                            item.update(status="failed", error=str(exc))
                    cached.append(item)
                if not any(t["status"] == "failed" for t in cached):
                    atomic_json(cache_path, cached)
        for track in cached:
            rows = relative_dialogue(track, start, end)
            message = (
                "位图字幕，无法提取文本"
                if track["status"] == "bitmap"
                else "字幕提取失败"
                if track["status"] == "failed"
                else "此镜头无字幕"
                if not rows
                else "字幕轨道结果"
            )
            subtitle_result["tracks"].append({**track, "utterances": rows, "message": message})
        if not cached:
            subtitle_result.update(status="no_tracks", message="无字幕轨道")
    except (MediaError, OSError) as exc:
        subtitle_result.update(status="failed", message="字幕轨道读取失败", error=str(exc))
    row = store.one("SELECT * FROM annotations WHERE segment_id=? ORDER BY rowid DESC LIMIT 1", (segment_id,))
    llm = (
        {
            "status": "complete",
            "message": "LLM 分析结果",
            "profile": row["profile"],
            **json.loads(row["data"]),
        }
        if row
        else {"status": "not_processed", "message": "LLM 未处理"}
    )
    return {
        "segment": segment_public(segment),
        "asr": asr_result,
        "subtitles": subtitle_result,
        "llm": llm,
        "person": person_section(store, segment),
    }
