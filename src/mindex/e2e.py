"""Explicit, resumable single-GPU stages. Never starts or switches remote models."""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import numpy as np
from filelock import FileLock

from .adapters import ModelError, make_embedder
from .config import Config, fingerprint
from .film import source_report
from .media import atomic_media, content_hash, cut, probe
from .pipeline import Pipeline
from .storage import Store

PHASES = (
    "smoke",
    "visual",
    "preview",
    "asr",
    "align",
    "analyze-smoke",
    "analyze",
    "enriched",
    "evaluate",
    "video-evaluate",
    "check",
)


def progress(config: Config, directory: Path) -> dict:
    state = json.loads((directory / "run.json").read_text())
    store = Store(config.data_dir)
    counts = store.rows(
        "SELECT s.kind,count(*) AS count FROM annotations a JOIN segments s ON s.id=a.segment_id "
        "WHERE a.profile=? AND s.source_id=? GROUP BY s.kind",
        (config.analysis_key, state["source_id"]),
    )
    active = store.active_index()
    return {
        "source_id": state["source_id"],
        "phases": {
            k: {
                field: v[field]
                for field in ("status", "started_at", "elapsed_ms", "error", "result_file")
                if field in v
            }
            for k, v in state["phases"].items()
        },
        "analysis_key": config.analysis_key,
        "annotations": counts,
        "expected_moments": len(json.loads((directory / "segments.json").read_text())),
        "active_index": active["id"] if active else None,
        "acceptance": state["acceptance"],
    }


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def curation(store: Store):
    return {
        name: store.rows(f"SELECT * FROM {name} ORDER BY 1")
        for name in ("source_curation", "segment_curation")
    }


def initialize(config: Config, source_id: str, directory: Path):
    directory = directory.resolve()
    if (directory / "run.json").exists():
        raise ValueError("实验已存在，请继续 phase；新实验使用新的目录")
    store = Store(config.data_dir)
    report = source_report(config, source_id)
    if not report["prepared"] or not report["configuration_matches"] or report["missing_artifacts"]:
        raise ValueError("素材未完成切片、配置不符或文件缺失")
    from .shot_media import ShotMedia
    from .subtitle_archive import load_archive
    ShotMedia(store).references(source_id)
    load_archive(store, source_id)
    segments = store.segments(source_id, "moment")
    manifest_rows = []
    for segment in segments:
        clip = store.resolve_media(segment["path"])
        info = probe(clip)
        if abs(info["duration"] - (segment["end"] - segment["start"])) > 0.15:
            raise ValueError(f"切片时长不匹配 {segment['id']}")
        manifest_rows.append({**segment, "sha256": content_hash(clip), "media": info})
    if len(manifest_rows) < 6:
        raise ValueError("六片段真实冒烟至少需要 6 个 moment")
    save(directory / "source.json", report)
    save(directory / "segments.json", manifest_rows)
    save(directory / "config.initial.json", config.model_dump(mode="json"))
    state = {
        "schema": 1,
        "source_id": source_id,
        "data_dir": str(store.root),
        "layout_key": config.layout_key,
        "segments_hash": fingerprint(manifest_rows),
        "curation_hash": fingerprint(curation(store)),
        "created_at": datetime.now(UTC).isoformat(),
        "phases": {phase: {"status": "pending"} for phase in PHASES},
        "acceptance": "pending-real-models-and-human-labels",
    }
    save(directory / "run.json", state)
    # A review sheet records intervals without pretending model captions are truth.
    lines = [
        "# 中文相关性标注草稿",
        "",
        "观看分镜后填写 queries.draft.jsonl 的 text、relevant 和 category。",
        "20 条文字：前 8 条 dev，后 12 条 test。确认后使用 e2e freeze；测试集不用于调参。",
        "参考时间段或 segment_id，grade 为 1/2/3。不要直接把模型输出当作真值。",
        "",
    ]
    drafts = [
        {
            "id": f"text-{i + 1:02}",
            "split": "dev" if i < 8 else "test",
            "category": ["scene", "action", "interaction", "camera", "dialogue"][i % 5],
            "text": "",
            "granularity": "moment",
            "relevant": [],
            "label_status": "draft",
        }
        for i in range(20)
    ]
    for i, segment in enumerate(segments):
        lines.append(f"- {i + 1:02} `{segment['id']}`：{segment['start']:.3f}–{segment['end']:.3f}s")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "LABELING.md").write_text("\n".join(lines))
    (directory / "queries.draft.jsonl").write_text(
        "\n".join(json.dumps(v, ensure_ascii=False) for v in drafts) + "\n"
    )
    diagnostics = [
        {
            "id": f"absent-{i + 1}",
            "split": "diagnostic",
            "text": text,
            "no_answer": True,
            "relevant": [],
            "label_status": "draft",
        }
        for i, text in enumerate(
            [
                "太空中宇航员修理空间站",
                "雪山顶上滑雪者跃下悬崖",
                "潜水员在海底抚摸珊瑚",
                "机器人在工厂焊接汽车",
                "沙漠里骆驼队穿过沙丘",
            ]
        )
    ]
    (directory / "no-answer.draft.jsonl").write_text(
        "\n".join(json.dumps(v, ensure_ascii=False) for v in diagnostics) + "\n"
    )
    make_video_queries(store, source_id, segments, directory)
    return state


