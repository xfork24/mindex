"""Shared platform delivery option types."""
from typing import Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from mindex.shared.config import StrictModel
from mindex.studio.platforms import PlatformId, PlatformPreset


class CoverOptions(StrictModel):
    """封面：frame 从成片挑一帧（``time`` 为成片秒数，None = 自动挑清晰度最高的镜头中点）；aigc 用图片生成模型按
    ``prompt`` 生成；upload 用任务 inputs 中上传的图片（``media_id``）。``title``/``subtitle`` 叠加在封面上，
    None = 用发布元数据的标题（不叠副标题）。"""

    source: Literal["frame", "aigc", "upload"] = "frame"
    time: float | None = Field(default=None, ge=0)
    prompt: str | None = Field(default=None, max_length=1500)
    media_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    title: str | None = Field(default=None, max_length=40)
    subtitle: str | None = Field(default=None, max_length=60)
    text: bool = True  # 是否叠加标题文字

    @field_validator("prompt", "title", "subtitle", mode="before")
    @classmethod
    def _blank(cls, value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


class PublishOptions(StrictModel):
    platforms: list[PlatformId] = Field(min_length=1)
    cover: CoverOptions = Field(default_factory=CoverOptions)
    platform_specs: dict[PlatformId, PlatformPreset] = Field(default_factory=dict)

    @model_validator(mode="after")
    def matching_specs(self):
        if any(key != item.id for key, item in self.platform_specs.items()):
            raise ValueError("平台快照标识不匹配")
        return self

    @model_serializer(mode="wrap")
    def omit_empty_specs(self, handler):
        data = handler(self)
        if not data.get("platform_specs"):
            data.pop("platform_specs", None)
        return data


    @field_validator("platforms", mode="after")
    @classmethod
    def _unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("平台不能重复")
        return value
