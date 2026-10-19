from __future__ import annotations

import json
import logging
import math
import os
import shutil
import tempfile
from pathlib import Path

from filelock import FileLock

from .adapters import (
    AnalysisContentRejected,
    ShotDetector,
    Transcriber,
    VisualAnalyzer,
    make_analyzer,
    make_detector,
    make_transcriber,
)
from .config import Config, fingerprint
from .media import content_hash, cut, extract_audio, probe, thumbnail, windows
from .storage import Store, dumps
from .types import Annotation

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        config: Config,
        *,
        detector: ShotDetector | None = None,
        analyzer: VisualAnalyzer | None = None,
        transcriber: Transcriber | None = None,
    ):
        self.config = config
        self.store = Store(config.data_dir)
        self._detector = detector
        self._analyzer = analyzer
        self._transcriber = transcriber

    @property
    def detector(self):
        if self._detector is None:
            self._detector = make_detector(self.config)
        return self._detector

    @property
    def analyzer(self):
        if self._analyzer is None:
            self._analyzer = make_analyzer(self.config)
        return self._analyzer

    @property
    def transcriber(self):
        if self._transcriber is None:
            self._transcriber = make_transcriber(self.config)
        return self._transcriber

    def enrich_dialogue(self, source_id=None, **options):
        from .dialogue import DialogueProcessor

        return DialogueProcessor(self.config, transcriber=self._transcriber).process(source_id, **options)

    def lock(self, source_id=None, work=None):
        from .processing_lock import ProcessingLock

        return ProcessingLock(self.config, source_id=source_id, work=work)

    def _segment(
        self,
        source: dict,
        kind: str,
        start: float,
        end: float,
        generation: str,
        parent: str | None,
        reason: str,
    ) -> dict:
        identifier = fingerprint([source["id"], kind, round(start, 6), round(end, 6), generation])
        relative = f"clips/{source['id']}/{kind}/{identifier}.mp4"
        image = f"thumbnails/{identifier}.jpg"
        output = self.store.root / relative
        same_shot = self.store.one("SELECT * FROM segments WHERE id=?", (parent,)) if parent else None
        if (
            not output.exists()
            and same_shot
            and same_shot["start"] == start
            and same_shot["end"] == end
            and (self.store.root / same_shot["path"]).is_file()
        ):
            # A short shot and its whole-shot moment contain identical media.
            output.parent.mkdir(parents=True, exist_ok=True)
            original = self.store.resolve_media(same_shot["path"])
            try:
                os.link(original, output)
            except OSError:
                shutil.copyfile(original, output)
        else:
            cut(self.store.resolve_media(source["path"]), start, end, output)
        thumbnail(self.store.root / relative, self.store.root / image)
        segment = {
            "id": identifier,
            "source_id": source["id"],
            "kind": kind,
            "start": start,
            "end": end,
            "path": relative,
            "thumbnail": image,
            "generation": generation,
            "parent_id": parent,
            "reason": reason,
        }
        self.store.execute(
            """INSERT OR IGNORE INTO segments
          (id,source_id,kind,start,end,path,thumbnail,generation,parent_id,reason)
          VALUES(:id,:source_id,:kind,:start,:end,:path,:thumbnail,:generation,:parent_id,:reason)""",
            segment,
        )
        return segment

    def prepare(self, path: Path) -> str:
        identifier = self.detect(path)
        self.split(identifier)
        return identifier

    def register(self, path: Path, *, original_name: str | None = None, move=False) -> dict:
        """只验证、去重和原子登记原片；调用方不需要启动任何模型。"""
        path = path.resolve()
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("上传文件为空或不存在")
        log.info("Probe and hash source: %s", path)
        info = probe(path)
        identifier = content_hash(path)
        existing = self.store.one("SELECT * FROM sources WHERE id=?", (identifier,))
        name = (original_name or path.name).replace("\\", "/").rsplit("/", 1)[-1]
        if name in {"", ".", ".."}:
            raise ValueError("无效的文件名")
        destination = self.store.root / (existing["path"] if existing else f"sources/{identifier}/{name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Registration has its own short lease; uploads need not wait for a running model.
        with FileLock(str(self.store.root / "sources" / ".register.lock")):
            existing = self.store.one("SELECT * FROM sources WHERE id=?", (identifier,))
            if existing:
                destination = self.store.root / existing["path"]
            created = not destination.exists()
            partial = None
            try:
                if created:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if move:
                        path.replace(destination)
                    else:
                        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                            partial = Path(handle.name)
                        shutil.copyfile(path, partial)
                        partial.replace(destination)
                self.store.execute(
                    "INSERT OR IGNORE INTO sources(id,name,path,media,layout_key,layout_config) VALUES(?,?,?,?,?,?)",
                    (identifier, name, str(destination.relative_to(self.store.root)), dumps(info),
                     self.config.layout_key, dumps({"detector": self.config.detector.model_dump(),
                                                   "video": self.config.video.model_dump()})),
                )
            except BaseException:
                if created:
                    destination.unlink(missing_ok=True)
                raise
            finally:
                if partial:
                    partial.unlink(missing_ok=True)
        return {"source_id": identifier, "duplicate": bool(existing)}

    def detect(self, path: Path) -> str:
        """Persist a complete cut plan before any expensive clip encoding."""
        identifier = self.register(path)["source_id"]
        self.detect_source(identifier)
        return identifier

    def detect_source(self, identifier: str, progress=None):
        source = self.store.one("SELECT * FROM sources WHERE id=?", (identifier,))
        if not source:
            raise ValueError("找不到素材")
        if source["prepared"]:
            return
        if source["layout_key"] != self.config.layout_key:
            raise ValueError("素材切分配置不符，请使用原配置或新的 data_dir")
        destination = self.store.resolve_media(source["path"])
        info = json.loads(source["media"])
        plan_path = self.store.root / "plans" / f"{identifier}.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        with self.store.run("detect", identifier):
            if not plan_path.exists():
                if progress:
                    progress({"stage": "detect", "message": "正在检测分镜"})
                log.info("Detect shots: %s (%.1f minutes)", source["name"], info["duration"] / 60)
                ranges = self.detector.detect(destination)
                self._validate_ranges(ranges, info["duration"])
                plan = {
                    "source_id": identifier,
                    "layout_key": self.config.layout_key,
                    "duration": info["duration"],
                    "detector": self.config.detector.backend,
                    "shots": [{"start": a, "end": b} for a, b in ranges],
                }
                partial = plan_path.with_suffix(".partial")
                partial.write_text(dumps(plan))
                partial.replace(plan_path)
            else:
                self._read_plan(source)
        log.info("Cut plan ready: %s", plan_path)
        return identifier

    @staticmethod
    def _validate_ranges(ranges, duration: float):
        if not ranges or abs(ranges[0][0]) > 0.001 or abs(ranges[-1][1] - duration) > 0.1:
            raise ValueError("分镜检测没有完整覆盖视频")
        previous = 0.0
        for start, end in ranges:
            if (
                not all(math.isfinite(v) for v in (start, end))
                or abs(start - previous) > 0.001
                or end <= start
            ):
                raise ValueError("分镜检测区间必须连续且非空")
            previous = end

    def _read_plan(self, source: dict) -> list[tuple[float, float]]:
        path = self.store.root / "plans" / f"{source['id']}.json"
        if not path.exists():
            raise ValueError("没有分镜计划，请先执行 detect 或 prepare")
        plan = json.loads(path.read_text())
        if plan["source_id"] != source["id"] or plan["layout_key"] != self.config.layout_key:
            raise ValueError("分镜计划与当前配置不符，请使用原配置或新的 data_dir")
        ranges = [(s["start"], s["end"]) for s in plan["shots"]]
        self._validate_ranges(ranges, json.loads(source["media"])["duration"])
        return ranges

    def split(self, source_id: str, progress=None):
        source = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        if source is None:
            raise ValueError("找不到素材 ID，请先执行 detect")
        if not source["prepared"] and source["layout_key"] != self.config.layout_key:
            raise ValueError("素材切分配置不符，请使用原配置或新的 data_dir")
        ranges = self._read_plan(source) if not source["prepared"] else []
        with self.store.run("prepare", source_id):
            if not source["prepared"]:
                if progress:
                    progress({"stage": "split", "completed": 0, "total": len(ranges), "unit": "shots"})
                for number, (start, end) in enumerate(ranges, 1):
                    log.info("Split %s shot %d/%d %.3f–%.3f", source["name"], number, len(ranges), start, end)
                    shot = self._segment(
                        source, "shot", start, end, self.config.layout_key, None, self.config.detector.backend
                    )
                    v = self.config.video
                    pieces = (
                        [(start, end)]
                        if end - start <= v.short_shot_seconds
                        else windows(start, end, v.window_seconds, v.stride_seconds)
                    )
                    for a, b in pieces:
                        self._segment(
                            source,
                            "moment",
                            a,
                            b,
                            self.config.layout_key,
                            shot["id"],
                            "whole_shot" if len(pieces) == 1 else "sliding_window",
                        )
                    if progress:
                        progress({"stage": "split", "completed": number, "total": len(ranges), "unit": "shots"})
                self.store.execute("UPDATE sources SET prepared=1 WHERE id=?", (source_id,))
            else:
                # Resume also repairs missing physical artifacts without redetecting shots.
                segments = self.store.segments(source_id)
                if progress:
                    progress({"stage": "repair", "completed": 0, "total": len(segments), "unit": "segments"})
                for number, segment in enumerate(segments, 1):
                    self._segment(
                        source,
                        segment["kind"],
                        segment["start"],
                        segment["end"],
                        segment["generation"],
                        segment["parent_id"],
                        segment["reason"],
                    )
                    if progress:
                        progress({"stage": "repair", "completed": number, "total": len(segments), "unit": "segments"})

    def visual_snapshots(self, source_id: str | None = None, kinds=("moment",)) -> list[dict]:
        """Native video embeddings need neither ASR nor generated descriptions."""
        result = []
        for source in self.selected_sources(source_id):
            if source["layout_key"] != self.config.layout_key:
                raise ValueError("当前切分配置与素材不一致，请使用原配置")
            for segment in self.store.segments(source["id"]):
                if segment["kind"] not in kinds:
                    continue
                result.append(
                    {
                        **segment,
                        "source_name": source["name"],
                        "source_path": source["path"],
                        "source_duration": json.loads(source["media"])["duration"],
                        "annotation": Annotation(description="尚未进行语义描述，仅索引视频画面").model_dump(),
                        "annotation_status": "not_analyzed",
                        "dialogue": [],
                        "transcript_status": "not_requested",
                        "scene_ids": [],
                        "views": {},
                    }
                )
        return result

    def selected_sources(self, source_id: str | None = None) -> list[dict]:
        sources = [s for s in self.store.sources() if source_id is None or s["id"] == source_id]
        if source_id is not None and not sources:
            raise ValueError("找不到素材 ID")
        if any(not s["prepared"] for s in sources):
            raise ValueError("存在未完成切片的素材，请先重新执行 prepare")
        return sources

    def transcribe(self, source_id: str | None = None):
        if self.config.asr.backend == "minimax" and self.config.asr.enabled:
            return self.enrich_dialogue(source_id)
        if self.config.asr.backend in {"llama_cpp", "openai"} and self.config.asr.enabled:
            return self.transcribe_raw(source_id)
        for source in self.selected_sources(source_id):
            if (
                self.store.json_value("transcripts", "source_id", source["id"], self.config.asr_key)
                is not None
            ):
                continue
            with self.store.run("transcribe", source["id"]):
                info = json.loads(source["media"])
                utterances = []
                if self.config.asr.enabled and info["audio"]:
                    # Separate 30-second chunks keep retries and alignment bounded.
                    # Persist each chunk so hours of audio do not restart after a failure.
                    cache = self.store.root / "transcripts" / source["id"] / self.config.asr_key
                    cache.mkdir(parents=True, exist_ok=True)
                    for number, start in enumerate(range(0, int(info["duration"]) + 1, 30)):
                        duration = min(30, info["duration"] - start)
                        if duration <= 0:
                            continue
                        checkpoint = cache / f"{number}.json"
                        if checkpoint.exists():
                            chunk = json.loads(checkpoint.read_text())
                        else:
                            with tempfile.TemporaryDirectory(prefix="asr-", dir=self.store.root) as directory:
                                audio = Path(directory) / "audio.wav"
                                extract_audio(
                                    self.store.resolve_media(source["path"]), audio, start, duration
                                )
                                results = self.transcriber.transcribe(audio)
                            chunk = []
                            for item in results:
                                if item.end > duration + 0.2:
                                    raise ValueError("ASR 时间戳超出音频区间")
                                chunk.append(
                                    {
                                        "start": start + item.start,
                                        "end": start + min(item.end, duration),
                                        "text": item.text,
                                    }
                                )
                            temporary = checkpoint.with_suffix(".partial")
                            temporary.write_text(dumps(chunk))
                            temporary.replace(checkpoint)
                        utterances.extend(chunk)
                self.store.put_json(
                    "transcripts",
                    "source_id",
                    source["id"],
                    self.config.asr_key,
                    {
                        "utterances": utterances,
                        "status": "disabled"
                        if not self.config.asr.enabled
                        else "complete"
                        if info["audio"]
                        else "no_audio",
                    },
                )

    def transcribe_raw(self, source_id: str | None = None):
        """Checkpoint raw GGUF ASR output independently from CPU alignment."""
        from .llama_cpp import checked_runtime, manifest

        profile = self.config.asr_key
        checked = False
        for source in self.selected_sources(source_id):
            current = self.store.json_value("transcripts", "source_id", source["id"], profile)
            if current and current["status"] in {"complete", "no_audio"}:
                continue
            info = json.loads(source["media"])
            if not info["audio"]:
                self.store.put_json(
                    "transcripts",
                    "source_id",
                    source["id"],
                    profile,
                    {"utterances": [], "status": "no_audio"},
                )
                continue
            if not checked:
                checked = True
                if self.config.asr.backend == "llama_cpp":
                    pinned = manifest(self.config.asr, "asr")
                    checked_runtime(self.config.asr, pinned)
            with self.store.run("transcribe_raw", source["id"]):
                cache = self.store.root / "transcripts" / source["id"] / profile / "raw"
                cache.mkdir(parents=True, exist_ok=True)
                for number, start in enumerate(range(0, math.ceil(info["duration"]), 30)):
                    target = cache / f"{number}.json"
                    if target.exists():
                        continue
                    duration = min(30, info["duration"] - start)
                    with tempfile.TemporaryDirectory(prefix="asr-", dir=self.store.root) as directory:
                        audio = Path(directory) / "audio.wav"
                        extract_audio(self.store.resolve_media(source["path"]), audio, start, duration)
                        result = self.transcriber.raw(audio)
                    if self.config.asr_key != profile:
                        raise ValueError("转写过程中部署清单发生变化")
                    temporary = target.with_suffix(".partial")
                    temporary.write_text(dumps({**result, "start": start, "duration": duration}))
                    temporary.replace(target)
                self.store.put_json(
                    "transcripts",
                    "source_id",
                    source["id"],
                    profile,
                    {"utterances": [], "status": "pending_alignment"},
                )

    def align(self, python: Path, source_id: str | None = None):
        """Run optional ForcedAligner in a separate CPU-only Python environment."""
        from .align_worker import coalesce_alignment
        from .media import command
        from .types import Utterance

        if self.config.asr.aligner_revision == "main":
            raise ValueError("请先在配置中固定 asr.aligner_revision 为实际模型 commit")
        profile = self.config.asr_key
        script = Path(__file__).with_name("align_worker.py")
        for source in self.selected_sources(source_id):
            record = self.store.json_value("transcripts", "source_id", source["id"], profile)
            if not record:
                raise ValueError("请先执行 transcribe 保存原始文本")
            if record["status"] != "pending_alignment":
                continue
            cache = self.store.root / "transcripts" / source["id"] / profile
            utterances = []
            with self.store.run("align", source["id"]):
                for path in sorted((cache / "raw").glob("*.json"), key=lambda p: int(p.stem)):
                    raw = json.loads(path.read_text())
                    target = cache / ("aligned-" + path.name)
                    if not target.exists():
                        with tempfile.TemporaryDirectory(prefix="align-", dir=self.store.root) as directory:
                            audio = Path(directory) / "audio.wav"
                            extract_audio(
                                self.store.resolve_media(source["path"]), audio, raw["start"], raw["duration"]
                            )
                            command(
                                [
                                    str(python.absolute()),
                                    "-I",
                                    str(script),
                                    "--audio",
                                    str(audio),
                                    "--raw",
                                    str(path),
                                    "--output",
                                    str(target),
                                    "--model",
                                    self.config.asr.aligner_model,
                                    "--revision",
                                    self.config.asr.aligner_revision,
                                    "--cache-dir",
                                    str(self.store.root / "model-cache"),
                                ],
                                timeout=1800,
                            )
                    aligned = json.loads(target.read_text())
                    if aligned["revision"] != self.config.asr.aligner_revision:
                        raise ValueError("对齐检查点模型版本不匹配")
                    for value in coalesce_alignment(aligned["utterances"]):
                        item = Utterance.model_validate(value)
                        if item.end > raw["duration"] + 0.2:
                            raise ValueError("对齐时间戳超出音频块")
                        utterances.append(
                            {
                                "start": raw["start"] + item.start,
                                "end": raw["start"] + min(item.end, raw["duration"]),
                                "text": item.text,
                            }
                        )
                if not list((cache / "raw").glob("*.json")):
                    raise ValueError("缺少原始转写检查点")
                self.store.put_json(
                    "transcripts",
                    "source_id",
                    source["id"],
                    profile,
                    {
                        "utterances": utterances,
                        "status": "complete",
                        "alignment_model": self.config.asr.aligner_model,
                        "alignment_revision": self.config.asr.aligner_revision,
                    },
                )

    def _dialogue(self, source_id: str, start: float, end: float) -> list[dict]:
        from .dialogue import current_dialogue, relative_dialogue

        current = current_dialogue(self.store, source_id)
        if current is not None:
            return relative_dialogue(current, start, end)
        transcript = self.store.json_value("transcripts", "source_id", source_id, self.config.asr_key)
        if transcript is None:
            return []
        if transcript["status"] not in {"complete", "disabled", "no_audio"}:
            return []
        return [
            {"start": max(start, u["start"]) - start, "end": min(end, u["end"]) - start, "text": u["text"]}
            for u in transcript["utterances"]
            if u["start"] < end and u["end"] > start
        ]

    def annotation_profile(self, source_id):
        from .dialogue import current_dialogue

        transcript = current_dialogue(self.store, source_id)
        version = transcript.get("content_version") if transcript and transcript.get("utterances") else None
        return fingerprint([self.config.analysis_key, version]) if version else self.config.analysis_key

    def _annotate(self, segment: dict):
        from .dialogue import current_dialogue

        transcript = current_dialogue(self.store, segment["source_id"])
        input_version = transcript.get("content_version") if transcript else None
        base_profile = self.config.analysis_key
        profile = self.annotation_profile(segment["source_id"])
        saved = self.store.json_value("annotations", "segment_id", segment["id"], profile)
        if saved is not None:
            return saved
        same = self.store.one(
            "SELECT a.data FROM annotations a JOIN segments s ON s.id=a.segment_id "
            "WHERE a.profile=? AND s.source_id=? AND ((s.start=? AND s.end=?) OR "
            "(json_extract(a.data,'$.status')='skipped' AND s.start<=? AND s.end>=?)) LIMIT 1",
            (profile, segment["source_id"], segment["start"], segment["end"], segment["start"], segment["end"]),
        )
        if same:
            saved = json.loads(same["data"])
            self.store.put_json("annotations", "segment_id", segment["id"], profile, saved)
            return saved
        dialogue = self._dialogue(segment["source_id"], segment["start"], segment["end"])
        try:
            annotation = self.analyzer.analyze(self.store.resolve_media(segment["path"]), dialogue)
            output = {"annotation": annotation.model_dump(), "content_version": fingerprint(annotation.model_dump())}
        except AnalysisContentRejected as exc:
            log.warning("LLM 跳过敏感片段 %s %s %.3f–%.3f：%s", segment["source_id"],
                        segment["kind"], segment["start"], segment["end"], exc)
            output = {"status": "skipped", "reason": str(exc), "message": "模型内容安全策略拒绝，已跳过此片段"}
            output["content_version"] = fingerprint(output)
        if self.config.analysis_key != base_profile:
            raise ValueError("分析过程中模型身份发生变化")
        self.store.put_json(
            "annotations",
            "segment_id",
            segment["id"],
            profile,
            {
                **output,
                "dialogue": dialogue,
                "dialogue_input_version": input_version,
                "model_identity": self.config.analyzer.model_dump(),
                "input_fingerprint": fingerprint(
                    [segment["source_id"], segment["start"], segment["end"], profile]
                ),
            },
        )
        return output

    def analyze(self, source_id: str | None = None, progress=None):
        from .dialogue import atomic_json

        for source in self.selected_sources(source_id):
            profile = self.annotation_profile(source["id"])
            counts = {"skipped": 0}

            def notify(value, counts=counts):
                if progress:
                    progress({**value, **counts})

            with self.store.run("analyze", source["id"]):
                base = [s for s in self.store.segments(source["id"]) if s["kind"] != "scene"]
                for number, segment in enumerate(base, 1):
                    if progress:
                        notify({"stage": "analyze", "completed": number - 1, "total": len(base), "unit": "segments"})
                    log.info(
                        "Analyze %s %s %.2f–%.2f",
                        source["name"],
                        segment["kind"],
                        segment["start"],
                        segment["end"],
                    )
                    saved = self._annotate(segment)
                    counts["skipped"] += saved.get("status") == "skipped"
                    if progress:
                        notify({"stage": "analyze", "completed": number, "total": len(base), "unit": "segments"})
                plan = self.store.json_value("scene_plans", "source_id", source["id"], profile)
                if plan is None:
                    # Turn overlapping moments into contiguous units, while retaining
                    # their descriptions and original boundaries for semantic grouping.
                    moments = [s for s in base if s["kind"] == "moment"]
                    rejected = [s for s in base if self.store.json_value(
                        "annotations", "segment_id", s["id"], profile
                    ).get("status") == "skipped"]
                    units = []
                    for index, segment in enumerate(moments):
                        end = moments[index + 1]["start"] if index + 1 < len(moments) else segment["end"]
                        # 场景不能重新包含已被拒绝的画面；保留原时间轴缺口。
                        if any(s["start"] < segment["end"] and s["end"] > segment["start"] for s in rejected):
                            continue
                        annotation = self.store.json_value(
                            "annotations", "segment_id", segment["id"], profile
                        )
                        units.append(
                            {
                                "start": segment["start"],
                                "end": end,
                                "description": annotation["annotation"]["description"],
                                "scene": annotation["annotation"].get("scene", []),
                            }
                        )
                    plan, batch = [], []
                    grouped_units = 0

                    def grouped(current, total_units, checkpoint_root, sid=source["id"]):
                        nonlocal grouped_units
                        if not current:
                            return []
                        if progress:
                            notify({
                                "stage": "scene_grouping", "completed": grouped_units, "total": total_units,
                                "unit": "segments",
                                "message": f"正在组织场景 {current[0]['start']:.2f}–{current[-1]['end']:.2f} 秒",
                            })
                        checkpoint = checkpoint_root / (fingerprint(current) + ".json")
                        if checkpoint.is_file():
                            groups = json.loads(checkpoint.read_text())
                        else:
                            try:
                                groups = self.analyzer.group_scenes(current)
                            except AnalysisContentRejected as exc:
                                log.warning("LLM 跳过敏感场景分组 %s %.3f–%.3f：%s", sid,
                                            current[0]["start"], current[-1]["end"], exc)
                                groups = []
                            atomic_json(checkpoint, groups)
                        grouped_units += len(current)
                        if progress:
                            notify({"stage": "scene_grouping", "completed": grouped_units,
                                      "total": total_units, "unit": "segments"})
                        return [[current[group[0]]["start"], current[group[-1]]["end"]] for group in groups]

                    checkpoint_root = self.store.root / "analysis" / source["id"] / profile
                    for unit in units:
                        if batch and (unit["end"] - batch[0]["start"] > 45 or len(batch) >= 32
                                      or unit["start"] > batch[-1]["end"] + 1e-6):
                            plan.extend(grouped(batch, len(units), checkpoint_root))
                            batch = []
                        batch.append(unit)
                    plan.extend(grouped(batch, len(units), checkpoint_root))
                    self.store.put_json("scene_plans", "source_id", source["id"], profile, plan)
                if not plan and progress:
                    notify({"stage": "scenes", "completed": 0, "total": 0, "unit": "scenes"})
                for scene_number, (start, end) in enumerate(plan, 1):
                    if progress:
                        notify({"stage": "scenes", "completed": scene_number - 1, "total": len(plan),
                                  "unit": "scenes",
                                  "message": "正在生成场景分析"})
                    scene = self._segment(source, "scene", start, end, profile, None, "semantic_grouping")
                    saved = self._annotate(scene)
                    counts["skipped"] += saved.get("status") == "skipped"
                    if progress:
                        notify({"stage": "scenes", "completed": scene_number, "total": len(plan), "unit": "scenes"})

    def snapshots(self, source_id: str | None = None) -> list[dict]:
        result = []
        for source in self.selected_sources(source_id):
            profile = self.annotation_profile(source["id"])
            if self.store.json_value("scene_plans", "source_id", source["id"], profile) is None:
                raise ValueError(f"{source['name']} 尚未完成当前配置的分析阶段")
            transcript = self.store.json_value("transcripts", "source_id", source["id"], self.config.asr_key)
            scenes = [s for s in self.store.segments(source["id"], "scene") if s["generation"] == profile]
            for segment in self.store.segments(source["id"]):
                if segment["kind"] == "scene" and segment["generation"] != profile:
                    continue
                value = self.store.json_value("annotations", "segment_id", segment["id"], profile)
                if value is None:
                    raise ValueError(f"片段 {segment['id']} 尚未分析")
                if value.get("status") == "skipped":
                    continue
                annotation = Annotation.model_validate(value["annotation"])
                dialogue = "".join(u["text"] for u in value["dialogue"])
                scene_ids = [s["id"] for s in scenes if s["start"] <= segment["start"] < s["end"]]
                result.append(
                    {
                        **segment,
                        "source_name": source["name"],
                        "source_path": source["path"],
                        "source_duration": json.loads(source["media"])["duration"],
                        "annotation": annotation.model_dump(),
                        "dialogue": value["dialogue"],
                        "transcript_status": transcript["status"] if transcript else "not_processed",
                        "scene_ids": scene_ids,
                        "views": annotation.views(dialogue),
                    }
                )
        return result
