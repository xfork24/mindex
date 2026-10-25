"""选题模块的严格模型：领域档案（权重、定时）。数据源、卡片与 LLM 输出模型在各自模块。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from mindex.shared.config import StrictModel

WEIGHT_KEYS = ("timeliness", "audience", "coverage", "novelty", "cost")
WEIGHT_LABELS = {"timeliness": "时效", "audience": "受众匹配", "coverage": "素材覆盖", "novelty": "去重",
                 "cost": "制作成本"}
Canvas = Literal["16:9", "9:16", "1:1"]


class Weights(StrictModel):
    """五项打分权重：各项非负，和为 1。"""

    timeliness: float = Field(default=0.25, ge=0, le=1)
    audience: float = Field(default=0.25, ge=0, le=1)
    coverage: float = Field(default=0.25, ge=0, le=1)
    novelty: float = Field(default=0.15, ge=0, le=1)
    cost: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def sums_to_one(self):
        total = sum(getattr(self, key) for key in WEIGHT_KEYS)
        if abs(total - 1) > 0.001:
            raise ValueError(f"打分权重之和必须为 1（当前 {total:.3f}）")
        return self


class Schedule(StrictModel):
    """每日定时挖掘：本地时刻 HH:MM；count 为每次写大纲的前 N 名。"""

    enabled: bool = False
    time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    count: int = Field(default=10, ge=1, le=50)


def _clean_list(values: list[str]) -> list[str]:
    seen, result = set(), []
    for value in values:
        text = " ".join(str(value).split())
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


class ProfileBody(StrictModel):
    """领域档案。受众、定位与禁区会作为写作约束进入从选题创建的创作任务的文案提示词。"""

    name: str = Field(min_length=1, max_length=40)
    domain: str = Field(min_length=1, max_length=40, description="领域，如 金融、科技、历史")
    subtopics: list[str] = Field(default_factory=list, max_length=20, description="子方向")
    audience: str = Field(default="", max_length=500, description="目标受众")
    positioning: str = Field(default="", max_length=500, description="账号定位与表达风格")
    taboos: list[str] = Field(default_factory=list, max_length=30, description="禁区：不做的选题与不写的内容")
    keywords: list[str] = Field(default_factory=list, max_length=30, description="检索与搜索关键词")
    language: str = Field(default="zh-hans", min_length=2, max_length=16, pattern=r"^[A-Za-z-]+$")
    region: str = Field(default="CN", min_length=2, max_length=8, pattern=r"^[A-Za-z]+$")
    canvas: Canvas = "16:9"
    weights: Weights = Field(default_factory=Weights)
    schedule: Schedule = Field(default_factory=Schedule)

    @field_validator("name", "domain")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("audience", "positioning")
    @classmethod
    def strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("subtopics", "keywords")
    @classmethod
    def short_items(cls, values: list[str]) -> list[str]:
        values = _clean_list(values)
        if any(len(value) > 60 for value in values):
            raise ValueError("每项不超过 60 字")
        return values

    @field_validator("taboos")
    @classmethod
    def taboo_items(cls, values: list[str]) -> list[str]:
        values = _clean_list(values)
        if any(len(value) > 200 for value in values):
            raise ValueError("每条禁区不超过 200 字")
        return values


class ProfileChanges(StrictModel):
    """部分修改档案：只给出要改的字段；weights/schedule 需整体给出。"""

    name: str | None = Field(default=None, min_length=1, max_length=40)
    domain: str | None = Field(default=None, min_length=1, max_length=40)
    subtopics: list[str] | None = Field(default=None, max_length=20)
    audience: str | None = Field(default=None, max_length=500)
    positioning: str | None = Field(default=None, max_length=500)
    taboos: list[str] | None = Field(default=None, max_length=30)
    keywords: list[str] | None = Field(default=None, max_length=30)
    language: str | None = Field(default=None, min_length=2, max_length=16, pattern=r"^[A-Za-z-]+$")
    region: str | None = Field(default=None, min_length=2, max_length=8, pattern=r"^[A-Za-z]+$")
    canvas: Canvas | None = None
    weights: Weights | None = None
    schedule: Schedule | None = None

    def values(self) -> dict:
        return {key: value for key, value in self.model_dump(exclude_unset=True).items() if value is not None}