def make_video_queries(store: Store, source_id: str, segments: list[dict], directory: Path):
    candidates = [s for s in segments if s["end"] - s["start"] >= 1]
    chosen = [candidates[int(i)] for i in np.linspace(0, len(candidates) - 2, 4)]
    entries = []
    from .shot_media import ShotMedia
    original = ShotMedia(store).materialize(source_id)
    for number in range(12):
        segment = chosen[number % 4]
        output = directory / "video-queries" / f"{number + 1:02}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        start, end = segment["start"], segment["end"]
        kind = "self" if number < 4 else "crop" if number < 6 else "crosscut" if number < 8 else "codec"
        if kind == "self":
            shutil.copyfile(store.resolve_media(segment["path"]), output)
        elif kind == "crop":
            margin = (end - start) * 0.2
            start, end = start + margin, end - margin
            cut(original, start, end, output)
        elif kind == "crosscut":
            start, end = max(0, end - 0.6), min(probe(original)["duration"], end + 0.6)
            cut(original, start, end, output)
        else:
            atomic_media(
                [
                    "-i",
                    str(store.resolve_media(segment["path"])),
                    "-vf",
                    "scale=320:-2",
                    "-c:v",
                    "libx264",
                    "-crf",
                    "30",
                    "-c:a",
                    "aac",
                ],
                output,
            )
        targets = [
            {"segment_id": s["id"], "grade": 3 if kind != "crosscut" else 2}
            for s in segments
            if min(s["end"], end) - max(s["start"], start) > 0.05
        ]
        entries.append(
            {
                "id": f"video-{number + 1:02}",
                "split": "video",
                "video": str(output.relative_to(directory)),
                "variant": kind,
                "relevant": targets,
                "label_status": "draft",
                "sha256": content_hash(output),
            }
        )
    (directory / "video-queries.draft.jsonl").write_text(
        "\n".join(json.dumps(v, ensure_ascii=False) for v in entries) + "\n"
    )


def freeze(directory: Path, path: Path, confirmed_by: str):
    entries = [json.loads(v) for v in path.read_text().splitlines() if v.strip()]
    if not confirmed_by.strip() or not entries or len({v["id"] for v in entries}) != len(entries):
        raise ValueError("需要确认人、唯一查询 ID 和非空查询集")
    for entry in entries:
        if entry.get("label_status") != "confirmed":
            raise ValueError(f"{entry['id']} 尚未人工确认 label_status=confirmed")
        if sum(bool(entry.get(key, "").strip()) for key in ("text", "video", "image")) != 1:
            raise ValueError("每条查询需且只能有一个非空 text/video/image")
        if not entry.get("relevant") and not entry.get("no_answer"):
            raise ValueError("普通查询需相关目标；无答案需 no_answer=true")
        for key in ("video", "image"):
            if entry.get(key) and content_hash(path.parent / entry[key]) != entry["sha256"]:
                raise ValueError("媒体查询文件发生变化")
    # Keep video paths relative to the input file's directory.
    target = directory / ("frozen-" + path.name.replace(".draft", ""))
    if target.exists():
        raise ValueError("冻结集已存在；修订标注必须创建新实验")
    for entry in entries:
        for key in ("video", "image"):
            if key in entry:
                entry[key] = str((path.parent / entry[key]).resolve())
    target.write_text("\n".join(json.dumps(v, ensure_ascii=False) for v in entries) + "\n")
    save(
        target.with_suffix(".manifest.json"),
        {
            "sha256": content_hash(target),
            "confirmed_by": confirmed_by,
            "confirmed_at": datetime.now(UTC).isoformat(),
            "counts": {
                split: sum(v["split"] == split for v in entries) for split in {v["split"] for v in entries}
            },
        },
    )
    return str(target)


