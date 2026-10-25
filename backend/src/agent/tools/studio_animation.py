"""AI 动画源码工具（studio_detail 领域）：读取与修改 animation 分镜的 HTML/JS 动画源码。

全部经回环调用 ``/api/studio/...``：

- ``get_animation_source``：``GET /versions/{vid}/animation.html`` 读取源码（按字符分页，单次不超过工具文本上限），
  附版本信息、沙箱预览地址与当前录制画面；
- ``edit_animation_source``（write）：整份替换（``html``）或逐条查找替换（``replacements``，每个 ``find`` 必须恰好
  出现一次）。预览先以 ``dry_run`` 提交 ``PATCH /plans/{pid}/shots/{sid}/animation`` 做服务端静态校验，未通过时直接
  报告违规项；确认后正式提交，生成并选用 ``origin=manual`` 的动画版本，该分镜的渲染（重新录制）与成片随之重建。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from mindex.agent.ids import short
from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import PlanView, check, load_view
from mindex.agent.tools.media import animation_media
from mindex.agent.tools.shot_details import animation_source_url
from mindex.agent.tools.studio_detail import plan_url, shot_label, shot_ref, version_ref
from mindex.agent.tools.studio_write import JobId, PlanId, mutation, planned, studio_focus
from mindex.shared.config import StrictModel

PAGE_CHARS = 10000  # 单次读取的源码字符数上限（工具文本整体不超过 registry.MAX_TEXT）
ORIGINS = {"manual": "人工修改", "generated": "模型生成"}
REBUILD = "保存后该分镜的渲染（逐帧重新录制）与成片会在下次构建时重建；配音不受影响"
PINNED = "之后的构建保留这份人工源码，不会被重新生成覆盖；需要模型重写时用 run_node(stage=animate, force=true)"


def animation_shot(view: PlanView, ref: str) -> str:
    sid = shot_ref(view, ref)
    if ((view.content(sid) or {}).get("source") or {}).get("kind") != "animation":
        raise ToolError(f"分镜 {shot_label(view, sid)} 的画面来源不是 AI 动画；可先用 edit_shots 把来源设为 animation",
                        code="invalid_source")
    return sid


def animation_version(view: PlanView, sid: str, ref: str | None) -> dict | None:
    if ref:
        item = version_ref(view, ref, stage="animate")
        if item.get("shot_id") != sid:
            raise ToolError(f"版本 {ref} 不属于分镜 {shot_label(view, sid)}", code="unresolved_version")
        return item
    return view.version(view.plan["shots"][sid].get("animate"))


async def read_source(ctx: ToolContext, view: PlanView, item: dict) -> str:
    if item.get("status") != "complete" or "animation" not in (item.get("files") or {}):
        raise ToolError(f"动画版本 {short(item['id'])} 没有可读取的源码（状态 {item.get('status')}）",
                        code="animation_missing")
    data = check(await ctx.client.get(f"/api/studio/jobs/{view.job['id']}/versions/{item['id']}/animation.html"),
                 "读取动画源码")
    return data if isinstance(data, str) else str(data)


# ------------------------------------------------------------ 读取
class GetAnimationSource(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    shot: str = Field(description="animation 分镜：完整 ID、短 ID 或 #序号")
    version: str | None = Field(default=None, description="动画版本短 ID 或完整 ID；省略为当前选用的版本")
    offset: int = Field(default=0, ge=0, description="从第几个字符开始读取（分页）")
    limit: int = Field(default=PAGE_CHARS, ge=200, le=PAGE_CHARS, description=f"本次读取的字符数（≤{PAGE_CHARS}）")


async def get_animation_source(args: GetAnimationSource, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = animation_shot(view, args.shot)
    item = animation_version(view, sid, args.version)
    if item is None:
        raise ToolError(f"分镜 {shot_label(view, sid)} 还没有动画源码；可用 run_node(stage=animate) 生成，或构建时自动生成",
                        code="animation_missing")
    html = await read_source(ctx, view, item)
    part = html[args.offset:args.offset + args.limit]
    end = args.offset + len(part)
    data = item.get("data") or {}
    selected = view.plan["shots"][sid].get("animate") == item["id"]
    render = view.version(view.plan["shots"][sid].get("render"))
    recorded = bool(render and render.get("status") == "complete"
                    and (render.get("inputs") or {}).get("animate") == item["id"])
    url = animation_source_url(view, item["id"])
    lines = [
        f"分镜 {shot_label(view, sid)} 动画源码 版本 {short(item['id'])}（{ORIGINS.get(data.get('origin'), '未知来源')}"
        f"{'，当前选用' if selected else ''}）：共 {len(html)} 字符，{data.get('seconds')!r} 秒，"
        f"{data.get('width')}×{data.get('height')} @ {data.get('fps')} fps",
        f"画面说明：「{data.get('brief') or ''}」",
        f"沙箱预览：{url}；录制：{'当前渲染版本已按此源码录制' if recorded else '尚未按此源码录制（需重新渲染）'}",
    ]
    if args.offset or end < len(html):
        more = f"，后续请用 offset={end} 继续读取" if end < len(html) else ""
        lines.append(f"本次显示第 {args.offset + 1}–{end} 个字符{more}；修改时优先用 edit_animation_source 的 "
                     "replacements（查找替换），不必整份重写")
    lines.append("```html\n" + part + "\n```")
    value = {"shot_id": sid, "version_id": item["id"], "origin": data.get("origin"), "chars": len(html),
             "offset": args.offset, "returned": len(part), "has_more": end < len(html),
             "next_offset": end if end < len(html) else None, "preview_url": url, "recorded": recorded}
    return ToolOutput(value, text="\n".join(lines),
                      focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="animate"),
                      media=animation_media(view, sid) or None)


# ------------------------------------------------------------ 修改
class Replacement(StrictModel):
    find: str = Field(min_length=1, max_length=20000, description="要替换的原文片段（必须在当前源码中恰好出现一次）")
    replace: str = Field(max_length=200000, description="替换后的内容（可为空字符串表示删除）")


class EditAnimationSource(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    shot: str = Field(description="animation 分镜：完整 ID、短 ID 或 #序号")
    html: str | None = Field(default=None, min_length=1, max_length=2_000_000,
                             description="整份替换的完整 HTML 文档（与 replacements 二选一）")
    replacements: list[Replacement] | None = Field(
        default=None, min_length=1, max_length=50,
        description="按顺序应用到当前选用源码的查找替换（与 html 二选一）；先用 get_animation_source 读取原文")

    @model_validator(mode="after")
    def one_mode(self):
        if (self.html is None) == (self.replacements is None):
            raise ValueError("html 与 replacements 必须且只能提供一个")
        return self


def apply_replacements(html: str, replacements: list[Replacement]) -> str:
    for index, item in enumerate(replacements, 1):
        count = html.count(item.find)
        if count != 1:
            reason = "没有找到" if count == 0 else f"出现了 {count} 次，请给出更长、唯一的上下文"
            raise ToolError(f"第 {index} 条替换的 find {reason}；请先用 get_animation_source 读取当前源码",
                            code="replacement_mismatch", value={"index": index, "matches": count})
        html = html.replace(item.find, item.replace, 1)
    return html


async def preview_edit_animation_source(args: EditAnimationSource, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = animation_shot(view, args.shot)
    current = animation_version(view, sid, None)
    before = await read_source(ctx, view, current) if current and current.get("status") == "complete" else None
    if args.replacements is not None:
        if before is None:
            raise ToolError(f"分镜 {shot_label(view, sid)} 还没有可修改的动画源码；请提供完整 html，"
                            "或先用 run_node(stage=animate) 生成", code="animation_missing")
        after = apply_replacements(before, args.replacements)
    else:
        after = args.html
    if after == before:
        raise ToolError("修改后的源码与当前源码相同，无需保存", code="no_change")
    url = plan_url(view, f"/shots/{sid}/animation")
    result = check(await ctx.client.patch(url, {"revision": view.revision, "html": after, "dry_run": True}),
                   "校验动画源码")
    if not result.get("valid"):
        raise ToolError("动画源码未通过安全检查：" + "；".join(result.get("errors") or []) + "。请修正后重试",
                        code="animation_invalid", value=result)
    planned(ctx, "animation", {"job_id": view.job["id"], "plan_id": view.plan_id, "shot_id": sid, "url": url,
                               "body": {"revision": view.revision, "html": after}})
    size = (f"{len(before)} → {len(after)} 字符" if before is not None else f"新建 {len(after)} 字符")
    changes = [
        {"label": f"分镜 {shot_label(view, sid)} 动画源码", "before": f"版本 {short(current['id'])}" if current else "无",
         "after": "新的人工修改版本（origin=manual）"},
        {"label": "源码大小", "after": size},
        {"label": "静态校验", "after": f"通过（{result.get('bytes', 0) / 1024:.1f} KB / 上限 {result.get('max_kb')} KB）"},
    ]
    if args.replacements is not None:
        changes.append({"label": "修改方式", "after": f"查找替换 {len(args.replacements)} 处"})
    return Preview(f"保存分镜 {shot_label(view, sid)} 的动画源码", changes, warnings=[REBUILD, PINNED],
                   focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="animate"))


async def edit_animation_source(args: EditAnimationSource, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "animation")
    if stored is None:
        await preview_edit_animation_source(args, ctx)
        stored = planned(ctx, "animation")
    job, pid, sid = stored["job_id"], stored["plan_id"], stored["shot_id"]
    detail = check(await ctx.client.patch(stored["url"], stored["body"]), "保存动画源码")
    view = PlanView(detail, pid)
    vid = view.plan["shots"][sid].get("animate")
    return ToolOutput({"shot_id": sid, "animate_version": vid, "chars": len(stored["body"]["html"])},
                      text=f"已保存分镜 {shot_label(view, sid)} 的动画源码（人工版本 {short(vid)}，"
                           f"{len(stored['body']['html'])} 字符）。{REBUILD}；可用 run_node(stage=render) 只重新录制该镜，"
                           "或 run_build 重建成片。",
                      mutation=mutation(job, pid),
                      focus=studio_focus(job, pid, shots=[sid], stage="animate"))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("get_animation_source", "studio_detail",
                 "读取 AI 动画分镜（来源 animation）的 HTML/JS 动画源码：版本、来源（模型生成/人工修改）、时长与画布、"
                 "沙箱预览地址与当前录制画面；源码按字符分页（offset/limit）。修改源码前先用它读取原文。只读。",
                 GetAnimationSource, "read", get_animation_source),
        ToolSpec("edit_animation_source", "studio_detail",
                 "修改 AI 动画分镜的源码：整份替换（html）或查找替换（replacements，每个 find 须恰好出现一次）。"
                 "预览时先做服务端静态校验（禁止外部地址、网络与存储 API、外部脚本等），通过后保存为人工版本并选用；"
                 "该分镜的渲染（重新录制）与成片随之重建，之后的构建不会覆盖人工源码。",
                 EditAnimationSource, "write", edit_animation_source, preview_edit_animation_source),
    ]
