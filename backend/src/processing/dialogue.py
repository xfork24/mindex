"""Subtitle-first dialogue acquisition on the original source timeline.

No embedder, visual analyzer or forced aligner is initialized here. A valid chunk
is committed only after its timestamps pass validation; raw responses survive failures.
"""

from __future__ import annotations

import html
import json
import math
import re
import tempfile
from pathlib import Path
from time import perf_counter

from models.adapters import make_transcriber
from models.transcribers import parse_minimax_words
from shared import jsonio
from shared.config import Config, fingerprint
from shared.db.fencing import FencedThreadPoolExecutor as ThreadPoolExecutor
from shared.media import MediaError, command
from shared.storage import Store, dumps

CHANNELS = ("visual", "dialogue", "analysis")
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "ttml", "microdvd"}
READY = {"complete", "empty", "no_audio", "disabled"}


def atomic_json(path: Path, value):
    jsonio.save_text(path, dumps(value))


def subtitle_tracks(path: Path) -> list[dict]:
    raw = json.loads(
        command(
            ["ffprobe", "-v", "error", "-show_streams", "-select_streams", "s", "-of", "json", str(path)],
            timeout=30,
        )
    )
    return [
        {
            "index": s["index"],
            "codec": s.get("codec_name", ""),
            "language": s.get("tags", {}).get("language", "und"),
            "title": s.get("tags", {}).get("title", ""),
            "default": bool(s.get("disposition", {}).get("default")),
            "forced": bool(s.get("disposition", {}).get("forced")),
            "text": s.get("codec_name") in TEXT_CODECS,
        }
        for s in raw.get("streams", [])
    ]


def choose_subtitle(tracks: list[dict], stream: int | None = None) -> dict | None:
    if stream is not None:
        selected = next((t for t in tracks if t["index"] == stream), None)
        if selected is None:
            raise ValueError(f"字幕轨道 {stream} 不存在（使用 ffprobe 的全局 stream index）")
        return selected
    candidates = [t for t in tracks if t["text"] and not t["forced"]]

    def priority(t):
        language = t["language"].casefold()
        chinese = language in {"chi", "zho", "cmn", "yue"} or language.startswith("zh")
        return (not chinese, not t["default"], t["index"])

    return min(candidates, key=priority, default=None)


def extract_subtitles(
    path: Path,
    track: dict,
    duration: float,
    *,
    input_format: str | None = None,
    subtitle_fps: float | None = None,
) -> list[dict]:
    # ffmpeg preserves source timestamps when no seek is applied. SRT conversion
    # removes ASS drawing/style sections; clean residual HTML and override tags.
    raw = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            *(["-f", input_format] if input_format else []),
            *(["-subfps", str(subtitle_fps)] if input_format == "microdvd" and subtitle_fps else []),
            "-i",
            str(path),
            "-map",
            f"0:{track['index']}",
            "-f",
            "srt",
            "pipe:1",
        ],
        timeout=120,
    ).decode("utf-8-sig", errors="replace")
    pattern = re.compile(
        r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)[^\n]*\n(.*?)(?=\n\s*\n|\Z)", re.DOTALL
    )
    utterances = []
    for match in pattern.finditer(raw.replace("\r", "")):
        fields = match.groups()

        def seconds(offset, fields=fields):
            h, m, s, ms = fields[offset : offset + 4]
            return int(h) * 3600 + int(m) * 60 + int(s) + float("0." + ms)

        start, end = max(0, seconds(0)), min(duration, seconds(4))
        text = html.unescape(re.sub(r"<[^>]*>|\{[^}]*\}", "", fields[8]))
        text = text.replace(r"\N", "\n").replace(r"\n", "\n").strip()
        if text and end > start:
            utterances.append(
                {
                    "start": start,
                    "end": end,
                    "text": text,
                    "language": track["language"],
                    "stream_index": track["index"],
                }
            )
    return sorted(utterances, key=lambda u: (u["start"], u["end"]))


