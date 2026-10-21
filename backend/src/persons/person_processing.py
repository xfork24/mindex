"""Shot checkpoints and conservative identity attribution; no WeMM or video LLM calls."""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from collections import defaultdict
from itertools import pairwise

import httpx
import numpy as np
from fastapi import HTTPException
from filelock import Timeout
from pydantic import Field, model_validator

from mindex.models.adapters import ModelError
from mindex.persons.persons import Persons, uid
from mindex.shared.config import StrictModel, fingerprint
from mindex.shared.processing_lock import ProcessingLock, recover_tasks
from mindex.shared.storage import Store, dumps
from mindex.shared.task_progress import reset_task_attempt, save_task_progress, task_logs


class PersonOptions(StrictModel):
    work_id: str | None = None
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=0, ge=0, description="终点原片秒；0 或 null 表示至片尾")
    publish: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if self.end not in (None, 0) and self.end <= self.start:
            raise ValueError("结束时间必须大于开始时间")
        return self


class PersonClient:
    reference_preprocessing = "padding-fallback-v1"

    def __init__(self, config):
        self.config = config.person

    def request(self, method, path, **kwargs):
        try:
            response = httpx.request(
                method,
                self.config.base_url.rstrip("/") + path,
                headers=self.config.headers(),
                timeout=self.config.timeout,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError("人物服务不可用：" + str(exc)) from exc

    def identity(self):
        health = self.request("GET", "/health")
        space = health.get("space")
        if not health.get("ready") or not space or space.get("model") != self.config.model:
            raise ModelError("人物模型未就绪或模型包不匹配")
        if self.config.expected_space is not None and space != self.config.expected_space:
            raise ModelError("人物模型摘要或预处理身份与固定配置不符")
        return space

    def image(self, path):
        with path.open("rb") as handle:
            result = self.request("POST", "/extract/image", files={"file": (path.name, handle, "image/png")})
        # Tight head crops can lose face alignment/embeddings even when the face
        # is clear. Retry with neutral padding, without changing the stored image.
        observations = result["observations"]
        if not any(d.get("face_feature") and d.get("face_bbox") for d in observations):
            import io

            from PIL import Image, ImageOps

            with Image.open(path) as image:
                border = max(1, round(max(image.size) * 0.1))
                padded = ImageOps.expand(image.convert("RGB"), border=border, fill=(128, 128, 128))
                buffer = io.BytesIO()
                padded.save(buffer, format="PNG")
            retry = self.request("POST", "/extract/image", files={"file": ("padded.png", buffer.getvalue(), "image/png")})
            if retry.get("space") != result.get("space"):
                raise ModelError("特征提取响应空间与任务固定空间不符")
            # Keep all detections so group photos remain subject to the same gate.
            for detection in retry["observations"]:
                for key in ("body_bbox", "face_bbox"):
                    if detection.get(key):
                        detection[key] = [v - border for v in detection[key]]
            result = retry
        return {**result, "reference_preprocessing": self.reference_preprocessing}

    def video(self, path, start, end, frame_interval):
        from mindex.persons.person_frames import sample_frames

        frames, batch = [], []
        space = None

        def flush():
            nonlocal space
            response = self.request(
                "POST",
                "/extract/frames",
                files=[("files", (f"{i}.jpg", raw, "image/jpeg")) for i, (_, raw) in enumerate(batch)],
            )
            if len(response.get("observations", [])) != len(batch):
                raise ModelError("人物服务返回的帧数不一致")
            if space is not None and space != response["space"]:
                raise ModelError("分镜处理期间模型空间发生变化")
            space = response["space"]
            for (timestamp, raw), observations in zip(batch, response["observations"]):
                frames.append(
                    {
                        "timestamp": timestamp,
                        "observations": observations,
                        "image_base64": base64.b64encode(raw).decode(),
                    }
                )
            batch.clear()

        for timestamp, raw in sample_frames(path, start, end, frame_interval):
            batch.append((timestamp, raw))
            if len(batch) == 16:
                flush()
        if batch:
            flush()
        return {"space": space, "frames": frames}


def _inside_ratio(face_box, body_box):
    """人脸框落在人体框内的面积比例。"""
    ix1, iy1 = max(face_box[0], body_box[0]), max(face_box[1], body_box[1])
    ix2, iy2 = min(face_box[2], body_box[2]), min(face_box[3], body_box[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    area = max(1.0, (face_box[2] - face_box[0]) * (face_box[3] - face_box[1]))
    return (ix2 - ix1) * (iy2 - iy1) / area


def reference_subject(detections):
    """挑出参考图里的人脸主体，不合格返回 None。

    模型按人体输出观测，画面边缘的路人、别人的肩膀都会单独成项；直接要求
    “整张图只有一条观测”会把单主角照误杀。这里只数清晰人脸：全图必须恰好一张，
    且它要落在最大人体的框内——这样主体背对镜头时不会把背景人脸当成主体。
    群照（多张清晰人脸）依旧拒绝。
    """
    faces = [d for d in detections if d.get("face_feature") and d.get("face_bbox")]
    if len(faces) != 1:
        return None
    bodies = [d["body_bbox"] for d in detections if d.get("body_bbox")]
    if not bodies:
        return faces[0]
    main = max(bodies, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    return faces[0] if _inside_ratio(faces[0]["face_bbox"], main) >= 0.6 else None


def reference_check(detections, min_quality):
    """参考图预检共用判定：返回 (人脸主体, 不合格原因)。

    处理任务预检与添加时的同步质检共用同一判定与文案；主体为 None 表示
    没有恰好一张属于画面主体的人脸，质量低于门槛时主体仍返回供调用方取用。
    """
    subject = reference_subject(detections)
    if subject is None:
        return None, "参考照片需要恰好一张清晰人脸，且属于画面主体"
    quality = subject.get("face_quality", 0)
    if quality < float(min_quality):
        # 质量门槛可配（默认 0）；被挡下的参考图要显式报出来，不能悄悄不参与。
        return subject, (
            f"参考照片人脸清晰度 {quality:.2f} 低于门槛 {float(min_quality):.2f}，未参与匹配"
        )
    return subject, None


def stale_reference_features(client, extracted):
    preprocessing = getattr(client, "reference_preprocessing", None)
    return bool(extracted is not None and preprocessing
                and extracted.get("reference_preprocessing") != preprocessing
                and reference_subject(extracted["observations"]) is None)


def ensure_reference_quality(store, config, client, item, space=None):
    """添加参考图前的同步质检：不合格抛 ValueError，服务不可用抛 ModelError。

    item 为 Persons.image() 的 {path, digest, ...}。检测结果（含被拒图片）按
    reference-v1 键缓存到 person_features，处理任务预检直接复用不重复调服务。
    返回服务空间，供同一请求处理多张图时省去重复 /health。
    """
    space = space or client.identity()
    key = fingerprint(["reference-v1", item["digest"], space])
    row = store.one("SELECT data FROM person_features WHERE id=?", (key,))
    extracted = json.loads(row["data"]) if row else None
    if extracted is None or stale_reference_features(client, extracted):
        extracted = client.image(store.resolve_media(item["path"]))
        if extracted.get("space") != space:
            raise ModelError("特征提取响应空间与任务固定空间不符")
        store.execute(
            "INSERT OR REPLACE INTO person_features VALUES(?,?,?,?,?)",
            (key, None, dumps(space), "reference", dumps(extracted)),
        )
    _, reason = reference_check(extracted["observations"], config.person.min_reference_quality)
    if reason is not None:
        raise ValueError(reason)
    return space


def cosine(a, b):
    if a is None or b is None:
        return -1.0
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if a.ndim != 1 or a.shape != b.shape or not np.isfinite(a).all() or not np.isfinite(b).all():
        return -1.0
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / norm) if norm else -1.0


def face_match(observation, references, config):
    if observation.get("face_feature") is None:
        return None, 0.0, False
    scores = defaultdict(list)
    quality = observation.get("face_quality", 0)
    for reference in references:
        score = cosine(observation.get("face_feature"), reference.get("face_feature"))
        if reference.get("role_id") and reference.get("face_quality", 0) >= config.min_reference_quality:
            scores[reference["role_id"]].append(score)
    ranked = sorted(((max(v), role) for role, v in scores.items()), reverse=True)
    if not ranked:
        return None, 0, False
    score, role = ranked[0]
    margin = score - (ranked[1][0] if len(ranked) > 1 else -1)
    accepted = (
        quality >= config.min_face_quality and score >= config.face_threshold and margin >= config.face_margin
    )
    return role, score, accepted


def matched_reference(observation, references, config, actor_id, role_id=None):
    """Return the actual winning reference, without borrowing a different role's photo."""
    if observation.get("face_feature") is None or actor_id is None:
        return None
    scored = [
        (cosine(observation["face_feature"], r.get("face_feature")), r)
        for r in references
        if r.get("actor_id") == actor_id and r.get("face_quality", 0) >= config.min_reference_quality
    ]
    if not scored:
        return None
    score = max(s for s, _ in scored)
    winners = [r for s, r in scored if s == score and r.get("role_id") == role_id]
    return min(winners, key=lambda r: (r.get("role_id") != role_id, r.get("id", "")), default=None)


class EvidenceReferences:
    """Resolve matched photos from provenance or cached features; never call a model."""

    def __init__(self, config, store):
        self.config, self.store = config, store
        self.catalogue = Persons(store)
        self.references = {r["id"]: r for r in self.catalogue.references()}
        self.features, self.spaces, self.shots = {}, {}, {}

    def feature(self, key):
        if key not in self.features:
            row = self.store.one("SELECT data FROM person_features WHERE id=?", (key,))
            self.features[key] = json.loads(row["data"]) if row else None
        return self.features[key]

    def candidates(self, space):
        key = dumps(space)
        if key not in self.spaces:
            references = []
            for r in self.references.values():
                cached = self.feature(fingerprint(["reference-v1", r["digest"], space]))
                subject = reference_subject(cached["observations"]) if cached else None
                if subject:
                    references.append(
                        {**subject, "id": r["id"], "actor_id": r["actor_id"], "role_id": r["role_id"]}
                    )
            self.spaces[key] = references
        return self.spaces[key]

    def reference_id(self, evidence):
        data = evidence.get("data") or {}
        recorded = data.get("reference_id")
        if recorded:
            return recorded
        if (
            data.get("confirmation") == "shot_review"
            and data.get("evidence_type") == "manual"
            and not data.get("cache_id")
        ):
            key = (evidence["source_id"], evidence["shot_id"])
            if key not in self.shots:
                self.shots[key] = [
                    {**r, "data": json.loads(r["data"])}
                    for r in self.store.rows(
                        "SELECT * FROM person_evidence WHERE source_id=? AND shot_id=?", key
                    )
                ]
            rows = [
                r
                for r in self.shots[key]
                if r["actor_id"] == evidence["actor_id"]
                and r["role_id"] in (None, evidence["role_id"])
                and (r["data"].get("confirmation") != "shot_review" or r["data"].get("cache_id"))
            ]
            for row in sorted(rows, key=lambda r: -(Persons.evidence_score(r["data"].get("score")) or 0)):
                resolved = self.reference_id({**row, "role_id": evidence["role_id"]})
                if resolved:
                    return resolved
            return None
        cached = self.feature(data.get("cache_id")) if data.get("cache_id") else None
        if not cached or data.get("evidence_type") not in {"face", "manual"}:
            return None
        for frame in cached.get("frames", []):
            for index, observation in enumerate(frame["observations"]):
                if evidence["id"] not in {fingerprint([data["cache_id"], frame["timestamp"], index]),
                                          fingerprint(["role-evidence-v1", data["cache_id"], frame["timestamp"], index])}:
                    continue
                ref = matched_reference(
                    observation,
                    self.candidates(data.get("space") or cached.get("space", {})),
                    self.config.person,
                    evidence["actor_id"],
                    evidence["role_id"],
                )
                score = Persons.evidence_score(data.get("original_score", data.get("score")))
                if ref and (score is None or not math.isclose(
                    cosine(observation.get("face_feature"), ref.get("face_feature")), score, abs_tol=1e-6
                )):
                    return None
                return ref["id"] if ref else None
        return None

    def url(self, evidence):
        identifier = self.reference_id(evidence)
        ref = self.references.get(identifier)
        if (
            not ref
            or ref["actor_id"] != evidence["actor_id"]
            or ref["role_id"] not in (None, evidence["role_id"])
        ):
            return None
        return ref["url"]

def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersection
    return intersection / union if union else 0


class ShotTracker:
    """ByteTrack associates bodies; app rules invalidate anchors on ambiguity or gaps."""

    def __init__(self, fps):
        try:
            import supervision as sv
        except ImportError as exc:
            raise ModelError(
                "人物跟踪需要 supervision；请执行 uv sync --extra person（见 docs/persons.md）"
            ) from exc
        self.sv = sv
        self.tracker = sv.ByteTrack(frame_rate=max(1, round(fps)), minimum_consecutive_frames=1)

    def update(self, observations):
        sv = self.sv
        indexes = [i for i, o in enumerate(observations) if o.get("body_bbox") is not None]
        detections = sv.Detections(
            xyxy=np.array([observations[i]["body_bbox"] for i in indexes], dtype=np.float32).reshape(-1, 4),
            confidence=np.array([observations[i].get("det_score", 0) for i in indexes]),
            class_id=np.zeros(len(indexes), dtype=int),
            data={"observation_index": np.array(indexes, dtype=int)},
        )
        tracked = self.tracker.update_with_detections(detections)
        if not len(tracked):
            return {}
        return {int(i): str(t) for i, t in zip(tracked.data["observation_index"], tracked.tracker_id)}


def attribute_frames(
    frames,
    references,
    config,
    shot,
    work_id,
    roles,
    tracker_factory=ShotTracker,
    body_samples=(),
    manual_anchors=None,
):
    tracker = tracker_factory(config.fps)
    manual_anchors = manual_anchors or {}
    anchors, previous, evidence = {}, {}, []
    for fidx, frame in enumerate(frames):
        t = frame["timestamp"]
        observations = frame["observations"]
        tracks = tracker.update(observations)
        present = set(tracks.values())
        for track in list(anchors):
            if track not in present:
                anchors.pop(track, None)
        next_previous = {}
        for index, observation in enumerate(observations):
            track = tracks.get(index, f"face-{fidx}-{index}")
            box = observation.get("body_bbox") or observation.get("face_bbox")
            ambiguous = any(
                i != index
                and o.get("body_bbox")
                and observation.get("body_bbox")
                and iou(o["body_bbox"], box) > 0.15
                for i, o in enumerate(observations)
            )
            old = previous.get(track)
            reliable = old is not None and t - old["t"] <= 1.5 / config.fps and iou(old["box"], box) > 0.2
            if ambiguous or not reliable:
                anchors.pop(track, None)
            role_id, score, accepted = face_match(observation, references, config)
            winning = [r for r in references if r.get("role_id") == role_id
                       and r.get("face_quality", 0) >= config.min_reference_quality] if role_id else []
            winner = max(winning, key=lambda r: cosine(observation.get("face_feature"), r.get("face_feature")), default=None)
            actor = winner["actor_id"] if winner else None
            reference_id = winner.get("id") if winner else None
            kind, status = "face", "candidate"
            legacy_id = fingerprint([shot["cache_id"], t, index])
            identifier = fingerprint(["role-evidence-v1", shot["cache_id"], t, index])
            reviewed = manual_anchors.get(identifier) or manual_anchors.get(legacy_id)
            if legacy_id in manual_anchors:
                identifier = legacy_id
            if reviewed:
                actor, role_id = reviewed["actor_id"], reviewed["role_id"]
                kind, status = "manual", reviewed["status"]
                reference_id = reviewed.get("reference_id")
                anchors.pop(track, None)
            elif accepted:
                status = "accepted"
                if not ambiguous and observation.get("body_bbox") and observation.get("body_feature"):
                    anchors[track] = (actor, None, None)
            elif observation.get("face_feature") is not None:
                # Conflicting/uncertain visible face always blocks identity inheritance.
                anchors.pop(track, None)
            elif track in anchors and reliable and not ambiguous:
                actor, role_id, reference_id = anchors[track]
                score, kind, status = 1.0, "track", "accepted"
            else:
                # Body samples never seed anchors. Cross-shot matches remain candidates.
                scores = sorted(
                    (
                        (cosine(observation.get("body_feature"), sample["feature"]), sample)
                        for sample in body_samples
                        if sample["work_id"] == work_id
                    ),
                    key=lambda v: -v[0],
                )
                if scores and scores[0][0] >= config.body_threshold:
                    score, sample = scores[0]
                    actor, role_id, kind = (
                        sample["actor_id"],
                        sample["role_id"],
                        "body_candidate",
                    )
            if actor is None:
                # Keep unknown observations reviewable too.
                kind = "unknown"
            if kind == "face":
                reference = matched_reference(observation, references, config, actor, role_id)
                reference_id = reference.get("id") if reference else None
            if (
                status == "accepted"
                and kind in {"face", "manual"}
                and not ambiguous
                and observation.get("body_bbox")
            ):
                anchors[track] = (actor, role_id, reference_id)
            next_t = frames[fidx + 1]["timestamp"] if fidx + 1 < len(frames) else shot["end"]
            end = min(shot["end"], next_t, t + 1 / config.fps)
            if end <= t:
                continue
            evidence.append(
                {
                    "id": identifier,
                    "source_id": shot["source_id"],
                    "shot_id": shot["id"],
                    "actor_id": actor,
                    "role_id": role_id,
                    "start": t,
                    "end": end,
                    "status": status,
                    "data": {
                        "track_id": shot["cache_id"] + ":" + track,
                        "bbox": box,
                        "face_bbox": observation.get("face_bbox"),
                        "score": score,
                        "quality": observation.get("face_quality", 0),
                        "evidence_type": kind,
                        "confirmation": "face_threshold_margin"
                        if kind == "face" and accepted
                        else "continuous_unambiguous_track"
                        if kind == "track"
                        else "requires_review",
                        "image_path": frame.get("image_path"),
                        "cache_id": shot["cache_id"],
                        "work_id": work_id,
                        "space": shot["space"],
                        "body_feature": observation.get("body_feature"),
                        "ambiguous": ambiguous,
                        "reference_id": reference_id,
                    },
                }
            )
            next_previous[track] = {"t": t, "box": box}
        previous = next_previous
    return evidence


class PersonProcessor:
    def __init__(self, config, client=None, tracker_factory=ShotTracker):
        self.config, self.store = config, Store(config.data_dir)
        self.catalogue = Persons(self.store)
        self.client = client or PersonClient(config)
        self.tracker_factory = tracker_factory

    def feature(self, key):
        row = self.store.one("SELECT data FROM person_features WHERE id=?", (key,))
        return json.loads(row["data"]) if row else None

    def cache(self, key, source, space, kind, value):
        self.store.execute(
            "INSERT OR REPLACE INTO person_features VALUES(?,?,?,?,?)",
            (key, source, dumps(space), kind, dumps(value)),
        )

    def run(self, source_id, options: PersonOptions, progress=None):
        started = time.perf_counter()
        work_id = self.catalogue.work_for(source_id)
        if options.work_id and options.work_id != work_id:
            raise ValueError("关联作品已改变，请重新配置人物任务")
        source = self.store.one("SELECT * FROM sources WHERE id=?", (source_id,))
        shots = self.store.segments(source_id, "shot")
        if not shots:
            raise ValueError("请先按原片分镜准备素材")
        duration = json.loads(source["media"])["duration"]
        if options.start >= duration or (options.end is not None and options.end > duration + 0.001):
            raise ValueError("处理区间超出原片")
        shots = [
            {**s, "start": max(s["start"], options.start), "end": min(s["end"], options.end or duration)}
            for s in shots
            if s["end"] > options.start and s["start"] < (options.end or duration)
        ]
        if progress:
            progress({"stage": "extracting", "completed_shots": 0, "total_shots": len(shots), "unit": "shots"})
        space = self.client.identity()
        roles = [r for r in self.catalogue.entities("role") if work_id in r["work_ids"]]
        entity_names = {e["id"]: e["name"] for e in self.catalogue.entities()}
        role_actors = {r["id"]: r["actor_ids"] for r in roles}
        references = []
        reference_errors = []
        for ref in self.catalogue.references():
            if ref["role_id"] not in role_actors:
                continue
            # 任务日志冻结当时的配对名称；后续改名、移除参考图不会丢失问题所属身份。
            identity = {
                "reference_id": ref["id"],
                "actor_id": ref["actor_id"],
                "role_id": ref["role_id"],
                "actor_name": entity_names.get(ref["actor_id"], "未登记演员"),
                "role_name": entity_names.get(ref["role_id"]) if ref["role_id"] else None,
            }
            if ref["role_id"] and ref["actor_id"] not in role_actors.get(ref["role_id"], []):
                reference_errors.append(
                    {**identity, "error": "参考图演员已不属于该角色，请重新配对"}
                )
                continue
            key = fingerprint(["reference-v1", ref["digest"], space])
            extracted = self.feature(key)
            if extracted is None or stale_reference_features(self.client, extracted):
                extracted = self.client.image(self.store.resolve_media(ref["path"]))
                self.validate_space(extracted, space)
                self.cache(key, None, space, "reference", extracted)
            detections = extracted["observations"]
            subject, reason = reference_check(detections, self.config.person.min_reference_quality)
            if reason is not None:
                reference_errors.append({**identity, "error": reason})
                continue
            references.append({**subject, "id": ref["id"], "actor_id": ref["actor_id"], "role_id": ref["role_id"]})
        report = {
            "source_id": source_id,
            "space": space,
            "shots": len(shots),
            "extracted_shots": 0,
            "reused_shots": 0,
            "accepted": 0,
            "candidates": 0,
            "reference_errors": reference_errors,
        }
        # Only user-confirmed, role-scoped appearances can suggest identities across shots.
        accepted_shots, candidate_shots = set(), set()
        body_samples = []
        for e in self.catalogue.evidence():
            d = e["data"]
            if (
                d.get("space") == space
                and d.get("work_id") == work_id
                and e["status"] == "accepted"
                and d.get("reviewed")
                and e["actor_id"] in role_actors.get(e["role_id"], [])
                and d.get("body_feature")
            ):
                body_samples.append(
                    {
                        "feature": d["body_feature"],
                        "work_id": d["work_id"],
                        "actor_id": e["actor_id"],
                        "role_id": e["role_id"],
                    }
                )
        for position, shot in enumerate(shots):
            key = fingerprint(
                [
                    "shot-features-frame-interval-v3",
                    source_id,
                    source["layout_key"],
                    shot["id"],
                    shot["start"],
                    shot["end"],
                    space,
                    self.config.person.frame_interval,
                ]
            )
            extracted = self.feature(key)
            if extracted is None:
                if progress:
                    progress(
                        {
                            "stage": "extracting",
                            "completed_shots": position,
                            "total_shots": len(shots),
                            "unit": "shots",
                            "shot_id": shot["id"],
                        }
                    )
                extracted = self.client.video(
                    self.store.resolve_media(shot["path"]),
                    0,
                    shot["end"] - shot["start"],
                    self.config.person.frame_interval,
                )
                self.validate_space(extracted, space)
                frames = extracted["frames"]
                for frame in frames:
                    frame["timestamp"] += shot["start"]
                self.validate_frames(frames, shot)
                for frame in frames:
                    if frame.get("image_base64"):
                        item = self.catalogue.image(
                            base64.b64decode(frame.pop("image_base64"), validate=True), "frames/" + source_id,
                            max_edge=self.config.person.frames_max_edge, image_format="JPEG",
                            jpeg_quality=self.config.person.frames_jpeg_quality,
                        )
                        frame["image_path"] = item["path"]
                # Detection cache is independent of reference labels, alongside feature cache.
                detection_key = fingerprint(["detections-v1", key])
                self.cache(
                    detection_key,
                    source_id,
                    space,
                    "detection",
                    [
                        {
                            "timestamp": f["timestamp"],
                            "observations": [
                                {k: v for k, v in o.items() if not k.endswith("_feature")}
                                for o in f["observations"]
                            ],
                        }
                        for f in frames
                    ],
                )
                self.cache(key, source_id, space, "shot", extracted)
                report["extracted_shots"] += 1
            else:
                report["reused_shots"] += 1
            # Matching cache is separately invalidated by references, rule configuration and review samples.
            manual_anchors = {}
            for row in self.store.rows(
                "SELECT * FROM person_evidence WHERE source_id=? AND shot_id=?", (source_id, shot["id"])
            ):
                data = json.loads(row["data"])
                if (data.get("reviewed") and data.get("cache_id") == key
                    and row["actor_id"] in role_actors.get(row["role_id"], [])):
                    manual_anchors[row["id"]] = {
                        **{k: row[k] for k in ("actor_id", "role_id", "status")},
                        "reference_id": data.get("reference_id"),
                    }
            match_key = fingerprint(
                [
                    key,
                    references,
                    roles,
                    body_samples,
                    manual_anchors,
                    self.config.person.model_dump(exclude={"frames_max_edge", "frames_jpeg_quality"}),
                    "rules-v4-role-only",
                ]
            )
            evidence = self.feature(match_key)
            if evidence is None:
                timestamps = [f["timestamp"] for f in extracted["frames"]]
                intervals = [b - a for a, b in pairwise(timestamps) if b > a]
                matching_config = self.config.person.model_copy(update={
                    "fps": 1 / float(np.median(intervals)) if intervals else self.config.person.fps,
                })
                evidence = attribute_frames(
                    extracted["frames"],
                    references,
                    matching_config,
                    {**shot, "cache_id": key, "space": space},
                    work_id,
                    roles,
                    self.tracker_factory,
                    body_samples,
                    manual_anchors,
                )
                self.cache(match_key, source_id, space, "matching", evidence)
            with self.store.connect() as db:
                # Whole-shot exclusions remain authoritative on reruns.
                excluded = db.execute(
                    "SELECT 1 FROM person_evidence e WHERE source_id=? AND shot_id=? AND ("
                    + self.catalogue.SHOT_EXCLUSION + ") LIMIT 1",
                    (source_id, shot["id"]),
                ).fetchone() is not None
                if excluded:
                    evidence = []
                # Replace stale candidates, retaining all accepted/rejected evidence.
                for old in db.execute(
                    "SELECT id,data,actor_id,role_id,status FROM person_evidence WHERE source_id=? AND shot_id=? AND start>=? AND start<?",
                    (source_id, shot["id"], shot["start"], shot["end"]),
                ).fetchall():
                    if old["actor_id"] and not old["role_id"]:
                        continue  # Keep historical actor-only manual rejections.
                    elif old["status"] == "candidate" and not json.loads(old["data"]).get("reviewed"):
                        db.execute("DELETE FROM person_evidence WHERE id=?", (old["id"],))
                for e in evidence:
                    # Reused matching caches may still contain paths retired by frame migration.
                    # Keep model results/cache identities intact and persist a usable display fallback.
                    image_path = e["data"].get("image_path")
                    if image_path and not (self.store.root / image_path).is_file():
                        e = {**e, "data": {**e["data"], "image_path": shot["thumbnail"]}}
                    old = db.execute("SELECT * FROM person_evidence WHERE id=?", (e["id"],)).fetchone()
                    if old and old["status"] in {"accepted", "rejected"}:
                        old_data = json.loads(old["data"])
                        e = {
                            **e,
                            **{k: old[k] for k in ("actor_id", "role_id", "status", "start", "end")},
                            "data": {
                                **old_data,
                                "image_path": e["data"].get("image_path", old_data.get("image_path")),
                            },
                        }
                    elif old and json.loads(old["data"]).get("reviewed"):
                        continue
                    db.execute(
                        "INSERT OR REPLACE INTO person_evidence(id,source_id,shot_id,actor_id,role_id,start,end,status,data) VALUES(?,?,?,?,?,?,?,?,?)",
                        tuple(
                            e[k]
                            for k in (
                                "id",
                                "source_id",
                                "shot_id",
                                "actor_id",
                                "role_id",
                                "start",
                                "end",
                                "status",
                            )
                        )
                        + (dumps(e["data"]),),
                    )
                db.execute(
                    "INSERT OR REPLACE INTO person_coverage VALUES(?,?,?)",
                    (
                        source_id,
                        key,
                        dumps(
                            {
                                "shot_id": shot["id"],
                                "start": shot["start"],
                                "end": shot["end"],
                                "space": space,
                                "frames": len(extracted["frames"]),
                                "frame_interval": self.config.person.frame_interval,
                                "sampling_rule": "frame-interval-v3",
                                "match_key": match_key,
                            }
                        ),
                    ),
                )
            persisted = self.store.one(
                "SELECT SUM(status='accepted') AS accepted, SUM(status='candidate') AS candidates "
                "FROM person_evidence WHERE source_id=? AND shot_id=?",
                (source_id, shot["id"]),
            )
            report["accepted"] += persisted["accepted"] or 0
            report["candidates"] += persisted["candidates"] or 0
            # Count persisted evidence, including authoritative human reviews and earlier partial runs.
            state = self.store.one(
                "SELECT * FROM " + self.catalogue.SHOT_STATES + " WHERE source_id=? AND shot_id=?",
                (source_id, shot["id"]),
            )
            if state and state["accepted"]:
                accepted_shots.add(shot["id"])
            elif state and (state["candidate"] or not state["rejected"]):
                candidate_shots.add(shot["id"])
            if progress:
                progress({"stage": "matching", "completed_shots": position + 1,
                          "total_shots": len(shots), "unit": "shots"})
        if self.client.identity() != space:
            raise ModelError("处理期间人物特征空间变化，拒绝发布；缓存仍按空间隔离")
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["accepted_shots"] = len(accepted_shots)
        report["candidate_shots"] = len(candidate_shots - accepted_shots)
        report["release"] = None
        if options.publish:
            from mindex.admin.source_tasks import publication_attempt
            report["publication"] = publication_attempt(self.config, source_id, "persons", progress=progress)
            if report["publication"]["status"] == "complete":
                report["release"] = report["publication"]
        report["verification"] = "engineering_only_pending_human_review"
        return report

    def validate_space(self, extracted, space):
        if extracted.get("space") != space:
            raise ModelError("特征提取响应空间与任务固定空间不符")

    def validate_frames(self, frames, shot):
        if not frames:
            raise ModelError("分镜未返回任何实际帧，不能记为已覆盖")
        previous = -math.inf
        for f in frames:
            t = f["timestamp"]
            if not math.isfinite(t) or not (shot["start"] <= t < shot["end"]) or t <= previous:
                raise ModelError("人物服务返回无效或不递增的原片时间戳")
            previous = t
            for o in f["observations"]:
                box = o.get("body_bbox") or o.get("face_bbox")
                if (
                    not box
                    or len(box) != 4
                    or not all(math.isfinite(v) for v in box)
                    or box[2] <= box[0]
                    or box[3] <= box[1]
                ):
                    raise ModelError("人物服务返回无效检测框")


class PersonTasks:
    def __init__(self, runtime, store):
        self.runtime, self.store, self.threads = runtime, store, {}
        recover_tasks(store, "person_tasks")

    def lock(self, source_id):
        return ProcessingLock(self.runtime.current(), source_id=source_id, work="persons")

    def get(self, identifier):
        row = self.store.one("SELECT * FROM person_tasks WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "找不到人物任务")
        return {
            k: json.loads(v) if k in {"options", "progress", "result", "publication"} and v else v
            for k, v in row.items()
            if k != "config"
        }

    def start(self, source_id, options):
        if not self.store.one("SELECT id FROM sources WHERE id=?", (source_id,)):
            raise HTTPException(404, "找不到素材")
        return self.launch(source_id, options)

    def retry(self, identifier):
        row = self.store.one("SELECT * FROM person_tasks WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "找不到人物任务")
        if row["status"] not in {"failed", "interrupted"} and json.loads(row["publication"] or "{}").get("status") != "failed":
            raise HTTPException(409, "仅失败或中断任务可以重试")
        return self.launch(
            row["source_id"],
            PersonOptions.model_validate_json(row["options"]),
            identifier,
            self.restore(row["config"]),
        )

    def restore(self, raw):
        from mindex.admin.source_tasks import restore_config
        return restore_config(raw, self.runtime, self.store)

    def delete(self, identifier):
        row = self.store.one("SELECT status FROM person_tasks WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "找不到人物任务")
        if row["status"] not in {"failed", "interrupted"}:
            raise HTTPException(409, "仅失败或中断的任务可以删除")
        self.store.execute("DELETE FROM person_tasks WHERE id=?", (identifier,))
        return {"removed": identifier}

    def launch(self, source_id, options, identifier=None, config=None):
        lock = self.lock(source_id)
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "GPU 独占锁或该素材人物任务正在运行，请稍后启动") from exc
        try:
            config = config or self.runtime.current().model_copy(deep=True)
            from mindex.admin.source_tasks import freeze_config, person_setup
            source = self.store.one("SELECT prepared FROM sources WHERE id=?", (source_id,))
            if not source or not source["prepared"]:
                raise HTTPException(409, "请先完成分镜处理")
            # 新工作台选择作品时做资料预检；兼容既有 CLI/API 的区间任务。
            prior = self.store.one("SELECT result FROM person_tasks WHERE id=?", (identifier,)) if identifier else None
            publish_only = bool(prior and prior["result"] and options.publish)
            if options.work_id and not publish_only:
                setup = person_setup(self.store, source_id, options.work_id)
                if not setup["ready"]:
                    raise HTTPException(409, "；".join(setup["missing"]))
                if not identifier:
                    Persons(self.store).assign_work(source_id, options.work_id)
            identifier = identifier or uid()
            if self.store.one("SELECT id FROM person_tasks WHERE id=?", (identifier,)):
                # 重试沿用冻结配置，凭据由档案引用或环境恢复。
                reset_task_attempt(self.store, "person_tasks", identifier)
            else:
                self.store.execute(
                    "INSERT INTO person_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
                    (identifier, source_id, "queued", freeze_config(config, self.store), options.model_dump_json()),
                )
            thread = threading.Thread(
                target=self.run, args=(identifier, config, source_id, options, lock), daemon=True
            )
            self.threads[identifier] = thread
            thread.start()
            return self.get(identifier)
        except BaseException:
            lock.release()
            raise

    @task_logs("person_tasks")
    def run(self, identifier, config, source_id, options, lock):
        try:
            self.store.execute(
                "UPDATE person_tasks SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (identifier,),
            )

            def progress(value):
                save_task_progress(self.store, "person_tasks", identifier, value)

            row = self.store.one("SELECT result FROM person_tasks WHERE id=?", (identifier,))
            result = json.loads(row["result"]) if row["result"] and options.publish else PersonProcessor(config).run(
                source_id, options.model_copy(update={"publish": False}), progress)
            self.store.execute("UPDATE person_tasks SET result=? WHERE id=?", (dumps(result), identifier))
            if options.publish:
                from mindex.admin.source_tasks import enqueue_publications

                publication = enqueue_publications(self.runtime, self.store, source_id, 'persons')
                self.store.execute('UPDATE person_tasks SET publication=? WHERE id=?', (dumps(publication), identifier))
            else:
                self.store.execute("UPDATE person_tasks SET publication=? WHERE id=?",
                                   (dumps({"status": "not_requested"}), identifier))
            self.store.execute(
                "UPDATE person_tasks SET status='complete',result=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (dumps(result), identifier),
            )
        except BaseException as exc:  # noqa: BLE001 — persist terminal worker failures before releasing its lease
            self.store.execute(
                "UPDATE person_tasks SET status='failed',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc)[:2000], identifier),
            )
        finally:
            lock.release()
            self.threads.pop(identifier, None)
