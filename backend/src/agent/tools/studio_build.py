"""Studio 构建与删除工具：整体构建、单节点执行、删除任务、删除方案（danger，每次显式确认）；
取消构建（write，可按会话授权）。

构建提交后立即返回；运行时登记 ``plugin:mindex/build-watch``，构建结束时写入结果并自动触发一轮总结。
同一时间全局只能有一个构建（studio.lock），冲突返回 409，作为可恢复结果交给模型。
取消只是请求：执行器在下一个节点开始前停止，结束（cancelled）时同样经跟踪触发总结。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mindex.agent.ids import resolve_shot, short
from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import STAGE_LABELS, PlanView, check, load_view
from mindex.agent.tools.studio_read import summarize_nodes
from mindex.agent.tools.studio_write import JobId, PlanId, mutation, planned, studio_focus
from mindex.shared.config import StrictModel


class RunBuild(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    force: bool = Field(default=False, description="true=忽略可复用版本全部重新生成（耗时且可能产生费用），默认只重建变化部分")
    all_canvases: bool = Field(default=False, description="true=同时构建多画幅“重新渲染”导出的关联子方案（构建全部画幅）")


async def build_plan(args: RunBuild, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    data = check(await ctx.client.get(
        f"/api/studio/jobs/{view.job['id']}/plans/{view.plan_id}/preflight", force=str(args.force).lower(),
        all_canvases=str(args.all_canvases).lower() if args.all_canvases else None), "构建预检")
    return view, data


async def preview_run_build(args: RunBuild, ctx: ToolContext) -> Preview:
    view, data = await build_plan(args, ctx)
    planned(ctx, "build", (view.job["id"], view.plan_id, data["token"]))
    counts, lines = summarize_nodes(data["nodes"])
    warnings = []
    if counts.get("blocked"):
        warnings.append(f"{counts['blocked']} 个节点被阻塞，构建将部分完成")
    generating = sum(1 for n in data["nodes"] if n.get("stage") == "generate" and n.get("action") == "rebuild")
    if generating:
        warnings.append(f"将为 {generating} 个 aigc 分镜调用视频生成服务生成画面，可能产生费用")
    drawing = sum(1 for n in data["nodes"] if n.get("stage") == "draw" and n.get("action") == "rebuild")
    if drawing:
        warnings.append(f"将为 {drawing} 个 aigc_image 分镜调用图片生成服务生成图片，可能产生费用")
    composing = sum(1 for n in data["nodes"] if n.get("stage") == "music" and n.get("action") == "rebuild")
    if composing:
        warnings.append(f"将为 {composing} 个背景音乐提示点调用音乐生成服务，可能产生费用")
    animating = sum(1 for n in data["nodes"] if n.get("stage") == "animate" and n.get("action") == "rebuild")
    if animating:
        warnings.append(f"将为 {animating} 个 animation 分镜调用动画生成模型编写动画源码，并在沙箱中逐帧录制")
    if args.force:
        warnings.append("强制模式会忽略已有版本，全部重新生成")
    if not counts.get("rebuild") and not counts.get("blocked"):
        warnings.append("所有节点都可复用，构建只会选用已有版本")
    changes = [{"label": "重建", "after": counts.get("rebuild", 0)}, {"label": "复用", "after": counts.get("reuse", 0)}]
    changes += [{"label": line.lstrip("- ")} for line in lines[:30]]
    shots = list(dict.fromkeys(n["shot_id"] for n in data["nodes"] if n["shot_id"] and n["action"] == "rebuild"))
    return Preview(f"构建方案「{view.plan['name']}」的成片（后台执行，可能调用模型服务并耗时数分钟）", changes,
                   warnings=warnings, value={"counts": counts},
                   focus=studio_focus(view.job["id"], view.plan_id, shots=shots[:20] or None, section="storyboard"))


async def run_build(args: RunBuild, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "build")
    if stored is None:
        view, data = await build_plan(args, ctx)
        stored = (view.job["id"], view.plan_id, data["token"])
    job, pid, token = stored
    body = {"token": token, "force": args.force, **({"all_canvases": True} if args.all_canvases else {})}
    build = check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/build", body), "提交构建")
    target = {"job_id": job, "build_id": build["id"], "plan_id": pid}
    return ToolOutput({"build_id": build["id"], "nodes": len(build["nodes"])},
                      text=f"已提交构建 {build['id']}（{len(build['nodes'])} 个节点）。系统会跟踪进度，结束后通知你总结。",
                      meta={"kind": "build", **target}, mutation=mutation(job, pid), watch_build=target,
                      focus=studio_focus(job, pid, section="log", build_id=build["id"]))


class RunNode(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    stage: Literal["script", "split", "recall", "tts", "music", "draw", "generate", "animate", "select", "overlay",
                   "render"] = Field(
        description="script=按大纲生成文案；split=按文案重新拆分镜；其余为单个分镜的阶段"
                    "（overlay=含 HTML 文字层分镜的叠加层源码，force=false 时保留人工修改）"
                    "（music=背景音乐提示点分镜的背景音乐，会调用音乐生成服务；draw=aigc_image 分镜的生成图片，会调用图片生成服务；"
                    "generate=aigc 分镜的生成画面，会调用视频生成服务；animate=animation 分镜的生成动画，"
                    "调用动画生成模型重写动画源码，force=false 时保留人工修改的源码）")
    shot: str | None = Field(default=None, description="分镜级阶段需要：完整 ID、短 ID 或 #序号")
    force: bool = Field(default=True, description="true=重新生成；false=有可复用版本时直接选用")


def node_inputs(view: PlanView, stage: str, sid: str | None) -> dict:
    """与 studio-stages.js 的 nodeInputs 一致：必须等于服务端当前规格的 inputs，否则提交被拒绝。"""
    p = view.plan
    if stage == "script":
        return {}
    if stage == "split":
        return {"script": p.get("script")}
    s = p["shots"][sid]
    inputs = {"content": s["content"]}
    kind = ((view.content(sid) or {}).get("source") or {}).get("kind", "recall")
    if stage in ("generate", "animate"):
        inputs["tts"] = s.get("tts")
    if stage in ("draw", "music"):
        return inputs
    if stage == "select":
        inputs["tts"] = s.get("tts")
        if kind == "aigc":
            inputs["generate"] = s.get("generate")
        rng = p.get("ranges", {}).get(sid)
        if kind == "recall" and (not rng or rng.get("anchor_ref")):
            inputs["recall"] = s.get("recall")
    if stage == "render":
        if kind == "image":
            inputs["tts"] = s.get("tts")
        elif kind == "aigc_image":
            inputs.update(tts=s.get("tts"), draw=s.get("draw"))
        elif kind == "animation":
            inputs.update(tts=s.get("tts"), animate=s.get("animate"))
        else:
            inputs.update(select=s.get("select"), tts=s.get("tts"))
    return inputs


async def node_body(args: RunNode, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    sid = None
    if args.stage in ("script", "split"):
        if args.shot:
            raise ToolError("生成文案和拆分镜不能指定分镜", code="invalid_arguments")
    else:
        if not args.shot:
            raise ToolError(f"{STAGE_LABELS[args.stage]}需要指定分镜", code="missing_shot")
        found = resolve_shot(view.order, args.shot)
        if found["status"] != "ok":
            raise ToolError(f"无法确定分镜 {args.shot}：{found['status']}", code="unresolved_shot", value=found)
        sid = found["shot_id"]
        kind = ((view.content(sid) or {}).get("source") or {}).get("kind", "recall")
        if args.stage == "generate" and kind != "aigc":
            raise ToolError(f"分镜 {short(sid)} 的画面来源不是 aigc，没有生成画面阶段；可先用 edit_shots 把来源设为 aigc",
                            code="invalid_stage")
        if args.stage == "draw" and kind != "aigc_image":
            raise ToolError(f"分镜 {short(sid)} 的画面来源不是 aigc_image，没有生成图片阶段；可先用 edit_shots 把来源设为 aigc_image",
                            code="invalid_stage")
        music = (view.content(sid) or {}).get("music") or {}
        if args.stage == "music" and music.get("kind") not in ("generate", "cover", "library"):
            raise ToolError(f"分镜 {short(sid)} 没有背景音乐提示点（生成/翻唱）；可先用 edit_shots 设置 music",
                            code="invalid_stage")
        if args.stage == "animate" and kind != "animation":
            raise ToolError(f"分镜 {short(sid)} 的画面来源不是 animation，没有生成动画阶段；"
                            "可先用 edit_shots 把来源设为 animation", code="invalid_stage")
    body = {"revision": view.revision, "stage": args.stage, "shot_id": sid, "force": args.force,
            "inputs": node_inputs(view, args.stage, sid)}
    return view, body


async def preview_run_node(args: RunNode, ctx: ToolContext) -> Preview:
    view, body = await node_body(args, ctx)
    planned(ctx, "node", (view.job["id"], view.plan_id, body))
    target = STAGE_LABELS[args.stage] + (f"（分镜 {short(body['shot_id'])}）" if body["shot_id"] else "")
    warnings = ["重新拆分镜会生成全新的分镜（新 ID），原分镜的产物只保留在历史版本中"] if args.stage == "split" else []
    if args.stage == "script":
        warnings.append("生成的新文案会成为当前文案，现有分镜随之过期")
    if args.stage == "generate":
        warnings.append("将调用视频生成服务生成画面，可能产生费用；生成通常需要数分钟，结束后该分镜的选片、渲染与成片"
                        "需要重新构建")
    if args.stage == "draw":
        warnings.append("将调用图片生成服务生成图片，可能产生费用；结束后该分镜的渲染与成片需要重新构建")
    if args.stage == "music":
        warnings.append("将调用音乐生成服务生成背景音乐，可能产生费用；结束后成片需要重新合成")
    if args.stage == "animate":
        if args.force:
            warnings.append("将调用动画生成模型重新编写动画源码（消耗模型调用额度），会替换当前源码（含人工修改，"
                            "历史版本仍保留）；结束后该分镜的渲染（逐帧录制）与成片需要重新构建")
        else:
            warnings.append("有可复用或人工修改的动画源码时直接选用，否则调用动画生成模型编写")
    return Preview(f"执行单个节点：{target}" + ("（重新生成）" if args.force else ""), [], warnings=warnings,
                   focus=node_focus(view.job["id"], view.plan_id, body))


def node_focus(job, pid, body, **extra) -> dict:
    if body["shot_id"]:
        return studio_focus(job, pid, shots=[body["shot_id"]], stage=body["stage"], **extra)
    return studio_focus(job, pid, section="script" if body["stage"] == "script" else "storyboard", **extra)


async def run_node(args: RunNode, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "node")
    if stored is None:
        view, body = await node_body(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    build = check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/nodes", body), "执行节点")
    target = {"job_id": job, "build_id": build["id"], "plan_id": pid}
    return ToolOutput({"build_id": build["id"]}, text=f"已提交执行 {build['id']}，结束后会通知你。",
                      meta={"kind": "build", **target}, mutation=mutation(job, pid), watch_build=target,
                      focus=node_focus(job, pid, body, build_id=build["id"]))


class DeleteJob(StrictModel):
    job_id: str = Field(min_length=1, description="要删除的创作任务 ID（必须明确给出，不使用页面默认值）")


async def preview_delete_job(args: DeleteJob, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id)
    return Preview(
        f"永久删除创作任务 {view.job['id']}「{view.job['title']}」",
        [{"label": "方案", "before": len(view.w["plans"])}, {"label": "产物版本", "before": len(view.w["versions"])}],
        warnings=["删除后任务目录、全部方案、版本与成片都无法恢复"],
        focus=studio_focus(view.job["id"]),
    )


async def delete_job(args: DeleteJob, ctx: ToolContext) -> ToolOutput:
    check(await ctx.client.delete(f"/api/studio/jobs/{args.job_id}"), "删除任务")
    return ToolOutput({"deleted": args.job_id}, text=f"已删除任务 {args.job_id}。",
                      mutation=mutation(args.job_id, deleted=True), focus={"page": "studio", "section": "jobs"})


# ------------------------------------------------------------ 取消构建 / 旧任务步骤执行
class CancelBuild(StrictModel):
    job_id: str | None = JobId
    build_id: str | None = Field(
        default=None,
        description="要取消的构建 ID；省略时取该任务正在执行的构建，旧任务则取消正在执行的步骤")


def node_label(node: dict) -> str:
    stage = STAGE_LABELS.get(node.get("stage"), node.get("stage") or "")
    return stage + (f"（分镜 {short(node['shot_id'])}）" if node.get("shot_id") else "")


async def cancel_target(args: CancelBuild, ctx: ToolContext) -> dict:
    """确定取消目标：``{"kind": "build", ...}`` 运行中的构建，或 ``{"kind": "steps", ...}`` 旧任务步骤执行。

    先读任务本身（不加锁、不迁移）判断旧任务是否在执行步骤；执行期间旧任务的工作台迁移会被锁拒绝。
    """
    job = ctx.job_id(args.job_id)
    if args.build_id is None:
        detail = check(await ctx.client.get(f"/api/studio/jobs/{job}"), "读取创作任务")
        execution = detail["job"].get("execution") or {}
        if execution.get("status") == "running":
            return {"kind": "steps", "job_id": job, "title": detail["job"].get("title"),
                    "current": execution.get("current_step"), "remaining": list(execution.get("remaining_steps") or [])}
    data = check(await ctx.client.get(f"/api/studio/jobs/{job}/workspace", compact="true"), "读取工作台")
    builds = data["builds"]
    if args.build_id:
        build = next((b for b in builds if b["id"] == args.build_id), None)
        if build is None:
            raise ToolError(f"找不到构建执行 {args.build_id}", code="missing_build")
        if build["status"] != "running":
            raise ToolError(f"构建 {build['id']} 不在执行中（状态：{build['status']}），无法取消",
                            code="not_running", value={"build_id": build["id"], "status": build["status"]})
    else:
        build = next((b for b in reversed(builds) if b["status"] == "running"), None)
        if build is None:
            raise ToolError(f"任务 {job} 没有正在执行的构建或步骤，无需取消", code="not_running")
    plan = data["plans"].get(build["plan_id"]) or {}
    return {"kind": "build", "job_id": job, "build_id": build["id"], "plan_id": build["plan_id"],
            "plan_name": plan.get("name"), "nodes": build["nodes"]}


async def preview_cancel_build(args: CancelBuild, ctx: ToolContext) -> Preview:
    target = await cancel_target(args, ctx)
    planned(ctx, "cancel", target)
    if target["kind"] == "steps":
        current = STAGE_LABELS.get(target["current"], target["current"]) if target["current"] else "—"
        remaining = "、".join(STAGE_LABELS.get(s, s) for s in target["remaining"]) or "无"
        return Preview(f"取消旧任务「{target['title'] or target['job_id']}」的步骤执行",
                       [{"label": "当前步骤（照常完成）", "after": current}, {"label": "不再执行的步骤", "after": remaining}],
                       warnings=["当前步骤会照常完成，之后停止；未开始的步骤保持原状态，之后可以继续执行"],
                       focus=studio_focus(target["job_id"]))
    nodes = target["nodes"]
    running = [node_label(n) for n in nodes if n.get("status") == "running"]
    pending = [n for n in nodes if n.get("status", "pending") == "pending"]
    done = len(nodes) - len(running) - len(pending)
    changes = [{"label": "已结束节点（保留结果）", "after": done},
               {"label": "执行中节点（照常完成）", "after": "、".join(running) or "无"},
               {"label": "将标为已取消的节点", "after": len(pending)}]
    changes += [{"label": f"- {node_label(n)}"} for n in pending[:20]]
    plan = f"（方案「{target['plan_name']}」）" if target.get("plan_name") else ""
    return Preview(f"取消构建 {target['build_id']}{plan}",
                   changes,
                   warnings=["正在执行的节点会照常完成，其余未开始的节点标为“已取消”；已生成的版本保留，之后重新构建可复用"],
                   value={"build_id": target["build_id"], "pending": len(pending)},
                   focus=studio_focus(target["job_id"], target["plan_id"], section="log", build_id=target["build_id"]))


def watched(ctx: ToolContext, build_id: str) -> bool:
    """本会话是否已在跟踪该构建：已跟踪时不重复登记（面板按 build-watch 事件渲染构建卡片）。"""
    runtime = ctx.extra.get("runtime")
    if runtime is None or ctx.session_id is None:
        return False
    events = runtime.store.events(ctx.session_id, types=("plugin:mindex/build-watch",))
    return any(e["data"].get("build_id") == build_id for e in events)


async def cancel_build(args: CancelBuild, ctx: ToolContext) -> ToolOutput:
    target = planned(ctx, "cancel") or await cancel_target(args, ctx)
    job = target["job_id"]
    if target["kind"] == "steps":
        check(await ctx.client.post(f"/api/studio/jobs/{job}/cancel"), "取消步骤执行")
        return ToolOutput({"job_id": job, "status": "cancelling"},
                          text=f"已请求取消任务 {job} 的步骤执行：当前步骤完成后停止，未开始的步骤保持原状态。"
                               "可在创作页查看执行状态。",
                          mutation=mutation(job), focus=studio_focus(job))
    bid, pid = target["build_id"], target["plan_id"]
    check(await ctx.client.post(f"/api/studio/jobs/{job}/builds/{bid}/cancel"), "取消构建")
    watch = {"job_id": job, "build_id": bid, "plan_id": pid}
    return ToolOutput({"build_id": bid, "status": "cancelling"},
                      text=f"已请求取消构建 {bid}：正在执行的节点完成后停止，其余节点标为“已取消”。构建结束后系统会通知你。",
                      meta={"kind": "build", **watch, "status": "cancelling"}, mutation=mutation(job, pid),
                      watch_build=None if watched(ctx, bid) else watch,
                      focus=studio_focus(job, pid, section="log", build_id=bid))


# ------------------------------------------------------------ 删除方案
class DeletePlan(StrictModel):
    job_id: str | None = JobId
    plan_id: str = Field(min_length=1, description="要删除的方案 ID（必须明确给出，不使用页面默认值）")


def plan_label(plan: dict) -> str:
    return f"「{plan.get('name')}」({plan['id']})"


async def preview_delete_plan(args: DeletePlan, ctx: ToolContext) -> Preview:
    job = ctx.job_id(args.job_id)
    data = check(await ctx.client.get(f"/api/studio/jobs/{job}/workspace", compact="true"), "读取工作台")
    plans = data["plans"]
    if args.plan_id not in plans:
        names = ", ".join(plan_label(p) for p in plans.values())
        raise ToolError(f"方案不存在：{args.plan_id}；可用方案：{names}", code="missing_plan")
    if len(plans) == 1:
        raise ToolError("至少保留一个方案：这是任务中唯一的方案，不能删除", code="last_plan")
    if any(b["status"] == "running" for b in data["builds"]):
        raise ToolError("有构建正在执行，请等待完成或取消后再删除方案", code="build_running")
    target = plans[args.plan_id]
    active = data["active_plan"] == args.plan_id
    rest = [p for p in plans.values() if p["id"] != args.plan_id]
    after = max(rest, key=lambda p: p.get("created_at") or "") if active else plans[data["active_plan"]]
    builds = sum(1 for b in data["builds"] if b["plan_id"] == args.plan_id)
    return Preview(
        f"删除方案{plan_label(target)}",
        [{"label": "方案", "before": plan_label(target)},
         {"label": "是否当前方案", "after": "是" if active else "否"},
         {"label": "删除后的当前方案", "before": plan_label(plans[data["active_plan"]]), "after": plan_label(after)},
         {"label": "该方案的构建记录（保留）", "after": builds}],
        warnings=["只删方案记录，版本与文件保留，未引用产物由 mindex clean 回收"],
        value={"active": active, "next_active_plan": after["id"]},
        focus=studio_focus(job, args.plan_id, section="plan"),
    )


async def delete_plan(args: DeletePlan, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    data = check(await ctx.client.delete(f"/api/studio/jobs/{job}/plans/{args.plan_id}"), "删除方案")
    active = data["active_plan"]
    name = (data["plans"].get(active) or {}).get("name")
    return ToolOutput({"deleted_plan": args.plan_id, "active_plan": active, "plans": list(data["plans"])},
                      text=f"已删除方案 {args.plan_id}；当前方案为「{name}」({active})。版本与文件均已保留。",
                      mutation=mutation(job, active, deleted_plan=args.plan_id),
                      focus=studio_focus(job, active, section="plan"))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("run_build", "studio",
                 "构建成片：按预检清单在后台执行（只重建变化的节点，其余复用）。提交后立即返回，结束时系统会通知你。",
                 RunBuild, "danger", run_build, preview_run_build),
        ToolSpec("run_node", "studio",
                 "执行单个节点：生成文案、重新拆分镜，或对某个分镜重新召回/配音/生成画面（仅 aigc 分镜，调用视频生成服务"
                 "可能产生费用）/生成动画（仅 animation 分镜，调用动画生成模型重写源码）/选片/渲染（animation 分镜即"
                 "重新录制）。后台执行，结束时系统会通知你。",
                 RunNode, "danger", run_node, preview_run_node),
        ToolSpec("delete_job", "studio", "永久删除一个创作任务及其全部方案与产物。", DeleteJob, "danger", delete_job,
                 preview_delete_job),
        ToolSpec("cancel_build", "studio",
                 "取消正在执行的构建（或旧任务的步骤执行）：正在执行的节点照常完成，其余节点标为已取消，已生成版本保留。",
                 CancelBuild, "write", cancel_build, preview_cancel_build),
        ToolSpec("delete_plan", "studio",
                 "删除一个方案记录（不能删除唯一的方案；有构建执行时不可删）。版本与文件保留，删的是当前方案时切到最新创建的其余方案。",
                 DeletePlan, "danger", delete_plan, preview_delete_plan),
    ]
