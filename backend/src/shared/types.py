from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    """拆分镜 LLM 的输出单元；shot id 由代码分配，不信任模型编号。"""

    text: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=400)
    estimated_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)


class StoryboardDraft(BaseModel):
    shots: list[ShotDraft] = Field(min_length=1)


class Filters(BaseModel):
    source_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
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

    def selected_sources(self) -> set[str]:
        """Multi-source selection takes precedence; an empty selection retains legacy behavior."""
        return set(self.source_ids) or ({self.source_id} if self.source_id else set())


class TextSearch(BaseModel):
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