def coalesce_words(words: list[dict]) -> list[dict]:
    groups, current = [], []
    for word in sorted(words, key=lambda w: (w["start"], w["end"])):
        if current and (
            word["start"] - current[-1]["end"] > 0.6
            or word.get("speaker_id") != current[-1].get("speaker_id")
            or word.get("chunk") != current[-1].get("chunk")
            or re.search(r"[。！？!?；;.]\s*$", current[-1]["text"])
            or word["end"] - current[0]["start"] > 12
        ):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    return [
        {
            "start": g[0]["start"],
            "end": max(w["end"] for w in g),
            "text": "".join(w["text"] for w in g),
            "words": g,
            **({"speaker_id": g[0]["speaker_id"]} if "speaker_id" in g[0] else {}),
        }
        for g in groups
    ]


def current_dialogue(store: Store, source_id: str) -> dict | None:
    row = store.one(
        "SELECT data,status FROM channel_data WHERE source_id=? AND channel='dialogue'", (source_id,)
    )
    if row and row["status"] in READY:
        return json.loads(row["data"])
    if row:
        previous = json.loads(row["data"]).get("previous_complete")
        if previous:
            return previous
    # Read old aligned transcripts independently of the currently configured ASR.
    for row in store.rows(
        "SELECT data,profile FROM transcripts WHERE source_id=? ORDER BY rowid DESC", (source_id,)
    ):
        data = json.loads(row["data"])
        if data.get("status") in READY:
            return {
                **data,
                "source": "legacy_asr",
                "content_version": fingerprint(data),
                "profile": row["profile"],
            }
    return None


def indexed_dialogue(data):
    """Request-local interval lookup; retain overlapping utterances and input order."""
    if not data:
        return data
    from bisect import bisect_left, bisect_right

    utterances = data.get("utterances", [])
    ordered = sorted(enumerate(utterances), key=lambda pair: pair[1]["start"])
    starts, ends = [], []
    maximum = float("-inf")
    for _, utterance in ordered:
        starts.append(utterance["start"])
        maximum = max(maximum, utterance["end"])
        ends.append(maximum)

    def lookup(start, end):
        selected = ordered[bisect_right(ends, start) : bisect_left(starts, end)]
        return [u for _, u in sorted(selected)]

    return {**data, "_interval_lookup": lookup}


def relative_dialogue(data: dict, start: float, end: float) -> list[dict]:
    result = []
    utterances = (
        data["_interval_lookup"](start, end) if "_interval_lookup" in data else data.get("utterances", [])
    )
    for u in utterances:
        if u["start"] >= end or u["end"] <= start:
            continue
        words = [w for w in u.get("words", []) if w["start"] < end and w["end"] > start]
        text = "".join(w["text"] for w in words) if u.get("words") else u["text"]
        if text.strip():
            result.append(
                {
                    **u,
                    "start": max(start, u["start"]) - start,
                    "end": min(end, u["end"]) - start,
                    "text": text,
                    "evidence_start": max(start, u["start"]),
                    "evidence_end": min(end, u["end"]),
                    "words": words,
                }
            )
    return result


