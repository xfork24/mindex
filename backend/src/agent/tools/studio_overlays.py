"""HTML 叠加层源码工具（studio_post 领域）：读取与修改分镜 HTML 文字层（engine=html）的透明叠加层源码。

- ``get_overlay_source``：``GET /versions/{vid}/overlay-{index}.html``
读取当前选用叠加层版本中某一层的源码（分页）；
- ``edit_overlay_source``（write）：整份替换某一层源码。预览先以 ``dry_run`` 做服务端静态校验；确认后
  ``PATCH /plans/{pid}/shots/{sid}/overlay`` 生成并选用含人工修改的叠加层版本（钉住），
该分镜的渲染与成片随之重建。
"""

from __future__ import annotations

from pydantic import Field

from agent.ids import short
from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import PlanView, check, load_view
from agent.tools.studio_detail import plan_url, shot_label, shot_ref
from agent.tools.studio_write import JobId, PlanId, mutation, planned, studio_focus
from shared.config import StrictModel

PAGE_CHARS = 10000


def html_layer(view: PlanView, ref: str, index: int) -> str:
    sid = shot_ref(view, ref)
    layers = (view.content(sid) or {}).get("overlays") or []
    if index >= len(layers) or layers[index].get("engine") != "html":
        raise ToolError(
            f"分镜 {shot_label(view, sid)} 的第 {index + 1} 个文字层不是 HTML 文字层；"
            "可先用 edit_shots 设置 overlays（engine=html）",
            code="invalid_overlay",
        )
    return sid


class GetOverlaySource(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    shot: str = Field(description="分镜：完整 ID、短 ID 或 #序号")
    index: int = Field(default=0, ge=0, le=3, description="文字层在分镜 overlays 中的序号（从 0 开始）")
    offset: int = Field(default=0, ge=0, description="从第几个字符开始读取（分页）")
    limit: int = Field(
        default=PAGE_CHARS, ge=200, le=PAGE_CHARS, description=f"本次读取的字符数（≤{PAGE_CHARS}）"
    )


async def get_overlay_source(args: GetOverlaySource, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = html_layer(view, args.shot, args.index)
    item = view.version(view.plan["shots"][sid].get("overlay"))
    if not item or f"overlay-{args.index}" not in (item.get("files") or {}):
        raise ToolError(
            f"分镜 {shot_label(view, sid)} 还没有这一层的叠加层源码；可用 run_node(stage=overlay) 生成",
            code="overlay_missing",
        )
    html = check(
        await ctx.client.get(
            f"/api/studio/jobs/{view.job['id']}/versions/{item['id']}/overlay-{args.index}.html"
        ),
        "读取叠加层源码",
    )
    html = html if isinstance(html, str) else str(html)
    part = html[args.offset : args.offset + args.limit]
    end = args.offset + len(part)
    origin = next(
        (
            layer.get("origin")
            for layer in (item.get("data") or {}).get("layers", [])
            if layer.get("index") == args.index
        ),
        None,
    )
    more = f"（未读完，下次 offset={end}）" if end < len(html) else ""
    text = (
        f"分镜 {shot_label(view, sid)} 第 {args.index + 1} 个文字层的叠加层源码（版本 {short(item['id'])}，"
        f"来源 {origin or '未知'}，共 {len(html)} 字符，本次 {args.offset}–{end}{more}）：\n{part}"
    )
    return ToolOutput(
        {
            "version": item["id"],
            "index": args.index,
            "origin": origin,
            "chars": len(html),
            "offset": args.offset,
            "end": end,
            "source": part,
        },
        text=text,
        focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="render"),
    )


class EditOverlaySource(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    shot: str = Field(description="分镜：完整 ID、短 ID 或 #序号")
    index: int = Field(default=0, ge=0, le=3, description="文字层在分镜 overlays 中的序号（从 0 开始）")
    html: str = Field(
        min_length=1,
        max_length=10_000_000,
        description="完整的新源码（自包含 HTML，背景必须透明，不能引用外部资源）",
    )


async def overlay_body(args: EditOverlaySource, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = html_layer(view, args.shot, args.index)
    return view, sid, {"revision": view.revision, "index": args.index, "html": args.html}


async def preview_edit_overlay_source(args: EditOverlaySource, ctx: ToolContext) -> Preview:
    view, sid, body = await overlay_body(args, ctx)
    url = plan_url(view) + f"/shots/{sid}/overlay"
    checked = check(await ctx.client.patch(url, {**body, "dry_run": True}), "校验叠加层源码")
    if not checked.get("valid"):
        raise ToolError(
            "叠加层源码未通过安全检查：" + "；".join(checked.get("errors") or []), code="overlay_rejected"
        )
    planned(ctx, "overlay", (view.job["id"], view.plan_id, sid, body))
    return Preview(
        f"保存分镜 {shot_label(view, sid)} 第 {args.index + 1} 个文字层的人工叠加层源码",
        [{"label": "源码", "after": f"{len(args.html)} 字符（{checked.get('bytes')} 字节）"}],
        warnings=["保存后该分镜的渲染（重新录制叠加层）与成片会在下次构建时重建；之后的构建保留人工源码"],
        focus=studio_focus(view.job["id"], view.plan_id, shots=[sid], stage="render"),
    )


async def edit_overlay_source(args: EditOverlaySource, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "overlay")
    if stored is None:
        view, sid, body = await overlay_body(args, ctx)
        stored = (view.job["id"], view.plan_id, sid, body)
    job, pid, sid, body = stored
    data = check(
        await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}/shots/{sid}/overlay", body),
        "保存叠加层源码",
    )
    vid = data["plans"][pid]["shots"][sid].get("overlay")
    return ToolOutput(
        {"version": vid, "index": body["index"]},
        text=f"已保存人工叠加层源码（版本 {short(vid or '')}）；下次构建重建该分镜的渲染与成片。",
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, shots=[sid], stage="render"),
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "get_overlay_source",
            "studio_post",
            "读取分镜 HTML 文字层（engine=html）的透明叠加层源码（按序号 index，分页）。修改前先调用它。",
            GetOverlaySource,
            "read",
            get_overlay_source,
        ),
        ToolSpec(
            "edit_overlay_source",
            "studio_post",
            "整份替换某个 HTML 文字层的叠加层源码（背景须透明）；先做安全检查，保存后该版本被钉住，"
            "只重建该分镜的渲染与成片。",
            EditOverlaySource,
            "write",
            edit_overlay_source,
            preview_edit_overlay_source,
        ),
    ]
