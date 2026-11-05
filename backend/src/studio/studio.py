"""创作任务状态机：任务即目录，状态即文件（同 e2e run.json 模式）。

五个步骤按依赖组成工作流：split → recall → tts → select → render，
其中 recall 与 tts 都只依赖 split，select 需要前两者都完成。
所有状态变更都原子落盘；重跑某一步只作废依赖它的下游步骤。
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from filelock import Timeout
from pydantic import Discriminator, Field, Tag, field_validator, model_serializer, model_validator

from mindex.admin.curation import EntityNotFound
from mindex.models.storyboard import coverage_warning
from mindex.shared import jsonio, lock_backend
from mindex.shared.config import Config, StrictModel, fingerprint, portable_parameters
from mindex.shared.trace import record as trace_record
from mindex.shared.types import Filters, Granularity
from mindex.shared.work_budget import copy_file

STEPS = ("split", "recall", "tts", "select", "render")
SPLIT_VERSION = 8  # 8: 创建语言决定 visual/query；7: 具体意象单独成镜、抽象列举不拆、最短长度、visual 字段与召回词公共规则
STEP_LABELS = {
    "split": "文本拆分镜",
    "recall": "语义召回",
    "tts": "生成配音",
    "select": "选片对齐",
    "render": "渲染合成",
}
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "split": (),
    "recall": ("split",),
    "tts": ("split",),
    "select": ("recall", "tts"),
    "render": ("select",),
}
_JOB_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")  # YYYYMMDD-HHMMSS-hex6
VISUAL_LIMIT = 120  # 分镜画面意象上限（按 Unicode 字符计）


def normalize_visual(value: str | None) -> str | None:
    """去除首尾空白；None、空串或纯空白返回 None（表示未填写）。"""
    if value is None:
        return None
    visual = value.strip()
    return visual or None


def clip_visual(value: str | None) -> tuple[str | None, bool]:
    """模型输出用：规范化后截到前 VISUAL_LIMIT 个字符，返回 (结果, 是否发生截断)。"""
    visual = normalize_visual(value)
    if visual is None or len(visual) <= VISUAL_LIMIT:
        return visual, False
    return visual[:VISUAL_LIMIT], True


def clipped_visual(index: int, value: str | None) -> str | None:
    """拆分产物用：截断超长 visual 并记录 split-visual-truncated 诊断，不让拆分失败。"""
    visual, truncated = clip_visual(value)
    if truncated:
        length = len(normalize_visual(value) or "")
        trace_record(
            "split-visual-truncated",
            {
                "index": index,
                "length": length,
                "message": f"第 {index} 个分镜的画面意象 {length} 字，超过 {VISUAL_LIMIT} 字上限，已截断",
            },
        )
    return visual


def visual_error(shots) -> str | None:
    """人工/智能助手提交用：返回第一个超长分镜的中文错误，全部合法时返回 None。

    shots 可以是模型对象（读 .visual）或字典（读 ["visual"]）。
    """
    for index, shot in enumerate(shots, 1):
        raw = shot.get("visual") if isinstance(shot, dict) else getattr(shot, "visual", None)
        visual = normalize_visual(raw) if isinstance(raw, str) else None
        if visual is not None and len(visual) > VISUAL_LIMIT:
            return f"第 {index} 个分镜的画面意象超过 {VISUAL_LIMIT} 字上限（当前 {len(visual)} 字）"
    return None


class StudioError(ValueError):
    """用户可见的创作工作流错误（中文消息，映射为 422）。"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def save(path: Path, value, *, config=None):
    """原子写入 JSON；实现见 ``shared/jsonio``。"""
    if config is not None and config.storage.backend == "s3":
        from mindex.shared import files
        from mindex.shared.storage import Store
        files.save_bytes(Store.open(config), path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
    else:
        jsonio.save(path, value)


def save_bytes(path: Path, payload: bytes):
    """原子写入字节；实现见 ``shared/jsonio``。"""
    jsonio.save_bytes(path, payload)


# ---------------------------------------------------------------- 数据模型


class ShotRecall(StrictModel):
    """分镜级召回设置：复用检索页四类召回能力（文字+人物 / 图片找视频 / 视频找相似）。"""

    mode: Literal["text", "image", "video"] = "text"
    person_ids: list[str] = Field(default_factory=list, max_length=30)
    person_mode: Literal["auto", "onscreen", "mentioned"] = "auto"
    person_relation: Literal["all", "any"] = "all"
    filters: Filters = Field(default_factory=Filters)
    media_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")  # 查询文件 sha256
    media_kind: Literal["image", "video"] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.mode == "text":
            if self.media_id is not None or self.media_kind is not None:
                raise ValueError("文字召回不能携带查询文件")
        else:
            if not self.media_id or self.media_kind != self.mode:
                raise ValueError("图片/视频召回必须携带对应类型的查询文件")
            if self.person_ids or self.person_mode != "auto" or self.person_relation != "all":
                raise ValueError("图片/视频召回不支持人物设置")
        return self


MEDIA_ID = r"^[0-9a-f]{64}$"  # 任务 inputs/ 内容寻址文件的 sha256
IMAGE_MOTIONS = ("zoom-in", "zoom-out", "pan-left", "pan-right", "none")


class RecallSource(StrictModel):
    """素材召回（缺省来源）：召回 → 选片对齐 → 渲染。"""

    kind: Literal["recall"] = "recall"


class ClipSource(StrictModel):
    """上传片段：在片段内按配音时长取定长窗口；``start`` 为人工起点（秒），None = 从头开始。"""

    kind: Literal["clip"]
    media_id: str = Field(pattern=MEDIA_ID)
    start: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ImageSource(StrictModel):
    """静态图片：按 motion 推拉/平移生成与配音等长的画面；strength 控制幅度（放大到 1 + strength/2）。"""

    kind: Literal["image"]
    media_id: str = Field(pattern=MEDIA_ID)
    motion: Literal["zoom-in", "zoom-out", "pan-left", "pan-right", "none"] = "zoom-in"
    strength: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)