def smoke(config: Config, directory: Path) -> dict:
    root = config.data_dir / "capabilities"
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "smoke.lock"), timeout=0):
        return _smoke(config, directory)


def _smoke(config: Config, directory: Path) -> dict:
    from . import compatibility
    from .deployment import ADAPTER_VERSION

    rows = json.loads((directory / "segments.json").read_text())
    unique = list({row["sha256"]: row for row in rows}.values())
    if len(unique) < 6:
        raise ValueError("至少需要六个不同的实际视频文件")
    samples = [unique[int(i)] for i in np.linspace(0, len(unique) - 1, 6)]
    embedder, store = make_embedder(config), Store(config.data_dir)
    space = embedder.identity()
    v2 = space.get("schema") == 2 and space.get("implementation") == ADAPTER_VERSION
    deployment = embedder._runtime if v2 else None
    baseline = compatibility.load_baseline(config, space, allow_missing=True) if v2 else None
    if v2:
        compatibility.revoke(config, space, deployment)
    else:
        save(
            config.data_dir / "capabilities" / (fingerprint(space) + ".json"),
            {"passed": False, "space": space, "status": "checking"},
        )
    texts = [
        "室外建筑前的人",
        "房间里交谈的人",
        "街道上的人群",
        "一个人骑自行车",
        "人物面部近景",
        "画面中的文字",
    ]
    if baseline is not None:
        samples = baseline["samples"]
        texts = [sample["text"] for sample in samples]
    samples = [
        {**sample, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
        for sample, text in zip(samples, texts)
    ]
    text_vectors, video_vectors, timings = [], [], []
    raw_text, raw_video, prepared_hashes = [], [], []
    with embedder.validating() if v2 else nullcontext():
        for row in samples:
            path = store.resolve_media(row["path"])
            if content_hash(path) != row["sha256"]:
                raise ModelError("smoke 视频输入发生变化")
            started = perf_counter()
            text_vectors.append(embedder.text([row["text"]], role="query")[0])
            if v2:
                raw_text.append(embedder.last_raw_vector)
            text_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            video_vectors.append(embedder.video(path))
            if v2:
                raw_video.append(embedder.last_raw_vector)
                prepared_hashes.append(embedder.last_prepared_sha256)
            timings.append(
                {"segment_id": row["id"], "text_ms": text_ms, "video_ms": (perf_counter() - started) * 1000}
            )
            if content_hash(path) != row["sha256"]:
                raise ModelError("smoke 视频输入发生变化")
        repeated = embedder.video(store.resolve_media(samples[0]["path"]))
        repeated_text = embedder.text([texts[0]], role="query")[0]
    videos, texts_array = np.asarray(video_vectors), np.asarray(text_vectors)
    similarity = videos @ videos.T
    off_diagonal = similarity[np.triu_indices(6, 1)]
    checks = {
        "finite": bool(np.isfinite(videos).all() and np.isfinite(texts_array).all()),
        "dimensions": videos.shape == texts_array.shape == (6, config.embedding.dimensions),
        "unit_norm": bool(
            np.allclose(np.linalg.norm(videos, axis=1), 1, atol=1e-4)
            and np.allclose(np.linalg.norm(texts_array, axis=1), 1, atol=1e-4)
        ),
        "video_content_changes_vector": bool(np.max(off_diagonal) < 0.99999),
        "text_content_changes_vector": bool(
            np.max((texts_array @ texts_array.T)[np.triu_indices(6, 1)]) < 0.99999
        ),
        "video_repeat_stable": float(np.dot(repeated, videos[0])) >= 0.999,
        "text_repeat_stable": float(np.dot(repeated_text, texts_array[0])) >= 0.999,
        "space_stable": embedder.identity() == space,
    }
    if v2 and (
        embedder._runtime["deployment_id"] != deployment["deployment_id"]
        or embedder._runtime["instance_marker"] != deployment["instance_marker"]
        or embedder._runtime["file_states"] != deployment["file_states"]
    ):
        raise ModelError("smoke 期间部署或本地文件状态发生变化")
    report = {
        "passed": all(checks.values()),
        "checks": checks,
        "space": space,
        "space_id": fingerprint(space),
        "samples": samples,
        "measurements": timings,
        "text_vectors": text_vectors,
        "video_vectors": video_vectors,
        "text_video_cosine": (texts_array @ videos.T).tolist(),
        "video_pair_cosine": similarity.tolist(),
        "note": "结构与六样本兼容门槛不代表中文检索质量通过；须人工相关性标注和浏览器验收。",
    }
    if v2:
        report = compatibility.complete_validation(
            config, space, deployment, samples, report, raw_text, raw_video, prepared_hashes, baseline
        )
        compatibility.save_validation(config, space, deployment["deployment_id"], report)
    else:
        save(config.data_dir / "capabilities" / (fingerprint(space) + ".json"), report)
    save(directory / "smoke.json", report)
    if not report["passed"]:
        raise ModelError("六片段嵌入能力/兼容性门槛未通过，详见 smoke.json；兼容失败须升级数值实现版本并重建")
    return report


def run_phase(
    config: Config,
    directory: Path,
    phase: str,
    *,
    python: Path | None = None,
    queries: Path | None = None,
    split: str = "test",
    index_id: str | None = None,
    activate: bool = False,
) -> dict:
    if phase not in PHASES:
        raise ValueError(f"phase 必须为 {PHASES}")
    directory = directory.resolve()
    with FileLock(str(directory / "run.lock"), timeout=0):
        state_path = directory / "run.json"
        state = json.loads(state_path.read_text())
        store = Store(config.data_dir)
        selected_index = index_id
        if selected_index is None:
            for mode in ("enriched", "visual"):
                completed = state["phases"].get(mode, {})
                if completed.get("status") == "complete":
                    selected_index = json.loads((directory / completed["result_file"]).read_text())[
                        "index_id"
                    ]
                    break
        if state["data_dir"] != str(store.root) or state["layout_key"] != config.layout_key:
            raise ValueError("实验数据目录或切分参数发生变化，请创建新实验")
        if state["curation_hash"] != fingerprint(curation(store)):
            raise ValueError("整理状态已变化；质量比较需要冻结相同候选集")
        source = store.one("SELECT path FROM sources WHERE id=?", (state["source_id"],))
        if not source:
            raise ValueError("素材登记不存在")
        from .shot_media import ShotMedia
        from .subtitle_archive import load_archive
        ShotMedia(store).references(state["source_id"])
        load_archive(store, state["source_id"])
        rows = json.loads((directory / "segments.json").read_text())
        if fingerprint(rows) != state["segments_hash"]:
            raise ValueError("实验切片清单发生变化")
        for row in rows:
            if content_hash(store.resolve_media(row["path"])) != row["sha256"]:
                raise ValueError("切片文件发生变化")
        previous = state["phases"].get(phase)
        if previous and previous["status"] != "pending":
            state.setdefault("history", []).append({"phase": phase, **previous})
        attempt = uuid4().hex[:12]
        phase_state = {
            "attempt": attempt,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "config": config.model_dump(mode="json"),
        }
        state["phases"][phase] = phase_state
        save(state_path, state)
        started = perf_counter()
        try:
            if phase == "smoke":
                result = smoke(config, directory)
            elif phase in {"visual", "enriched"}:
                from .indexing import IndexBuilder

                with Pipeline(config).lock():
                    index_id = IndexBuilder(config).build(
                        mode="visual" if phase == "visual" else "enriched",
                        source_id=state["source_id"],
                        kinds=("moment",),
                        activate=activate,
                    )
                result = {"index_id": index_id, "active": store.active_index()}
            elif phase in {"asr", "align", "analyze-smoke", "analyze"}:
                pipeline = Pipeline(config)
                with pipeline.lock():
                    if phase == "asr":
                        pipeline.transcribe(state["source_id"])
                    elif phase == "align":
                        if python is None:
                            raise ValueError("align 需要 --python 指定独立 CPU 环境解释器")
                        pipeline.align(python, state["source_id"])
                    elif phase == "analyze-smoke":
                        moments = store.segments(state["source_id"], "moment")
                        samples = [moments[i] for i in (0, len(moments) // 3, 2 * len(moments) // 3)]
                        for segment in samples:
                            pipeline._annotate(segment)
                    else:
                        pipeline.analyze(state["source_id"])
                result = {
                    "source_id": state["source_id"],
                    "transcript": store.json_value(
                        "transcripts", "source_id", state["source_id"], config.asr_key
                    ),
                }
                if phase == "analyze-smoke":
                    result["samples"] = [
                        {
                            "segment": segment,
                            "analysis": store.json_value(
                                "annotations", "segment_id", segment["id"], config.analysis_key
                            ),
                        }
                        for segment in samples
                    ]
            elif phase == "preview":
                from .search import SearchEngine
                from .types import TextSearch

                if queries is None:
                    raise ValueError("preview 需要 --queries 指定文字或视频查询草稿")
                entries = [json.loads(v) for v in queries.read_text().splitlines() if v.strip()]
                if selected_index is None:
                    raise ValueError("请先建立实验索引或指定 --index-id")
                engine = SearchEngine(config, index_id=selected_index)
                details = []
                for entry in entries:
                    if entry.get("text", "").strip():
                        response = engine.text(TextSearch(text=entry["text"], limit=10))
                    elif entry.get("video"):
                        response = engine.video(queries.parent / entry["video"], limit=10)
                    elif entry.get("image"):
                        response = engine.image(queries.parent / entry["image"], limit=10)
                    else:
                        raise ValueError("查询草稿中有空查询")
                    details.append({"query": entry, **response})
                result = {
                    "status": "unreviewed_preview",
                    "queries_sha256": content_hash(queries),
                    "details": details,
                    "note": "仅提供排名供人工检查，未计算质量分数",
                }
                active = store.index_version(selected_index)
                save(directory / f"preview-{active['id']}-{content_hash(queries)[:12]}.json", result)
            elif phase in {"evaluate", "video-evaluate"}:
                from .evaluation import evaluate

                if queries is None:
                    raise ValueError("需要 --queries 指定已冻结的人工标注")
                label_manifest = queries.with_suffix(".manifest.json")
                if not label_manifest.is_file() or json.loads(label_manifest.read_text())[
                    "sha256"
                ] != content_hash(queries):
                    raise ValueError("标注未冻结或已变化，请先 e2e freeze")
                if selected_index is None:
                    raise ValueError("请先建立实验索引或指定 --index-id")
                active = store.index_version(selected_index)
                selected_split = "video" if phase == "video-evaluate" and split == "test" else split
                result = evaluate(
                    config,
                    queries,
                    selected_split,
                    directory / f"evaluation-{active['id']}-{selected_split}-{phase}.json",
                    index_id=selected_index,
                )
            else:
                from .indexing import check_index

                active = store.index_version(selected_index) if selected_index else store.active_index()
                if not active:
                    raise ValueError("没有已发布索引")
                issues = check_index(store, active["id"])
                result = {"index_id": active["id"], "ok": not issues, "issues": issues}
                if issues:
                    raise ValueError("索引检查失败")
            save(directory / f"{phase}-latest.json", result)
            artifact = f"attempts/{phase}-{attempt}-result.json"
            save(directory / artifact, result)
            phase_state.update(status="complete", result_file=artifact)
        except (Exception, KeyboardInterrupt) as exc:
            phase_state.update(
                status="failed", error=str(exc) or type(exc).__name__, error_type=type(exc).__name__
            )
            raise
        finally:
            phase_state["elapsed_ms"] = (perf_counter() - started) * 1000
            save(directory / f"attempts/{phase}-{attempt}.json", phase_state)
            save(state_path, state)
        return {"phase": phase, **phase_state}
