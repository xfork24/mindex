"""跨模块共享的字面量类型与公共数据结构定义。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

Granularity = Literal["shot", "moment", "scene"]


class Utterance(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("Utterance end must be after start")
        return self


class Annotation(BaseModel):
    description: str = Field(min_length=1)
    actions: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    scene: list[str] = Field(default_factory=list)
    emotions: list[str] = Field(default_factory=list)
    atmosphere: list[str] = Field(default_factory=list)
    shot_size: list[str] = Field(default_factory=list)
    angle: list[str] = Field(default_factory=list)
    camera_motion: list[str] = Field(default_factory=list)
    visual_evidence: list[str] = Field(default_factory=list)
    dialogue_inference: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)

    def views(self, dialogue: str) -> dict[str, str]:
        description = "\n".join(
            filter(
                None,
                [
                    self.description,
                    "动作：" + "；".join(self.actions) if self.actions else "",
                    "场景：" + "；".join(self.scene) if self.scene else "",
                    "人物：" + "；".join(self.people) if self.people else "",
                    "对象：" + "；".join(self.objects) if self.objects else "",
                ],
            )
        )
        style = "\n".join(
            f"{label}：{'；'.join(values)}"
            for label, values in [
                ("人物情绪", self.emotions),
                ("氛围", self.atmosphere),
                ("景别", self.shot_size),
                ("角度", self.angle),
                ("镜头运动", self.camera_motion),
            ]
            if values
        )
        return {
            k: v
            for k, v in {"description": description, "style": style, "dialogue": dialogue}.items()
            if v.strip()
        }


class ShotDraft(BaseModel):
    """拆分镜 LLM 的输出单元；shot id 由代码分配，不信任模型编号。

    visual 为第一个字段：模型先规划画面意象，再切分 text、编写 query。
    这里不设长度上限，超长由 Studio 截断并记警告（不让拆分失败）。
    """

    visual: str = Field(default="", description="一句中文画面意象（≤120 字），先于 text 规划")
    text: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=400)
    estimated_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)

    @field_validator("visual", mode="before")
    @classmethod
    def strip_visual(cls, value):
        if value is None:
            return ""
        return value.strip() if isinstance(value, str) else value


class StoryboardDraft(BaseModel):
    shots: list[ShotDraft] = Field(min_length=1)


class Filters(BaseModel):
    source_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    source_group_ids: list[str] = Field(default_factory=list, max_length=200)
    min_duration: float = Field(default=0, ge=0)
    max_duration: float | None = Field(default=None, gt=0)
    scene: str | None = None
    emotion: str | None = None
    shot_size: str | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.max_duration is not None and self.max_duration < self.min_duration:
            raise ValueError("max_duration must be >= min_duration")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        if not self.source_group_ids:
            value.pop("source_group_ids", None)
        return value

    def effective_sources(self, store) -> set[str] | None:
        """共享范围：多组取并集，与手动素材限制取交集；保留空集合语义。"""
        from shared.media_groups import Groups

        manual = self.selected_sources()
        members = Groups(store).resolve("source", self.source_group_ids)
        if members is None:
            return manual or None
        return manual & members if manual else members

    def selected_sources(self) -> set[str]:
        """Multi-source selection takes precedence; an empty selection retains legacy behavior."""
        return set(self.source_ids) or ({self.source_id} if self.source_id else set())


class TextSearch(BaseModel):
    target_duration: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    person_ids: list[str] = Field(default_factory=list, max_length=30)
    person_mode: Literal["auto", "onscreen", "mentioned"] = "auto"
    person_relation: Literal["all", "any"] = "all"
    text: str = Field(default="", max_length=4000)
    granularity: Granularity = "moment"
    limit: int = Field(default=20, ge=1, le=50)
    filters: Filters = Field(default_factory=Filters)
    channels: list[Literal["visual", "dialogue", "analysis"]] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def nonblank(self):
        self.text = self.text.strip()
        if not self.text and not self.person_ids:
            raise ValueError("请输入查询文字或选择人物")
        return self


class SimilarSearch(BaseModel):
    """按库内片段找相似（POST /api/search/similar）；粒度缺省时沿用种子片段的粒度。"""

    segment_id: str = Field(min_length=1)
    granularity: Granularity | None = None
    limit: int = Field(default=20, ge=1, le=50)
    filters: Filters = Field(default_factory=Filters)


DigestEmptyReason = Literal["预算为 0", "无生效索引", "无已分析镜头"]


class DigestSource(BaseModel):
    """素材摘要中的单个素材条目（由 studio/material_digest.py 生成）。"""

    source_id: str
    name: str  # 显示名（display_name 优先）
    duration: float = Field(ge=0)  # 秒
    segments: int = Field(ge=0)  # 纳入的 Analyzed_Segment 数（抽样前）
    storyline: list[str] = Field(default_factory=list)  # 实际写进正文的故事线条目（抽样、截断后）
    vocabulary: list[str] = Field(default_factory=list)  # 前 30 个画面词（与预算无关）


class MaterialDigest(BaseModel):
    """可用素材摘要：文案、拆分镜与重写召回词共用的只读参考资料。

    不变式：empty ⇔ source_count == 0 ⇔ text == "" ⇔ empty_reason 非空。
    定义在 shared 层，使 models/ 下的 Writer 不必依赖 studio/。
    """

    text: str = ""  # 送给 Writer 的正文，len(text) ≤ 预算
    sources: list[DigestSource] = Field(default_factory=list)
    source_count: int = Field(default=0, ge=0)
    segment_count: int = Field(default=0, ge=0)
    fingerprint: str | None = None
    content_index_id: str | None = None
    empty_reason: DigestEmptyReason | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.source_count != len(self.sources):
            raise ValueError("素材摘要的素材数与素材条目数不一致")
        if not self.sources:
            if self.text or self.segment_count or self.empty_reason is None:
                raise ValueError("空素材摘要必须正文为空、镜头数为 0 并给出为空原因")
        elif not self.text or self.empty_reason is not None:
            raise ValueError("非空素材摘要必须有正文且不能带为空原因")
        return self

    @property
    def empty(self) -> bool:
        return not self.sources

    def stats(self) -> dict:
        """写入文案版本 data.materials 的统计信息。"""
        return {
            "sources": self.source_count,
            "segments": self.segment_count,
            "fingerprint": self.fingerprint,
            "content_index_id": self.content_index_id,
        }

    def public(self) -> dict:
        """GET material-digest 接口的响应体。"""
        return {
            "text": self.text,
            "sources": [s.model_dump() for s in self.sources],
            "stats": self.stats(),
            "empty_reason": self.empty_reason,
        }

    @classmethod
    def empty_of(cls, reason: DigestEmptyReason, content_index_id: str | None = None) -> MaterialDigest:
        return cls(empty_reason=reason, content_index_id=content_index_id)


ScriptTemplateKind = Literal["outline", "rewrite"]
SCRIPT_TEMPLATE_ID = r"^[a-z0-9][a-z0-9-]{0,63}$"


class ScriptTemplateConstraints(BaseModel):
    """文案输出约束；max_chars 为 None 时使用 studio.max_input_chars。"""

    model_config = ConfigDict(extra="forbid")

    max_chars: int | None = Field(default=None, ge=50, le=20000)


class ScriptTemplate(BaseModel):
    """文案模板（内置 YAML 或用户模板）。定义在 shared 层，使 models/ 下的 Writer 不必依赖 studio/。

    kind：outline=按视频大纲写；rewrite=把参考文稿改写为视频文案。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=SCRIPT_TEMPLATE_ID)
    name: str = Field(min_length=1, max_length=40)
    category: str = Field(min_length=1, max_length=40)
    kind: ScriptTemplateKind = "outline"
    version: int = Field(default=1, ge=1)
    description: str = Field(default="", max_length=200)
    instructions: str = Field(min_length=1, max_length=6000)
    constraints: ScriptTemplateConstraints = Field(default_factory=ScriptTemplateConstraints)

    @field_validator("name", "category", "instructions")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value
