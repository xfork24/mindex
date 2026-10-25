"""平台发布预设：内置 bilibili、douyin、youtube（``studio/platforms/*.yaml``，只读）。

预设给出平台的成片画幅、封面尺寸与格式、标题/简介/标签的长度与数量限制和章节规则；封面、发布元数据与发布包
阶段按预设生成与校验。数值写在 YAML 注释里注明核对日期与来源，平台调整时只改 YAML。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError

from mindex.admin.curation import EntityNotFound
from mindex.shared.config import StrictModel

BUILTIN_DIR = Path(__file__).resolve().parent / "platforms"
BUILTIN_ORDER = ("bilibili", "douyin", "youtube")
PlatformId = Literal["bilibili", "douyin", "youtube"]


class CoverSpec(StrictModel):
    width: int = Field(gt=0, le=4096)
    height: int = Field(gt=0, le=4096)
    format: Literal["jpeg", "png"] = "jpeg"
    max_kb: int = Field(default=2048, gt=0)


class TextLimit(StrictModel):
    max_chars: int = Field(gt=0)


class TagLimit(StrictModel):
    max_count: int = Field(gt=0)
    max_chars_each: int = Field(gt=0)
    max_total_chars: int | None = Field(default=None, gt=0)
    hashtag: bool = False  # 标签以 #话题 的形式写进简介（抖音）


class ChapterRule(StrictModel):
    enabled: bool = True
    min_count: int = Field(default=2, ge=1)
    first_at_zero: bool = True
    min_seconds: float = Field(default=5, gt=0)


class PlatformPreset(StrictModel):
    id: str
    name: str
    canvas: Literal["16:9", "9:16", "1:1"]
    cover: CoverSpec
    title: TextLimit
    description: TextLimit
    tags: TagLimit
    chapters: ChapterRule
    language: str = "zh"


@lru_cache(maxsize=1)
def presets() -> dict[str, PlatformPreset]:
    found = {}
    for path in sorted(BUILTIN_DIR.glob("*.yaml")):
        try:
            item = PlatformPreset.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise RuntimeError(f"平台预设 {path.name} 无效：{exc}") from exc
        if item.id != path.stem:
            raise RuntimeError(f"平台预设 {path.name} 的 id 必须与文件名一致")
        found[item.id] = item
    if set(found) != set(BUILTIN_ORDER):
        raise RuntimeError(f"平台预设不完整：{sorted(set(found) ^ set(BUILTIN_ORDER))}")
    return {key: found[key] for key in BUILTIN_ORDER}


def preset(platform: str) -> PlatformPreset:
    found = presets().get(platform)
    if found is None:
        raise EntityNotFound(f"找不到平台预设：{platform}")
    return found


def list_presets() -> list[dict]:
    return [item.model_dump() for item in presets().values()]
