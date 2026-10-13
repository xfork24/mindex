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
    model_config = ConfigDict(extra="forbid")


class Service(StrictModel):
    base_url: str
    model: str
    revision: str = "main"
    timeout: float = Field(default=180, gt=0)
    api_key_env: str = "MINDEX_MODEL_API_KEY"

    def headers(self) -> dict[str, str]:
        key = os.environ.get(self.api_key_env, "")
        return {"Authorization": f"Bearer {key}"} if key else {}


class EmbeddingConfig(Service):
    backend: Literal["mindex", "llama_cpp"] = "mindex"
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4010"
    model: str = "tencent/WeMM-Embedding-9B"
    quantization: str = "int8"
    dimensions: int = Field(default=4096, ge=1)


class AnalyzerConfig(Service):
    backend: Literal["openai", "llama_cpp"] = "openai"
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4040/v1"
    model: str = "qwen3.8:27b"
    revision: str = "deployed-2026-10-12"
    api_key_env: str = "MINDEX_QWEN_API_KEY"


class ASRConfig(Service):
    backend: Literal["mindex", "llama_cpp"] = "mindex"
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4020"
    model: str = "Qwen/Qwen3-ASR-1.7B"
    enabled: bool = True
    timeout: float = 300
    aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    aligner_revision: str = "main"


class TTSConfig(StrictModel):
    """Reserved deployment address; TTS is not part of the retrieval pipeline."""

    base_url: str = "http://192.168.1.86:4030"
    model: str = "Qwen3-TTS"


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
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    analyzer: AnalyzerConfig = Field(default_factory=AnalyzerConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
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
