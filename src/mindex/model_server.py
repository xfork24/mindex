"""可选模型服务：重依赖位于独立环境，TransNetV2 支持本地 CPU。"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import secrets
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from filelock import FileLock
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .config import Config, fingerprint, load_config
from .media import content_hash, probe
from .uploads import save_upload


class TextBatch(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=32)
    role: Literal["query", "document"] = "document"


def resolve_weights(model: str, revision: str) -> tuple[str, str]:
    from huggingface_hub import snapshot_download

    local = Path(model)
    if local.is_dir():
        files = sorted(local.glob("*.safetensors"))
        if not files:
            raise ValueError("Local model has no safetensors weights")
        return str(local.resolve()), fingerprint(
            [(p.name, content_hash(p)) for p in files]
            + [(p.name, content_hash(p)) for p in sorted(local.glob("*.json"))]
        )
    path = snapshot_download(model, revision=revision)
    return path, Path(path).name


class WeMMEngine:
    def __init__(self, config: Config):
        import torch
        import transformers
        from transformers import AutoModel, AutoProcessor, BitsAndBytesConfig

        if not torch.cuda.is_available():
            raise RuntimeError("WeMM GPU service requires CUDA; run the web/processing client on macOS")
        profile = config.embedding
        path, revision = resolve_weights(profile.model, profile.revision)
        kwargs = {"trust_remote_code": True, "dtype": torch.bfloat16, "device_map": {"": 0}}
        if profile.quantization == "int8":
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_8bit=True, llm_int8_enable_fp32_cpu_offload=False
            )
        elif profile.quantization == "nf4":
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
            )
        elif profile.quantization != "bf16":
            raise ValueError("Supported quantization: int8, nf4, bf16")
        self.model = AutoModel.from_pretrained(path, **kwargs).eval()
        self.processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
        if any(
            str(device) in {"cpu", "disk"} for device in getattr(self.model, "hf_device_map", {}).values()
        ):
            raise RuntimeError("CPU/disk offload is disabled for this demo")
        self.config = config
        self.space = {
            "model": profile.model,
            "revision": revision,
            "quantization": profile.quantization,
            "dimensions": profile.dimensions,
            "normalized": True,
            "fps": config.video.fps,
            "max_frames": config.video.max_frames,
            "max_pixels": config.video.max_pixels,
            "implementation": fingerprint(
                {
                    "adapter": content_hash(Path(__file__)),
                    "transformers": transformers.__version__,
                    "torch": torch.__version__,
                    "bitsandbytes": importlib.metadata.version("bitsandbytes")
                    if profile.quantization != "bf16"
                    else None,
                    "vision_utils": importlib.metadata.version("qwen-vl-utils"),
                    "template": content_hash(Path(path) / "chat_template.jinja")
                    if (Path(path) / "chat_template.jinja").exists()
                    else "processor",
                }
            ),
        }
        self.lock = threading.Lock()

    def metrics(self) -> dict:
        import torch

        with self.lock:
            return {
                "device": torch.cuda.get_device_name(0),
                "allocated_bytes": torch.cuda.memory_allocated(0),
                "reserved_bytes": torch.cuda.memory_reserved(0),
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
                "total_device_bytes": torch.cuda.get_device_properties(0).total_memory,
                "note": "PyTorch process allocator peak since startup; excludes other GPU processes.",
            }

    def _encode(self, item: dict) -> list[float]:
        import torch
        from qwen_vl_utils import process_vision_info

        messages = [{"role": "user", "content": [item]}]
        rendered = self.processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
        images, videos, kwargs = process_vision_info(
            messages, image_patch_size=16, return_video_kwargs=True, return_video_metadata=True
        )
        metadata = None
        if videos is not None:
            videos, metadata = zip(*videos)
            videos, metadata = list(videos), list(metadata)
        inputs = self.processor(
            text=rendered,
            images=images,
            videos=videos,
            video_metadata=metadata,
            return_tensors="pt",
            **kwargs,
        ).to("cuda")
        with torch.inference_mode():
            output = self.model.embedding(**inputs).float()
            if output.shape[-1] != self.config.embedding.dimensions:
                allowed = getattr(self.model.config, "matryoshka_dimensions", [])
                if self.config.embedding.dimensions not in allowed:
                    raise ValueError("Requested dimension is not supported by this model")
                output = output[..., : self.config.embedding.dimensions]
            vector = torch.nn.functional.normalize(output, dim=-1)[0].cpu().tolist()
        return vector

    def text(self, texts: list[str], role: str) -> list[list[float]]:
        if any(not text.strip() or len(text) > 16000 for text in texts):
            raise ValueError("Text inputs must contain 1–16000 characters")
        # WeMM's official direct Transformers example uses the same user-content template
        # for standalone text and video; no invented task instruction is added.
        with self.lock:
            return [self._encode({"type": "text", "text": text}) for text in texts]

    def video(self, path: Path) -> list[list[float]]:
        if probe(path)["duration"] > self.config.video.short_shot_seconds + 0.1:
            raise ValueError(
                "Embedding endpoint accepts a single short window; split longer videos in the client"
            )
        with self.lock:
            return [
                self._encode(
                    {
                        "type": "video",
                        "video": str(path),
                        "fps": self.config.video.fps,
                        "max_frames": self.config.video.max_frames,
                        "max_pixels": self.config.video.max_pixels,
                    }
                )
            ]

    def image(self, path: Path) -> list[list[float]]:
        from .images import image_png

        png = image_png(path, self.config.video.max_pixels)
        with tempfile.TemporaryDirectory(prefix="wemm-image-") as directory:
            normalized = Path(directory) / "image.png"
            normalized.write_bytes(png)
            with self.lock:
                return [
                    self._encode(
                        {
                            "type": "image",
                            "image": str(normalized),
                            "max_pixels": self.config.video.max_pixels,
                        }
                    )
                ]


class ASREngine:
    def __init__(self, config: Config):
        import torch
        from qwen_asr import Qwen3ASRModel

        if not torch.cuda.is_available():
            raise RuntimeError("ASR service requires CUDA")
        path, self.revision = resolve_weights(config.asr.model, config.asr.revision)
        self.model_name = config.asr.model
        self.model = Qwen3ASRModel.from_pretrained(
            path,
            dtype=torch.bfloat16,
            device_map="cuda:0",
            max_inference_batch_size=1,
            max_new_tokens=1024,
            forced_aligner="Qwen/Qwen3-ForcedAligner-0.6B",
            forced_aligner_kwargs={"dtype": torch.bfloat16, "device_map": "cuda:0"},
        )
        self.lock = threading.Lock()

    def transcribe(self, path: Path) -> dict:
        with self.lock:
            result = self.model.transcribe(audio=str(path), language="Chinese", return_time_stamps=True)[0]
        utterances = []
        if result.text.strip():
            timestamps = result.time_stamps
            if timestamps is None:
                raise RuntimeError("ASR returned text without aligned timestamps")
            for token in timestamps:
                if token.end_time > token.start_time and token.text.strip():
                    utterances.append(
                        {"start": float(token.start_time), "end": float(token.end_time), "text": token.text}
                    )
        return {"model": self.model_name, "revision": self.revision, "utterances": utterances}


class TransNetEngine:
    def __init__(self):
        # Use upstream inference and weights, not an unverified third-party conversion.
        directory = Path(os.environ.get("MINDEX_TRANSNET_DIR", "models/TransNetV2/inference")).resolve()
        if not (directory / "transnetv2.py").is_file():
            raise ValueError("缺少 TransNetV2 上游代码，请先执行 ./scripts/setup-transnet-service.sh")
        import tensorflow as tf

        device = os.environ.get("MINDEX_TRANSNET_DEVICE", "auto")
        if device not in {"auto", "cpu"}:
            raise ValueError("MINDEX_TRANSNET_DEVICE 必须是 cpu 或 auto")
        if device == "cpu":
            tf.config.set_visible_devices([], "GPU")
        for gpu in tf.config.get_visible_devices("GPU"):
            tf.config.experimental.set_memory_growth(gpu, True)
        weights = directory / "transnetv2-weights"
        for name in ("saved_model.pb", "variables/variables.index", "variables/variables.data-00000-of-00001"):
            path = weights / name
            if not path.is_file() or path.stat().st_size < 1024:
                raise ValueError("TransNetV2 权重缺失或仍为 Git LFS 指针，请执行 ./scripts/setup-transnet-service.sh")
        sys.path.insert(0, str(directory))
        from transnetv2 import TransNetV2

        self.model = TransNetV2(model_dir=str(directory / "transnetv2-weights"))
        self.lock = threading.Lock()
        self.metadata = {
            "model": "TransNetV2", "tensorflow": tf.__version__,
            "device": "gpu" if tf.config.get_visible_devices("GPU") else "cpu",
            "weights_sha256": content_hash(weights / "variables/variables.data-00000-of-00001"),
        }

    def detect(self, path: Path, threshold: float) -> dict:
        # Upstream predict_video materializes all frames. Stream low-resolution frames
        # through bounded 100-frame windows instead, preserving every original frame.
        import subprocess

        probe(path)
        process = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-vf",
                "scale=48:27",
                "-fps_mode",
                "passthrough",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        frame_bytes = 48 * 27 * 3
        pending = np.empty((0, 27, 48, 3), dtype=np.uint8)
        predictions = []
        first = True
        try:
            with self.lock:
                while True:
                    raw = process.stdout.read(frame_bytes * 50)
                    if not raw:
                        break
                    if len(raw) % frame_bytes:
                        raise RuntimeError("Incomplete decoded frame")
                    frames = np.frombuffer(raw, dtype=np.uint8).reshape((-1, 27, 48, 3))
                    if first:
                        pending = np.repeat(frames[:1], 25, axis=0)
                        first = False
                    pending = np.concatenate([pending, frames])
                    while len(pending) >= 100:
                        single, _ = self.model.predict_raw(pending[:100][None])
                        predictions.extend(np.asarray(single)[0, 25:75, 0].tolist())
                        pending = pending[50:]
                if first:
                    raise ValueError("视频中没有可解码帧")
                while len(pending) > 25:
                    remaining = min(50, len(pending) - 25)
                    padded = np.concatenate([pending, np.repeat(pending[-1:], 100 - len(pending), axis=0)])
                    single, _ = self.model.predict_raw(padded[None])
                    predictions.extend(np.asarray(single)[0, 25 : 25 + remaining, 0].tolist())
                    pending = pending[remaining:]
            if process.wait() != 0:
                raise ValueError("FFmpeg 解码失败")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        cuts, interval = [], []
        for frame, probability in enumerate([*predictions, 0]):
            if probability > threshold:
                interval.append(frame)
            elif interval:
                midpoint = (interval[0] + interval[-1]) // 2
                if 0 < midpoint < len(predictions):
                    cuts.append(midpoint)
                interval = []
        return {"model": "TransNetV2", "frame_count": len(predictions), "cut_frames": cuts}


def create_model_app(config: Config, role: str, engine_factory=None) -> FastAPI:
    def authorize(authorization: Annotated[str | None, Header()] = None):
        expected = os.environ.get("MINDEX_MODEL_API_KEY", "")
        if expected and not secrets.compare_digest(authorization or "", f"Bearer {expected}"):
            raise HTTPException(401, "Invalid model API key")

    engine = None
    gpu_lock = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal engine, gpu_lock
        lock_path = Path(os.environ.get("MINDEX_GPU_LOCK", str(Path.home() / ".cache/mindex/gpu.lock")))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        gpu_lock = FileLock(str(lock_path), timeout=0)
        from contextlib import nullcontext
        # 本地 CPU 分镜服务不持有 GPU 角色锁，可与已有 llama.cpp 服务共存。
        lease = nullcontext() if role == "detector" and os.environ.get("MINDEX_TRANSNET_DEVICE") == "cpu" else gpu_lock
        with lease:
            factory = (
                engine_factory
                or {
                    "embedding": lambda: WeMMEngine(config),
                    "asr": lambda: ASREngine(config),
                    "detector": TransNetEngine,
                }[role]
            )
            engine = await run_in_threadpool(factory)
            yield
            engine = None

    app = FastAPI(title=f"Mindex {role} service", lifespan=lifespan, dependencies=[Depends(authorize)])

    @app.get("/health")
    def health():
        result = {"ready": engine is not None, "role": role}
        if role == "embedding" and engine is not None:
            result["space"] = engine.space
        if role == "detector" and engine is not None:
            result.update(getattr(engine, "metadata", {}))
        return result

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(RuntimeError)
    async def inference_error(_, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"detail": str(exc)})

    if role == "embedding":

        @app.get("/metrics")
        def metrics():
            return engine.metrics() if hasattr(engine, "metrics") else {}

        @app.post("/embed/text")
        def embed_text(batch: TextBatch):
            vectors = engine.text(batch.texts, batch.role)
            return {"vectors": vectors, "space_id": fingerprint(engine.space)}

        @app.post("/embed/video")
        async def embed_video(file: Annotated[UploadFile, File()]):
            with tempfile.TemporaryDirectory(prefix="mindex-embed-") as directory:
                path = Path(directory) / "video.media"
                await save_upload(file, path, 100 * 1024 * 1024)
                vectors = await run_in_threadpool(engine.video, path)
                return {"vectors": vectors, "space_id": fingerprint(engine.space)}

        @app.post("/embed/image")
        async def embed_image(file: Annotated[UploadFile, File()]):
            with tempfile.TemporaryDirectory(prefix="mindex-image-") as directory:
                path = Path(directory) / "image.media"
                await save_upload(file, path, config.search.max_image_upload_mb * 1024 * 1024)
                vectors = await run_in_threadpool(engine.image, path)
                return {"vectors": vectors, "space_id": fingerprint(engine.space)}

    if role == "asr":

        @app.post("/transcribe")
        async def transcribe(file: Annotated[UploadFile, File()]):
            with tempfile.TemporaryDirectory(prefix="mindex-asr-") as directory:
                path = Path(directory) / "audio.wav"
                await save_upload(file, path, 20 * 1024 * 1024)
                return await run_in_threadpool(engine.transcribe, path)

    if role == "detector":

        @app.post("/detect/video")
        async def detect(
            file: Annotated[UploadFile, File()], threshold: Annotated[float, Form(gt=0, lt=1)] = 0.5
        ):
            with tempfile.TemporaryDirectory(prefix="mindex-detect-") as directory:
                path = Path(directory) / "video.media"
                await save_upload(file, path, config.source_upload_max_bytes)
                return await run_in_threadpool(engine.detect, path, threshold)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=["embedding", "asr", "detector"], required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    ports = {"embedding": 4010, "asr": 4020, "detector": 18203}
    uvicorn.run(
        create_model_app(load_config(args.config), args.role),
        host=args.host,
        port=args.port or ports[args.role],
        workers=1,
    )


if __name__ == "__main__":
    main()
