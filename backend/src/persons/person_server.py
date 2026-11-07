"""Isolated InsightFace PersonAnalysis service. Holds the existing GPU lease for its lifetime."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import io
import json
import os
import secrets
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from filelock import FileLock
from PIL import Image
from starlette.concurrency import run_in_threadpool

from shared.config import load_config


def execution_providers() -> list[str]:
    backend = os.environ.get("MINDEX_PERSON_BACKEND")
    if not backend:
        return [
            p.strip() for p in os.environ.get("MINDEX_PERSON_PROVIDERS", "CPUExecutionProvider").split(",")
        ]
    if backend == "metal":
        raise RuntimeError(
            "当前固定版本的 InsightFace PersonAnalysis 仅支持 CPU/CUDA，暂不支持 Metal/CoreML；"
            "请使用 --backend cpu 或 --backend cuda"
        )
    choices = {"cuda": "CUDAExecutionProvider", "cpu": "CPUExecutionProvider"}
    if backend not in choices:
        raise ValueError("MINDEX_PERSON_BACKEND 必须是 metal、cuda 或 cpu")
    import onnxruntime as ort

    provider = choices[backend]
    if provider not in ort.get_available_providers():
        raise RuntimeError(f"所选后端 {backend} 不可用：当前 ONNX Runtime 未提供 {provider}")
    return [provider] if backend == "cpu" else [provider, "CPUExecutionProvider"]


class PersonEngine:
    def __init__(self, config):
        providers = execution_providers()
        from insightface.app import PersonAnalysis

        root = (
            Path(os.environ.get("MINDEX_PERSON_MODEL_ROOT", config.person.model_root)).expanduser().resolve()
        )
        package = root / "models" / config.person.model
        manifests = list(package.glob("*manifest*.json"))
        if len(manifests) != 1:
            raise RuntimeError("需要完整的 cheetah_l V2 模型包及唯一 manifest JSON；服务不会自动下载模型")
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        tasks = manifest.get("tasks", {})
        required = {"person_detection", "person_reid", "detection", "recognition"}
        if manifest.get("manifest_version") != 2 or not required <= tasks.keys():
            raise RuntimeError("cheetah_l 模型包缺少 V2 人脸/人体任务")
        hashes = {}
        for name, task in tasks.items():
            file = (package / task["file"]).resolve()
            if not file.is_relative_to(package) or not file.is_file() or not task.get("sha256"):
                raise RuntimeError("模型文件缺失或未声明 sha256")
            with file.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            if digest != task["sha256"]:
                raise RuntimeError("模型文件摘要不符：" + name)
            hashes[name] = digest
        self.space = {
            "model": config.person.model,
            "weights": hashes,
            "manifest_sha256": hashlib.sha256(manifests[0].read_bytes()).hexdigest(),
            "tasks": tasks,
            "implementation": "mindex-person-v1",
            "insightface": importlib.metadata.version("insightface"),
            "onnxruntime": importlib.metadata.version("onnxruntime-gpu")
            if "CUDAExecutionProvider" in providers
            else importlib.metadata.version("onnxruntime"),
            "providers": providers,
            "face_det_size": [640, 640],
            "normalized": True,
        }
        if config.person.expected_space and self.space != config.person.expected_space:
            raise RuntimeError("模型身份与 expected_space 不符")
        self.analysis = PersonAnalysis(name=config.person.model, root=str(root), providers=providers)
        self.analysis.prepare()
        self.lock = threading.Lock()

    def observations(self, image):
        import cv2
        import numpy as np

        result = []
        for person in self.analysis.get(image):
            face = person.face
            box = None if face is None else face.bbox.tolist()
            quality = 0.0
            if box:
                x1, y1, x2, y2 = [int(x) for x in box]
                crop = image[max(0, y1) : max(0, y2), max(0, x1) : max(0, x2)]
                if crop.size:
                    sharpness = min(1.0, cv2.Laplacian(crop, cv2.CV_64F).var() / 100)
                    size = min(1.0, min(x2 - x1, y2 - y1) / 64)
                    quality = float(face.det_score) * size * sharpness

            def feature(value):
                if value is None:
                    return None
                v = np.asarray(value, dtype=np.float32)
                return v.tolist() if np.isfinite(v).all() else None

            result.append(
                {
                    "body_bbox": None if person.body_bbox is None else person.body_bbox.tolist(),
                    "det_score": float(person.det_score) if person.det_score is not None else 0,
                    "body_feature": feature(person.reid_feature),
                    "face_bbox": box,
                    "face_feature": feature(None if face is None else face.embedding),
                    "face_quality": quality,
                }
            )
        return result

    def image(self, path):
        import numpy as np

        with Image.open(path) as im:
            image = np.asarray(im.convert("RGB"))[:, :, ::-1].copy()
        with self.lock:
            return {"space": self.space, "observations": self.observations(image)}

    def video(self, path, start, end, fps):
        import av

        frames = []
        with self.lock, av.open(str(path)) as container:
            stream = container.streams.video[0]
            # Seek to the preceding keyframe, then retain original PTS. No CFR conversion.
            origin = float(stream.start_time * stream.time_base) if stream.start_time else 0
            container.seek(int((start + origin) / stream.time_base), stream=stream, backward=True)
            next_time = start
            for frame in container.decode(stream):
                if frame.pts is None:
                    raise ValueError("原片帧缺少时间戳，拒绝猜测时间")
                timestamp = float(frame.pts * stream.time_base) - origin
                if timestamp >= end:
                    break
                if timestamp + 1e-8 < next_time or timestamp < start:
                    continue
                image = frame.to_ndarray(format="bgr24")
                buffer = io.BytesIO()
                Image.fromarray(image[:, :, ::-1]).save(buffer, format="JPEG", quality=85)
                frames.append(
                    {
                        "timestamp": timestamp,
                        "observations": self.observations(image),
                        "image_base64": base64.b64encode(buffer.getvalue()).decode(),
                    }
                )
                next_time = timestamp + 1 / fps
        if not frames:
            raise ValueError("此区间没有可解码的实际帧")
        return {"space": self.space, "frames": frames}


def create_person_app(config, engine_factory=None):
    engine = None

    def authorize(authorization: Annotated[str | None, Header()] = None):
        key = os.environ.get("MINDEX_MODEL_API_KEY", "")
        if key and not secrets.compare_digest(authorization or "", "Bearer " + key):
            raise HTTPException(401, "Invalid model API key")

    @asynccontextmanager
    async def lifespan(app):
        nonlocal engine
        lock_path = Path(os.environ.get("MINDEX_GPU_LOCK", str(Path.home() / ".cache/mindex/gpu.lock")))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=0):
            engine = await run_in_threadpool(engine_factory or (lambda: PersonEngine(config)))
            yield
            engine = None

    app = FastAPI(title="Mindex person service", lifespan=lifespan, dependencies=[Depends(authorize)])

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"ready": engine is not None, "role": "person", "space": engine.space if engine else None}

    @app.post("/extract/image")
    async def image(file: Annotated[UploadFile, File()]):
        from shared.uploads import save_upload

        with tempfile.TemporaryDirectory(prefix="person-image-") as directory:
            path = Path(directory) / "image"
            await save_upload(file, path, 20 * 1024 * 1024)
            return await run_in_threadpool(engine.image, path)

    @app.post("/extract/frames")
    async def batch_frames(files: Annotated[list[UploadFile], File()]):
        from shared.uploads import save_upload

        if not 1 <= len(files) <= 16:
            raise ValueError("帧批次大小必须为 1–16")
        observations = []
        with tempfile.TemporaryDirectory(prefix="person-frames-") as directory:
            for i, file in enumerate(files):
                path = Path(directory) / str(i)
                await save_upload(file, path, 20 * 1024 * 1024)
                response = await run_in_threadpool(engine.image, path)
                observations.append(response["observations"])
        return {"space": engine.space, "observations": observations}

    @app.post("/extract/video")
    async def video(
        file: Annotated[UploadFile, File()],
        start: Annotated[float, Form(ge=0)],
        end: Annotated[float, Form(gt=0)],
        fps: Annotated[float, Form(gt=0, le=5)] = 5,
    ):
        from shared.uploads import save_upload

        if end <= start:
            raise ValueError("结束时间必须大于开始时间")
        with tempfile.TemporaryDirectory(prefix="person-video-") as directory:
            path = Path(directory) / "video"
            await save_upload(
                file, path, int(os.environ.get("MINDEX_PERSON_MAX_VIDEO_MB", "20000")) * 1024 * 1024
            )
            return await run_in_threadpool(engine.video, path, start, end, fps)

    return app


def main():
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=4050, type=int)
    args = parser.parse_args()
    uvicorn.run(create_person_app(load_config(args.config)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
