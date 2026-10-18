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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter

from .adapters import ModelError, make_transcriber
from .config import Config, fingerprint
from .media import MediaError, command, extract_audio, probe
from .storage import Store, dumps

CHANNELS = ("visual", "dialogue", "analysis")
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "ttml", "microdvd"}
READY = {"complete", "empty", "no_audio", "disabled"}


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".partial")
    partial.write_text(dumps(value), encoding="utf-8")
    partial.replace(path)


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


def extract_subtitles(path: Path, track: dict, duration: float) -> list[dict]:
    # ffmpeg preserves source timestamps when no seek is applied. SRT conversion
    # removes ASS drawing/style sections; clean residual HTML and override tags.
    raw = command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
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


def parse_minimax_words(payload: dict, duration: float | None = None) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ModelError("MiniMax ASR 缺少 text 字段")
    if payload.get("base_resp", {}).get("status_code", 0):
        raise ModelError(f"MiniMax ASR 失败：{payload['base_resp']}")
    values = payload.get("words")
    if values is None:
        values = payload.get("segments", [])
    if not isinstance(values, list):
        raise ModelError("MiniMax ASR 时间戳格式无效")
    words = []
    previous = -1.0
    for value in values:
        if not isinstance(value, dict):
            raise ModelError("MiniMax ASR 时间戳格式无效")
        text = value.get("word", value.get("text", ""))
        if not isinstance(text, str):
            raise ModelError("MiniMax ASR 词文本无效")
        if not text.strip():
            continue
        try:
            start, end = float(value["start"]), float(value["end"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelError("非空对白缺少有效时间戳") from exc
        if (
            not all(math.isfinite(v) for v in (start, end))
            or start < 0
            or end <= start
            or start < previous
            or (duration is not None and end > duration + 0.2)
        ):
            raise ModelError("MiniMax ASR 时间戳无效或超出音频区间")
        previous = start
        word = {"start": start, "end": min(end, duration) if duration is not None else end, "text": text}
        for field in ("speaker", "speaker_id"):
            if field in value:
                word["speaker_id"] = value[field]
        words.append(word)
    if payload["text"].strip() and not words:
        raise ModelError("非空对白缺少有效词级时间戳，拒绝伪造定位")
    return words


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


def relative_dialogue(data: dict, start: float, end: float) -> list[dict]:
    result = []
    for u in data.get("utterances", []):
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
        self.config, self.store, self.transcriber = config, Store(config.data_dir), transcriber

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
            "INSERT OR REPLACE INTO channel_data(source_id,channel,profile,status,data) VALUES(?,'dialogue',?,?,?)",
            (source_id, profile, data["status"], dumps(data)),
        )
        return data

    def process(self, source_id=None, *, method="auto", subtitle_stream=None, start=0, end=None,
                progress=None, workers=1):
        if method not in {"auto", "subtitle", "asr"}:
            raise ValueError("对白方式必须是 auto、subtitle 或 asr")
        started = perf_counter()
        sources = [s for s in self.store.sources() if source_id is None or s["id"] == source_id]
        if not sources:
            raise ValueError("找不到素材")
        results = []
        for source in sources:
            with self.store.run("dialogue", source["id"]):
                results.append(self._source(source, subtitle_stream, start, end, progress, workers, method))
        return {"sources": results, "elapsed_seconds": round(perf_counter() - started, 3)}

    def _source(self, source, stream, start, end, progress, workers, method):
        sid = source["id"]
        path = self.store.resolve_media(source["path"])
        info = probe(path)
        tracks = subtitle_tracks(path) if method != "asr" else []
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
                progress({"stage": "subtitle", "message": "正在提取文本字幕"})
            profile = fingerprint(["subtitle-srt-v1", sid, selected])
            previous = self.store.one(
                "SELECT * FROM channel_data WHERE source_id=? AND channel='dialogue'", (sid,)
            )
            if previous and previous["profile"] == profile and previous["status"] in READY and (
                manual_subtitle or json.loads(previous["data"]).get("utterances")
            ):
                return {"source_id": sid, **json.loads(previous["data"]), "reused": True}
            try:
                utterances = extract_subtitles(path, selected, info["duration"])
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
                    return {"source_id": sid, **data}
                reason = "所选文本字幕提取结果为空"
            except MediaError as exc:
                if manual_subtitle:
                    raise
                reason = "字幕提取失败：" + str(exc)
        elif selected:
            reason = "指定字幕为位图字幕，无法提取文本"
        # The ASR profile deliberately excludes local aligner and subtitle identity.
        model = self.config.asr.model_dump(
            exclude={"timeout", "api_key_env", "aligner_model", "aligner_revision", "deployment_manifest"}
        )
        profile = fingerprint(["asr-words-context1-v1", sid, model])
        common.update(source="asr", fallback_reason=reason if method != "asr" else None, model_identity=model)
        old = self.store.one("SELECT * FROM channel_data WHERE source_id=? AND channel='dialogue'", (sid,))
        if old and old["profile"] == profile and old["status"] in READY:
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
        self.save(sid, profile, {**common, "status": "processing", "utterances": [], "words": []})
        if not 1 <= workers <= 8:
            raise ValueError("workers 必须在 1–8 之间")

        def chunk(number):
            core_start, core_end = number * 30, min((number + 1) * 30, info["duration"])
            target = cache / f"{number}.json"
            if not target.exists():
                a, b = max(0, core_start - 1), min(info["duration"], core_end + 1)
                if self.transcriber is None:
                    self.transcriber = make_transcriber(self.config)
                with tempfile.TemporaryDirectory(prefix="dialogue-", dir=self.store.root) as directory:
                    audio = Path(directory) / "audio.wav"
                    extract_audio(path, audio, a, b - a)
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
                    },
                )
            return core_end

        selected = [
            n
            for n in range(total)
            if min((n + 1) * 30, info["duration"]) > start and (end is None or n * 30 < end)
        ]
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
                                }
                            )
            words, completed = [], 0
            for number in range(total):
                target = cache / f"{number}.json"
                if target.exists():
                    words.extend(json.loads(target.read_text())["words"])
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
