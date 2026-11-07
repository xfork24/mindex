"""Studio 品牌模板工具：列出、读取、复制、修改、删除（``/api/studio/brands``）
与上传品牌素材（``/api/studio/brand-assets``）。

- 品牌模板统一字幕样式、文字层样式、片头、片尾与水印；方案用 update_plan_options 的 brand_id 选用，
  brand_overrides 覆盖其中几项或关闭片头、片尾、水印。
- 内置模板只读；用户模板由复制产生。服务端拒绝（403 内置只读、409 被方案引用）原样转述。
- 改动影响成片的字段后，使用该模板的方案在下次构建时只重新合成成片（分镜产物照常复用）。
- 模板是全局数据：改动事件为 ``{domain: "studio", brands: true}``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import check
from shared.config import StrictModel
from studio.models import BrandClip, OverlayStyle, SubtitleStyle, Watermark

BrandId = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="品牌模板 ID（list_brands 列出）")
FOCUS = {"page": "studio_post", "section": "plan"}
MUTATION = {"domain": "studio", "brands": True}  # 页面事件域（与工具所属的 studio_post 助手领域无关）
EFFECT_KEYS = ("subtitle", "overlay", "intro", "outro", "watermark")
LABELS = {
    "name": "名称",
    "description": "说明",
    "subtitle": "字幕样式",
    "overlay": "文字层样式",
    "intro": "片头",
    "outro": "片尾",
    "watermark": "水印",
}
POSITIONS = {"top-left": "左上", "top-right": "右上", "bottom-left": "左下", "bottom-right": "右下"}


def origin(item: dict) -> str:
    return "内置" if item.get("builtin") else "用户"


def clip_text(value: dict | None) -> str:
    if not value:
        return "无"
    asset = value["asset"]
    return f"{asset.get('name') or asset['sha256'][:8]}（原声音量 {value.get('volume', 1):g}）"


def watermark_text(value: dict | None) -> str:
    if not value:
        return "无"
    asset = value["asset"]
    return (
        f"{asset.get('name') or asset['sha256'][:8]}，"
        f"{POSITIONS.get(value.get('position'), value.get('position'))}，"
        f"不透明度 {value.get('opacity', 0.8):g}，大小 {value.get('scale', 0.12):g}"
        + ("，含片头片尾" if value.get("include_bookends") else "")
    )


def style_text(value: dict | None) -> str:
    if not value:
        return "跟随方案（未设置）"
    return "，".join(f"{key}={value[key]}" for key in sorted(value) if value[key] not in (None, False))


def field_text(key: str, value) -> str:
    if key in ("intro", "outro"):
        return clip_text(value)
    if key == "watermark":
        return watermark_text(value)
    if key in ("subtitle", "overlay"):
        return style_text(value)
    return value if value else "（空）"


def brand_text(item: dict) -> str:
    lines = [
        f"品牌模板 {item['id']}「{item['name']}」：{origin(item)}模板，版本 {item.get('version')}",
        f"说明：{item.get('description') or '（无）'}",
    ]
    lines += [f"{LABELS[key]}：{field_text(key, item.get(key))}" for key in EFFECT_KEYS]
    if item.get("builtin"):
        lines.append("内置模板只读，也没有片头片尾与水印；要定制请先 copy_brand 复制为用户模板。")
    return "\n".join(lines)


async def fetch_brand(ctx: ToolContext, brand_id: str) -> dict:
    return check(await ctx.client.get(f"/api/studio/brands/{brand_id}"), "读取品牌模板")


class ListBrands(StrictModel):
    pass


async def list_brands(args: ListBrands, ctx: ToolContext) -> ToolOutput:
    items = check(await ctx.client.get("/api/studio/brands"), "列出品牌模板")["items"]
    rows = [
        {key: item.get(key) for key in ("id", "name", "description", "builtin", "base_id", "version")}
        | {key: bool(item.get(key)) for key in ("intro", "outro", "watermark")}
        for item in items
    ]
    text = "\n".join(
        f"{r['id']} | {r['name']} | {origin(r)} | 片头 {'有' if r['intro'] else '无'} | "
        f"片尾 {'有' if r['outro'] else '无'} | 水印 {'有' if r['watermark'] else '无'} | {r['description']}"
        for r in rows
    )
    return ToolOutput(
        {"items": rows},
        text=f"共 {len(rows)} 个品牌模板（ID | 名称 | 来源 | 片头 | 片尾 | 水印 | 说明）：\n"
        f"{text}\n用 update_plan_options 的 brand_id 为方案选用。",
        focus=FOCUS,
    )


class GetBrand(StrictModel):
    brand_id: str = BrandId


async def get_brand(args: GetBrand, ctx: ToolContext) -> ToolOutput:
    item = await fetch_brand(ctx, args.brand_id)
    return ToolOutput(item, text=brand_text(item), focus=FOCUS)


class CopyBrand(StrictModel):
    brand_id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="来源品牌模板 ID（内置或用户模板）"
    )
    name: str = Field(min_length=1, max_length=40, description="新模板名称")

    @model_validator(mode="after")
    def named(self):
        if not self.name.strip():
            raise ValueError("模板名称不能为空")
        return self


async def preview_copy_brand(args: CopyBrand, ctx: ToolContext) -> Preview:
    item = await fetch_brand(ctx, args.brand_id)
    return Preview(
        "复制品牌模板为新的用户模板",
        [
            {"label": "来源模板", "after": f"{item['name']}（{item['id']}，{origin(item)}）"},
            {"label": "新模板名称", "after": args.name.strip()},
        ],
        warnings=["复制不会改变任何方案所选的品牌；需要时再用 update_plan_options 选用新模板"],
        focus=FOCUS,
    )


async def copy_brand(args: CopyBrand, ctx: ToolContext) -> ToolOutput:
    item = check(
        await ctx.client.post(f"/api/studio/brands/{args.brand_id}/copy", {"name": args.name.strip()}),
        "复制品牌模板",
    )
    return ToolOutput(
        item,
        text=f"已复制为用户品牌模板 {item['id']}「{item['name']}」；可用 update_brand 修改，"
        "片头片尾与水印素材先用 upload_brand_asset 上传。",
        mutation=MUTATION,
        focus=FOCUS,
    )


class UpdateBrand(StrictModel):
    brand_id: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="要修改的用户品牌模板 ID（user- 开头）"
    )
    name: str | None = Field(default=None, min_length=1, max_length=40, description="新名称")
    description: str | None = Field(default=None, max_length=200, description="新说明")
    subtitle: SubtitleStyle | None = Field(default=None, description="字幕样式（整体替换）；显式 null 去掉")
    overlay: OverlayStyle | None = Field(default=None, description="文字层样式（整体替换）；显式 null 去掉")
    intro: BrandClip | None = Field(
        default=None, description="片头（asset 用 upload_brand_asset 返回的视频素材）；显式 null 去掉"
    )
    outro: BrandClip | None = Field(default=None, description="片尾；显式 null 去掉")
    watermark: Watermark | None = Field(default=None, description="水印（asset 为图片素材）；显式 null 去掉")

    @model_validator(mode="after")
    def something(self):
        if not self.changes():
            raise ValueError("至少修改一项")
        return self

    def changes(self) -> dict:
        """给出的字段；影响成片的字段显式 null 也算修改（去掉该项）。"""
        given = self.model_fields_set - {"brand_id"}
        result = {}
        for key in given:
            value = getattr(self, key)
            if value is None and key not in EFFECT_KEYS:
                continue
            result[key] = value.model_dump() if hasattr(value, "model_dump") else value
        return result


async def preview_update_brand(args: UpdateBrand, ctx: ToolContext) -> Preview:
    item = await fetch_brand(ctx, args.brand_id)
    if item.get("builtin"):
        raise ToolError("内置品牌模板只读，请先用 copy_brand 复制为自己的模板再修改", code="brand_readonly")
    changes = {key: value for key, value in args.changes().items() if item.get(key) != value}
    if not changes:
        raise ToolError("给出的字段与模板当前内容相同，没有需要修改的内容", code="no_changes")
    rows = [
        {"label": LABELS[key], "before": field_text(key, item.get(key)), "after": field_text(key, value)}
        for key, value in changes.items()
    ]
    warnings = (
        ["使用该品牌模板的方案下次构建会重新合成成片（分镜产物照常复用）"]
        if set(changes) & set(EFFECT_KEYS)
        else ["只修改名称或说明，不影响成片"]
    )
    return Preview(f"修改用户品牌模板 {item['id']}「{item['name']}」", rows, warnings=warnings, focus=FOCUS)


async def update_brand(args: UpdateBrand, ctx: ToolContext) -> ToolOutput:
    result = await ctx.client.put(f"/api/studio/brands/{args.brand_id}", args.changes())
    if not result["ok"] and result["status"] == 403:
        raise ToolError(f"修改品牌模板失败：{result['error']}", code="http_403", value=result)
    item = check(result, "修改品牌模板")
    return ToolOutput(
        item,
        text=f"已修改品牌模板 {item['id']}「{item['name']}」（版本 {item.get('version')}）。\n"
        + brand_text(item),
        mutation=MUTATION,
        focus=FOCUS,
    )


class DeleteBrand(StrictModel):
    brand_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="要删除的用户品牌模板 ID")


async def preview_delete_brand(args: DeleteBrand, ctx: ToolContext) -> Preview:
    item = await fetch_brand(ctx, args.brand_id)
    if item.get("builtin"):
        raise ToolError("内置品牌模板只读，不能删除", code="brand_readonly")
    return Preview(
        "删除用户品牌模板（只删模板记录；素材文件与已合成的成片保留）",
        [{"label": "品牌模板", "before": f"{item['name']}（{item['id']}）", "after": "（删除）"}],
        warnings=["正被任一方案选用时服务端会拒绝删除；需先为这些方案换用其他品牌"],
        focus=FOCUS,
    )


async def delete_brand(args: DeleteBrand, ctx: ToolContext) -> ToolOutput:
    result = await ctx.client.delete(f"/api/studio/brands/{args.brand_id}")
    if not result["ok"] and result["status"] in (403, 409):
        raise ToolError(f"删除品牌模板失败：{result['error']}", code=f"http_{result['status']}", value=result)
    check(result, "删除品牌模板")
    return ToolOutput(
        {"deleted": args.brand_id}, text=f"已删除品牌模板 {args.brand_id}。", mutation=MUTATION, focus=FOCUS
    )


class UploadBrandAsset(StrictModel):
    attachment: str = Field(
        pattern=r"^attachment://", description="图片（水印，建议透明 PNG）或视频（片头片尾，≤30 秒）附件引用"
    )
    purpose: Literal["watermark", "intro", "outro"] | None = Field(
        default=None,
        description="用途提示（只影响返回说明）；上传后用 update_brand 或 update_plan_options 的 "
        "brand_overrides 引用",
    )


async def preview_upload_brand_asset(args: UploadBrandAsset, ctx: ToolContext) -> Preview:
    return Preview(
        "上传品牌素材到本机数据目录（按内容去重，不改动任何模板或方案）",
        [{"label": "附件", "after": args.attachment}],
        focus=FOCUS,
    )


async def upload_brand_asset(args: UploadBrandAsset, ctx: ToolContext) -> ToolOutput:
    item = check(
        await ctx.client.upload("/api/studio/brand-assets", files={"file": args.attachment}), "上传品牌素材"
    )
    asset = {key: item[key] for key in ("sha256", "suffix", "kind", "name")}
    hint = {"watermark": "watermark.asset", "intro": "intro.asset", "outro": "outro.asset"}.get(
        args.purpose or "", ""
    )
    return ToolOutput(
        {"asset": asset, **{k: item.get(k) for k in ("duration", "width", "height", "alpha", "url")}},
        text=f"已上传品牌素材（{'图片' if item['kind'] == 'image' else '视频'}）：{asset}。"
        f"在 update_brand{('（' + hint + '）') if hint else ''} 或方案的 brand_overrides 中引用 asset。",
        mutation=MUTATION,
        focus=FOCUS,
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "list_brands",
            "studio_post",
            "列出品牌模板（内置与用户模板）：ID、名称、是否有片头/片尾/水印、说明。为方案选品牌前先调用它。",
            ListBrands,
            "read",
            list_brands,
        ),
        ToolSpec(
            "get_brand",
            "studio_post",
            "读取单个品牌模板：字幕样式、文字层样式、片头、片尾与水印。修改前先调用它。",
            GetBrand,
            "read",
            get_brand,
        ),
        ToolSpec(
            "copy_brand",
            "studio_post",
            "把内置或用户品牌模板复制为新的用户模板（内置模板只读，要定制须先复制）。",
            CopyBrand,
            "write",
            copy_brand,
            preview_copy_brand,
        ),
        ToolSpec(
            "update_brand",
            "studio_post",
            "修改用户品牌模板，只传要改的字段（name/description/subtitle/overlay/intro/outro/watermark；"
            "影响成片的字段传 null 去掉）。素材先用 upload_brand_asset "
            "上传。使用它的方案下次构建只重新合成成片。",
            UpdateBrand,
            "write",
            update_brand,
            preview_update_brand,
        ),
        ToolSpec(
            "delete_brand",
            "studio_post",
            "删除用户品牌模板（只删记录）。内置模板不能删除；正被方案选用时拒绝。",
            DeleteBrand,
            "danger",
            delete_brand,
            preview_delete_brand,
        ),
        ToolSpec(
            "upload_brand_asset",
            "studio_post",
            "上传品牌素材：图片（水印）或视频（片头片尾，≤30 秒），返回可在 update_brand / brand_overrides "
            "中引用的 asset。",
            UploadBrandAsset,
            "write",
            upload_brand_asset,
            preview_upload_brand_asset,
        ),
    ]
