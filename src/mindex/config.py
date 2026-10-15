from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


def fingerprint(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Service(StrictModel):
    base_url: str
    model: str
    revision: str = "main"
    timeout: float = Field(default=180, gt=0)
    api_key_env: str = "MINDEX_MODEL_API_KEY"
    # 明文密钥只存 SQLite 覆盖层；exclude=True 保证指纹、任务快照与序列化永不携带。
    api_key: str = Field(default="", exclude=True)

    def headers(self) -> dict[str, str]:
        key = self.api_key or os.environ.get(self.api_key_env, "")
        return {"Authorization": f"Bearer {key}"} if key else {}


class EmbeddingConfig(Service):
    backend: Literal["mindex", "llama_cpp", "tokenhub"] = "mindex"
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4010"
    model: str = "tencent/WeMM-Embedding-9B"
    quantization: str = "int8"
    dimensions: int = Field(default=4096, ge=1)
    numerical_implementation: str = Field(default="llama-cpp-wemm-1", min_length=1)


class AnalyzerConfig(Service):
    backend: Literal["openai", "llama_cpp"] = "openai"
    # llama-server 专有的 chat_template_kwargs 扩展只发给本地服务，线上厂商须关闭。
    llama_template: bool = True
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4040/v1"
    model: str = "qwen3.8:27b"
    revision: str = "deployed-2026-10-12"
    api_key_env: str = "MINDEX_QWEN_API_KEY"


class ASRConfig(Service):
    backend: Literal["mindex", "llama_cpp", "openai", "minimax"] = "mindex"
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4020"
    model: str = "Qwen/Qwen3-ASR-1.7B"
    enabled: bool = True
    timeout: float = 300
    aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    aligner_revision: str = "main"


class GeneratorConfig(Service):
    """文本拆分镜 LLM（llama-server GGUF，OpenAI 兼容端点）。"""

    backend: Literal["openai"] = "openai"
    llama_template: bool = True
    base_url: str = "http://192.168.1.86:4040/v1"
    model: str = "qwen3.8:27b"
    revision: str = "deployed-2026-10-12"
    timeout: float = 300
    api_key_env: str = "MINDEX_QWEN_API_KEY"
    max_tokens: int = Field(default=4000, gt=0)
    split_retries: int = Field(default=2, ge=0)


class SpeakerConfig(Service):
    """TTS 语音合成服务（llama-server GGUF，OpenAI /audio/speech 风格端点；线上为 minimax/dashscope 专用协议）。"""

    backend: Literal["openai", "minimax", "dashscope"] = "openai"
    base_url: str = "http://192.168.1.86:4030/v1"
    model: str = "Qwen3-TTS"
    voice: str = "Cherry"
    response_format: str = "wav"
    speech_path: str = "/audio/speech"
    timeout: float = 300
    api_key_env: str = "MINDEX_QWEN_API_KEY"


class StudioRecallConfig(StrictModel):
    # visual 模式索引可能只含 moment；选片算法基于 source+时间区间，不依赖候选 kind。
    granularity: Literal["shot", "moment"] = "shot"
    limit: int = Field(default=20, ge=1, le=50)


class SelectionConfig(StrictModel):
    atempo_limit: float = Field(default=0.1, gt=0, le=0.25)
    gap_epsilon: float = Field(default=0.5, gt=0)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "semantic": 1.0,
            "duration": 1.0,
            "extension": 0.3,
            "repeat": 0.5,
            "overlap": 1.0,
        }
    )

    @model_validator(mode="after")
    def valid_weights(self):
        allowed = {"semantic", "duration", "extension", "repeat", "overlap"}
        if set(self.weights) != allowed or any(v < 0 for v in self.weights.values()):
            raise ValueError("Provide non-negative weights for semantic/duration/extension/repeat/overlap")
        if self.weights["semantic"] <= 0 or self.weights["duration"] <= 0:
            raise ValueError("semantic and duration weights must be positive")
        return self


class StudioAudioConfig(StrictModel):
    mode: Literal["tts_only", "ducking"] = "tts_only"
    duck_gain: float = Field(default=0.15, gt=0, le=1)
    loudnorm: str = "I=-16:TP=-1.5:LRA=11"


class SubtitleConfig(StrictModel):
    burn: bool = True
    font_family: str = "Noto Sans CJK SC"
    font_file: str | None = None
    font_size: int = Field(default=7, ge=1, le=100)


class RenderConfig(StrictModel):
    width: int = Field(default=1280, gt=0)
    height: int = Field(default=720, gt=0)
    fps: float = Field(default=25, gt=0)
    crf: int = Field(default=20, ge=0, le=51)
    preset: str = "fast"

    @model_validator(mode="after")
    def valid_canvas(self):
        if self.width % 2 or self.height % 2:
            raise ValueError("render width/height must be even numbers for x264")
        if self.preset not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"}:
            raise ValueError("render.preset must be a valid x264 preset")
        return self


class StudioConfig(StrictModel):
    """创作（文本生成视频）工作流配置；任务级 options 可覆盖部分默认值。"""

    max_input_chars: int = Field(default=2000, ge=1)
    max_shots: int = Field(default=60, ge=1)
    generator: GeneratorConfig = Field(default_factory=GeneratorConfig)
    tts: SpeakerConfig = Field(default_factory=SpeakerConfig)
    recall: StudioRecallConfig = Field(default_factory=StudioRecallConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    audio: StudioAudioConfig = Field(default_factory=StudioAudioConfig)
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)


