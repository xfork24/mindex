"""Shot checkpoints and conservative identity attribution; no WeMM or video LLM calls."""

from __future__ import annotations

import base64
import json
import math
import threading
import time
from collections import defaultdict

import httpx
import numpy as np
from fastapi import HTTPException
from filelock import FileLock, Timeout
from pydantic import Field, model_validator

from .adapters import ModelError
from .config import Config, StrictModel, fingerprint
from .persons import Persons, uid
from .storage import Store, dumps


class PersonOptions(StrictModel):
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    publish: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("结束时间必须大于开始时间")
        return self


class PersonClient:
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
            return self.request("POST", "/extract/image", files={"file": (path.name, handle, "image/png")})

    def video(self, path, start, end, fps):
        from .person_frames import sample_frames

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
                raise ModelError("镜头处理期间模型空间发生变化")
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

        for timestamp, raw in sample_frames(path, start, end, fps):
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
        if reference.get("face_quality", 0) >= config.min_reference_quality:
            scores[reference["actor_id"]].append(score)
    ranked = sorted(((max(v), actor) for actor, v in scores.items()), reverse=True)
    if not ranked:
        return None, 0, False
    score, actor = ranked[0]
    margin = score - (ranked[1][0] if len(ranked) > 1 else -1)
    accepted = (
        quality >= config.min_face_quality and score >= config.face_threshold and margin >= config.face_margin
    )
    return actor, score, accepted


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
            actor, score, accepted = face_match(observation, references, config)
            kind, status, role_id = "face", "candidate", None
            identifier = fingerprint([shot["cache_id"], t, index])
            reviewed = manual_anchors.get(identifier)
            if reviewed:
                actor, role_id = reviewed["actor_id"], reviewed["role_id"]
                kind, status = "manual", reviewed["status"]
                anchors.pop(track, None)
            elif accepted:
                status = "accepted"
                if not ambiguous and observation.get("body_bbox") and observation.get("body_feature"):
                    anchors[track] = (actor, None)
            elif observation.get("face_feature") is not None:
                # Conflicting/uncertain visible face always blocks identity inheritance.
                anchors.pop(track, None)
            elif track in anchors and reliable and not ambiguous:
                actor, role_id = anchors[track]
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
            if status == "accepted" and kind == "face":
                # Face identity alone cannot distinguish multiple roles.
                possible = [r for r in roles if work_id in r.get("work_ids", []) and actor in r["actor_ids"]]
                role_id = possible[0]["id"] if len(possible) == 1 else None
            if (
                status == "accepted"
                and kind in {"face", "manual"}
                and not ambiguous
                and observation.get("body_bbox")
            ):
                anchors[track] = (actor, role_id)
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
        space = self.client.identity()
        roles = self.catalogue.entities("role")
        role_actors = {r["id"]: r["actor_ids"] for r in roles}
        references = []
        reference_errors = []
        for ref in self.catalogue.references():
            if ref["role_id"] and ref["actor_id"] not in role_actors.get(ref["role_id"], []):
                reference_errors.append(
                    {"reference_id": ref["id"], "error": "参考图演员已不属于该角色，请重新配对"}
                )
                continue
            key = fingerprint(["reference-v1", ref["digest"], space])
            extracted = self.feature(key)
            if extracted is None:
                extracted = self.client.image(self.store.resolve_media(ref["path"]))
                self.validate_space(extracted, space)
                self.cache(key, None, space, "reference", extracted)
            detections = extracted["observations"]
            subject = reference_subject(detections)
            if subject is None:
                reference_errors.append(
                    {"reference_id": ref["id"], "error": "参考照片需要恰好一张清晰人脸，且属于画面主体"}
                )
                continue
            quality = subject.get("face_quality", 0)
            if quality < float(self.config.person.min_reference_quality):
                # 质量门槛可配（默认 0）；被挡下的参考图要显式报出来，不能悄悄不参与。
                reference_errors.append(
                    {
                        "reference_id": ref["id"],
                        "error": (
                            f"参考照片人脸清晰度 {quality:.2f} 低于门槛 "
                            f"{float(self.config.person.min_reference_quality):.2f}，未参与匹配"
                        ),
                    }
                )
                continue
            references.append({**subject, "actor_id": ref["actor_id"], "role_id": ref["role_id"]})
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
                and e["role_id"]
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
                    "shot-features-v1",
                    source_id,
                    source["layout_key"],
                    shot["id"],
                    shot["start"],
                    shot["end"],
                    space,
                    float(self.config.person.fps),
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
                            "shot_id": shot["id"],
                        }
                    )
                extracted = self.client.video(
                    self.store.resolve_media(source["path"]),
                    shot["start"],
                    shot["end"],
                    float(self.config.person.fps),
                )
                self.validate_space(extracted, space)
                frames = extracted["frames"]
                self.validate_frames(frames, shot)
                for frame in frames:
                    if frame.get("image_base64"):
                        item = self.catalogue.image(
                            base64.b64decode(frame.pop("image_base64"), validate=True), "frames/" + source_id
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
                if json.loads(row["data"]).get("reviewed") and json.loads(row["data"]).get("cache_id") == key:
                    manual_anchors[row["id"]] = {k: row[k] for k in ("actor_id", "role_id", "status")}
            match_key = fingerprint(
                [
                    key,
                    references,
                    roles,
                    body_samples,
                    manual_anchors,
                    self.config.person.model_dump(),
                    "rules-v3-no-person-stages",
                ]
            )
            evidence = self.feature(match_key)
            if evidence is None:
                evidence = attribute_frames(
                    extracted["frames"],
                    references,
                    self.config.person,
                    {**shot, "cache_id": key, "space": space},
                    work_id,
                    roles,
                    self.tracker_factory,
                    body_samples,
                    manual_anchors,
                )
                self.cache(match_key, source_id, space, "matching", evidence)
            with self.store.connect() as db:
                # A whole-shot human review remains authoritative across reference/model-space changes.
                shot_reviewed = any(
                    json.loads(r["data"]).get("confirmation") == "shot_review"
                    for r in db.execute(
                        "SELECT data FROM person_evidence WHERE source_id=? AND shot_id=?",
                        (source_id, shot["id"]),
                    ).fetchall()
                )
                if shot_reviewed:
                    evidence = [
                        {
                            **e,
                            "status": "rejected",
                            "data": {**e["data"], "reviewed": True, "confirmation": "shot_review"},
                        }
                        for e in evidence
                    ]
                # Old partial/range or space runs cannot leave stale automatic identities in this interval.
                for old in db.execute(
                    "SELECT id,data FROM person_evidence WHERE source_id=? AND shot_id=? AND start>=? AND start<?",
                    (source_id, shot["id"], shot["start"], shot["end"]),
                ).fetchall():
                    if not json.loads(old["data"]).get("reviewed"):
                        db.execute("DELETE FROM person_evidence WHERE id=?", (old["id"],))
                for e in evidence:
                    old = db.execute("SELECT data FROM person_evidence WHERE id=?", (e["id"],)).fetchone()
                    if old and json.loads(old["data"]).get("reviewed"):
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
                                "match_key": match_key,
                            }
                        ),
                    ),
                )
            report["accepted"] += sum(e["status"] == "accepted" for e in evidence)
            report["candidates"] += sum(e["status"] == "candidate" for e in evidence)
            # Count persisted evidence, including authoritative human reviews and earlier partial runs.
            statuses = {
                r["status"]
                for r in self.store.rows(
                    "SELECT DISTINCT status FROM person_evidence WHERE source_id=? AND shot_id=?",
                    (source_id, shot["id"]),
                )
            }
            if "accepted" in statuses:
                accepted_shots.add(shot["id"])
            elif "candidate" in statuses:
                candidate_shots.add(shot["id"])
            if progress:
                progress({"stage": "matching", "completed_shots": position + 1, "total_shots": len(shots)})
        if self.client.identity() != space:
            raise ModelError("处理期间人物特征空间变化，拒绝发布；缓存仍按空间隔离")
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["accepted_shots"] = len(accepted_shots)
        report["candidate_shots"] = len(candidate_shots - accepted_shots)
        report["release"] = self.catalogue.publish() if options.publish else None
        report["verification"] = "engineering_only_pending_human_review"
        return report

    def validate_space(self, extracted, space):
        if extracted.get("space") != space:
            raise ModelError("特征提取响应空间与任务固定空间不符")

    def validate_frames(self, frames, shot):
        if not frames:
            raise ModelError("镜头未返回任何实际帧，不能记为已覆盖")
        previous = -math.inf
        for f in frames:
            t = f["timestamp"]
            if not math.isfinite(t) or not (shot["start"] <= t < shot["end"]) or t <= previous:
                raise ModelError("人物服务返回无效或不递增的原片时间戳")
            if previous != -math.inf and t - previous < 1 / self.config.person.fps - 1e-6:
                raise ModelError("人物服务返回的采样率超过任务上限")
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
        try:
            with self.lock():
                store.execute(
                    "UPDATE person_tasks SET status='interrupted',error='进程中断；重试将继续镜头检查点' WHERE status IN ('queued','running')"
                )
        except Timeout:
            pass

    def lock(self):
        return FileLock(str(self.store.root / "pipeline.lock"), timeout=0, thread_local=False)

    def get(self, identifier):
        row = self.store.one("SELECT * FROM person_tasks WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "找不到人物任务")
        return {
            k: json.loads(v) if k in {"options", "progress", "result"} and v else v
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
        if row["status"] not in {"failed", "interrupted"}:
            raise HTTPException(409, "仅失败或中断任务可以重试")
        return self.launch(
            row["source_id"],
            PersonOptions.model_validate_json(row["options"]),
            identifier,
            Config.model_validate_json(row["config"]),
        )

    def delete(self, identifier):
        row = self.store.one("SELECT status FROM person_tasks WHERE id=?", (identifier,))
        if not row:
            raise HTTPException(404, "找不到人物任务")
        if row["status"] not in {"failed", "interrupted"}:
            raise HTTPException(409, "仅失败或中断的任务可以删除")
        self.store.execute("DELETE FROM person_tasks WHERE id=?", (identifier,))
        return {"removed": identifier}

    def launch(self, source_id, options, identifier=None, config=None):
        lock = self.lock()
        try:
            lock.acquire()
        except Timeout as exc:
            raise HTTPException(409, "管线正在运行，请稍后启动人物任务") from exc
        try:
            config = config or self.runtime.current().model_copy(deep=True)
            identifier = identifier or uid()
            if self.store.one("SELECT id FROM person_tasks WHERE id=?", (identifier,)):
                # Credentials are kept outside serialized snapshots; use current profile/environment key.
                config.person.api_key = self.runtime.current().person.api_key
                self.store.execute(
                    "UPDATE person_tasks SET status='queued',error=NULL,result=NULL WHERE id=?", (identifier,)
                )
            else:
                self.store.execute(
                    "INSERT INTO person_tasks(id,source_id,status,config,options) VALUES(?,?,?,?,?)",
                    (identifier, source_id, "queued", config.model_dump_json(), options.model_dump_json()),
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

    def run(self, identifier, config, source_id, options, lock):
        try:
            self.store.execute(
                "UPDATE person_tasks SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (identifier,),
            )

            def progress(value):
                self.store.execute(
                    "UPDATE person_tasks SET progress=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (dumps(value), identifier),
                )

            result = PersonProcessor(config).run(source_id, options, progress)
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