class DialogueProcessor:
    def __init__(self, config: Config, transcriber=None):
        self.config, self.store, self.transcriber = config, Store.open(config), transcriber

    def save(self, source_id, profile, data):
        if data["status"] not in READY:
            previous = current_dialogue(self.store, source_id)
            if previous:
                data = {**data, "previous_complete": previous}
        data = {
            **data,
            "input_fingerprint": profile,
            "content_version": fingerprint([profile, data.get("utterances", []), data.get("status")]),
        }
        self.store.execute(
            "INSERT OR REPLACE INTO channel_data(source_id,channel,profile,status,data) "
            "VALUES(?,'dialogue',?,?,?)",
            (source_id, profile, data["status"], dumps(data)),
        )
        return data

    def _reuse_chunks(self, sid, cache, total, duration, previous):
        previous_data = json.loads(previous["data"]) if previous else {}
        for path in (self.store.root / "dialogue" / sid).glob("*/*.json"):
            if path.parent == cache or not path.stem.isdigit():
                continue
            number = int(path.stem)
            target = cache / path.name
            if number >= total or target.exists():
                continue
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                a, b = number * 30, min((number + 1) * 30, duration)
                bounds = (a, b, max(0, a - 1), min(duration, b + 1))
                if any(
                    abs(saved[key] - expected) > 1e-6
                    for key, expected in zip(
                        ("core_start", "core_end", "context_start", "context_end"), bounds
                    )
                ):
                    continue
                if not isinstance(saved["words"], list) or any(
                    not isinstance(w.get("text"), str)
                    or not w["text"].strip()
                    or not all(math.isfinite(w[k]) for k in ("start", "end"))
                    or not (bounds[2] <= w["start"] < w["end"] <= bounds[3])
                    or not (a <= (w["start"] + w["end"]) / 2 < b)
                    for w in saved["words"]
                ):
                    continue
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            identity = (
                previous_data.get("model_identity")
                if previous and path.parent.name == previous["profile"]
                else None
            )
            atomic_json(
                target,
                {
                    **saved,
                    "model_identity": saved.get("model_identity")
                    or identity
                    or {"profile": path.parent.name},
                },
            )

    def process(
        self,
        source_id=None,
        *,
        method="auto",
        subtitle_stream=None,
        subtitle_file=None,
        start=0,
        end=None,
        progress=None,
        workers=1,
        force=False,
    ):
        if method not in {"auto", "subtitle", "asr"}:
            raise ValueError("对白方式必须是 auto、subtitle 或 asr")
        if subtitle_file and (method != "subtitle" or subtitle_stream is not None or source_id is None):
            raise ValueError("上传字幕必须指定素材、使用字幕方式，且不能同时选择内嵌字幕轨道")
        started = perf_counter()
        sources = [s for s in self.store.sources() if source_id is None or s["id"] == source_id]
        if not sources:
            raise ValueError("找不到素材")
        results = []
        for source in sources:
            with self.store.run("dialogue", source["id"]):
                results.append(
                    self._source(
                        source,
                        subtitle_stream,
                        start,
                        end,
                        progress,
                        workers,
                        method,
                        subtitle_file,
                        force=force,
                    )
                )
        return {"sources": results, "elapsed_seconds": round(perf_counter() - started, 3)}

    def _source(
        self, source, stream, start, end, progress, workers, method, subtitle_file=None, *, force=False
    ):
        sid = source["id"]
        previous_complete = current_dialogue(self.store, sid) if not force else None
        if (
            not subtitle_file
            and stream is None
            and previous_complete
            and previous_complete.get("status") in {"complete", "empty", "no_audio"}
            and method == "asr"
            and previous_complete.get("source") in {"asr", "legacy_asr"}
        ):
            if progress:
                progress(
                    {
                        "stage": "asr"
                        if previous_complete.get("source") in {"asr", "legacy_asr"}
                        else "subtitle",
                        "message": "复用已完成对白，不重新转写",
                        "completed": 1,
                        "total": 1,
                    }
                )
            return {"source_id": sid, **previous_complete, "reused": True}
        if subtitle_file:
            from processing.subtitle_uploads import parse_subtitle_file, resolve_subtitle

            path, metadata = resolve_subtitle(self.store, sid, subtitle_file)
            profile = fingerprint(["uploaded-subtitle-v1", sid, subtitle_file])
            previous = self.store.one(
                "SELECT * FROM channel_data WHERE source_id=? AND channel='dialogue'", (sid,)
            )
            if not force and previous and previous["profile"] == profile and previous["status"] in READY:
                if progress:
                    progress({"stage": "subtitle", "completed": 1, "total": 1, "unit": "files"})
                return {"source_id": sid, **json.loads(previous["data"]), "reused": True}
            if progress:
                progress(
                    {
                        "stage": "subtitle",
                        "completed": 0,
                        "total": 1,
                        "unit": "files",
                        "message": "正在解析上传字幕文件",
                    }
                )
            media = json.loads(source["media"])
            rows = parse_subtitle_file(path, "." + metadata["format"], media["duration"], fps=media["fps"])
            data = self.save(
                sid,
                profile,
                {
                    "source": "subtitle",
                    "subtitle_file": metadata,
                    "selected_track": None,
                    "status": "complete",
                    "utterances": rows,
                    "words": [],
                    "language": "und",
                    "model_identity": {"extractor": "ffmpeg-srt-v1"},
                    "fallback_reason": None,
                },
            )
            if progress:
                progress({"stage": "subtitle", "completed": 1, "total": 1, "unit": "files"})
            return {"source_id": sid, **data}
        from processing.subtitle_archive import load_archive
        from shared.shot_media import ShotMedia

        info = json.loads(source["media"])
        archive = load_archive(self.store, sid) if method != "asr" else None
        tracks = archive["tracks"] if archive else []
        selected = choose_subtitle(tracks, stream) if method != "asr" else None
        manual_subtitle = method == "subtitle"
        if manual_subtitle and stream is None:
            raise ValueError("手动字幕方式必须选择文本字幕轨道")
        if manual_subtitle and (not selected or not selected["text"]):
            raise ValueError("请选择存在的文本字幕轨道；手动字幕方式不使用 ASR")
        common = {"subtitle_tracks": tracks, "selected_track": selected}
        reason = "没有可用的完整文本字幕（forced/位图字幕不自动选用；烧录字幕不做 OCR）"
        if selected and selected["text"]:
            if progress:
                progress(
                    {
                        "stage": "subtitle",
                        "completed": 0,
                        "total": 1,
                        "unit": "tracks",
                        "message": "正在提取文本字幕",
                    }
                )
            profile = fingerprint(["subtitle-srt-v1", sid, selected])
            previous = self.store.one(
                "SELECT * FROM channel_data WHERE source_id=? AND channel='dialogue'", (sid,)
            )
            if (
                not force
                and previous
                and previous["profile"] == profile
                and previous["status"] in READY
                and (manual_subtitle or json.loads(previous["data"]).get("utterances"))
            ):
                if progress:
                    progress({"stage": "subtitle", "completed": 1, "total": 1, "unit": "tracks"})
                return {"source_id": sid, **json.loads(previous["data"]), "reused": True}
            try:
                utterances = archive["utterances"][str(selected["index"])]
                if utterances or manual_subtitle:
                    data = self.save(
                        sid,
                        profile,
                        {
                            **common,
                            "source": "subtitle",
                            "status": "complete" if utterances else "empty",
                            "utterances": utterances,
                            "words": [],
                            "language": selected["language"],
                            "model_identity": {"extractor": "ffmpeg-srt-v1"},
                            "fallback_reason": None,
                        },
                    )
                    if progress:
                        progress({"stage": "subtitle", "completed": 1, "total": 1, "unit": "tracks"})
                    return {"source_id": sid, **data}
                reason = "所选文本字幕提取结果为空"
            except MediaError as exc:
                if manual_subtitle:
                    raise
                reason = "字幕提取失败：" + str(exc)
        elif selected:
            reason = "指定字幕为位图字幕，无法提取文本"
        if (
            not force
            and previous_complete
            and previous_complete.get("source") in {"asr", "legacy_asr"}
            and previous_complete.get("status") in {"complete", "empty", "no_audio"}
        ):
            if progress:
                progress(
                    {"stage": "asr", "completed": 1, "total": 1, "message": "复用已完成对白，不重新转写"}
                )
            return {"source_id": sid, **previous_complete, "reused": True}
        # The ASR profile deliberately excludes local aligner and subtitle identity.
        model = self.config.asr.model_dump(
            exclude={
                "timeout",
                "api_key_env",
                "max_tokens",
                "aligner_model",
                "aligner_revision",
                "deployment_manifest",
            }
        )
        profile = fingerprint(["asr-words-context1-v1", sid, model])
        common.update(source="asr", fallback_reason=reason if method != "asr" else None, model_identity=model)
        old = self.store.one("SELECT * FROM channel_data WHERE source_id=? AND channel='dialogue'", (sid,))
        if not force and old and old["profile"] == profile and old["status"] in READY:
            if progress:
                previous = json.loads(old["data"])
                progress(
                    {
                        "stage": "asr",
                        "completed_chunks": previous.get("completed_chunks", 0),
                        "total_chunks": previous.get("total_chunks", 0),
                        "unit": "chunks",
                    }
                )
            return {"source_id": sid, **json.loads(old["data"]), "reused": True}
        if not info["audio"] or not self.config.asr.enabled:
            data = self.save(
                sid,
                profile,
                {
                    **common,
                    "status": "no_audio" if not info["audio"] else "disabled",
                    "utterances": [],
                    "words": [],
                },
            )
            return {"source_id": sid, **data}
        if start < 0 or end is not None and end <= start:
            raise ValueError("ASR 时间区间无效")
        total = math.ceil(info["duration"] / 30)
        cache = self.store.root / "dialogue" / sid / profile
        cache.mkdir(parents=True, exist_ok=True)
        if not force:
            self._reuse_chunks(sid, cache, total, info["duration"], old)
        self.save(sid, profile, {**common, "status": "processing", "utterances": [], "words": []})
        if not 1 <= workers <= 8:
            raise ValueError("workers 必须在 1–8 之间")

        def chunk(number):
            core_start, core_end = number * 30, min((number + 1) * 30, info["duration"])
            target = cache / f"{number}.json"
            if force or not target.exists():
                a, b = max(0, core_start - 1), min(info["duration"], core_end + 1)
                if self.transcriber is None:
                    self.transcriber = make_transcriber(self.config)
                with tempfile.TemporaryDirectory(prefix="dialogue-", dir=self.store.root) as directory:
                    audio = Path(directory) / "audio.wav"
                    ShotMedia(self.store).audio(sid, audio, a, b - a)
                    if self.config.asr.backend == "minimax":
                        raw = self.transcriber.raw(audio, timestamps=True)
                        atomic_json(cache / f"raw-{number}.json", {**raw, "offset": a, "duration": b - a})
                        words = parse_minimax_words(raw["raw_response"], b - a)
                    else:
                        words = [u.model_dump() for u in self.transcriber.transcribe(audio)]
                        words = parse_minimax_words(
                            {"text": "".join(w["text"] for w in words), "words": words}, b - a
                        )
                accepted = [
                    {**w, "start": a + w["start"], "end": a + w["end"], "chunk": number}
                    for w in words
                    if core_start <= a + (w["start"] + w["end"]) / 2 < core_end
                ]
                atomic_json(
                    target,
                    {
                        "words": accepted,
                        "core_start": core_start,
                        "core_end": core_end,
                        "context_start": a,
                        "context_end": b,
                        "model_identity": model,
                    },
                )
            return core_end

        selected = [
            n
            for n in range(total)
            if min((n + 1) * 30, info["duration"]) > start and (end is None or n * 30 < end)
        ]
        if progress:
            progress(
                {
                    "stage": "asr",
                    "completed_chunks": sum((cache / f"{n}.json").exists() for n in range(total)),
                    "total_chunks": total,
                    "unit": "chunks",
                }
            )
        if self.transcriber is None and any(not (cache / f"{n}.json").exists() for n in selected):
            self.transcriber = make_transcriber(self.config)
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                # Bound outstanding uploads so a failure never queues the rest of a film.
                for offset in range(0, len(selected), workers):
                    for core_end in pool.map(chunk, selected[offset : offset + workers]):
                        completed = sum((cache / f"{n}.json").exists() for n in range(total))
                        if progress:
                            progress(
                                {
                                    "source_id": sid,
                                    "completed_chunks": completed,
                                    "total_chunks": total,
                                    "processed_until": core_end,
                                    "stage": "asr",
                                    "unit": "chunks",
                                }
                            )
            words, completed = [], 0
            chunk_models = {}
            for number in range(total):
                target = cache / f"{number}.json"
                if target.exists():
                    saved = json.loads(target.read_text(encoding="utf-8"))
                    words.extend(saved["words"])
                    chunk_models[str(number)] = saved.get("model_identity", model)
                    completed += 1
            state = "partial" if completed < total else "complete" if words else "empty"
            data = self.save(
                sid,
                profile,
                {
                    **common,
                    "status": state,
                    "words": words,
                    "utterances": coalesce_words(words),
                    "completed_chunks": completed,
                    "total_chunks": total,
                    "chunk_model_identities": chunk_models,
                    "model_identity": model
                    if all(value == model for value in chunk_models.values())
                    else {
                        "source": "mixed_asr_chunks",
                        "models": list({fingerprint(v): v for v in chunk_models.values()}.values()),
                    },
                },
            )
            return {"source_id": sid, **data}
        except (Exception, KeyboardInterrupt) as exc:
            self.save(
                sid,
                profile,
                {
                    **common,
                    "status": "failed",
                    "error": str(exc),
                    "utterances": [],
                    "words": [],
                    "completed_chunks": sum((cache / f"{n}.json").exists() for n in range(total)),
                    "total_chunks": total,
                },
            )
            raise