class DetectorConfig(StrictModel):
    backend: str = "adaptive"
    base_url: str = "http://192.168.1.86:18203"
    api_key_env: str = "MINDEX_MODEL_API_KEY"
    threshold: float = Field(default=0.5, gt=0, lt=1)
    adaptive_threshold: float = Field(default=3, gt=0)
    min_content_val: float = Field(default=10, ge=0)

    @model_validator(mode="after")
    def valid_backend(self):
        if self.backend not in {"adaptive", "transnet"}:
            raise ValueError("detector.backend must be adaptive or transnet")
        return self


class VideoConfig(StrictModel):
    short_shot_seconds: float = Field(default=6, gt=0)
    window_seconds: float = Field(default=4, gt=0)
    stride_seconds: float = Field(default=3, gt=0)
    fps: float = Field(default=4, gt=0)
    max_frames: int = Field(default=24, ge=2)
    max_pixels: int = Field(default=262144, ge=4096)
    analysis_frames: int = Field(default=12, ge=2, le=24)

    @model_validator(mode="after")
    def valid_windows(self):
        if self.stride_seconds > self.window_seconds:
            raise ValueError("stride_seconds must not exceed window_seconds")
        return self


class SearchConfig(StrictModel):
    lexical_exclude: list[Literal["description", "style", "dialogue"]] = Field(default_factory=list)
    candidates: int = Field(default=100, ge=1, le=1000)
    rrf_k: int = Field(default=60, ge=1)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "video": 1,
            "description": 1,
            "style": 1,
            "dialogue": 0.5,
            "lexical": 0.5,
        }
    )
    max_upload_mb: int = Field(default=100, ge=1)
    max_image_upload_mb: int = Field(default=20, ge=1, le=100)
    max_query_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def valid_weights(self):
        allowed = {"video", "description", "style", "dialogue", "lexical"}
        if set(self.weights) != allowed or any(v < 0 for v in self.weights.values()):
            raise ValueError("Provide non-negative weights for video/description/style/dialogue/lexical")
        return self


class Config(StrictModel):
    data_dir: Path = Path("data")
    runtime_overrides: bool = True
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    analyzer: AnalyzerConfig = Field(default_factory=AnalyzerConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    studio: StudioConfig = Field(default_factory=StudioConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)

    @property
    def layout_key(self) -> str:
        return fingerprint(
            {"schema": 1, "detector": self.detector.model_dump(), "video": self.video.model_dump()}
        )

    @property
    def analysis_key(self) -> str:
        return fingerprint(
            {
                "prompt": 1,
                "model": self.analyzer.model_dump(exclude={"timeout", "api_key_env"}),
                "deployment": self.analyzer_deployment,
                "asr": self.asr.model_dump(exclude={"timeout", "api_key_env"}),
                "asr_key": self.asr_key,
                "layout": self.layout_key,
            }
        )

    @property
    def analyzer_deployment(self):
        path = Path(self.analyzer.deployment_manifest or "")
        return (
            json.loads(path.read_text()) if self.analyzer.backend == "llama_cpp" and path.is_file() else None
        )

    @property
    def asr_key(self) -> str:
        value = self.asr.model_dump(exclude={"timeout", "api_key_env"})
        if self.asr.backend == "llama_cpp":
            path = Path(self.asr.deployment_manifest or "")
            value["deployment"] = json.loads(path.read_text()) if path.is_file() else "missing"
        return fingerprint(value)

    @property
    def studio_key(self) -> str:
        """影响成片内容的创作配置指纹；任务创建时快照，供追溯与复现。"""
        return fingerprint(
            {
                "schema": 1,
                "generator": self.studio.generator.model_dump(exclude={"timeout", "api_key_env"}),
                "tts": self.studio.tts.model_dump(exclude={"timeout", "api_key_env"}),
                "selection": self.studio.selection.model_dump(),
                "audio": self.studio.audio.model_dump(),
                "subtitle": self.studio.subtitle.model_dump(),
                "render": self.studio.render.model_dump(),
            }
        )


def load_config(path: str | Path | None = None) -> Config:
    selected = Path(path or os.environ.get("MINDEX_CONFIG", "config.local.yaml"))
    if path and not selected.exists():
        raise FileNotFoundError(selected)
    values = (yaml.safe_load(selected.read_text()) or {}) if selected.exists() else {}
    config = Config.model_validate(values)
    # Preserve existing layout IDs when a full YAML spells default 4 as 4.0.
    # Pydantic validates explicit floats, but leaves numeric defaults as declared.
    for name, defaults in (("video", VideoConfig()), ("detector", DetectorConfig())):
        section = getattr(config, name)
        for field, default in defaults.model_dump().items():
            if getattr(section, field) == default:
                setattr(section, field, default)
    base = selected.resolve().parent if selected.exists() else Path.cwd()
    config.data_dir = (base / config.data_dir).resolve()
    for service in (config.embedding, config.asr, config.analyzer):
        if service.deployment_manifest:
            service.deployment_manifest = str((base / service.deployment_manifest).resolve())
    return config
