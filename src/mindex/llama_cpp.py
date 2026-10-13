"""Native llama.cpp wire adapters. Weight digests are operator attestations, not server facts."""

from __future__ import annotations

import base64
import json
import math
import re
import tempfile
from pathlib import Path

from . import adapters
from .adapters import HTTPEmbedder, ModelError
from .config import Config, Service, fingerprint
from .media import atomic_media, content_hash, probe

ADAPTER_VERSION = "llama-native-wemm-1"
PREFIX = "<|im_start|>user\n"
SUFFIX = "<|im_end|><embedding>"


def root(service: Service) -> str:
    return service.base_url.rstrip("/").removesuffix("/v1")


def inspect_service(service: Service) -> dict:
    """No inference and no model activation. Exclude volatile counters from identity."""

    def get(route):
        return adapters.request(
            "GET", root(service) + route, timeout=5, attempts=1, headers=service.headers()
        )

    health = get("/health")
    if health.get("status") != "ok":
        raise ModelError(f"llama.cpp 未就绪：{health}")
    props, models = get("/props"), get("/v1/models")
    data = models.get("data", [])
    if len(data) != 1:
        raise ModelError("需要单模型 llama-server；不能用自动切换模型的路由服务")
    model = data[0]
    observable = {
        "model_path": props.get("model_path"),
        "build_info": props.get("build_info"),
        "chat_template": props.get("chat_template"),
        "modalities": props.get("modalities", {}),
        "model_id": model.get("id"),
        "model_meta": model.get("meta", {}),
        "eos_token": props.get("eos_token"),
        "bos_token": props.get("bos_token"),
    }
    if not observable["build_info"] or not observable["model_id"]:
        raise ModelError("/props 或 /v1/models 缺少 build_info/model_id，无法固定运行身份")
    return {"observable": observable, "runtime_id": fingerprint(observable), "health": health}


def manifest(service: Service, role: str) -> dict:
    path = Path(getattr(service, "deployment_manifest", None) or "")
    if not path.is_file():
        raise ModelError(f"缺少 {role} 部署清单；先执行 e2e probe，再用 e2e pin 固定 GGUF/mmproj 哈希")
    try:
        value = json.loads(path.read_text())
        if value["role"] != role or value["model"] != service.model:
            raise ValueError("role/model 不匹配")
        for field in ("weights_sha256", "mmproj_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", value[field]):
                raise ValueError(f"{field} 必须为实际文件 SHA256")
        if not value["quantization"] or value["quantization"] in {"unknown", "int8", "nf4"}:
            raise ValueError("填写实际 GGUF 量化类型，例如 Q8_0")
        if not value["runtime_id"] or value["schema"] != 1:
            raise ValueError("部署清单无效")
        if role == "embedding" and value["pooling"] != "last":
            raise ValueError("WeMM 必须使用最后一个 <embedding> token 池化：--pooling last")
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelError(f"部署清单无效 {path}: {exc}") from exc


def checked_runtime(service: Service, pinned: dict) -> dict:
    observed = inspect_service(service)
    if observed["runtime_id"] != pinned["runtime_id"]:
        raise ModelError("llama.cpp 运行身份发生变化，请核对权重并重新 pin/smoke 或切回原服务")
    return observed["observable"]


