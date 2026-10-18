"""llama.cpp 原生适配：区分文件内容、向量契约与部署观测，推理前后核验。"""

from __future__ import annotations

import base64
import copy
import json
import math
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from . import adapters, deployment
from .adapters import HTTPEmbedder, ModelError
from .config import Config, Service, fingerprint
from .deployment import (
    PREFIX,
    SUFFIX,
    check_local_files,
    normalized_metadata,
    read_manifest,
    space_contract,
    validate_role,
)
from .media import MediaError, atomic_media, content_hash, probe

ADAPTER_VERSION = deployment.ADAPTER_VERSION


def root(service: Service) -> str:
    return service.base_url.rstrip("/").removesuffix("/v1")


def inspect_service(service: Service) -> dict:
    """只探测；完整报告包含原始响应、结构元数据、能力与部署观测，不执行推理。"""

    def get(route):
        return adapters.request(
            "GET", root(service) + route, timeout=5, attempts=1, headers=service.headers()
        )

    health = get("/health")
    if not isinstance(health, dict) or health.get("status") != "ok":
        raise ModelError(f"llama.cpp 未就绪：{health}")
    props, models = copy.deepcopy(get("/props")), copy.deepcopy(get("/v1/models"))
    if not isinstance(props, dict) or not isinstance(models, dict):
        raise ModelError("/props 和 /v1/models 必须返回对象")
    data = models.get("data", [])
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ModelError("需要单模型 llama-server；不能用自动切换模型的路由服务")
    model = data[0]
    # 保留 schema 1 的八字段指纹及其原校验；它从来不是文件内容身份。
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
    if (
        not isinstance(observable["model_id"], str)
        or not observable["model_id"].strip()
        or not observable["build_info"]
    ):
        raise ModelError("/props 或 /v1/models 缺少 build_info/model_id，无法固定运行身份")
    # 七字段强校验对所有探测路径生效（含 schema 1 清单），比旧实现的宽松检查更严：
    # 元数据稀疏的旧 llama-server 会被拒绝。方向 fail-closed；回滚旧清单须配套恢复旧代码。
    metadata = normalized_metadata(observable["model_meta"])
    capabilities = observable["modalities"]
    if not isinstance(capabilities, dict) or any(type(v) is not bool for v in capabilities.values()):
        raise ModelError("modalities 能力声明必须为布尔值")
    defaults = props.get("default_generation_settings", {})
    if not isinstance(defaults, dict):
        raise ModelError("default_generation_settings 必须为对象")
    parameters = {
        key: props[key]
        for key in (
            "total_slots",
            "default_generation_settings",
            "n_ctx",
            "n_ctx_slot",
            "n_batch",
            "n_ubatch",
            "pooling_type",
            "embeddings",
            "cache_type_k",
            "cache_type_v",
            "flash_attn",
            "backend",
            "devices",
            "video_fps",
            "mmproj_path",
            "tensor_split",
            "n_gpu_layers",
            "chat_template_caps",
            "chat_template_tool_use",
        )
        if key in props
    }
    deployment = {"base_url": root(service), "observable": observable, "parameters": parameters}
    return {
        "observable": observable,
        "runtime_id": fingerprint(observable),
        "health": health,
        "props": props,
        "models": models,
        "model_id": observable["model_id"],
        "metadata": metadata,
        "capabilities": capabilities,
        "deployment": deployment,
        "deployment_id": fingerprint(deployment),
        "instance_marker": props.get("media_marker"),
        "diagnostics": {
            "metadata_valid": True,
            "single_model": True,
            "ctx": defaults.get("n_ctx", model["meta"].get("n_ctx")),
            "file_loading_verified_by_http": False,
        },
    }


def manifest(service: Service, role: str) -> dict:
    path = Path(getattr(service, "deployment_manifest", None) or "")
    if not path.is_file():
        raise ModelError(f"缺少 {role} 部署清单；先执行 e2e probe，再用 e2e pin 固定 GGUF/mmproj 哈希")
    return read_manifest(path, service, role)


def checked_runtime(service: Service, pinned: dict) -> dict:
    """统一返回完整报告；v1 固定旧 runtime_id，v2 固定部署观测并核验本地内容。"""
    observed = inspect_service(service)
    if pinned["schema"] == 1:
        if observed["runtime_id"] != pinned["runtime_id"]:
            raise ModelError("llama.cpp 运行身份发生变化，请核对权重并重新 pin/smoke 或切回原服务")
        observed["file_states"] = {}
    else:
        observed["deployment"] = {
            **observed["deployment"],
            "launch_parameters": pinned["trust"]["launch_parameters"],
        }
        observed["deployment_id"] = fingerprint(observed["deployment"])
        if observed["deployment_id"] != pinned["deployment_id"]:
            raise ModelError("llama.cpp 部署观测发生变化，请核对清单并重新 pin/smoke")
        validate_role(service, pinned["role"], observed, pinned["quantization"])
        observed["file_states"] = check_local_files(pinned, observed)
    return observed


def check_after(service: Service, pinned: dict, before: dict) -> dict:
    after = checked_runtime(service, pinned)
    if (
        before["deployment_id"] != after["deployment_id"]
        or before["instance_marker"] != after["instance_marker"]
        or before["file_states"] != after["file_states"]
        or manifest(service, pinned["role"]) != pinned
    ):
        raise ModelError("推理期间部署观测、清单或本地文件状态发生变化，已拒绝结果")
    return after