AIGC_PROMPT_LIMIT = 1000
# MiniMax H3（V2 多模态）专用的逐镜生成选项；其余视频生成后端只支持 text / first_frame。
VIDEO_MODES = Literal["text", "first_frame", "last_frame", "first_last", "reference"]
VIDEO_MODELS = Literal["MiniMax-H3", "MiniMax-H3-Highspeed", "MiniMax-H3-Lite", "MiniMax-H3-Max"]
VIDEO_RESOLUTIONS = Literal["480P", "768P", "2K"]
VIDEO_RATIOS = Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
# 新增字段为 None 时 dump 省略：老 aigc 分镜再次保存时 content 不变。
AIGC_OPTIONAL = ("mode", "last_image_id", "references", "model", "resolution", "ratio", "duration")


class MediaReference(StrictModel):
    """多模态参考素材：任务 ``inputs/`` 内的图片、视频或音频（sha256），经 query-media purpose=source 上传。"""

    kind: Literal["image", "video", "audio"]
    media_id: str = Field(pattern=MEDIA_ID)


class AigcSource(StrictModel):
    """AI 生成视频：generate 阶段按提示词（缺省用画面意象，再缺省用召回查询）生成片段，再在片段内选窗对齐。

    ``image_id`` 是首帧参考图（任务 ``inputs/`` 内的图片 sha256，经 query-media purpose=source 上传）。
    生成方式 ``mode``：text 文生视频、first_frame 首帧图生视频、last_frame 尾帧图生视频、first_last 首尾帧生视频、
    reference 主体（多模态）参考生视频；None = 有首帧图时为 first_frame，否则 text。尾帧图 ``last_image_id``、
    参考素材 ``references``（≤9 图、≤3 视频、≤3 音频）、模型档位、分辨率、比例与时长（None = 按配音时长取整）
    只对 MiniMax H3 生效。"""

    kind: Literal["aigc"]
    prompt: str | None = Field(default=None, max_length=AIGC_PROMPT_LIMIT)
    negative: str | None = Field(default=None, max_length=500)
    image_id: str | None = Field(default=None, pattern=MEDIA_ID)
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    mode: VIDEO_MODES | None = None
    last_image_id: str | None = Field(default=None, pattern=MEDIA_ID)
    references: list[MediaReference] | None = Field(default=None, max_length=12)
    model: VIDEO_MODELS | None = None
    resolution: VIDEO_RESOLUTIONS | None = None
    ratio: VIDEO_RATIOS | None = None
    duration: int | None = Field(default=None, ge=4, le=15)

    @field_validator("prompt", "negative", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("references", mode="after")
    @classmethod
    def _reference_limits(cls, value):
        if not value:
            return None
        counts = {kind: sum(1 for item in value if item.kind == kind) for kind in ("image", "video", "audio")}
        if counts["image"] > 9 or counts["video"] > 3 or counts["audio"] > 3:
            raise ValueError("参考素材最多 9 张图片、3 段视频、3 段音频")
        return value

    @model_validator(mode="after")
    def _mode_inputs(self):
        mode = self.effective_mode
        if mode in ("first_frame", "first_last") and not self.image_id:
            raise ValueError("首帧/首尾帧生视频需要上传首帧图片")
        if mode in ("last_frame", "first_last") and not self.last_image_id:
            raise ValueError("尾帧/首尾帧生视频需要上传尾帧图片")
        if mode == "reference" and not self.references:
            raise ValueError("主体参考生视频至少需要一个参考图片或视频")
        if mode == "reference" and all(item.kind == "audio" for item in self.references or []):
            raise ValueError("主体参考生视频不能只有参考音频，请至少加入一张参考图或一段参考视频")
        if self.model == "MiniMax-H3-Max" and self.duration == 4:
            raise ValueError("MiniMax-H3-Max 的时长为 5–15 秒")
        if self.resolution == "2K" and self.model not in (None, "MiniMax-H3"):
            raise ValueError("只有 MiniMax-H3 支持 2K 分辨率")
        if self.resolution == "480P" and self.model in (None, "MiniMax-H3", "MiniMax-H3-Highspeed"):
            raise ValueError("480P 只适用于 MiniMax-H3-Lite 与 MiniMax-H3-Max")
        return self

    @property
    def effective_mode(self) -> str:
        if self.mode:
            return self.mode
        return "first_frame" if self.image_id else "text"

    @model_serializer(mode="wrap")
    def _omit_new_defaults(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for key in AIGC_OPTIONAL:
                if data.get(key) is None:
                    data.pop(key, None)
        return data


IMAGE_PROMPT_LIMIT = 1500


class AiImageSource(StrictModel):
    """AI 生成图片：draw 阶段调用图片生成模型（MiniMax image-01，文生图；给出 ``reference_id`` 时为主体参考图生图）
    按提示词（缺省用画面意象，再缺省用召回查询）生成一张图片，之后与静态图片相同：按 motion/strength 推拉/平移渲染。"""

    kind: Literal["aigc_image"]
    prompt: str | None = Field(default=None, max_length=IMAGE_PROMPT_LIMIT)
    reference_id: str | None = Field(default=None, pattern=MEDIA_ID)
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    prompt_optimizer: bool = False
    motion: Literal["zoom-in", "zoom-out", "pan-left", "pan-right", "none"] = "zoom-in"
    strength: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)

    @field_validator("prompt", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


ANIMATION_BRIEF_LIMIT = 2000


class AnimationSource(StrictModel):
    """AI 动画：animate 阶段由动画生成 LLM 按画面说明写 HTML/JS 动画，render 阶段在沙箱里逐帧录制。

    ``brief`` 为画面说明（≤2000 字）；None = 用画面意象，再缺省用旁白。动画源码可在创作页人工修改（origin=manual）。"""

    kind: Literal["animation"]
    brief: str | None = Field(default=None, max_length=ANIMATION_BRIEF_LIMIT)

    @field_validator("brief", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


TOOL_SOURCE_ID = r"^[a-z0-9][a-z0-9-]{0,39}$"
TOOL_NAME = r"^[A-Za-z0-9_.:/-]{1,128}$"
TOOL_ARGUMENTS_LIMIT = 8000


class ToolShotSource(StrictModel):
    """第三方工具：invoke 阶段调用登记的第三方工具（``tool_id`` 为工具源 ID，``tool`` 为白名单中的工具名），
    产物为视频（之后选片对齐、渲染）或图片（直接按运动参数渲染）。

    ``arguments`` 为传给工具的 JSON 参数（≤8000 字符），字符串值里的 ``{{text}}``/``{{visual}}``/``{{query}}``
    替换为本镜旁白、画面意象与查询；``uploads`` 为随调用上传的任务素材（需要该工具勾选 allow_upload）。"""

    kind: Literal["tool"]
    tool_id: str = Field(pattern=TOOL_SOURCE_ID)
    tool: str = Field(pattern=TOOL_NAME)
    arguments: dict[str, Any] = Field(default_factory=dict)
    uploads: list[MediaReference] | None = Field(default=None, max_length=4)
    output: Literal["video", "image"] = "video"
    motion: Literal["zoom-in", "zoom-out", "pan-left", "pan-right", "none"] = "zoom-in"
    strength: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)

    @field_validator("arguments", mode="after")
    @classmethod
    def _arguments_size(cls, value):
        if len(json.dumps(value, ensure_ascii=False)) > TOOL_ARGUMENTS_LIMIT:
            raise ValueError(f"工具参数不能超过 {TOOL_ARGUMENTS_LIMIT} 字符")
        return value

    @field_validator("uploads", mode="after")
    @classmethod
    def _empty_uploads(cls, value):
        return value or None

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        data = handler(self)
        if self.uploads is None:
            data.pop("uploads", None)
        if self.output == "video":  # 运动参数只对图片产物生效
            data.pop("motion", None)
            data.pop("strength", None)
        return data


ShotSource = Annotated[
    RecallSource | ClipSource | ImageSource | AiImageSource | AigcSource | AnimationSource | ToolShotSource,
    Field(discriminator="kind"),
]

MUSIC_PROMPT_LIMIT = 2000
MUSIC_LYRICS_LIMIT = 3500


class ShotMusic(StrictModel):
    """背景音乐提示点：从这个分镜开始换成新的背景音乐（或停止），之后的分镜顺延，直到下一个提示点。

    kind：generate 由音乐生成模型（music-3.0）按描述生成（缺省纯音乐；给出歌词或关闭纯音乐时为带人声歌曲）；
    cover 按参考音频（``audio_id``，inputs 内的音频 sha256）翻唱；library 选用曲库曲目（``track_id``，曲目内容 sha256）；
    none 从此分镜起不再有背景音乐。``volume`` 为混音音量（0–1），配音出现时背景音乐自动压低。
    ``track_id`` 为 None 时 dump 省略，旧提示点数据形态不变。"""

    kind: Literal["generate", "cover", "library", "none"]
    prompt: str | None = Field(default=None, max_length=MUSIC_PROMPT_LIMIT)
    lyrics: str | None = Field(default=None, max_length=MUSIC_LYRICS_LIMIT)
    instrumental: bool = True
    audio_id: str | None = Field(default=None, pattern=MEDIA_ID)
    track_id: str | None = Field(default=None, pattern=MEDIA_ID)
    volume: float = Field(default=0.25, ge=0, le=1, allow_inf_nan=False)

    @field_validator("prompt", "lyrics", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _inputs(self):
        if self.kind == "generate" and not self.prompt and not self.lyrics:
            raise ValueError("生成背景音乐需要填写音乐描述或歌词")
        if self.kind == "cover":
            if not self.audio_id:
                raise ValueError("翻唱需要上传参考音频")
            if not self.prompt or not 10 <= len(self.prompt) <= 300:
                raise ValueError("翻唱需要 10–300 字的目标风格描述")
        if self.kind == "library" and not self.track_id:
            raise ValueError("选用曲库音乐需要曲目 ID（track_id）")
        if self.kind != "library" and self.track_id:
            raise ValueError("只有曲库音乐（kind=library）可以填写 track_id")
        return self

    @model_serializer(mode="wrap")
    def _omit_track(self, handler):
        data = handler(self)
        if self.track_id is None:
            data.pop("track_id", None)
        return data


# 镜内转场：只作用于本镜开头（enter）或结尾（leave），不改变镜头时长。
EDGE_KINDS = Literal["fade", "white", "zoom", "slide"]
# 重叠转场（ffmpeg xfade 的 transition 取值子集）：本镜与下一镜交叉过渡，由本镜多渲染的尾部余量承担。
OVERLAP_KINDS = Literal["fade", "dissolve", "fadeblack", "fadewhite", "wipeleft", "wiperight", "slideleft",
                        "slideright", "circleopen", "circleclose", "smoothleft", "smoothright"]


class TransitionEdge(StrictModel):
    """镜内转场：fade 黑场淡入/淡出；white 白场；zoom 缩放并淡入/淡出；slide 从右滑入 / 向左滑出（黑底）。"""

    kind: EDGE_KINDS
    duration: float = Field(default=0.5, ge=0.1, le=2, allow_inf_nan=False)


class OverlapTransition(StrictModel):
    """本镜与下一镜之间的重叠转场：成片时长与时间线不变（本镜多渲染 ``duration`` 秒的尾部画面供过渡消耗）。"""

    kind: OVERLAP_KINDS = "fade"
    duration: float = Field(default=0.5, ge=0.1, le=2, allow_inf_nan=False)


class ShotTransition(StrictModel):
    """分镜转场：enter/leave 为镜内转场，next 为通往下一镜的重叠转场；三者为 None 时 dump 省略。"""

    enter: TransitionEdge | None = None
    leave: TransitionEdge | None = None
    next: OverlapTransition | None = None

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for key in ("enter", "leave", "next"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data

    @property
    def empty(self) -> bool:
        return self.enter is None and self.leave is None and self.next is None


HIGHLIGHT_LIMIT = 10
HIGHLIGHT_CHARS = 20
OVERLAY_LIMIT = 4


class CardItem(StrictModel):
    """数据卡片的一行：标签与数值（数值以强调色显示）。"""

    label: str = Field(min_length=1, max_length=20)
    value: str = Field(min_length=1, max_length=20)


class ShotOverlay(StrictModel):
    """文字层：叠加在本镜画面上的标题卡、下三分之一信息条或数据卡片。

    ``engine``：ass 由字幕引擎在成片时烧录（轻量，文字与简单入场）；html 由叠加层阶段生成透明 HTML 动画、
    在分镜渲染时叠加（效果更丰富，需要动画录制组件）。custom 只用于 html，按 ``brief`` 由动画生成模型编写。
    ``start``/``end`` 为相对本镜开头的秒数，None = 从头 / 到尾。"""

    engine: Literal["ass", "html"] = "ass"
    kind: Literal["title", "lower_third", "data_card", "custom"]
    title: str | None = Field(default=None, max_length=60)
    subtitle: str | None = Field(default=None, max_length=80)
    items: list[CardItem] | None = Field(default=None, max_length=6)
    brief: str | None = Field(default=None, max_length=1000)
    start: float | None = Field(default=None, ge=0, le=60, allow_inf_nan=False)
    end: float | None = Field(default=None, gt=0, le=60, allow_inf_nan=False)
    effect: Literal["none", "fade", "slide"] = "fade"

    @field_validator("title", "subtitle", "brief", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @model_validator(mode="after")
    def _inputs(self):
        if self.kind in ("title", "lower_third") and not self.title:
            raise ValueError("标题卡与信息条需要填写标题")
        if self.kind == "data_card" and not self.items:
            raise ValueError("数据卡片至少需要一行数据")
        if self.kind == "custom" and (self.engine != "html" or not self.brief):
            raise ValueError("自定义文字层只能用 HTML 方式，并需要填写画面说明")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("文字层结束时间必须晚于开始时间")
        return self

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for key in ("title", "subtitle", "items", "brief", "start", "end"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data


def normalize_highlights(value) -> list[str] | None:
    """分镜高亮词：去空白、去重、保持顺序；空列表为 None（不落键）。"""
    if value is None:
        return None
    if isinstance(value, str):
        value = re.split(r"[,，、\s]+", value)
    if not isinstance(value, (list, tuple)):
        raise ValueError("高亮词必须是字符串列表")
    words = []
    for item in value:
        word = str(item).strip()
        if word and word not in words:
            words.append(word)
    if len(words) > HIGHLIGHT_LIMIT:
        raise ValueError(f"高亮词最多 {HIGHLIGHT_LIMIT} 个")
    if any(len(word) > HIGHLIGHT_CHARS for word in words):
        raise ValueError(f"每个高亮词不超过 {HIGHLIGHT_CHARS} 字")
    return words or None


class Shot(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
    text: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=400)
    estimated_seconds: float = Field(gt=0, le=60)
    recall: ShotRecall | None = None  # 缺省 = 文字召回（旧 content 无此键）
    # 分镜级召回覆盖：None = 跟随方案（粒度取服务配置，数量取方案 recall_limit）。
    granularity: Granularity | None = None
    recall_limit: int | None = Field(default=None, ge=1, le=50)
    # 画面意象：一句中文（≤120 字）；不参与任何签名。None/空白 = 未填写，dump 时省略该键，
    # 使未含 visual 的老分镜再次保存时 content 版本号不变。
    visual: str | None = None
    # 画面来源：None = 素材召回（缺省）。与 visual 一样为 None 时 dump 省略该键，旧 content 版本号不变；
    # 显式的 {"kind": "recall"} 规范化为 None。
    source: ShotSource | None = None
    # 背景音乐提示点：None = 沿用前面分镜的背景音乐（没有则无背景音乐）；None 时 dump 省略，旧 content 不变。
    music: ShotMusic | None = None
    # 转场：None（或三项皆空）= 硬切；None 时 dump 省略，旧 content 不变。
    transition: ShotTransition | None = None
    # 字幕高亮词（烧录 ASS 字幕时变色）：None/空 = 无，dump 省略。
    highlights: list[str] | None = None
    # 文字层（标题卡、信息条、数据卡片）：None/空 = 无，dump 省略。
    overlays: list[ShotOverlay] | None = Field(default=None, max_length=OVERLAY_LIMIT)

    @field_validator("overlays", mode="after")
    @classmethod
    def _empty_overlays(cls, value):
        return value or None

    @field_validator("highlights", mode="before")
    @classmethod
    def _normalize_highlights(cls, value):
        return normalize_highlights(value)

    @field_validator("transition", mode="after")
    @classmethod
    def _empty_transition(cls, value):
        return None if value is not None and value.empty else value

    @field_validator("visual", mode="before")
    @classmethod
    def _normalize_visual(cls, value):
        if value is None or not isinstance(value, str):
            return value
        visual = normalize_visual(value)
        if visual is not None and len(visual) > VISUAL_LIMIT:
            raise ValueError(f"画面意象超过 {VISUAL_LIMIT} 字上限（当前 {len(visual)} 字）")
        return visual

    @field_validator("source", mode="after")
    @classmethod
    def _recall_is_default(cls, value):
        return None if isinstance(value, RecallSource) else value

    @model_serializer(mode="wrap")
    def _omit_empty_visual(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for key in ("visual", "source", "music", "transition", "highlights", "overlays"):
                if data.get(key) is None:
                    data.pop(key, None)
        return data

    @property
    def source_kind(self) -> str:
        return self.source.kind if self.source else "recall"


class Storyboard(StrictModel):
    job_id: str = Field(min_length=1)
    shots: list[Shot] = Field(min_length=1)
    coverage_warning: str | None = None

    @model_validator(mode="after")
    def unique_ids(self):
        if len({shot.id for shot in self.shots}) != len(self.shots):
            raise ValueError("分镜 id 不能重复")
        return self


class TimelineAudio(StrictModel):
    file: str = Field(min_length=1)
    duration: float = Field(gt=0)
    atempo: float = Field(gt=0)
    render_duration: float = Field(gt=0)


ALIGNMENTS = Literal[
    "nearest",
    "extended",
    "atempo",
    "extended+atempo",
    "padded",
    "trimmed",
    "extended+padded",
    "extended+trimmed",
    "manual",
]


class LibraryVideo(StrictModel):
    """素材库区间（缺省形态）：旧数据没有 kind；dump 时省略 kind，保证旧选片版本逐字节不变。"""

    kind: Literal["library"] = "library"
    source_id: str = Field(min_length=1)
    source_name: str = ""
    segment_id: str = Field(min_length=1)
    segment_ids: list[str] = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    score: float = 0.0
    rank: int = 0
    alignment: ALIGNMENTS

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("视频区间 end 必须大于 start")
        return self

    @model_serializer(mode="wrap")
    def _omit_kind(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            data.pop("kind", None)
        return data


class FileRef(StrictModel):
    """文件区间引用：任务 ``inputs/`` 的上传文件（``input`` = 文件名，``<sha256>.<suffix>``），
    或某个版本的产物文件（``version`` + ``file``）。"""

    input: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}\.[a-z0-9]{1,8}$")
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    file: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_target(self):
        if (self.input is None) == (self.version is None) or (self.version is None) != (self.file is None):
            raise ValueError("文件引用必须是 input，或 version + file 之一")
        return self

    @model_serializer(mode="wrap")
    def _omit_none(self, handler):
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None} if isinstance(data, dict) else data


class FileVideo(StrictModel):
    """文件区间：画面来自上传片段、图片生成的画面等文件，start/end 是文件内的秒数。"""

    kind: Literal["file"]
    ref: FileRef
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    alignment: ALIGNMENTS

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("视频区间 end 必须大于 start")
        return self

    @property
    def media_id(self) -> str | None:
        return self.ref.input.split(".")[0] if self.ref.input else None


def _video_kind(value):
    if isinstance(value, dict):
        return value.get("kind", "library")
    return getattr(value, "kind", "library")


TimelineVideo = Annotated[
    Annotated[LibraryVideo, Tag("library")] | Annotated[FileVideo, Tag("file")],
    Discriminator(_video_kind),
]


class TimelineSubtitle(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("字幕区间 end 必须大于 start")
        return self


class TimelineEvent(StrictModel):
    shot_id: str = Field(min_length=1)
    order: int = Field(ge=1)
    audio: TimelineAudio
    video: TimelineVideo
    subtitle: TimelineSubtitle
    warning: str | None = None

    @model_validator(mode="after")
    def subtitle_within_event(self):
        if self.subtitle.end > self.audio.render_duration + 1e-6:
            raise ValueError("字幕结束时间不能超过分镜时长")
        return self


class Timeline(StrictModel):
    job_id: str = Field(min_length=1)
    mode: Literal["auto", "manual"]
    audio_mode: Literal["tts_only", "ducking"]
    burn: bool
    events: list[TimelineEvent] = Field(min_length=1)

    @model_validator(mode="after")
    def sequential(self):
        if [event.order for event in self.events] != list(range(1, len(self.events) + 1)):
            raise ValueError("时间线事件 order 必须是 1..n 连续编号")
        if len({event.shot_id for event in self.events}) != len(self.events):
            raise ValueError("时间线事件的 shot_id 不能重复")
        return self

    @property
    def total_seconds(self) -> float:
        return sum(event.audio.render_duration for event in self.events)


class JobOptions(StrictModel):
    voice: str | None = Field(default=None, max_length=100)
    audio_mode: Literal["tts_only", "ducking"] | None = None
    recall_limit: int | None = Field(default=None, ge=1, le=50)
    burn: bool | None = None
    source_ids: list[str] | None = Field(default=None, max_length=200)
    source_group_ids: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def known_sources(self):
        # None = 不限（全选）；空列表没有合理语义，直接拒绝而不是当成全库。
        if self.source_ids is None:
            return self
        if not self.source_ids:
            raise ValueError("请至少选择一个素材")
        if any(not value for value in self.source_ids):
            raise ValueError("素材 ID 不能为空")
        return self


# ---------------------------------------------------------------- 路径与目录


def studio_root(config: Config) -> Path:
    return config.data_dir / "studio"


def job_dir(config: Config, job_id: str) -> Path:
    _validate_job_id(job_id)
    directory = studio_root(config) / job_id
    if not directory.resolve().is_relative_to(studio_root(config).resolve()):
        raise EntityNotFound("找不到创作任务")
    return directory


def job_path(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "job.json"


def lock_path(config: Config) -> Path:
    return studio_root(config) / "studio.lock"


def storyboard_path(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "storyboard.json"


def timeline_path(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "timeline.json"


def candidates_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "candidates"


def audio_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "audio"


def render_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "render"


def attempts_dir(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "attempts"


def _validate_job_id(job_id: str):
    if not _JOB_ID.match(job_id):
        raise EntityNotFound("找不到创作任务")


def _read_json(path: Path, missing_message: str) -> dict:
    if not path.is_file():
        raise EntityNotFound(missing_message)
    return json.loads(path.read_text(encoding="utf-8"))


def _write_model(path: Path, model, **extra):
    save(path, {"schema": 1, **model.model_dump(), **extra})


def _read_model(path: Path, model_cls, missing_message: str):
    data = _read_json(path, missing_message)
    if data.get("schema") != 1:
        raise StudioError(f"{path.name} 的 schema 版本不受支持")
    envelope = {"schema", "updated_at"}
    return model_cls.model_validate({k: v for k, v in data.items() if k not in envelope})


# ---------------------------------------------------------------- 任务生命周期


def fresh_step() -> dict:
    return {
        "status": "pending",
        "attempt": None,
        "started_at": None,
        "finished_at": None,
        "elapsed_ms": None,
        "error": None,
        "error_type": None,
    }


def invalidation_targets(step: str) -> tuple[str, ...]:
    """依赖传递闭包内的下游步骤；重跑 step 时它们全部作废。"""
    targets: set[str] = set()
    changed = True
    while changed:
        changed = False
        for other in STEPS:
            if other in targets or other == step:
                continue
            if set(DEPENDENCIES[other]) & ({step} | targets):
                targets.add(other)
                changed = True
    return tuple(s for s in STEPS if s in targets)


def _summary(state: dict) -> dict:
    steps = {step: info["status"] for step, info in state["steps"].items()}
    current = next((s for s in STEPS if steps[s] != "complete"), "done")
    return {
        "id": state["id"],
        "title": state["title"],
        "title_pending": state.get("title_pending", False),
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "steps": steps,
        "current_step": current,
    }


def scope_status(state: dict, store) -> dict:
    """用于页面、候选和签名的最新任务范围；失效任务仍可打开并重新选择。"""
    ids = state.get("source_ids")
    filters = Filters(source_ids=ids or [], source_group_ids=state.get("source_group_ids") or [])
    try:
        scope = filters.effective_sources(store)
        error = "所选素材组与素材范围没有可用成员，请重新选择" if scope == set() else None
        return {"source_ids": sorted(scope) if scope is not None else None, "error": error}
    except ValueError as exc:
        return {"source_ids": [], "error": str(exc)}


def material_scope(state: dict, store=None) -> set[str] | None:
    """动态组范围与手动素材范围取交集，失效引用或空范围明确报错。"""
    if not state.get("source_group_ids"):
        ids = state.get("source_ids")
        return set(ids) if ids else None
    if store is None:
        raise StudioError("解析素材组需要素材数据库")
    status = scope_status(state, store)
    if status["error"]:
        raise StudioError(status["error"])
    return set(status["source_ids"]) if status["source_ids"] is not None else None


def scoped_filters(filters: Filters, scope: set[str] | None) -> Filters:
    """任务范围是召回的硬上限：分镜限定素材时取交集，未限定时直接用任务范围。

    空交集说明分镜选中的素材全部被排除在任务范围之外，直接报错而不是退化成「不限」——
    Filters 的空选择表示全库，静默放宽会让任务范围失效。
    """
    if scope is None:
        return filters
    if not scope:
        raise StudioError("素材范围没有可用成员，请重新选择")
    picked = filters.selected_sources()
    if picked and not picked & scope:
        raise StudioError("分镜素材范围与本任务允许的素材范围没有交集，请调整分镜的素材范围")
    values = filters.model_dump()
    values["source_id"] = None
    values["source_ids"] = sorted(picked & scope) if picked else sorted(scope)
    return Filters.model_validate(values)


def create_job(config: Config, text: str, title: str | None = None, options: dict | None = None,
               *, store=None, defer_title: bool = False, original_input: str | None = None, language: str = "zh") -> dict:
    """创建创作任务。``text`` 是视频大纲，文案在任务内由文案创作模型生成。

    标题生成是一次阻塞的 LLM 往返，会显著拖慢创建的感知延迟。调用方传入
    ``defer_title=True`` 时，这里只写占位标题并标记 ``title_pending``，由上层在后台
    线程里补全真实标题；不需要延迟（如 CLI、测试）时留空即用占位标题。

    ``original_input`` 是用户的原始输入（智能助手创建时为用户原话，``text`` 是助手整理后的大纲）；
    省略时即 ``text``。只用于任务元信息展示，不参与文案生成与签名。
    """
    if language not in {"zh", "en"}:
        raise StudioError("Unsupported job language")
    text = (text or "").strip()
    if not text:
        raise StudioError("请输入创作视频大纲")
    if len(text) > config.studio.max_input_chars:
        raise StudioError(f"大纲超过 {config.studio.max_input_chars} 字上限，请精简后再试")
    try:
        parsed = JobOptions.model_validate(options or {})
    except ValueError as exc:
        raise StudioError(f"任务选项无效：{exc}") from exc
    if store is None:
        from mindex.shared.storage import Store

        store = Store.open(config)
    if parsed.source_group_ids:
        material_scope(parsed.model_dump(), store)
    known = store.source_ids() if parsed.source_ids else set()
    unknown = [value for value in parsed.source_ids or [] if value not in known]
    if unknown:
        raise StudioError(f"素材不存在：{'、'.join(unknown[:3])}")
    active = store.active_index()
    # 未指定标题且请求延迟生成时，占位标题先行，真实标题由后台补全。
    title_pending = not (title or "").strip() and defer_title
    job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}"
    directory = job_dir(config, job_id)
    if directory.exists():
        raise StudioError("任务目录冲突，请重试")
    state = {
        "schema": 1,
        "id": job_id,
        "title": (title or text[:20]).strip()[:60] or "未命名创作",
        "title_pending": title_pending,
        # 新任务只保存大纲；旧任务的 input_text 即文案，迁移为方案的 legacy 文案版本。
        "outline": text,
        "original_input": (original_input or "").strip() or text,
        "created_at": now(),
        "updated_at": now(),
        "voice_mode": "custom" if parsed.voice else "inherit",
        # 素材范围是任务级约束，独立于方案参数，通过 material-scope 接口编辑。
        "source_ids": sorted(set(parsed.source_ids)) if parsed.source_ids else None,
        **({"source_group_ids": sorted(set(parsed.source_group_ids))} if parsed.source_group_ids else {}),
        "options": {
            "voice": parsed.voice or config.studio.tts.voice,
            "audio_mode": parsed.audio_mode or config.studio.audio.mode,
            "recall_limit": parsed.recall_limit or config.studio.recall.limit,
            "burn": config.studio.subtitle.burn if parsed.burn is None else parsed.burn,
        },
        "config_snapshot": {
            "studio_key": config.studio_key,
            "generator_model": config.studio.generator.model,
            "writer_model": config.studio.writer.model,
            "tts_model": config.studio.tts.model,
            "index_id": active["id"] if active else None,
        },
        "steps": {step: fresh_step() for step in STEPS},
        "history": [],
        "execution": {"status": "idle", "current_step": None, "remaining_steps": []},
        "artifact_version": 1,
    }
    from mindex.shared.storage import Store
    from mindex.studio.delivery_templates import apply, default_snapshot
    state["language"] = language
    state["options"] = apply(state["options"], default_snapshot(store or Store.open(config)))
    save(job_path(config, job_id), state, config=config)
    return state


def load_job(config: Config, job_id: str) -> dict:
    if config.storage.backend == "s3":
        from mindex.shared import files
        from mindex.shared.storage import Store
        payload = files.read_bytes(Store.open(config), job_path(config, job_id))
        if payload is None:
            raise EntityNotFound("找不到创作任务")
    state = _read_json(job_path(config, job_id), "找不到创作任务")
    if state.get("schema") != 1:
        raise StudioError("任务状态 schema 版本不受支持")
    state.setdefault("execution", {"status": "idle", "current_step": None, "remaining_steps": []})
    state.setdefault("source_ids", None)  # 老任务没有素材范围，即不限
    state.setdefault("title_pending", False)  # 老任务标题早已确定，不处于待生成态
    state.setdefault("outline", "")  # 老任务没有大纲，input_text 本身就是文案
    state.setdefault("topic", None)  # 不是从选题创建的任务（含老任务）没有选题来源与档案快照
    return state


def list_jobs(config: Config, *, recover: bool = False) -> list[dict]:
    """列出任务摘要。

    ``recover=True`` 时顺带做崩溃恢复：以前这是 ``recover_stale`` 独立再扫一遍任务
    目录（读完每个 job.json 后 ``list_jobs`` 又全量读一次），现在合并成一次扫描。
    只有拿到空闲锁时才会改写被中断的 running 状态，语义与原先一致。
    """
    root = studio_root(config)
    if config.storage.backend == "s3":
        from mindex.shared import files
        from mindex.shared.storage import Store
        store = Store.open(config)
        for row in files.entries(store, "studio/"):
            if row["path"].endswith("/job.json"):
                files.hydrate(store, store.root / row["path"])
    if not root.is_dir():
        return []
    lock = None
    if recover and config.queue.backend != "postgres":
        lock = lock_backend.named(config, "studio")
        try:
            lock.acquire()
        except Timeout:
            lock = None  # 有步骤在执行，不动状态；仍照常列出任务
    try:
        summaries = []
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            try:
                state = load_job(config, directory.name)
            except (EntityNotFound, StudioError, ValueError):
                continue
            if lock is not None:
                running = [s for s, v in state.get("steps", {}).items() if v.get("status") == "running"]
                if running or state.get("execution", {}).get("status") == "running":
                    for step in running:
                        fail_step(config, directory.name, step,
                                  StudioError("服务重启导致步骤中断，请重跑该步骤"))
                    set_execution(config, directory.name, "failed", None, [])
                    state = load_job(config, directory.name)  # 恢复后重读，摘要反映新状态
            summaries.append(_summary(state))
        return sorted(summaries, key=lambda item: item["created_at"], reverse=True)
    finally:
        if lock is not None:
            lock.release()


def delete_job(config: Config, job_id: str):
    if config.queue.backend == "postgres":
        from mindex.shared.storage import Store

        if Store.open(config).one("SELECT id FROM studio_tasks WHERE job_id=? AND status IN ('queued','running') LIMIT 1",
                                  (job_id,)):
            raise StudioError("创作任务仍在排队或执行，请先取消再删除")
    directory = job_dir(config, job_id)
    if not directory.is_dir():
        raise EntityNotFound("找不到创作任务")
    if config.storage.backend == "s3":
        from mindex.shared import files
        from mindex.shared.storage import Store
        files.delete_tree(Store.open(config), f"studio/{job_id}/")
    shutil.rmtree(directory)


def legacy_text(state: dict) -> str:
    """旧版分步接口只认任务创建时传入的文案；新任务的文案在版本工作台的方案里。"""
    text = state.get("input_text")
    if not text:
        raise StudioError("该任务的文案在构建方案中创作，请在版本工作台生成文案后构建")
    return text


# ---------------------------------------------------------------- 步骤状态机


def _validate_dependencies(state: dict, step: str):
    missing = [STEP_LABELS[d] for d in DEPENDENCIES[step] if state["steps"][d]["status"] != "complete"]
    if missing:
        raise StudioError(f"步骤 {STEP_LABELS[step]} 依赖的 {'、'.join(missing)} 尚未完成，请先执行")


def _archive_downstream(state: dict, step: str, reason: str):
    timestamp = now()
    for target in invalidation_targets(step):
        prior = state["steps"][target]
        if prior["status"] != "pending":
            state["history"].append({"step": target, "reason": reason, "at": timestamp, "previous": prior})
            state["steps"][target] = fresh_step()


def check_step(state: dict, step: str):
    """步骤执行前的同步预检：步骤名与依赖完整性（不修改状态）。"""
    if step not in STEPS:
        raise StudioError(f"未知创作步骤 {step}")
    _validate_dependencies(state, step)


def begin_step(config: Config, job_id: str, step: str) -> dict:
    state = reconcile(config, job_id)
    check_step(state, step)
    _archive_downstream(state, step, f"rerun-{step}")
    prior = state["steps"][step]
    if prior["status"] != "pending":
        state["history"].append({"step": step, "reason": f"rerun-{step}", "at": now(), "previous": prior})
    timestamp = now()
    state["steps"][step] = {
        **fresh_step(),
        "status": "running",
        "attempt": uuid4().hex[:12],
        "started_at": timestamp,
        "configuration": step_configuration(config, state, step),
    }
    state["updated_at"] = timestamp
    save(job_path(config, job_id), state, config=config)
    save(
        attempts_dir(config, job_id) / f"{step}-{state['steps'][step]['attempt']}.json",
        {"step": step, **state["steps"][step]},
        config=config)
    return state


def _finish_step(config: Config, job_id: str, step: str, status: str, **fields):
    state = load_job(config, job_id)
    info = state["steps"][step]
    if info["status"] != "running":
        raise StudioError(f"步骤 {STEP_LABELS[step]} 不在执行中，无法记录 {status}")
    timestamp = now()
    try:
        elapsed = (
            datetime.fromisoformat(timestamp) - datetime.fromisoformat(info["started_at"])
        ).total_seconds()
    except (TypeError, ValueError):
        elapsed = 0.0
    info.update(status=status, finished_at=timestamp, elapsed_ms=round(elapsed * 1000), **fields)
    state["updated_at"] = timestamp
    record = {"step": step, **info}
    save(attempts_dir(config, job_id) / f"{step}-{info['attempt']}.json", record, config=config)
    save(job_path(config, job_id), state, config=config)
    return state


def complete_step(config: Config, job_id: str, step: str, result: dict | None = None) -> dict:
    return _finish_step(config, job_id, step, "complete", result=result or {})


def fail_step(config: Config, job_id: str, step: str, exc: Exception) -> dict:
    return _finish_step(config, job_id, step, "failed", error=str(exc)[:2000], error_type=type(exc).__name__)


def recover_stale(config: Config):
    """启动恢复：锁空闲却仍有步骤 running，说明服务重启中断了执行。"""
    if config.queue.backend == "postgres":
        return  # 租约回收和账本负责恢复；API 本机锁不能证明其他 worker 已停止。
    root = studio_root(config)
    if not root.is_dir():
        return
    lock = lock_backend.named(config, "studio")
    try:
        lock.acquire()
    except Timeout:
        return  # 有步骤在执行，不动状态
    try:
        for directory in root.iterdir():
            path = directory / "job.json" if directory.is_dir() else None
            if path is None or not path.is_file():
                continue
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            running = [s for s, v in state.get("steps", {}).items() if v.get("status") == "running"]
            if not running and state.get("execution", {}).get("status") != "running":
                continue
            for step in running:
                fail_step(config, directory.name, step, StudioError("服务重启导致步骤中断，请重跑该步骤"))
            set_execution(config, directory.name, "failed", None, [])
    finally:
        lock.release()


# ---------------------------------------------------------------- 产物读写


def write_storyboard(config: Config, job_id: str, storyboard: Storyboard):
    _write_model(storyboard_path(config, job_id), storyboard, updated_at=now())


def _hydrate_document(config, path):
    if config.storage.backend == "s3":
        from mindex.shared import files
        from mindex.shared.storage import Store

        try:
            files.hydrate(Store.open(config), path)
        except FileNotFoundError:
            raise EntityNotFound("找不到创作产物") from None


def load_storyboard(config: Config, job_id: str) -> Storyboard:
    _hydrate_document(config, storyboard_path(config, job_id))
    return _read_model(storyboard_path(config, job_id), Storyboard, "尚未生成分镜，请先执行文本拆分镜")


def save_storyboard(config: Config, job_id: str, storyboard: Storyboard) -> dict:
    """人工编辑分镜：保持 split 完成态，作废其下游步骤。"""
    state = load_job(config, job_id)
    if state["steps"]["split"]["status"] != "complete":
        raise StudioError("请先完成文本拆分镜，再编辑分镜")
    prior = load_storyboard(config, job_id) if storyboard_path(config, job_id).is_file() else None
    old = {s.id: s for s in prior.shots} if prior else {}
    new = {s.id: s for s in storyboard.shots}
    structural = old.keys() != new.keys()
    if structural or any(old[k].query != new[k].query for k in old.keys() & new.keys()):
        invalidate(state, ("recall", "select", "render"), "storyboard-query-edited")
    if structural or any(old[k].text != new[k].text for k in old.keys() & new.keys()):
        invalidate(state, ("tts", "select", "render"), "storyboard-text-edited")
    if prior and [s.id for s in prior.shots] != [s.id for s in storyboard.shots]:
        invalidate(state, ("select", "render"), "storyboard-reordered")
    storyboard.coverage_warning = coverage_warning(legacy_text(state), [s.text for s in storyboard.shots])
    archive_edit(config, job_id, "storyboard", storyboard_path(config, job_id))
    registry = job_dir(config, job_id) / "checkpoints.json"
    if registry.is_file():
        checkpoints = json.loads(registry.read_text(encoding="utf-8"))
        checkpoints.pop("storyboard.json", None)
        save(registry, checkpoints, config=config)
    state["updated_at"] = now()
    write_storyboard(config, job_id, storyboard)
    save(job_path(config, job_id), state, config=config)
    return state


def write_timeline(config: Config, job_id: str, timeline: Timeline):
    _write_model(timeline_path(config, job_id), timeline, updated_at=now())


def load_timeline(config: Config, job_id: str) -> Timeline:
    _hydrate_document(config, timeline_path(config, job_id))
    return _read_model(timeline_path(config, job_id), Timeline, "尚未生成时间线，请先完成选片对齐")


def save_timeline(config: Config, job_id: str, timeline: Timeline) -> dict:
    """人工换选/微调：保持 select 完成态，仅作废渲染。"""
    state = load_job(config, job_id)
    if state["steps"]["select"]["status"] != "complete":
        raise StudioError("请先完成选片对齐，再调整时间线")
    _archive_downstream(state, "select", "timeline-edited")
    archive_edit(config, job_id, "timeline", timeline_path(config, job_id))
    state["updated_at"] = now()
    write_timeline(config, job_id, timeline)
    save(job_path(config, job_id), state, config=config)
    return state


def invalidate(state: dict, steps, reason: str):
    for target in steps:
        if state["steps"][target]["status"] != "pending":
            state["history"].append(
                {"step": target, "reason": reason, "at": now(), "previous": state["steps"][target]}
            )
            state["steps"][target] = fresh_step()


def archive_edit(config, job_id, name, path):
    if path.is_file():
        directory = attempts_dir(config, job_id) / f"edit-{name}-{uuid4().hex[:12]}"
        directory.mkdir(parents=True)
        copy_file(path, directory / path.name)


def set_execution(config, job_id, status, current_step, remaining_steps):
    state = load_job(config, job_id)
    error = state.get("execution", {}).get("error")
    state["execution"] = {"status": status, "current_step": current_step, "remaining_steps": remaining_steps}
    if status == "failed" and error:
        state["execution"]["error"] = error
    state["updated_at"] = now()
    save(job_path(config, job_id), state, config=config)
    return state


def step_configuration(config, state, step):
    from mindex.shared.storage import Store

    studio = config.studio
    recall = {
        "recall": studio.recall.model_dump(),
        "search": config.search.model_dump(),
        "embedding": portable_parameters(config, config.embedding.model_dump()),
        "video": config.video.model_dump(),
        "limit": state["options"]["recall_limit"],
    }
    # 只在有限制时加键，保证不限任务的指纹与历史检查点逐字节一致。
    if scope := material_scope(state, Store.open(config)):
        recall["sources"] = sorted(scope)
    values = {
        "split": {"generator": portable_parameters(config, studio.generator.model_dump()), "version": SPLIT_VERSION},
        "recall": recall,
        "tts": {"tts": portable_parameters(config, studio.tts.model_dump()), "voice": state["options"]["voice"]},
        "select": studio.selection.model_dump(),
        "render": {
            "render": studio.render.model_dump(),
            "audio": studio.audio.model_dump(),
            "subtitle": portable_parameters(config, studio.subtitle.model_dump()),
        },
    }
    return fingerprint(values[step])


def reconcile(config, job_id):
    """仅在持执行锁时迁移旧任务并使变化后的配置失效。"""
    state = load_job(config, job_id)
    changed = False
    if state.get("artifact_version") != 1:
        invalidate(state, ("recall", "tts", "select", "render"), "legacy-unverified")
        state["artifact_version"] = 1
        changed = True
    for step in STEPS:
        info = state["steps"][step]
        if info.get("configuration") and info["configuration"] != step_configuration(config, state, step):
            invalidate(state, (step, *invalidation_targets(step)), "configuration-changed")
            changed = True
    if changed:
        state["updated_at"] = now()
        save(job_path(config, job_id), state, config=config)
    return state
