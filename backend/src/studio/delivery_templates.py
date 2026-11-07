"""Reusable delivery settings. Plans own snapshots, never live template references."""

from __future__ import annotations

import json
import re
from typing import Literal
from uuid import uuid4

from pydantic import Field, field_validator

from shared.config import StrictModel
from studio import brands, platforms, studio
from studio.delivery_models import PublishOptions

CANVASES = ("16:9", "9:16", "1:1")


class DeliveryExport(StrictModel):
    canvas: Literal["16:9", "9:16", "1:1"]
    fit: Literal["crop", "pad"] = "crop"
    mode: Literal["derive", "rerender"] = "derive"


class MetadataRule(StrictModel):
    title_instructions: str = Field(default="", max_length=1500)
    description_instructions: str = Field(default="", max_length=3000)
    fixed_tags: list[str] = Field(default_factory=list, max_length=50)
    footer: str = Field(default="", max_length=3000)


class PackageOptions(StrictModel):
    cover: bool = True
    srt: bool = True
    vtt: bool = True
    metadata_json: bool = True
    metadata_txt: bool = True
    filename: str = Field(default="{job_id}-{platform}", min_length=1, max_length=160)

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value):
        rest = re.sub(r"\{(?:job_id|plan_id|platform|canvas)\}", "", value)
        if "{" in rest or "}" in rest:
            raise ValueError("文件命名仅支持 job_id、plan_id、platform、canvas 变量")
        return value


class DeliveryChecks(StrictModel):
    canvas: Literal["warn", "block"] = "warn"
    cover: Literal["warn", "block"] = "warn"
    metadata: Literal["warn", "block"] = "warn"


class DeliveryConfig(StrictModel):
    exports: list[DeliveryExport] = Field(
        default_factory=lambda: [DeliveryExport(canvas=c) for c in CANVASES], max_length=3
    )
    publish: PublishOptions | None = Field(
        default_factory=lambda: PublishOptions(platforms=list(platforms.BUILTIN_ORDER))
    )
    cover_asset: brands.AssetRef | None = None
    rules: dict[platforms.PlatformId, MetadataRule] = Field(default_factory=dict)
    package: PackageOptions = Field(default_factory=PackageOptions)
    checks: DeliveryChecks = Field(default_factory=DeliveryChecks)

    @field_validator("exports")
    @classmethod
    def unique_exports(cls, value):
        if len({x.canvas for x in value}) != len(value):
            raise ValueError("每个画幅只能导出一次")
        return value

    @field_validator("cover_asset")
    @classmethod
    def image_only(cls, value):
        if value and value.kind != "image":
            raise ValueError("封面必须为图片")
        return value


class DeliverySnapshot(StrictModel):
    template_id: str = Field(default="base", max_length=64)
    template_name: str = Field(default="基本模板", max_length=40)
    config: DeliveryConfig = Field(default_factory=DeliveryConfig)


class TemplateBody(StrictModel):
    name: str = Field(min_length=1, max_length=40)
    config: DeliveryConfig = Field(default_factory=DeliveryConfig)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("模板名称不能为空")
        return value.strip()


def default_config(store):
    config = DeliveryConfig()
    keys = [item["id"] for item in platforms.catalogue(store) if item["id"] in platforms.BUILTIN_ORDER]
    config.publish = (
        PublishOptions.model_validate(platforms.snapshot_publish({"platforms": keys}, store))
        if keys
        else None
    )
    return config


def default_snapshot(store):
    return DeliverySnapshot(config=default_config(store))


def freeze_config(config, store=None, *, legacy=False):
    if config.publish:
        config.publish = PublishOptions.model_validate(
            platforms.snapshot_publish(config.publish.model_dump(), store, legacy=legacy)
        )
    return config


def get(store, template_id):
    if template_id == "base":
        return {
            "id": "base",
            "name": "基本模板",
            "builtin": True,
            "config": default_config(store).model_dump(),
        }
    row = store.one("SELECT * FROM delivery_templates WHERE id=?", (template_id,))
    if not row:
        from admin.curation import EntityNotFound

        raise EntityNotFound("找不到交付模板")
    return {
        "id": row["id"],
        "name": row["name"],
        "builtin": False,
        "config": freeze_config(
            DeliveryConfig.model_validate(json.loads(row["body"])), legacy=True
        ).model_dump(),
    }


def apply(options, snapshot):
    """Translate the complete snapshot to existing execution options, excluding the master canvas."""
    data = (
        snapshot.model_dump()
        if isinstance(snapshot, DeliverySnapshot)
        else DeliverySnapshot.model_validate(snapshot).model_dump()
    )
    cfg = data["config"]
    return {
        **options,
        "delivery": data,
        "exports": [x for x in cfg["exports"] if x["canvas"] != options.get("canvas", "16:9")] or None,
        "publish": cfg["publish"],
    }


def copy_cover(config, job_id, snapshot):
    asset = snapshot.config.cover_asset
    if asset and snapshot.config.publish and snapshot.config.publish.cover.source == "upload":
        target = studio.job_dir(config, job_id) / "inputs" / f"{asset.sha256}{asset.suffix}"
        if not target.is_file() or brands.digest_file(target) != asset.sha256:
            path = brands.require_asset(config, asset)
            studio.save_bytes(target, path.read_bytes())
        snapshot.config.publish.cover.media_id = asset.sha256


def asset_paths(config):
    return json.dumps([config.cover_asset.path] if config.cover_asset else [])


def delivery_routes(router, store, mutation_lock, *, current):
    from fastapi import HTTPException

    @router.get("/delivery-templates")
    def listing():
        return {
            "items": [get(store, "base")]
            + [
                get(store, r["id"])
                for r in store.rows("SELECT id FROM delivery_templates ORDER BY created_at,id")
            ],
            "default": "base",
        }

    @router.get("/delivery-templates/{template_id}")
    def detail(template_id: str):
        return get(store, template_id)

    @router.post("/delivery-templates/{template_id}/copy", status_code=201)
    def duplicate(template_id: str, body: TemplateBody):
        with mutation_lock():
            source = get(store, template_id)
            key = "user-" + uuid4().hex[:16]
            now = studio.now()
            store.execute(
                "INSERT INTO delivery_templates(id,name,body,asset_paths,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    key,
                    body.name,
                    json.dumps(source["config"], ensure_ascii=False),
                    asset_paths(DeliveryConfig.model_validate(source["config"])),
                    now,
                    now,
                ),
            )
            return get(store, key)

    @router.put("/delivery-templates/{template_id}")
    def update(template_id: str, body: TemplateBody):
        with mutation_lock():
            item = get(store, template_id)
            if item["builtin"]:
                raise HTTPException(403, "基本模板只读，请先复制")
            freeze_config(body.config, store)
            if body.config.cover_asset:
                brands.require_asset(current(), body.config.cover_asset)
            store.execute(
                "UPDATE delivery_templates SET name=?,body=?,asset_paths=?,updated_at=? WHERE id=?",
                (
                    body.name,
                    body.config.model_dump_json(),
                    asset_paths(body.config),
                    studio.now(),
                    template_id,
                ),
            )
            return get(store, template_id)

    @router.delete("/delivery-templates/{template_id}")
    def remove(template_id: str):
        with mutation_lock():
            if get(store, template_id)["builtin"]:
                raise HTTPException(403, "基本模板只读")
            store.execute("DELETE FROM delivery_templates WHERE id=?", (template_id,))
            return {"deleted": template_id}
