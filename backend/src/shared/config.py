from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from mindex.shared.agent_domains import LOADABLE_DOMAINS
from mindex.shared.comfyui import IMAGE_MAPPING_FIELDS, MUSIC_MAPPING_FIELDS, validate_workflow, workflow_hash


def fingerprint(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# 图片、视频、音乐与动画生成类模型的缺省请求超时（秒）；其他模型缺省 180 秒（Service.timeout）。
GENERATION_TIMEOUT = 600


class Service(StrictModel):
    base_url: str
    model: str
    revision: str = "main"
    # 单次模型请求的超时（秒）：设置页模型档案可改，所有调用该模型的地方都用它。
    # 缺省 3 分钟；图片、视频、音乐与动画生成类模型缺省 10 分钟（GENERATION_TIMEOUT）。
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


class PersonConfig(Service):
    backend: Literal["mindex"] = "mindex"
    base_url: str = "http://127.0.0.1:4050"
    model: Literal["cheetah_l"] = "cheetah_l"
    revision: str = "manifest-v2"
    model_root: str = "~/.insightface"
    expected_space: dict | None = None
    frames_max_edge: int = Field(default=640, ge=0)
    frames_jpeg_quality: int = Field(default=85, ge=1, le=100)
    frame_interval: int = Field(default=15, ge=1, strict=True)
    fps: float = Field(default=5, gt=0, le=5)
    face_threshold: float = Field(default=0.55, gt=0, lt=1)
    face_margin: float = Field(default=0.1, gt=0, lt=1)
    # 视频帧的人脸质量门槛：低质量帧不接受自动确认。
    min_face_quality: float = Field(default=0.6, ge=0, le=1)
    # 参考图的质量门槛，默认 0 = 所有可用参考图都参与匹配。
    min_reference_quality: float = Field(default=0.0, ge=0, le=1)
    body_threshold: float = Field(default=0.8, gt=0, lt=1)


class AnalyzerConfig(Service):
    backend: Literal["openai", "llama_cpp"] = "openai"
    # llama-server 专有的 chat_template_kwargs 扩展只发给本地服务，线上厂商须关闭。
    llama_template: bool = True
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4040/v1"
    model: str = "qwen3.8:27b"
    revision: str = "deployed-2026-10-12"
    api_key_env: str = "MINDEX_QWEN_API_KEY"
    response_retries: int = Field(default=5, ge=0, le=20)
    initial_output_tokens: int = Field(default=12768, ge=64, le=131072)
    max_output_tokens: int = Field(default=131072, ge=64, le=131072)
    thinking_mode: Literal["auto", "low", "provider"] = "auto"


class ASRConfig(Service):
    backend: Literal["mindex", "llama_cpp", "openai", "minimax"] = "mindex"
    deployment_manifest: str | None = None
    base_url: str = "http://192.168.1.86:4020"
    model: str = "Qwen/Qwen3-ASR-1.7B"
    enabled: bool = True
    # 聊天式 ASR（llama_cpp / openai 后端）单次请求的输出 token 上限；只影响截断，不进入转写指纹。
    max_tokens: int = Field(default=32768, gt=0)
    aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    aligner_revision: str = "main"


class GeneratorConfig(Service):
    """文本拆分镜 LLM（llama-server GGUF，OpenAI 兼容端点）。"""

    backend: Literal["openai"] = "openai"
    llama_template: bool = True
    base_url: str = "http://192.168.1.86:4040/v1"
    model: str = "qwen3.8:27b"
    revision: str = "deployed-2026-10-12"
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
    api_key_env: str = "MINDEX_QWEN_API_KEY"


class VideoGeneratorConfig(Service):
    """AIGC 视频生成服务（aigc 分镜的 generate 阶段）：MiniMax H3（V2 多模态）/ MiniMax 海螺 / 阿里通义万相 / 自建 ComfyUI。

    缺省关闭：设置页激活「视频生成」档案或 yaml 设 enabled: true 后才会提交生成任务；
    测试连接只做鉴权/可达性探测，从不发起付费生成。``minimax_h3`` 后端在设置页只需配置 endpoint 与密钥，
    生成方式（文生/首帧/尾帧/首尾帧/多模态参考）、模型档位、分辨率、比例与时长在创作页逐镜设置。
    """

    backend: Literal["minimax_h3", "minimax", "dashscope", "comfyui"] = "comfyui"
    base_url: str = "http://127.0.0.1:8188"
    model: str = ""
    timeout: float = Field(default=GENERATION_TIMEOUT, gt=0)  # 单次 HTTP 请求超时（含下载生成结果）
    api_key_env: str = "MINDEX_VIDEO_API_KEY"
    poll_interval: float = Field(default=5, gt=0, le=300)  # 首次轮询间隔（秒），之后按 1.5 倍退避
    max_wait: float = Field(default=900, gt=0, le=86400)  # 单个分镜从提交到完成的总等待上限（秒）
    # 后端允许的生成时长（秒）：配音时长向上取整到最近的允许值，超过最大值时取最大值（不足部分冻结帧兜底）。
    durations: list[Annotated[float, Field(gt=0, le=120)]] = Field(default_factory=lambda: [5.0], min_length=1)
    fps: int = Field(default=16, ge=1, le=60)  # ComfyUI 帧数映射 = 时长 × fps
    comfyui_workflow: dict | None = None  # ComfyUI「导出（API）」格式工作流
    comfyui_mapping: dict[str, str] | None = None  # 请求字段 → "节点ID.inputs.输入名"
    enabled: bool = False

    @model_validator(mode="after")
    def valid_comfyui(self):
        self.durations = sorted(set(self.durations))
        if self.comfyui_workflow is not None or self.comfyui_mapping is not None:
            if self.comfyui_workflow is None:
                raise ValueError("已填写 ComfyUI 节点映射，但没有导入工作流")
            validate_workflow(self.comfyui_workflow, self.comfyui_mapping or {})
        return self

    def signature(self) -> dict:
        """影响生成画面的字段（无密钥、超时与轮询参数）；工作流以内容摘要代替全文。"""
        value = {"backend": self.backend, "model": self.model, "durations": list(self.durations)}
        if self.backend == "comfyui":
            value.update(fps=self.fps, workflow=workflow_hash(self.comfyui_workflow),
                         mapping=dict(sorted((self.comfyui_mapping or {}).items())))
        return value


# MiniMax H3 V2：整数时长 4–15 秒（H3-Max 为 5–15 秒）。
H3_DURATIONS = tuple(float(value) for value in range(4, 16))


class _ComfyUIMixin(StrictModel):
    """图片/音乐生成的 ComfyUI 字段：本地 ComfyUI 后端时导入「导出（API）」工作流与节点映射。"""

    poll_interval: float = Field(default=2, gt=0, le=300)
    max_wait: float = Field(default=900, gt=0, le=86400)
    comfyui_workflow: dict | None = None
    comfyui_mapping: dict[str, str] | None = None

    def _check_comfyui(self, fields):
        if self.comfyui_workflow is not None or self.comfyui_mapping is not None:
            if self.comfyui_workflow is None:
                raise ValueError("已填写 ComfyUI 节点映射，但没有导入工作流")
            validate_workflow(self.comfyui_workflow, self.comfyui_mapping or {}, fields)
        return self

    def _comfy_signature(self, value: dict) -> dict:
        if self.backend == "comfyui":
            value.update(workflow=workflow_hash(self.comfyui_workflow),
                         mapping=dict(sorted((self.comfyui_mapping or {}).items())))
        return value


class ImageGeneratorConfig(_ComfyUIMixin, Service):
    """AI 生成图片服务（aigc_image 分镜的 draw 阶段）：MiniMax image-01（文生图 / 主体参考图生图）或本地 ComfyUI。

    缺省关闭：设置页激活「图片生成」档案或 yaml 设 enabled: true 后才会调用；测试连接只做鉴权探测，不生成图片。
    生成的图片按静态图片的方式（推拉/平移）渲染成分镜画面。
    """

    backend: Literal["minimax", "comfyui"] = "minimax"
    base_url: str = "https://api.minimax.cn/v1"
    model: str = "image-01"
    timeout: float = Field(default=GENERATION_TIMEOUT, gt=0)
    api_key_env: str = "MINDEX_IMAGE_API_KEY"
    enabled: bool = False

    @model_validator(mode="after")
    def valid_comfyui(self):
        return self._check_comfyui(IMAGE_MAPPING_FIELDS)

    def signature(self) -> dict:
        return self._comfy_signature({"backend": self.backend, "model": self.model})


class MusicGeneratorConfig(_ComfyUIMixin, Service):
    """背景音乐生成服务（分镜工作台配音列的背景音乐 music 阶段）：MiniMax music-3.0（音乐生成）、
    music-cover（翻唱）与歌词生成（lyrics_generation），或本地 ComfyUI 工作流（不含歌词生成）。

    缺省关闭：设置页激活「音乐生成」档案或 yaml 设 enabled: true 后才会调用；测试连接只做鉴权探测，不生成音乐。
    ``audiocpp`` 后端对接本地 audio.cpp 服务（``audiocpp_server`` 上的 MiniMax-Music3-GGUF），见 scripts/run-music3.sh。
    """

    backend: Literal["minimax", "comfyui", "audiocpp"] = "minimax"
    base_url: str = "https://api.minimax.cn/v1"
    model: str = "music-3.0"
    cover_model: str = "music-cover"
    # audiocpp 后端（本地 audio.cpp 的 MiniMax-Music3-GGUF）的 flow matching 步数：越小越快，音质略降。
    num_inference_steps: int = Field(default=30, ge=1, le=200)
    # audiocpp 后端单次生成的最长秒数（耗时近似与之成正比）；成片合成时背景音乐会循环/截断到段落长度。
    max_seconds: int = Field(default=20, ge=1, le=300)
    timeout: float = Field(default=GENERATION_TIMEOUT, gt=0)  # 音乐生成为同步接口，单次请求需要较长超时
    api_key_env: str = "MINDEX_MUSIC_API_KEY"
    enabled: bool = False

    @model_validator(mode="after")
    def valid_comfyui(self):
        return self._check_comfyui(MUSIC_MAPPING_FIELDS)

    def signature(self) -> dict:
        value = {"backend": self.backend, "model": self.model, "cover_model": self.cover_model}
        if self.backend == "audiocpp":  # 只在本地后端加键：其他后端的指纹与旧版逐字节一致
            value.update(num_inference_steps=self.num_inference_steps, max_seconds=self.max_seconds)
        return self._comfy_signature(value)


class AnimatorConfig(Service):
    """AI 动画生成 LLM（animation 分镜的 animate 阶段）：按画面说明写自包含 HTML/JS 动画，再由沙箱逐帧录制。

    协议与智能助手相同（OpenAI 兼容 /chat/completions 或 Anthropic /v1/messages，非流式、无工具调用）；
    yaml 省略 ``studio.animator`` 时由 ``Config`` 复制 ``agent.llm`` 的连接字段（backend/base_url/model/
    revision/api_key_env），设置页未单独激活动画生成档案时也跟随激活的智能助手档案。
    """

    backend: Literal["openai", "anthropic"] = "openai"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    revision: str = "main"
    timeout: float = Field(default=GENERATION_TIMEOUT, gt=0)
    api_key_env: str = "MINDEX_AGENT_API_KEY"
    max_tokens: int = Field(default=16000, gt=0)
    retries: int = Field(default=2, ge=0, le=10)  # 静态校验未通过时带反馈重试的次数
    max_capture_seconds: float = Field(default=60, gt=0, le=3600)  # 单个分镜录制的墙钟时长上限（秒）
    max_html_kb: int = Field(default=200, ge=1, le=5000)  # 动画源码大小上限（KiB）
    enabled: bool = True
    # 是否由 agent.llm 复制而来（yaml 未写 studio.animator）；运行时据此跟随激活的智能助手档案。
    _inherited: bool = PrivateAttr(default=False)

    def signature(self) -> dict:
        """影响生成源码的字段（无密钥与超时）；提示词版本由 animate 阶段另行加入。"""
        return {"backend": self.backend, "model": self.model, "max_html_kb": self.max_html_kb}


# 动画生成从智能助手复制的连接字段。
ANIMATOR_INHERITED = ("backend", "base_url", "model", "revision", "api_key_env")


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
    # 创建任务不再提供字幕选项；默认软字幕轨道（烧录依赖 libass，可在方案参数开启）。
    burn: bool = False
    font_family: str = "Noto Sans CJK SC"
    font_file: str | None = None
    font_size: int = Field(default=7, ge=1, le=100)


CanvasName = Literal["16:9", "9:16", "1:1"]
DEFAULT_CANVASES: dict[str, tuple[int, int]] = {"9:16": (720, 1280), "1:1": (1080, 1080)}


class RenderConfig(StrictModel):
    width: int = Field(default=1280, gt=0)
    height: int = Field(default=720, gt=0)
    fps: float = Field(default=25, gt=0)
    crf: int = Field(default=20, ge=0, le=51)
    preset: str = "fast"
    # 方案画幅 → 输出分辨率。16:9 始终取 width/height（保持旧配置与旧签名不变），这里给出时必须一致；
    # 未给出的画幅用 DEFAULT_CANVASES。exclude=True：不进入任何 model_dump，因此既有渲染签名、
    # 配置指纹与任务快照逐字节不变；非默认画幅的尺寸由 render/compose 阶段显式写入签名参数。
    canvases: dict[CanvasName, tuple[Annotated[int, Field(gt=0, strict=True)],
                                     Annotated[int, Field(gt=0, strict=True)]]] = Field(
        default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def valid_canvas(self):
        if self.width % 2 or self.height % 2:
            raise ValueError("render width/height must be even numbers for x264")
        if self.preset not in {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"}:
            raise ValueError("render.preset must be a valid x264 preset")
        for name, (width, height) in self.canvases.items():
            if width % 2 or height % 2:
                raise ValueError(f"render.canvases.{name} 的宽高必须是偶数")
        if "16:9" in self.canvases and tuple(self.canvases["16:9"]) != (self.width, self.height):
            raise ValueError("render.canvases.16:9 必须与 render.width/height 一致（16:9 即基础分辨率）")
        return self

    def size(self, canvas: str = "16:9") -> tuple[int, int]:
        """画幅对应的输出宽高；16:9 读当前 width/height（测试与运行时覆盖都即时生效）。"""
        if canvas == "16:9":
            return self.width, self.height
        width, height = self.canvases.get(canvas) or DEFAULT_CANVASES[canvas]
        return int(width), int(height)


class StudioConfig(StrictModel):
    """创作（文本生成视频）工作流配置；任务级 options 可覆盖部分默认值。"""

    max_input_chars: int = Field(default=2000, ge=1)
    max_shots: int = Field(default=1024, ge=1)
    # 文案/拆分镜参考的可用素材摘要总字符预算（按 Unicode 字符计）；0 表示关闭。
    # strict 拒绝 1.5、"12000"、true 等非整数值。
    material_digest_chars: int = Field(default=12000, ge=0, le=100000, strict=True)
    # 每个方案登记的参考文稿总字符数上限（按 Unicode 字符计，规范化后统计）；超限返回 422。
    max_reference_chars: int = Field(default=30000, ge=1, le=1000000, strict=True)
    # 分镜画面来源（clip/image）上传上限：片段时长（秒）与文件大小（MB）；图片沿用 search.max_image_upload_mb。
    max_clip_seconds: float = Field(default=300, gt=0, le=3600)
    max_clip_mb: int = Field(default=500, ge=1, le=10000)
    generator: GeneratorConfig = Field(default_factory=GeneratorConfig)
    # 文案创作 LLM：按视频大纲写旁白正文。与拆分镜同为 OpenAI 兼容聊天服务，
    # yaml 未单独配置时复制 generator，保证升级后无需改配置即可使用。
    writer: GeneratorConfig = Field(default_factory=GeneratorConfig)
    tts: SpeakerConfig = Field(default_factory=SpeakerConfig)
    video_generator: VideoGeneratorConfig = Field(default_factory=VideoGeneratorConfig)
    # AI 生成图片（aigc_image 分镜）与背景音乐生成（配音列的背景音乐）。
    image_generator: ImageGeneratorConfig = Field(default_factory=ImageGeneratorConfig)
    music_generator: MusicGeneratorConfig = Field(default_factory=MusicGeneratorConfig)
    # AI 动画生成 LLM；yaml 未单独配置时由 Config 复制 agent.llm（见 Config.animator_defaults_to_agent）。
    animator: AnimatorConfig = Field(default_factory=AnimatorConfig)
    recall: StudioRecallConfig = Field(default_factory=StudioRecallConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    audio: StudioAudioConfig = Field(default_factory=StudioAudioConfig)
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)

    @model_validator(mode="before")
    @classmethod
    def writer_defaults_to_generator(cls, value):
        if isinstance(value, dict) and "writer" not in value and "generator" in value:
            generator = value["generator"]
            value = {**value, "writer": generator.model_dump() if isinstance(generator, BaseModel) else generator}
        return value


class DetectorConfig(StrictModel):
    backend: str = "adaptive"
    base_url: str = "http://192.168.1.86:18203"
    api_key_env: str = "MINDEX_MODEL_API_KEY"
    # 整段视频上传检测，长视频耗时较长；该服务不在设置页，超时只能在 yaml 配置。
    timeout: float = Field(default=1800, gt=0)
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
    channel_weights: dict[str, float] = Field(
        default_factory=lambda: {"visual": 0.35, "dialogue": 0.15, "analysis": 0.5}
    )
    lexical_exclude: list[Literal["description", "style", "dialogue"]] = Field(default_factory=list)
    candidates: int = Field(default=100, ge=1, le=1000)
    rrf_k: int = Field(default=30, ge=1)
    duration_weight: float = Field(default=0.15, ge=0, le=0.5, allow_inf_nan=False)
    duration_exponent: float = Field(default=2.0, ge=0.1, le=8, allow_inf_nan=False)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "video": 0.35,
            "description": 0.35,
            "style": 0.1,
            "dialogue": 0.1,
            "lexical": 0.1,
        }
    )
    max_upload_mb: int = Field(default=100, ge=1)
    max_image_upload_mb: int = Field(default=20, ge=1, le=100)
    max_query_seconds: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def valid_weights(self):
        if set(self.channel_weights) != {"visual", "dialogue", "analysis"} or any(
            not math.isfinite(value) or value < 0 for value in self.channel_weights.values()
        ):
            raise ValueError("Provide non-negative channel weights for visual/dialogue/analysis")
        allowed = {"video", "description", "style", "dialogue", "lexical"}
        if set(self.weights) != allowed or any(not math.isfinite(v) or v < 0 for v in self.weights.values()):
            raise ValueError("Provide non-negative weights for video/description/style/dialogue/lexical")
        return self


class CleanConfig(StrictModel):
    """磁盘清理策略（mindex clean）；天数为 mtime 年龄，详见 docs/cleanup.md。"""

    # 除活动版本外额外保留的最近 ready 索引版本数（向量复用与回退来源）
    index_keep_versions: int = Field(default=2, ge=1)
    # building 版本视为中断残留的年龄：超期并入删除（真实构建远短于此；
    # 删除即放弃同 spec 的跨运行续跑能力）
    index_building_max_age_days: int = Field(default=7, ge=1)
    # 未注册上传文件的宽限小时数（防正在进行的上传）
    temp_grace_hours: int = Field(default=24, ge=1)
    # 旧配置指纹的检查点保留天数：转写 / 分析 / 对白
    transcripts_max_age_days: int = Field(default=30, ge=1)
    analysis_max_age_days: int = Field(default=30, ge=1)
    dialogue_max_age_days: int = Field(default=30, ge=1)
    # 可按需重建的缓存：在线转码 / shot 媒体合成 / studio 预览
    context_max_age_days: int = Field(default=14, ge=1)
    shot_media_max_age_days: int = Field(default=7, ge=1)
    studio_preview_max_age_days: int = Field(default=14, ge=1)
    # studio 旧 attempt 目录（未被检查点或当前 attempt 引用）与导出 zip
    studio_attempt_max_age_days: int = Field(default=30, ge=1)
    studio_export_max_age_days: int = Field(default=60, ge=1)
    # S3 孤立对象（未被元数据引用）的回收宽限小时数：先标记、过宽限再复验引用与 pin
    s3_orphan_grace_hours: float = Field(default=24, ge=0)
    # reports 导出产物 / 字幕解析缓存（不含 uploads 用户原件）
    reports_max_age_days: int = Field(default=60, ge=1)
    subtitles_max_age_days: int = Field(default=30, ge=1)
    # runs 审计表已完成记录（running 一律保留）
    runs_max_age_days: int = Field(default=90, ge=1)


class AgentLlmConfig(Service):
    """智能助手模型：OpenAI 兼容 tools 协议或 Anthropic 原生 Messages 协议，须支持工具调用。"""

    backend: Literal["openai", "anthropic"] = "openai"
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    revision: str = "main"
    api_key_env: str = "MINDEX_AGENT_API_KEY"
    max_tokens: int = Field(default=4096, gt=0)
    # 模型总上下文容量；未填写时 dsh 沿用 agent.max_context_tokens。
    context_window: int | None = Field(default=None, ge=4000)
    # 模型是否接受图片输入；开启后助手会把图片附件与工具抽取的画面一并发给模型（最多 agent.max_images 张）。
    vision: bool = False


class AgentMcpConfig(StrictModel):
    """把工具注册表导出为 MCP 服务；外部 harness 不支持确认交互，因此由 policy 在服务端兜底。"""

    # streamable-http /mcp 默认关闭；开启后必须设置 token_env 指向的 Bearer 令牌。
    http_enabled: bool = False
    # read=只允许只读工具；write=再允许可逆写操作；all=再允许删除、改模型、构建等高风险操作。
    policy: Literal["read", "write", "all"] = "read"
    token_env: str = "MINDEX_MCP_TOKEN"
    # 导出的工具领域；None=全部领域，列表=只导出这些领域（另外总会导出 report_capability_gap）。
    domains: list[str] | None = None

    @field_validator("domains")
    @classmethod
    def valid_domains(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("agent.mcp.domains 至少指定一个领域；导出全部领域请设为 null")
        unknown = [item for item in value if item not in LOADABLE_DOMAINS]
        if unknown:
            raise ValueError(
                f"agent.mcp.domains 包含未知领域：{'、'.join(unknown)}；"
                f"可选：{'、'.join(LOADABLE_DOMAINS)}"
            )
        return list(dict.fromkeys(value))


class AgentConfig(StrictModel):
    runtime: Literal["builtin", "dsh"] = "builtin"
    enabled: bool = True
    # 执行步数不限；模型输入滚动保留最近的操作步骤（每步含全部工具调用与结果）。
    context_window_steps: int = Field(default=256, ge=1, le=256)
    max_context_tokens: int = Field(default=60000, ge=4000)
    # 表单收集的密钥只以 secret:// 引用出现在会话里，引用在此时长后失效。
    secret_ttl_seconds: int = Field(default=1800, ge=60)
    # 助手附件（attachment://）有效期；过期后由 mindex clean 的「过期助手附件」规则回收。
    attachment_ttl_hours: int = Field(default=168, ge=1)
    # 单个附件上限（MiB）；超出时上传返回 413。
    attachment_max_mb: int = Field(default=500, ge=1, le=20480)
    # 单次模型请求最多携带的图片数（只保留最近的）。
    max_images: int = Field(default=8, ge=0, le=32)
    # cluster 形态下业务 API 的 /api/agent 由单实例网关提供；此处的网关基址用于
    # 拒绝响应中给出明确指引（plans §9；本地/pg-single 形态不使用）。
    gateway_url: str | None = None
    llm: AgentLlmConfig = Field(default_factory=AgentLlmConfig)
    mcp: AgentMcpConfig = Field(default_factory=AgentMcpConfig)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_step_limit(cls, value):
        """旧配置中的 max_steps 不再限制执行，也不作为窗口大小。"""
        if isinstance(value, dict) and "max_steps" in value:
            return {k: v for k, v in value.items() if k != "max_steps"}
        return value


class LocalServiceConfig(StrictModel):
    """一个本机模型服务（例如 scripts/run-wemm.sh 启动的 llama-server）。只能在配置文件里定义：
    页面与接口只能启停、看状态和日志、改 autostart/stop_on_exit，不能改命令。"""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    label: str = Field(default="", max_length=60)
    role: str = Field(default="", max_length=32)  # 用途说明，例如 embedding、asr、tts、music、detector、person
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None  # 相对路径按配置文件所在目录解析
    env: dict[str, str] = Field(default_factory=dict)
    port: int = Field(ge=1, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/")
    autostart: bool = False  # 缺省值；设置页修改后保存在数据目录的 settings 表
    stop_on_exit: bool = True


class LocalServicesConfig(StrictModel):
    enabled: bool = False
    services: list[LocalServiceConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self):
        names = [service.name for service in self.services]
        if len(set(names)) != len(names):
            raise ValueError("local_services.services 的 name 不能重复")
        ports = [service.port for service in self.services]
        if len(set(ports)) != len(ports):
            raise ValueError("local_services.services 的 port 不能重复")
        return self

    def get(self, name: str) -> LocalServiceConfig | None:
        return next((service for service in self.services if service.name == name), None)


class TopicsSchedulerConfig(StrictModel):
    # mindex serve 内置的每日定时挖掘；各档案的时刻与条数在档案里设置。
    enabled: bool = True
    interval_seconds: int = Field(default=60, ge=5, le=3600)
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value is not None:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("调度时区必须是有效的 IANA 名称，例如 Asia/Shanghai 或 UTC") from exc
        return value


class TopicsFetchConfig(StrictModel):
    timeout: float = Field(default=15, gt=0, le=120)
    max_bytes: int = Field(default=2 * 1024**2, ge=1024, le=50 * 1024**2)
    # 候选条目保留天数；更早的条目在抓取时清理。
    item_days: int = Field(default=30, ge=1, le=365)
    max_items_per_source: int = Field(default=100, ge=1, le=1000)


class TopicsClusterConfig(StrictModel):
    # 余弦相似度不低于阈值的条目归为同一候选选题。
    threshold: float = Field(default=0.82, gt=0, le=1)
    max_clusters: int = Field(default=40, ge=1, le=200)
    # 每次请求 LLM 归纳的簇数。
    batch: int = Field(default=8, ge=1, le=40)


class TopicsCoverageConfig(StrictModel):
    # 候选粗筛阈值；还须通过基于镜头画面分析的相关性核验才能计为覆盖。
    min_similarity: float = Field(default=0.3, ge=0, le=1)
    limit: int = Field(default=3, ge=1, le=10)
    min_duration: float = Field(default=1.0, gt=0)


class TopicsBraveConfig(StrictModel):
    base_url: str = "https://api.search.brave.com"
    # 每次挖掘调用 Brave 的次数上限（计费保护）。
    max_queries: int = Field(default=10, ge=1, le=100)


class TopicsConfig(StrictModel):
    """选题挖掘（topics/）。llm 缺省复用 studio.writer。"""

    llm: GeneratorConfig | None = None
    scheduler: TopicsSchedulerConfig = Field(default_factory=TopicsSchedulerConfig)
    fetch: TopicsFetchConfig = Field(default_factory=TopicsFetchConfig)
    cluster: TopicsClusterConfig = Field(default_factory=TopicsClusterConfig)
    coverage: TopicsCoverageConfig = Field(default_factory=TopicsCoverageConfig)
    brave: TopicsBraveConfig = Field(default_factory=TopicsBraveConfig)


class PostgresConfig(StrictModel):
    """PostgreSQL 元数据库连接（阶段 1 的 pg-single 形态；plans §4）。

    DSN 只从环境变量读取（dsn_env），不落配置文件/日志/任务快照；远程 PG 应使用
    校验证书的 TLS（本地开发可用 sslmode=disable 的显式例外）。
    """

    dsn_env: str = "MINDEX_DATABASE_DSN"
    pool_min: int = Field(default=0, ge=0)
    pool_max: int = Field(default=8, ge=1)
    control_pool_max: int = Field(default=2, ge=1)
    connection_budget: int = Field(default=10, ge=2)
    checkout_timeout: float = Field(default=30, gt=0)
    control_checkout_timeout: float = Field(default=5, gt=0)

    @model_validator(mode="after")
    def validate_pools(self):
        if self.pool_min > self.pool_max:
            raise ValueError("postgres.pool_min 不能大于 pool_max")
        if self.pool_max + self.control_pool_max > self.connection_budget:
            raise ValueError("查询池与控制池的总连接数不能超过 postgres.connection_budget")
        return self


class ChromaServerConfig(StrictModel):
    """独立 Chroma 服务（阶段 2；plans §4/§6.2）。

    向量仍在元数据库登记 representations、embedding_function=None 与空间身份不变；
    服务器版本/镜像 digest 与 HNSW 参数由部署固定。凭据只从环境变量读取。
    """

    url: str = "http://127.0.0.1:8000"
    token_env: str = "MINDEX_CHROMA_TOKEN"


class VectorConfig(StrictModel):
    """向量后端选择：本地嵌入 chroma 或独立 Chroma 服务。

    chroma_server 属共享后端：与 sqlite 单机形态的组合未定义，需 database.backend
    为 postgres（pg-single 过渡形态即可使用；多 API 副本在阶段 5 集群装配后开放）。
    """

    backend: Literal["chroma_local", "chroma_server"] = "chroma_local"
    chroma_server: ChromaServerConfig = Field(default_factory=ChromaServerConfig)


class QueueConfig(StrictModel):
    """任务分发后端（阶段 3）：inline 为进程内立即执行（现有行为）；postgres 为
    持久队列 + worker（``mindex worker``），至少一次交付。需要 database.backend
    为 postgres。切换需重启。"""

    backend: Literal["inline", "postgres"] = "inline"


class GpuConfig(StrictModel):
    """GPU 准入容量（plans §7.2 阶段 3/5）：按物理资源/共驻模型组声明并发槽位。

    ``capacity=0``（缺省）不限制，保持现有行为；``queues`` 为受容量约束的任务类别。
    槽位经租约持有（locks=postgres 时跨节点、TTL 到期才回收），失联 worker 不会
    立即释放槽位造成 GPU 超卖。模型服务自身仍需限制真实执行并发。
    """

    capacity: int = Field(default=0, ge=0)
    queues: list[str] = Field(default_factory=lambda: ["source", "dialogue", "person"])


class LocksConfig(StrictModel):
    """具名锁后端（阶段 3）：file 为 data_dir 文件锁（现有行为，跨进程）；postgres
    为 PG 租约锁（epoch/fencing token、TTL+心跳，plans §7.1）。需要
    database.backend 为 postgres。多 API 副本在阶段 5 集群装配后开放。"""

    backend: Literal["file", "postgres"] = "file"


class S3Config(StrictModel):
    """S3 兼容对象存储（阶段 4B；plans §8）。凭据只从环境变量读取；具体产品的
    multipart/checksum/version 能力需逐项验证，不默认等同 AWS。"""

    endpoint_url: str = "http://127.0.0.1:9000"
    bucket: str = "mindex"
    prefix: str = ""
    region: str = ""
    access_key_id_env: str = "MINDEX_S3_ACCESS_KEY"
    secret_access_key_env: str = "MINDEX_S3_SECRET_KEY"
    staging_quota_gb: float = Field(default=100, gt=0)
    min_free_gb: float = Field(default=20, ge=0)
    presign_expire_seconds: int = Field(default=900, ge=60)


class StorageConfig(StrictModel):
    """媒体存储后端（阶段 4）：local 为单机文件系统（现有行为）；s3 为对象存储
    （阶段 4B 交付实现；NFS 为后续可选共享媒体后端，须实测 rename/同步语义）。"""

    backend: Literal["local", "s3"] = "local"
    s3: S3Config = Field(default_factory=S3Config)
    # 本地形态同样可用 staging 预算约束缓存/临时工作区（plans §8）；0 表示不限。
    staging_quota_gb: float = Field(default=0, ge=0)


class ServerConfig(StrictModel):
    """进程角色（plans §4/§9 阶段 5）：api 为业务 API；agent-gateway 为单实例
    智能助手网关（agent.sqlite3/附件留在网关持久卷，其余 API 不建独立运行时）。
    worker 是独立 CLI（``mindex worker``），不经此配置。"""

    role: Literal["api", "agent-gateway"] = "api"
    form: Literal["auto", "local", "pg-single", "cluster"] = "auto"
    library_id: str | None = None


class DatabaseConfig(StrictModel):
    """元数据库后端选择；其余后端（vector/storage/queue/locks）随阶段 2–4 加入。

    pg-single 为过渡测试形态：单主机单 API、媒体/向量/锁仍为本地实现，禁止多机写入
    （plans §4 运行形态表）。切换 backend 需重启进程，不做运行时热切换。
    """

    backend: Literal["sqlite", "postgres"] = "sqlite"
    postgres: PostgresConfig = Field(default_factory=PostgresConfig)


class UploadConfig(StrictModel):
    max_sessions: int = Field(default=256, ge=1, le=10000)
    pending_bytes: int = Field(default=100 * 1024**3, ge=1)
    part_bytes: int = Field(default=8 * 1024**2, ge=1, le=64 * 1024**2)
    ttl_seconds: int = Field(default=86400, ge=1, le=604800)


class Config(StrictModel):
    data_dir: Path = Path("data")
    uploads: UploadConfig = Field(default_factory=UploadConfig, exclude=True)
    source_upload_max_bytes: int = Field(default=20 * 1024**3, ge=1)
    runtime_overrides: bool = True
    enforce_gpu_lock: bool = False
    database: DatabaseConfig = Field(default_factory=DatabaseConfig, exclude=True)
    vector: VectorConfig = Field(default_factory=VectorConfig, exclude=True)
    queue: QueueConfig = Field(default_factory=QueueConfig, exclude=True)
    locks: LocksConfig = Field(default_factory=LocksConfig, exclude=True)
    gpu: GpuConfig = Field(default_factory=GpuConfig, exclude=True)
    storage: StorageConfig = Field(default_factory=StorageConfig, exclude=True)
    server: ServerConfig = Field(default_factory=ServerConfig, exclude=True)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    analyzer: AnalyzerConfig = Field(default_factory=AnalyzerConfig)
    person: PersonConfig = Field(default_factory=PersonConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    studio: StudioConfig = Field(default_factory=StudioConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    clean: CleanConfig = Field(default_factory=CleanConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    # 本机模型服务管理（桌面版缺省开启）。exclude=True：不进入任何 model_dump，任务快照、备份配置与指纹逐字节不变。
    local_services: LocalServicesConfig = Field(default_factory=LocalServicesConfig, exclude=True)
    # 选题挖掘；exclude=True 同上：不进入任务快照、备份配置与任何指纹。
    topics: TopicsConfig = Field(default_factory=TopicsConfig, exclude=True)

    @model_validator(mode="after")
    def animator_defaults_to_agent(self):
        """yaml 未写 ``studio.animator`` 时复制 ``agent.llm`` 的连接字段（含 api_key_env），其余取动画生成默认值。"""
        if "animator" not in self.studio.model_fields_set:
            llm = self.agent.llm
            inherited = AnimatorConfig(**{key: getattr(llm, key) for key in ANIMATOR_INHERITED})
            inherited._inherited = True
            self.studio.animator = inherited
            # 赋值会把 animator 记入 fields_set；移除以保持「未显式配置」的语义（序列化不受影响）。
            self.studio.model_fields_set.discard("animator")
        return self

    @property
    def layout_key(self) -> str:
        # 超时只影响请求等待，不影响切分结果；排除它使切分布局（及片段 ID）与加入 timeout 字段之前一致。
        return fingerprint(
            {"schema": 1, "detector": self.detector.model_dump(exclude={"timeout"}), "video": self.video.model_dump()}
        )

    @property
    def analysis_key(self) -> str:
        return fingerprint(
            {
                "prompt": 2,  # Chinese / English paired analysis; do not reuse monolingual checkpoints.
                "model": self.analyzer.model_dump(
                    exclude={"timeout", "api_key_env", "response_retries", "initial_output_tokens", "max_output_tokens", "thinking_mode"}
                ),
                "deployment": self.analyzer_deployment,
                "layout": self.layout_key,
            }
        )

    @property
    def analyzer_deployment(self):
        path = Path(self.analyzer.deployment_manifest or "")
        return (
            json.loads(path.read_text(encoding="utf-8")) if self.analyzer.backend == "llama_cpp" and path.is_file() else None
        )

    @property
    def asr_key(self) -> str:
        value = self.asr.model_dump(exclude={"timeout", "api_key_env", "max_tokens"})
        if self.asr.backend == "llama_cpp":
            path = Path(self.asr.deployment_manifest or "")
            value["deployment"] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else "missing"
        return fingerprint(value)

    @property
    def studio_key(self) -> str:
        """影响成片内容的创作配置指纹；任务创建时快照，供追溯与复现。"""
        return fingerprint(
            {
                "schema": 1,
                "generator": self.studio.generator.model_dump(exclude={"timeout", "api_key_env"}),
                "writer": self.studio.writer.model_dump(exclude={"timeout", "api_key_env"}),
                "tts": self.studio.tts.model_dump(exclude={"timeout", "api_key_env"}),
                "selection": self.studio.selection.model_dump(),
                "audio": self.studio.audio.model_dump(),
                "subtitle": self.studio.subtitle.model_dump(),
                "render": self.studio.render.model_dump(),
                # 视频生成只在启用时加键：未启用的配置指纹与旧版逐字节一致。
                **({"video_generator": self.studio.video_generator.signature()}
                   if self.studio.video_generator.enabled else {}),
                **({"image_generator": self.studio.image_generator.signature()}
                   if self.studio.image_generator.enabled else {}),
                **({"music_generator": self.studio.music_generator.signature()}
                   if self.studio.music_generator.enabled else {}),
            }
        )


def load_config(path: str | Path | None = None) -> Config:
    selected = Path(path or os.environ.get("MINDEX_CONFIG", "config.local.yaml"))
    if path and not selected.exists():
        raise FileNotFoundError(selected)
    values = (yaml.safe_load(selected.read_text(encoding="utf-8")) or {}) if selected.exists() else {}
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
    if config.studio.subtitle.font_file:
        config.studio.subtitle.font_file = str((base / Path(config.studio.subtitle.font_file).expanduser()).resolve())
    for service in config.local_services.services:
        if service.cwd:
            service.cwd = str((base / Path(service.cwd).expanduser()).resolve())
        if service.argv[0].startswith("~"):
            service.argv[0] = str(Path(service.argv[0]).expanduser())
    return config


def portable_parameters(config, value, reference=None):
    """Store local dependencies relative to the project, including pre-move snapshots.

    与当前参数（reference）比较历史参数时，请求超时 ``timeout`` 按当前值看待：超时只影响等待时长，
    不影响产物；调整缺省超时不应让历史版本失去复用。
    """
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            current = reference.get(key) if isinstance(reference, dict) else None
            if key == "timeout" and isinstance(reference, dict) and key in reference:
                result[key] = current
                continue
            if key in {"deployment_manifest", "font_file"} and item:
                path = Path(item)
                if path.is_absolute():
                    # A historical project root may no longer exist. Only accept the
                    # complete current relative path as a suffix, never just a basename.
                    if (
                        current
                        and not Path(current).is_absolute()
                        and ".." not in Path(current).parts
                        and path.as_posix().endswith("/" + current)
                    ):
                        item = current
                    else:
                        item = Path(os.path.relpath(path, config.data_dir.parent)).as_posix()
                result[key] = item
            else:
                result[key] = portable_parameters(config, item, current)
        return result
    return value


# 需要在 data_dir 移动/改名后仍然可用的路径字段。data_dir 本身与指向
# 数据目录内部的依赖文件（部署清单、字幕字体）都相对 data_dir.parent 存储；
# 指向数据目录之外的路径（如 person.model_root、外部清单）保持绝对，由目标机器补齐。
PORTABLE_PATH_KEYS = frozenset({"data_dir", "deployment_manifest", "font_file"})


def _relativize_one(data_dir: Path, item: str, current=None):
    """把单个绝对路径相对化到 data_dir.parent；外部路径保持绝对。

    与 portable_parameters 一致，优先沿用历史相对拼写（current），以免数据目录
    改名后快照指纹变化。只有同时位于 data_dir.parent 之内的绝对路径才被相对化。
    """
    base = data_dir.parent
    path = Path(item).expanduser()
    if not path.is_absolute():
        # 已经是相对路径，原样保留（可能是旧数据或已迁移数据）。
        return item
    # 历史项目根可能已不存在：只接受完整当前相对后缀，绝不只按文件名匹配。
    if (
        current
        and not Path(current).is_absolute()
        and ".." not in Path(current).parts
        and path.as_posix().endswith("/" + current)
    ):
        return current
    try:
        if path.is_relative_to(base):
            return path.relative_to(base).as_posix()
    except AttributeError:  # pragma: no cover - Python < 3.9 兼容
        pass
    # 位于数据目录之外：无法相对表达，保留绝对路径。
    return item


def relativize_config_paths(data_dir, value, reference=None):
    """递归把配置快照里的可移植路径字段相对化到 data_dir.parent。

    覆盖 data_dir / deployment_manifest / font_file；data_dir 外的路径保持绝对。
    reference 提供历史相对拼写以保持指纹稳定（见 _relativize_one）。
    """
    data_dir = Path(data_dir)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            current = reference.get(key) if isinstance(reference, dict) else None
            if key in PORTABLE_PATH_KEYS and isinstance(item, str) and item:
                result[key] = _relativize_one(data_dir, item, current)
            else:
                result[key] = relativize_config_paths(data_dir, item, current)
        return result
    if isinstance(value, list):
        return [relativize_config_paths(data_dir, item) for item in value]
    return value


def absolutize_config_paths(data_dir, value):
    """relativize_config_paths 的逆操作：把相对的可移植路径按当前 data_dir 还原为绝对。

    只有相对值会被重定位到 data_dir.parent 下；已是绝对的路径原样保留
    （例如 data_dir 外的 model_root 或外部清单）。"""
    data_dir = Path(data_dir)
    base = data_dir.parent
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in PORTABLE_PATH_KEYS and isinstance(item, str) and item:
                path = Path(item).expanduser()
                result[key] = item if path.is_absolute() else str((base / path).resolve())
            else:
                result[key] = absolutize_config_paths(data_dir, item)
        return result
    if isinstance(value, list):
        return [absolutize_config_paths(data_dir, item) for item in value]
    return value
