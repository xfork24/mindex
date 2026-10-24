"""Studio 高风险工具（danger）：整体构建、单节点执行、删除任务。每次都需要用户显式确认。

构建提交后立即返回；运行时登记 ``plugin:mindex/build-watch``，构建结束时写入结果并自动触发一轮总结。
同一时间全局只能有一个构建（studio.lock），冲突返回 409，作为可恢复结果交给模型。
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


async def build_plan(args: RunBuild, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    data = check(await ctx.client.get(
        f"/api/studio/jobs/{view.job['id']}/plans/{view.plan_id}/preflight", force=str(args.force).lower()), "构建预检")
    return view, data


async def preview_run_build(args: RunBuild, ctx: ToolContext) -> Preview:
    view, data = await build_plan(args, ctx)
    planned(ctx, "build", (view.job["id"], view.plan_id, data["token"]))
    counts, lines = summarize_nodes(data["nodes"])
    warnings = []
    if counts.get("blocked"):
        warnings.append(f"{counts['blocked']} 个节点被阻塞，构建将部分完成")
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
    build = check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/build", {"token": token, "force": args.force}),
                  "提交构建")
    target = {"job_id": job, "build_id": build["id"], "plan_id": pid}
    return ToolOutput({"build_id": build["id"], "nodes": len(build["nodes"])},
                      text=f"已提交构建 {build['id']}（{len(build['nodes'])} 个节点）。系统会跟踪进度，结束后通知你总结。",
                      meta={"kind": "build", **target}, mutation=mutation(job, pid), watch_build=target,
                      focus=studio_focus(job, pid, section="log", build_id=build["id"]))


class RunNode(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    stage: Literal["script", "split", "recall", "tts", "select", "render"] = Field(
        description="script=按大纲生成文案；split=按文案重新拆分镜；其余为单个分镜的阶段")
    shot: str | None = Field(default=None, description="分镜级阶段需要：完整 ID、短 ID 或 #序号")
    force: bool = Field(default=True, description="true=重新生成；false=有可复用版本时直接选用")


def node_inputs(view: PlanView, stage: str, sid: str | None) -> dict:
    """与 studio.js nodeInputs 一致：必须等于服务端当前规格的 inputs，否则提交被拒绝。"""
    p = view.plan
    if stage == "script":
        return {}
    if stage == "split":
        return {"script": p.get("script")}
    s = p["shots"][sid]
    inputs = {"content": s["content"]}
    if stage == "select":
        inputs["tts"] = s.get("tts")
        rng = p.get("ranges", {}).get(sid)
        if not rng or rng.get("anchor_ref"):
            inputs["recall"] = s.get("recall")
    if stage == "render":
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
    job_id: str = Field(description="要删除的创作任务 ID（必须明确给出，不使用页面默认值）")


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


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("run_build", "studio",
                 "构建成片：按预检清单在后台执行（只重建变化的节点，其余复用）。提交后立即返回，结束时系统会通知你。",
                 RunBuild, "danger", run_build, preview_run_build),
        ToolSpec("run_node", "studio",
                 "执行单个节点：生成文案、重新拆分镜，或对某个分镜重新召回/配音/选片/渲染。后台执行，结束时系统会通知你。",
                 RunNode, "danger", run_node, preview_run_node),
        ToolSpec("delete_job", "studio", "永久删除一个创作任务及其全部方案与产物。", DeleteJob, "danger", delete_job,
                 preview_delete_job),
    ]
