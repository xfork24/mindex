"""交付工具（studio_post 领域）：多画幅导出设置；后续的平台预设、封面、发布元数据与发布包也在这里。

- ``set_exports``（write）：整体替换方案的多画幅导出（``options.exports``）。derive 快速派生只重建导出；
  rerender 会自动创建或同步一个按该画幅设置的关联子方案，用 ``run_build(all_canvases=true)`` 一起构建。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check, load_view
from mindex.agent.tools.studio_write import JobId, PlanId, mutation, planned, studio_focus
from mindex.shared.config import StrictModel
from mindex.studio.platforms import PlatformId, for_plan

MODES = {"derive": "快速派生", "rerender": "重新渲染"}
FITS = {"crop": "裁切铺满", "pad": "加边完整保留"}


class ExportArgs(StrictModel):
    canvas: Literal["16:9", "9:16", "1:1"] = Field(description="导出画幅（不能与方案画幅相同）")
    fit: Literal["pad", "crop"] | None = Field(default=None, description="crop 裁切铺满（缺省）/ pad 加边完整保留")
    mode: Literal["derive", "rerender"] | None = Field(
        default=None, description="derive 由成片快速派生（缺省）；rerender 用关联子方案逐镜重新渲染，画质更好、耗时更长")


class SetExports(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    exports: list[ExportArgs] = Field(max_length=2, description="整体替换；空列表表示不再导出其他画幅")


def exports_text(items) -> str:
    if not items:
        return "不导出其他画幅"
    return "；".join(f"{item['canvas']} {MODES[item.get('mode', 'derive')]}（{FITS[item.get('fit', 'crop')]}）"
                    for item in items)


async def exports_body(args: SetExports, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    if (view.plan.get("variant_of") or {}).get("plan"):
        raise ToolError("这是多画幅重新渲染的子方案；请在主方案上设置导出", code="variant_plan")
    exports = [{"canvas": item.canvas, "fit": item.fit or "crop", "mode": item.mode or "derive"} for item in args.exports]
    options = {key: value for key, value in view.plan["options"].items() if key != "references"}
    body = {"revision": view.revision, "options": {**options, "exports": exports or None}}
    return view, exports, body


async def preview_set_exports(args: SetExports, ctx: ToolContext) -> Preview:
    view, exports, body = await exports_body(args, ctx)
    planned(ctx, "exports", (view.job["id"], view.plan_id, body))
    warnings = ["快速派生只在下次构建时重建导出成片，分镜与主成片照常复用"]
    if any(item["mode"] == "rerender" for item in exports):
        warnings.append("重新渲染会创建或同步按该画幅设置的子方案；用 run_build(all_canvases=true) 构建全部画幅，"
                        "子方案逐镜重新渲染（配音、选片复用），耗时较长")
    return Preview(f"修改方案「{view.plan['name']}」的多画幅导出",
                   [{"label": "多画幅导出", "before": exports_text(view.plan["options"].get("exports")),
                     "after": exports_text(exports)}], warnings=warnings,
                   focus=studio_focus(view.job["id"], view.plan_id, section="final"))


async def set_exports(args: SetExports, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "exports")
    if stored is None:
        view, _, body = await exports_body(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "修改多画幅导出")
    children = [item["id"] for item in data["plans"].values() if (item.get("variant_of") or {}).get("plan") == pid]
    note = f"；关联子方案：{'、'.join(children)}，用 run_build(all_canvases=true) 一起构建" if children else ""
    return ToolOutput({"exports": data["plans"][pid]["options"].get("exports"), "children": children},
                      text=f"已设置多画幅导出：{exports_text(data['plans'][pid]['options'].get('exports'))}{note}。",
                      mutation=mutation(job, pid), focus=studio_focus(job, pid, section="final"))


def specs() -> list[ToolSpec]:
    return [*publish_specs(),
        ToolSpec("set_exports", "studio_post",
                 "设置方案的多画幅导出（整体替换，最多两个其他画幅）：derive 由成片快速派生并按新画幅重新排版字幕、"
                 "文字层与水印；rerender 维护按该画幅设置的子方案逐镜重新渲染，用 run_build(all_canvases=true) 一起构建。",
                 SetExports, "write", set_exports, preview_set_exports),
    ]


# ------------------------------------------------------------ 平台发布
def platform_name(value, key):
    return (value or {}).get("platform_specs", {}).get(key, {}).get("name", key)


COVER_SOURCES = {"frame": "成片挑帧", "aigc": "AI 生成", "upload": "上传图片"}


class ListPlatforms(StrictModel):
    pass


async def list_platforms(args: ListPlatforms, ctx: ToolContext) -> ToolOutput:
    items = check(await ctx.client.get("/api/studio/platforms"), "读取平台预设")["items"]
    lines = [f"{item['id']} | {item['name']} | 画幅 {item['canvas']} | 封面 {item['cover']['width']}×{item['cover']['height']} | "
             f"标题 ≤{item['title']['max_chars']} 字 | 简介 ≤{item['description']['max_chars']} 字 | 标签 ≤{item['tags']['max_count']} 个"
             f" | 章节 {'支持' if item['chapters']['enabled'] else '不支持'}" for item in items]
    return ToolOutput({"items": items}, text="平台预设：\n" + "\n".join(lines) + "\n用 set_publish 为方案选择平台。")


class CoverArgs(StrictModel):
    source: Literal["frame", "aigc", "upload"] | None = Field(
        default=None, description="frame 成片挑帧（缺省，time 省略时自动选最清晰的镜头）；aigc 图片生成模型按 prompt 生成；"
                                  "upload 用 attach_query_media(purpose=source) 上传的图片 media_id")
    time: float | None = Field(default=None, ge=0, description="frame：成片中的秒数（可先用 cover_candidates 查看候选）")
    prompt: str | None = Field(default=None, max_length=1500, description="aigc：封面画面提示词")
    media_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="upload：图片 media_id")
    title: str | None = Field(default=None, max_length=40, description="封面标题；省略用发布元数据的标题")
    subtitle: str | None = Field(default=None, max_length=60, description="封面副标题")
    text: bool | None = Field(default=None, description="false=不叠加文字")


class SetPublish(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    platforms: list[PlatformId] = Field(
        description="发布平台标识（list_platforms 列出）；整体替换，空列表表示不再发布")
    cover: CoverArgs | None = Field(default=None, description="封面设置（整体替换）；省略保留现有设置")


def publish_text(value) -> str:
    if not value:
        return "不发布"
    cover = value.get("cover") or {}
    return (f"平台 {'、'.join(platform_name(value, item) for item in value['platforms'])}；封面 "
            f"{COVER_SOURCES[cover.get('source', 'frame')]}" + (f"（{cover['time']:g} 秒）" if cover.get("time") is not None
                                                             else "") + (f"，标题「{cover['title']}」" if cover.get("title") else ""))


async def publish_body(args: SetPublish, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    current = view.plan["options"].get("publish") or {}
    value = None
    if args.platforms:
        cover = args.cover.model_dump(exclude_none=True) if args.cover else (current.get("cover") or {})
        catalogue = {item["id"]: item for item in check(await ctx.client.get("/api/studio/platforms"), "读取平台")["items"]}
        specs = {}
        for key in args.platforms:
            if key in current.get("platforms", []):
                specs[key] = for_plan(view.plan, key).model_dump()
            elif key in catalogue:
                specs[key] = catalogue[key]
            else:
                raise ToolError(f"找不到平台：{key}", code="platform_missing")
        value = {"platforms": list(dict.fromkeys(args.platforms)), "cover": cover, "platform_specs": specs}
    options = {key: item for key, item in view.plan["options"].items() if key != "references"}
    return view, current or None, value, {"revision": view.revision, "options": {**options, "publish": value}}


async def preview_set_publish(args: SetPublish, ctx: ToolContext) -> Preview:
    view, before, after, body = await publish_body(args, ctx)
    planned(ctx, "publish", (view.job["id"], view.plan_id, body))
    warnings = []
    canvas = view.plan["options"].get("canvas") or "16:9"
    exports = {item["canvas"] for item in view.plan["options"].get("exports") or []}
    wanted = {key: spec["canvas"] for key, spec in (after or {}).get("platform_specs", {}).items()}
    for platform in (after or {}).get("platforms", []):
        if wanted[platform] != canvas and wanted[platform] not in exports:
            warnings.append(f"{platform_name(after, platform)} 需要 {wanted[platform]} 画幅，方案没有该画幅的导出，"
                            "发布包会用方案成片（画幅不符）；可先用 set_exports 增加导出")
    if after:
        warnings.append("下次构建会生成发布元数据（调用文案模型）、封面与发布包；用 build_publish_package 构建")
    return Preview(f"修改方案「{view.plan['name']}」的平台发布设置",
                   [{"label": "平台发布", "before": publish_text(before), "after": publish_text(after)}],
                   warnings=warnings, focus=studio_focus(view.job["id"], view.plan_id, section="final"))


async def set_publish(args: SetPublish, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "publish")
    if stored is None:
        view, _, _, body = await publish_body(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "修改平台发布设置")
    value = data["plans"][pid]["options"].get("publish")
    return ToolOutput({"publish": value}, text=f"已设置：{publish_text(value)}。", mutation=mutation(job, pid),
                      focus=studio_focus(job, pid, section="final"))


class CoverCandidates(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    platform: PlatformId
    limit: int = Field(default=6, ge=1, le=20)


async def cover_candidates(args: CoverCandidates, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    data = check(await ctx.client.get(f"/api/studio/jobs/{view.job['id']}/plans/{view.plan_id}/cover-candidates",
                                      platform=args.platform, limit=args.limit), "读取封面候选帧")
    lines = [f"{item['time']:g} 秒（分镜 {item['shot_id'][:8]}，清晰度 {item['sharpness']:g}）" for item in data["items"]]
    return ToolOutput(data, text="封面候选帧（按清晰度）：\n" + "\n".join(lines) + "\n用 set_publish 的 cover.time 选用。",
                      images=[item["url"] for item in data["items"][:4]])


class GetPublishMetadata(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    platform: PlatformId


def metadata_of(view, platform) -> dict | None:
    vid = (view.plan.get("finals") or {}).get(f"metadata:{platform}")
    return view.version(vid) if vid else None


async def get_publish_metadata(args: GetPublishMetadata, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    item = metadata_of(view, args.platform)
    if not item or item.get("status") != "complete":
        raise ToolError(f"{platform_name(view.plan['options'].get('publish'), args.platform)} 还没有发布元数据；先 set_publish 再构建", code="metadata_missing")
    data = item["data"]
    text = (f"{platform_name(view.plan['options'].get('publish'), args.platform)} 发布元数据（{'人工修改' if data.get('origin') == 'manual' else '模型生成'}）：\n"
            f"标题：{data['title']}\n简介：\n{data['description']}\n标签：{'、'.join(data['tags'])}")
    if data.get("notes"):
        text += "\n校验修正：" + "；".join(data["notes"])
    return ToolOutput({key: data.get(key) for key in ("title", "description", "body", "tags", "chapters", "origin")},
                      text=text)


class EditPublishMetadata(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    platform: PlatformId
    title: str | None = Field(default=None, min_length=1, max_length=200, description="新标题")
    description: str | None = Field(default=None, max_length=10000, description="新简介正文（章节、署名与话题由系统附加）")
    tags: list[str] | None = Field(default=None, max_length=50, description="新标签（整体替换）")


async def metadata_body(args: EditPublishMetadata, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    item = metadata_of(view, args.platform)
    if not item or item.get("status") != "complete":
        raise ToolError("还没有发布元数据，请先构建生成", code="metadata_missing")
    changes = args.model_dump(exclude_none=True, include={"title", "description", "tags"})
    if not changes:
        raise ToolError("至少修改标题、简介或标签之一", code="no_changes")
    return view, item, {"revision": view.revision, **changes}


async def preview_edit_publish_metadata(args: EditPublishMetadata, ctx: ToolContext) -> Preview:
    view, item, body = await metadata_body(args, ctx)
    planned(ctx, "metadata", (view.job["id"], view.plan_id, args.platform, body))
    labels = {"title": "标题", "description": "简介正文", "tags": "标签"}
    before = {"title": item["data"]["title"], "description": item["data"].get("body"), "tags": "、".join(item["data"]["tags"])}
    rows = [{"label": labels[key], "before": before[key], "after": "、".join(value) if key == "tags" else value}
            for key, value in body.items() if key in labels]
    return Preview(f"修改 {platform_name(view.plan['options'].get('publish'), args.platform)} 的发布元数据（按平台限制校验修正，保存后被钉住）", rows,
                   warnings=["封面（标题来自元数据时）与发布包会在下次构建时重建"],
                   focus=studio_focus(view.job["id"], view.plan_id, section="final"))


async def edit_publish_metadata(args: EditPublishMetadata, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "metadata")
    if stored is None:
        view, _, body = await metadata_body(args, ctx)
        stored = (view.job["id"], view.plan_id, args.platform, body)
    job, pid, platform, body = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}/publish-metadata/{platform}", body),
                 "修改发布元数据")
    vid = data["plans"][pid]["finals"][f"metadata:{platform}"]
    saved = data["versions"][vid]["data"]
    return ToolOutput({"version": vid, "title": saved["title"], "tags": saved["tags"]},
                      text=f"已保存 {platform_name(data['plans'][pid]['options'].get('publish'), platform)} 发布元数据：「{saved['title']}」。", mutation=mutation(job, pid))


class BuildPublishPackage(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    all_canvases: bool = Field(default=True, description="同时构建重新渲染导出的子方案（缺省 true）")


async def build_body(args: BuildPublishPackage, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    if not (view.plan["options"].get("publish") or {}).get("platforms"):
        raise ToolError("方案还没有设置发布平台；先用 set_publish", code="publish_missing")
    rerender = any(item.get("mode") == "rerender" for item in view.plan["options"].get("exports") or [])
    params = {"all_canvases": "true"} if args.all_canvases and rerender else {}
    data = check(await ctx.client.get(f"/api/studio/jobs/{view.job['id']}/plans/{view.plan_id}/preflight", **params),
                 "构建预检")
    return view, data, bool(params)


async def preview_build_publish_package(args: BuildPublishPackage, ctx: ToolContext) -> Preview:
    view, data, all_canvases = await build_body(args, ctx)
    planned(ctx, "package", (view.job["id"], view.plan_id, data["token"], all_canvases))
    counts = {}
    for node in data["nodes"]:
        counts[node["action"]] = counts.get(node["action"], 0) + 1
    packages = [node["key"] for node in data["nodes"] if node["stage"] == "package"]
    warnings = [f"{counts.get('blocked')} 个节点被阻塞" ] if counts.get("blocked") else []
    return Preview("构建发布包（成片、导出、发布元数据、封面与发布包；后台执行，可能调用模型服务）",
                   [{"label": "重建", "after": counts.get("rebuild", 0)}, {"label": "复用", "after": counts.get("reuse", 0)},
                    {"label": "发布包", "after": "、".join(packages)}], warnings=warnings, value={"counts": counts},
                   focus=studio_focus(view.job["id"], view.plan_id, section="final"))


async def build_publish_package(args: BuildPublishPackage, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "package")
    if stored is None:
        view, data, all_canvases = await build_body(args, ctx)
        stored = (view.job["id"], view.plan_id, data["token"], all_canvases)
    job, pid, token, all_canvases = stored
    build = check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/build",
                                        {"token": token, **({"all_canvases": True} if all_canvases else {})}), "提交构建")
    target = {"job_id": job, "build_id": build["id"], "plan_id": pid}
    return ToolOutput({"build_id": build["id"], "nodes": len(build["nodes"])},
                      text=f"已提交构建 {build['id']}；结束后发布包可在创作页“交付”区或 mindex studio publish download 下载。",
                      meta={"kind": "build", **target}, mutation=mutation(job, pid), watch_build=target,
                      focus=studio_focus(job, pid, section="log", build_id=build["id"]))


def publish_specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_platforms", "studio_post", "列出平台发布预设（哔哩哔哩、抖音、YouTube）：画幅、封面尺寸与标题/简介/标签/章节限制。",
                 ListPlatforms, "read", list_platforms),
        ToolSpec("set_publish", "studio_post",
                 "设置方案的发布平台与封面（整体替换）：封面可成片挑帧、AI 生成或上传图片，并叠加标题与副标题。"
                 "平台画幅与方案不同时先用 set_exports 增加该画幅的导出。",
                 SetPublish, "write", set_publish, preview_set_publish),
        ToolSpec("cover_candidates", "studio_post", "列出某平台成片的封面候选帧（各镜头中点，按清晰度排序），附缩略图。",
                 CoverCandidates, "read", cover_candidates),
        ToolSpec("get_publish_metadata", "studio_post", "读取某平台的发布元数据：标题、简介（含章节与署名）、标签与校验修正说明。",
                 GetPublishMetadata, "read", get_publish_metadata),
        ToolSpec("edit_publish_metadata", "studio_post",
                 "人工修改某平台的发布元数据（标题、简介正文、标签），按平台限制校验修正；保存后被钉住，封面与发布包重建。",
                 EditPublishMetadata, "write", edit_publish_metadata, preview_edit_publish_metadata),
        ToolSpec("build_publish_package", "studio_post",
                 "构建发布包：成片、多画幅导出、发布元数据、封面与各平台发布包（zip），后台执行并跟踪进度。",
                 BuildPublishPackage, "danger", build_publish_package, preview_build_publish_package),
    ]
