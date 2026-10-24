"""Studio 只读工具：任务列表、工作台摘要、短 ID 解析、构建预检、构建状态、版本历史。"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from mindex.agent.ids import resolve_shot, short
from mindex.agent.registry import ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import STAGE_LABELS, STAGES, board_text, check, clip, load_view
from mindex.agent.tools.media import final_media, shot_media, version_media
from mindex.agent.tools.shot_details import (
    details_focus,
    page_details,
    plan_header,
    render_list,
    render_refs,
    resolve_refs,
)
from mindex.shared.config import StrictModel

JobId = Field(default=None, description="创作任务 ID；省略时使用当前页面打开的任务")
PlanId = Field(default=None, description="方案 ID；省略时使用任务当前激活的方案")


class ListJobs(StrictModel):
    limit: int = Field(default=20, ge=1, le=100, description="最多返回的任务数，按创建时间倒序")


async def list_jobs(args: ListJobs, ctx: ToolContext) -> ToolOutput:
    items = check(await ctx.client.get("/api/studio/jobs"), "列出任务")["items"][: args.limit]
    rows = [{"id": i["id"], "title": i["title"], "created_at": i["created_at"]} for i in items]
    text = "\n".join(f"{r['id']} | {r['title']} | {r['created_at']}" for r in rows) or "（还没有创作任务）"
    return ToolOutput({"items": rows}, text=f"共 {len(rows)} 个任务（ID | 标题 | 创建时间）：\n{text}",
                      meta={"kind": "jobs", "items": rows}, focus={"page": "studio", "section": "jobs"})


class WorkspaceSummary(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId


async def get_workspace_summary(args: WorkspaceSummary, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    p, w = view.plan, view.w
    builds = [b for b in w["builds"] if b["plan_id"] == view.plan_id][-3:]
    value = {
        "job": {"id": view.job["id"], "title": view.job["title"], "outline": view.job.get("outline", ""),
                "source_ids": view.job.get("source_ids")},
        "plans": [{"id": k, "name": v["name"], "active": k == w["active_plan"]} for k, v in w["plans"].items()],
        "plan": {"id": view.plan_id, "name": p["name"], "revision": view.revision, "options": p["options"],
                 "final": short(p.get("final")), "script_stale": bool(w.get("script_stale")) if view.active else None},
        "script": view.script(),
        "shots": view.shot_rows(),
        "recent_builds": [{"id": b["id"], "status": b["status"], "created_at": b["created_at"]} for b in builds],
        "tts": w.get("tts_settings"),
    }
    lines = [
        f"任务 {view.job['id']}「{view.job['title']}」",
        "方案：" + "；".join(f"{x['name']}({x['id']}){'【当前】' if x['active'] else ''}" for x in value["plans"]),
        f"查看方案 {p['name']}({view.plan_id}) revision={view.revision}",
        f"方案参数：{p['options']}",
        f"文案：{clip(view.script(), 300) if view.script() else '（尚无文案）'}",
    ]
    if value["plan"]["script_stale"]:
        lines.append("注意：文案已变化，现有分镜按旧文案拆出，构建时会重新拆分镜。")
    lines.append(f"分镜 {len(view.order)} 个（状态标记 ✓完成 ✗失败 —缺失）：")
    lines.append(board_text(view))
    lines.append(f"成片版本：{short(p.get('final')) or '（尚无成片）'}")
    if builds:
        lines.append("最近构建：" + "；".join(f"{b['id']} {b['status']}" for b in builds))
    return ToolOutput(value, text="\n".join(lines),
                      meta={"kind": "workspace", "job_id": view.job["id"], "plan_id": view.plan_id},
                      focus={"page": "studio", "job_id": view.job["id"], "plan_id": view.plan_id,
                             "section": "storyboard"},
                      media=final_media(view))


class ResolveShots(StrictModel):
    refs: list[str] = Field(min_length=1, max_length=50,
                            description="用户提到的分镜：完整 ID、界面短 ID（如 a1daad59 或 shot-a1daad59）或 #序号")
    job_id: str | None = JobId
    plan_id: str | None = PlanId


async def resolve_shots(args: ResolveShots, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    order = view.order
    results = []
    for ref in args.refs:
        item = resolve_shot(order, ref)
        if item["status"] == "ok":
            item["text"] = clip(view.content(item["shot_id"])["text"])
        results.append(item)
    lines = []
    for item in results:
        if item["status"] == "ok":
            lines.append(f"{item['ref']} → #{item['index']} {item['shot_id']}「{item['text']}」")
        elif item["status"] == "ambiguous":
            options = "、".join(f"#{c['index']} {c['shot_id']}" for c in item["candidates"])
            lines.append(f"{item['ref']} → 有歧义，候选：{options}（请向用户确认）")
        else:
            lines.append(f"{item['ref']} → 未找到{('：' + item['reason']) if item.get('reason') else ''}")
    shots = [item["shot_id"] for item in results if item["status"] == "ok"]
    focus = {"page": "studio", "job_id": view.job["id"], "plan_id": view.plan_id}
    if shots:
        focus["shots"] = shots
    else:
        focus["section"] = "storyboard"
    media = [entry for sid in shots[:4] for entry in shot_media(view, sid)]
    return ToolOutput({"plan_id": view.plan_id, "results": results}, text="\n".join(lines), focus=focus, media=media)


class ShotDetailsArgs(StrictModel):
    refs: list[str] | None = Field(
        default=None, min_length=1, max_length=50,
        description="要读取的分镜：完整 ID、界面短 ID（如 a1daad59 或 shot-a1daad59）或 #序号；省略时按顺序读取全部分镜")
    offset: StrictInt | None = Field(
        default=None, ge=0, description="仅在省略 refs 时使用：从第几个分镜开始（从 0 计），默认 0")
    limit: StrictInt | None = Field(
        default=None, ge=1, le=100, description="仅在省略 refs 时使用：最多返回多少个分镜（1–100），默认 100")
    job_id: str | None = JobId
    plan_id: str | None = PlanId

    @field_validator("refs")
    @classmethod
    def no_blank(cls, value):
        if value is not None and any(not item.strip() for item in value):
            raise ValueError("refs 不能包含空字符串或仅含空白的项")
        return value

    @model_validator(mode="after")
    def exclusive(self):
        if self.refs is not None and (self.offset is not None or self.limit is not None):
            raise ValueError("refs 与 offset/limit 不能同时提供")
        return self


async def get_shot_details(args: ShotDetailsArgs, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    header = plan_header(view)
    if args.refs is None:
        offset = 0 if args.offset is None else args.offset
        limit = 100 if args.limit is None else args.limit
        details = page_details(view, offset, limit)
        text, shown = render_list(header, details, offset)
        has_more = offset + shown < header["total"]
        value = {**header, "mode": "list", "offset": offset, "returned": shown, "has_more": has_more,
                 "next_offset": offset + shown if has_more else None, "shots": details[:shown]}
        focus = details_focus(view, None)
    else:
        items = resolve_refs(view, args.refs)
        text, rendered = render_refs(header, items)
        value = {**header, "mode": "refs", "returned": sum(item["status"] == "ok" for item in items),
                 "unrendered_refs": [item["ref"] for item in items[rendered:] if item["status"] == "ok"],
                 "results": items}
        focus = details_focus(view, items)
    return ToolOutput(value, text=text,
                      meta={"kind": "shot_details", "job_id": view.job["id"], "plan_id": view.plan_id},
                      focus=focus)


class Preflight(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    force: bool = Field(default=False, description="true 表示忽略可复用版本，全部重新生成")


def summarize_nodes(nodes: list[dict]) -> tuple[dict, list[str]]:
    counts = Counter(n["action"] for n in nodes)
    lines = []
    for n in nodes:
        if n["action"] == "reuse":
            continue
        target = f"{STAGE_LABELS.get(n['stage'], n['stage'])}" + (f" {short(n['shot_id'])}" if n["shot_id"] else "")
        lines.append(f"- {target}：{ {'rebuild': '重建', 'blocked': '阻塞', 'skip': '跳过'}.get(n['action'], n['action'])}"
                     f"（{n.get('reason', '')}）")
    return dict(counts), lines


async def get_preflight(args: Preflight, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id, args.plan_id)
    data = check(await ctx.client.get(
        f"/api/studio/jobs/{view.job['id']}/plans/{view.plan_id}/preflight", force=str(args.force).lower()),
        "构建预检")
    counts, lines = summarize_nodes(data["nodes"])
    text = (f"预检（方案 {view.plan_id}）：重建 {counts.get('rebuild', 0)}，复用 {counts.get('reuse', 0)}，"
            f"阻塞 {counts.get('blocked', 0)}，跳过 {counts.get('skip', 0)}\n" + ("\n".join(lines) or "全部可复用"))
    shots = list(dict.fromkeys(n["shot_id"] for n in data["nodes"]
                               if n["shot_id"] and n["action"] in ("rebuild", "blocked")))[:20]
    focus = {"page": "studio", "job_id": view.job["id"], "plan_id": view.plan_id, "section": "storyboard"}
    if shots:
        focus["shots"] = shots
    return ToolOutput({"counts": counts, "nodes": data["nodes"], "token": data["token"]}, text=text,
                      meta={"kind": "preflight", "counts": counts}, focus=focus)


class BuildStatus(StrictModel):
    job_id: str | None = JobId
    build_id: str | None = Field(default=None, description="构建 ID；省略时取当前方案最近一次构建")
    log_lines: int = Field(default=20, ge=0, le=200, description="返回最近多少行执行日志")


async def get_build_status(args: BuildStatus, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id)
    builds = view.w["builds"]
    if args.build_id:
        build = next((b for b in builds if b["id"] == args.build_id), None)
    else:
        build = next((b for b in reversed(builds) if b["plan_id"] == view.plan_id), None)
    if build is None:
        raise ToolError("没有找到构建记录", code="missing_build")
    counts = dict(Counter(n.get("status", "pending") for n in build["nodes"]))
    problems = [
        {"key": n["key"], "stage": n["stage"], "shot_id": n["shot_id"], "status": n["status"], "error": n.get("error")}
        for n in build["nodes"] if n.get("status") in ("failed", "blocked")
    ]
    logs = []
    if args.log_lines:
        result = await ctx.client.get(f"/api/studio/jobs/{view.job['id']}/builds/{build['id']}/logs")
        if result["ok"]:
            logs = [
                f"{(i.get('stage') or '')} {short(i.get('shot_id')) or ''} {i.get('message', '')}".strip()
                for i in result["data"]["items"]
            ][-args.log_lines:]
    value = {"build_id": build["id"], "plan_id": build["plan_id"], "status": build["status"], "counts": counts,
             "problems": problems, "error": build.get("error"), "logs": logs}
    lines = [f"构建 {build['id']}：{build['status']}，节点状态 {counts}"]
    if build.get("error"):
        lines.append(f"错误：{build['error']}")
    for item in problems:
        lines.append(f"- {STAGE_LABELS.get(item['stage'], item['stage'])} {short(item['shot_id']) or ''}"
                     f" {item['status']}：{item['error']}")
    if logs:
        lines.append("最近日志：\n" + "\n".join(logs))
    focus = {"page": "studio", "job_id": view.job["id"], "plan_id": build["plan_id"], "section": "log",
             "build_id": build["id"]}
    failed = list(dict.fromkeys(item["shot_id"] for item in problems if item["shot_id"]))[:20]
    if failed:
        focus["shots"] = failed
    return ToolOutput(value, text="\n".join(lines),
                      meta={"kind": "build", "job_id": view.job["id"], "build_id": build["id"], "status": build["status"]},
                      focus=focus, media=final_media(view) if build["status"] == "complete" else None)


class ListVersions(StrictModel):
    job_id: str | None = JobId
    stage: Literal["script", "content", "recall", "tts", "select", "render", "compose"] | None = None
    shot: str | None = Field(default=None, description="分镜：完整 ID、短 ID 或 #序号")
    limit: int = Field(default=20, ge=1, le=100)


async def list_versions(args: ListVersions, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id)
    shot_id = None
    if args.shot:
        resolved = resolve_shot(view.order, args.shot)
        if resolved["status"] != "ok":
            raise ToolError(f"无法确定分镜 {args.shot}：{resolved['status']}", code="unresolved_shot", value=resolved)
        shot_id = resolved["shot_id"]
    selected = {view.plan.get("script"), view.plan.get("final"), view.plan.get("board")}
    for item in view.plan["shots"].values():
        selected.update(item.values())
    items = [
        v for v in view.w["versions"].values()
        if (args.stage is None or v["stage"] == args.stage) and (shot_id is None or v["shot_id"] == shot_id)
    ][-args.limit:]
    rows = [{"version_id": v["id"], "short_id": short(v["id"]), "stage": v["stage"], "shot_id": v["shot_id"],
             "status": v["status"], "created_at": v["created_at"], "selected": v["id"] in selected,
             "error": v.get("error")} for v in reversed(items)]
    text = "\n".join(
        f"{r['short_id']} {STAGE_LABELS.get(r['stage'], r['stage'])} {short(r['shot_id']) or ''} {r['status']} "
        f"{r['created_at']}{' 【当前选用】' if r['selected'] else ''}{(' 错误：' + r['error']) if r['error'] else ''}"
        for r in rows
    ) or "（没有匹配的版本）"
    focus = {"page": "studio", "job_id": view.job["id"], "plan_id": view.plan_id}
    if shot_id:
        focus.update(shots=[shot_id], stage=args.stage if args.stage in ("content", *STAGES) else "content")
    elif args.stage == "script":
        focus["section"] = "script"
    elif args.stage == "compose":
        focus["section"] = "final"
    else:
        focus["section"] = "storyboard"
    media = [entry for row in rows[:10]
             for entry in version_media(view.version(row["version_id"]),
                                        f"{STAGE_LABELS.get(row['stage'], row['stage'])} {row['short_id']}",
                                        collapsed=row["stage"] != "compose")]
    return ToolOutput({"items": rows}, text=text, focus=focus, media=media)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_jobs", "studio", "列出创作任务（ID、标题、创建时间）。", ListJobs, "read", list_jobs),
        ToolSpec("get_workspace_summary", "studio",
                 "读取创作任务的工作台：方案、方案参数、revision、文案、分镜表（序号、短ID、完整ID、旁白、查询、各阶段状态；"
                 "旁白与画面意象截断到 60 字、查询截断到 30 字，完整内容用 get_shot_details 读取）、"
                 "成片与最近构建。修改分镜或方案前先调用它获取最新状态。",
                 WorkspaceSummary, "read", get_workspace_summary),
        ToolSpec("resolve_shots", "studio",
                 "把用户提到的分镜（界面短 ID、完整 ID 或 #序号）解析为完整分镜 ID；有歧义时返回候选，须向用户确认。",
                 ResolveShots, "read", resolve_shots),
        ToolSpec("get_shot_details", "studio",
                 "读取分镜的完整详情（不截断）：旁白、查询、画面意象、预估时长、召回粒度/数量与召回设置、人工区间、"
                 "召回/配音/选片/渲染各阶段状态、字幕文本与时间轴（秒）、配音时长（秒）。省略 refs 时按顺序分页读取全部分镜"
                 "（offset/limit，结果给出下一次 offset）；提供 refs（短 ID、完整 ID 或 #序号）时读取指定分镜。"
                 "拆分、合并或改写旁白前必须先用它读取完整旁白。",
                 ShotDetailsArgs, "read", get_shot_details),
        ToolSpec("get_preflight", "studio",
                 "构建预检：列出构建成片时哪些节点会重建、复用、阻塞或跳过，以及阻塞原因。不执行任何构建。",
                 Preflight, "read", get_preflight),
        ToolSpec("get_build_status", "studio", "查看构建执行状态、失败或阻塞的节点及原因、最近执行日志。",
                 BuildStatus, "read", get_build_status),
        ToolSpec("list_versions", "studio", "列出产物版本历史（可按阶段与分镜过滤），标出当前选用的版本。",
                 ListVersions, "read", list_versions),
    ]