def prepare_video(path: Path, output: Path, config: Config):
    """Sample the whole bounded clip, preserve presentation times, strip all audio."""
    info, video = probe(path), config.video
    if info["duration"] > video.short_shot_seconds + 0.05:
        raise ModelError("原生嵌入输入超过短片长度；请先分窗")
    rate = min(float(video.fps), video.max_frames / info["duration"])
    ratio = min(1, math.sqrt(video.max_pixels / (info["width"] * info["height"])))
    width = max(2, int(info["width"] * ratio) // 2 * 2)
    height = max(2, int(info["height"] * ratio) // 2 * 2)
    fps_filter = f"fps=fps={rate}:start_time=0"
    filters = [fps_filter]
    # The default EOF rounding can discard every frame of a very short clip.
    # Retry only invalid samples; using eof_action=pass for all clips would also
    # add a last frame to some valid clips and change their existing embeddings.
    if info["duration"] * rate < 1:
        filters.append(fps_filter + ":eof_action=pass")
    for sampling in filters:
        atomic_media(
            [
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-an",
                "-vf",
                f"{sampling},scale={width}:{height}",
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
        try:
            probe(output)
            return
        except MediaError as exc:
            output.unlink(missing_ok=True)
            failure = exc
    raise ModelError(
        f"视频嵌入预处理失败：{path.name}（{info['duration']:.3f} 秒），{failure}"
    ) from failure


class LlamaEmbedder(HTTPEmbedder):
    @contextmanager
    def validating(self):
        """仅供 smoke 建立/对照基线；普通建索引、检索和直接推理必须经过门槛。"""
        previous = getattr(self, "_validating", False)
        self._validating = True
        try:
            yield self
        finally:
            self._validating = previous

    def identity(self) -> dict:
        pinned = manifest(self.profile, "embedding")
        observed = checked_runtime(self.profile, pinned)
        if pinned["schema"] == 2:
            spec = space_contract(self.config, pinned["file_identity"])
            if pinned["space_contract"] != spec:
                raise ModelError("部署清单空间契约与当前配置/协议不同；请使用新清单和新索引")
        else:
            observable = observed["observable"]
            if not observable["modalities"].get("vision"):
                raise ModelError("llama.cpp 未启用视觉投影，禁止将纯文字模型作为视频嵌入")
            if observable["modalities"].get("video") is False:
                raise ModelError("llama.cpp 明确声明不支持视频")
            if observable["model_meta"].get("ftype") != pinned["quantization"]:
                raise ModelError("部署清单量化与 GGUF 元数据不一致")
            if pinned.get("video_fps") != float(self.config.video.fps):
                raise ModelError("部署清单 video_fps 与本地采样配置不同")
            if observable["model_meta"].get("n_embd") != self.profile.dimensions:
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
                "implementation": "llama-native-wemm-1",
                "adapter_sha256": content_hash(Path(__file__)),
                "template": {"prefix": PREFIX, "suffix": SUFFIX},
                "media": "input_video/h264-crf18-noaudio-whole-clip-v1",
                "trust": "operator-attested-file-hashes;server-observable-runtime-checked",
            }
        if self._identity is not None and self._identity != spec:
            raise ModelError("推理过程中部署清单发生变化，已拒绝向量")
        self._identity = spec
        self._pinned, self._runtime = pinned, observed
        return spec

    def _embed(self, content: list[dict]) -> list[float]:
        self.identity()
        pinned, before = self._pinned, self._runtime
        gated = pinned["schema"] == 2 and not getattr(self, "_validating", False)
        if gated:
            self._require_gate(self._identity, before)
        payload = adapters.request(
            "POST",
            root(self.profile) + "/v1/embeddings",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            attempts=1,
            json={
                "model": before["model_id"],
                "encoding_format": "float",
                "embd_normalize": 2,
                "input": [{"content": content}],
            },
        )
        after = check_after(self.profile, pinned, before)
        if gated:
            self._require_gate(self._identity, after)
        try:
            data = payload["data"]
            if len(data) != 1 or data[0]["index"] != 0:
                raise ValueError("错误的向量条数/索引")
            vector = data[0]["embedding"]
            normalized = self._vectors({"space_id": fingerprint(self._identity), "vectors": [vector]}, 1)[0]
            self.last_raw_vector = copy.deepcopy(vector)
            return normalized
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError("llama.cpp 未返回有效的 pooled embedding") from exc

    def text(self, texts: list[str], role="document") -> list[list[float]]:
        # Official WeMM template is identical for query/document; no instruction prefixes.
        return [self._embed([{"type": "text", "text": PREFIX + text + SUFFIX}]) for text in texts]

    def video(self, path: Path) -> list[float]:
        with tempfile.TemporaryDirectory(prefix="wemm-video-") as directory:
            output = Path(directory) / "sample.mp4"
            prepare_video(path, output, self.config)
            self.last_prepared_sha256 = content_hash(output)
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

    def _require_gate(self, space: dict, report: dict):
        if space.get("schema") == 2:
            from .compatibility import require_gate

            require_gate(self.config, space, report)
            return
        path = self.config.data_dir / "capabilities" / (fingerprint(space) + ".json")
        try:
            gate = json.loads(path.read_text()) if path.is_file() else {}
            if not gate.get("passed") or gate.get("space") != space:
                raise ValueError("未通过")
        except (OSError, ValueError, TypeError) as exc:
            raise ModelError("尚未通过六片段能力门槛；请先执行 e2e smoke，再建索引或检索") from exc

    def require_gate(self):
        space = self.identity()
        self._require_gate(space, self._runtime)


class LlamaTranscriber:
    """Native Qwen3-ASR emits raw text; separate CPU alignment supplies timestamps."""

    def __init__(self, config: Config):
        self.config, self.profile = config, config.asr

    def raw(self, path: Path) -> dict:
        pinned = manifest(self.profile, "asr")
        before = checked_runtime(self.profile, pinned)
        payload = adapters.request(
            "POST",
            root(self.profile) + "/v1/chat/completions",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": before["model_id"],
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
        check_after(self.profile, pinned, before)
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
