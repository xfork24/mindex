"""可管理的平台目录与方案规则快照。

YAML 保留为首次初始化值及旧方案的固定兼容规则；可选平台来自 metadata 数据库。
平台目录的更新和删除不改变已经复制到交付模板、任务方案中的规则。
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from fastapi import HTTPException
from pydantic import Field, StringConstraints, ValidationError, field_validator

from admin.curation import EntityNotFound
from shared.config import StrictModel
from studio import studio

BUILTIN_DIR = Path(__file__).resolve().parent / "platforms"
BUILTIN_ORDER = ("bilibili", "douyin", "youtube")
PlatformId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]


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
    min_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)


class PlatformPreset(StrictModel):
    id: PlatformId
    name: str = Field(min_length=1, max_length=80)
    canvas: Literal["16:9", "9:16", "1:1"]
    cover: CoverSpec
    title: TextLimit
    description: TextLimit
    tags: TagLimit
    chapters: ChapterRule
    language: str = Field(default="zh", min_length=1, max_length=40)

    @field_validator("name", "language")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("平台名称和语言不能为空")
        return value.strip()


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


def catalogue(store) -> list[dict]:
    """Persistent selectable platforms; deleted IDs remain reserved."""

    return [
        PlatformPreset.model_validate(json.loads(row["body"])).model_dump()
        for row in store.rows("SELECT body FROM delivery_platforms WHERE deleted=0 ORDER BY created_at,id")
    ]


def registered(store, platform: str) -> PlatformPreset:

    row = store.one("SELECT body FROM delivery_platforms WHERE id=? AND deleted=0", (platform,))
    if not row:
        raise EntityNotFound(f"找不到平台：{platform}")
    return PlatformPreset.model_validate(json.loads(row["body"]))


def for_plan(p, platform: str) -> PlatformPreset:
    """Build exclusively from plan snapshots; old built-in IDs use frozen legacy presets."""
    publish = (p.get("options") or {}).get("publish") or {}
    value = publish.get("platform_specs", {}).get(platform)
    return PlatformPreset.model_validate(value) if value else preset(platform)


def snapshot_publish(value, store=None, *, previous=None, legacy=False):
    """Fill missing rules, preserving explicit snapshots and existing plan semantics."""
    if value is None:
        return None

    result = copy.deepcopy(value)
    before = previous or {}
    specs = {}
    for key in result["platforms"]:
        raw = result.get("platform_specs", {}).get(key) or before.get("platform_specs", {}).get(key)
        if raw:
            item = PlatformPreset.model_validate(raw)
        elif legacy or key in before.get("platforms", []):
            item = preset(key)
        elif store is not None:
            item = registered(store, key)
        else:
            item = preset(key)
        specs[key] = item.model_dump()
    result["platform_specs"] = specs
    return result


def platform_routes(router, store, mutation_lock):

    @router.get("/platforms")
    def listing():
        return {"items": catalogue(store)}

    @router.get("/platforms/{platform_id}")
    def detail(platform_id: str):
        return registered(store, platform_id).model_dump()

    @router.post("/platforms", status_code=201)
    def create(body: PlatformPreset):
        with mutation_lock():
            if store.one("SELECT id FROM delivery_platforms WHERE id=?", (body.id,)):
                raise HTTPException(409, "平台标识已使用，不可重复或复用")
            now = studio.now()
            store.execute(
                "INSERT INTO delivery_platforms(id,body,created_at,updated_at) VALUES(?,?,?,?)",
                (body.id, body.model_dump_json(), now, now),
            )
            return body.model_dump()

    @router.put("/platforms/{platform_id}")
    def update(platform_id: str, body: PlatformPreset):
        with mutation_lock():
            registered(store, platform_id)
            if body.id != platform_id:
                raise HTTPException(422, "平台标识不能修改")
            store.execute(
                "UPDATE delivery_platforms SET body=?,updated_at=? WHERE id=?",
                (body.model_dump_json(), studio.now(), platform_id),
            )
            return body.model_dump()

    @router.delete("/platforms/{platform_id}")
    def remove(platform_id: str):
        with mutation_lock():
            registered(store, platform_id)
            store.execute(
                "UPDATE delivery_platforms SET deleted=1,updated_at=? WHERE id=?", (studio.now(), platform_id)
            )
            return {"deleted": platform_id}