def prepare_video(path: Path, output: Path, config: Config):
    """Sample the whole bounded clip, preserve presentation times, strip all audio."""
    info, video = probe(path), config.video
    if info["duration"] > video.short_shot_seconds + 0.05:
        raise ModelError("原生嵌入输入超过短片长度；请先分窗")
    rate = min(float(video.fps), video.max_frames / info["duration"])
    ratio = min(1, math.sqrt(video.max_pixels / (info["width"] * info["height"])))
    width = max(2, int(info["width"] * ratio) // 2 * 2)
    height = max(2, int(info["height"] * ratio) // 2 * 2)
    # Start at zero so even a sub-frame-duration clip produces at least one frame.
    atomic_media(
        [
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"fps=fps={rate}:start_time=0,scale={width}:{height}",
            "-frames:v",
            str(video.max_frames),
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ],
        output,
    )


class LlamaEmbedder(HTTPEmbedder):
    def identity(self) -> dict:
        pinned = manifest(self.profile, "embedding")
        observable = checked_runtime(self.profile, pinned)
        if not observable["modalities"].get("vision"):
            raise ModelError("llama.cpp 未启用视觉投影，禁止将纯文字模型作为视频嵌入")
        if observable["modalities"].get("video") is False:
            raise ModelError("llama.cpp 明确声明不支持视频")
        ftype = observable["model_meta"].get("ftype")
        if ftype and ftype != pinned["quantization"]:
            raise ModelError("部署清单量化与 GGUF 元数据不一致")
        if pinned.get("video_fps") != float(self.config.video.fps):
            raise ModelError("部署清单 video_fps 与本地采样配置不同")
        dims = observable["model_meta"].get("n_embd")
        if dims is not None and dims != self.profile.dimensions:
            raise ModelError("GGUF n_embd 与目标嵌入维度不符；禁止截断或补齐")
        spec = {
            "model": self.profile.model,
            "revision": pinned["weights_sha256"],
            "mmproj_sha256": pinned["mmproj_sha256"],
            "quantization": pinned["quantization"],
            "dimensions": self.profile.dimensions,
            "normalized": True,
            "fps": self.config.video.fps,
            "max_frames": self.config.video.max_frames,
            "max_pixels": self.config.video.max_pixels,
            "pooling": "last",
            "runtime_id": pinned["runtime_id"],
            "implementation": ADAPTER_VERSION,
            "adapter_sha256": content_hash(Path(__file__)),
            "template": {"prefix": PREFIX, "suffix": SUFFIX},
            "media": "input_video/h264-crf18-noaudio-whole-clip-v1",
            "trust": "operator-attested-file-hashes;server-observable-runtime-checked",
        }
        if self._identity is not None and self._identity != spec:
            raise ModelError("推理过程中部署清单发生变化，已拒绝向量")
        self._identity = spec
        return spec

    def _embed(self, content: list[dict]) -> list[float]:
        self.identity()  # Before AND after each request: native API has no space_id.
        pinned = manifest(self.profile, "embedding")
        payload = adapters.request(
            "POST",
            root(self.profile) + "/v1/embeddings",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": pinned["observable"]["model_id"],
                "encoding_format": "float",
                "embd_normalize": 2,
                "input": [{"content": content}],
            },
        )
        self.identity()
        try:
            data = payload["data"]
            if len(data) != 1 or data[0]["index"] != 0:
                raise ValueError("错误的向量条数/索引")
            # Reuse strict shape/finite/nonzero checks and canonical normalization.
            vector = data[0]["embedding"]
            return self._vectors({"space_id": fingerprint(self._identity), "vectors": [vector]}, 1)[0]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError("llama.cpp 未返回有效的 pooled embedding") from exc

    def text(self, texts: list[str], role="document") -> list[list[float]]:
        # Official WeMM template is identical for query/document; no instruction prefixes.
        return [self._embed([{"type": "text", "text": PREFIX + text + SUFFIX}]) for text in texts]

    def video(self, path: Path) -> list[float]:
        with tempfile.TemporaryDirectory(prefix="wemm-video-") as directory:
            output = Path(directory) / "sample.mp4"
            prepare_video(path, output, self.config)
            return self._embed(
                [
                    {"type": "text", "text": PREFIX},
                    {
                        "type": "input_video",
                        "input_video": {"data": base64.b64encode(output.read_bytes()).decode()},
                    },
                    {"type": "text", "text": SUFFIX},
                ]
            )

    def require_gate(self):
        space = self.identity()
        path = self.config.data_dir / "capabilities" / (fingerprint(space) + ".json")
        gate = json.loads(path.read_text()) if path.is_file() else {}
        if not gate.get("passed") or gate.get("space") != space:
            raise ModelError("尚未通过六片段能力门槛；请先执行 e2e smoke，再建正式索引")


class LlamaTranscriber:
    """Native Qwen3-ASR emits raw text; separate CPU alignment supplies timestamps."""

    def __init__(self, config: Config):
        self.config, self.profile = config, config.asr

    def raw(self, path: Path) -> dict:
        pinned = manifest(self.profile, "asr")
        checked_runtime(self.profile, pinned)
        payload = adapters.request(
            "POST",
            root(self.profile) + "/v1/chat/completions",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": pinned["observable"]["model_id"],
                "temperature": 0,
                "max_tokens": 2048,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": base64.b64encode(path.read_bytes()).decode(),
                                    "format": "wav",
                                },
                            }
                        ],
                    }
                ],
            },
        )
        checked_runtime(self.profile, pinned)
        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or "<asr_text>" not in content:
                raise ValueError("缺少 Qwen3-ASR <asr_text> 输出，检查模型/音频投影/模板")
            language, text = content.split("<asr_text>", 1)
            language = language.removeprefix("language ").strip()
            text = re.sub(r"<\|(?:im_end|endoftext)\|>", "", text).strip()
            return {
                "text": text,
                "language": language,
                "raw_response": payload,
                "deployment_id": fingerprint(pinned),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("Qwen3-ASR 返回格式无效；原始文本不能伪造成带时间戳对白") from exc

    def transcribe(self, path: Path):
        raise ModelError("llama.cpp ASR 需先保存原始文本，再执行 align；不自动伪造时间戳")
