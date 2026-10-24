"""Studio 写操作工具（风险级别 write，执行前展示预览并经用户确认）。

所有改动都经既有 REST 接口提交，带 ``revision`` 乐观锁：预览时读取的 revision 会在确认后原样提交，
若确认期间工作台被改动（例如用户在页面上编辑），服务端拒绝并返回可恢复错误，助手需重新读取后再试。

``edit_shots`` 的合并规则与创作页一致（studio.js changeStructure）：旁白直接拼接、查询以「；」连接、
预计时长相加；保留前一镜的召回设置，前一镜为默认文字召回时继承被合并镜的设置。
"""

from __future__ import annotations

import copy
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from mindex.agent.ids import resolve_shot, resolve_version, short
from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import STAGE_LABELS, PlanView, check, clip, load_view
from mindex.shared.config import StrictModel

JobId = Field(default=None, description="创作任务 ID；省略时使用当前页面打开的任务")
PlanId = Field(default=None, description="方案 ID；省略时使用任务当前激活的方案")


def mutation(job_id, plan_id=None, **extra) -> dict:
    return {"domain": "studio", "job_id": job_id, "plan_id": plan_id, **extra}


def studio_focus(job_id, plan_id=None, **extra) -> dict:
    focus = {"page": "studio", "job_id": job_id}
    if plan_id:
        focus["plan_id"] = plan_id
    focus.update({k: v for k, v in extra.items() if v})
    return focus


def planned(ctx: ToolContext, key: str, value=None):
    """预览与执行共享同一个 ToolContext：执行时沿用预览阶段计算出的提交内容与 revision。"""
    store = ctx.extra.setdefault("planned", {})
    if value is not None:
        store[key] = value
    return store.get(key)


# ------------------------------------------------------------ 创建任务
class CreateJob(StrictModel):
    outline: str = Field(min_length=1, max_length=2000, description="视频大纲；文案由文案创作模型按大纲生成")
    title: str | None = Field(default=None, max_length=60, description="任务标题；省略时自动生成")
    source_ids: list[str] | None = Field(default=None, description="限定可用素材 ID；省略表示不限")


async def preview_create_job(args: CreateJob, ctx: ToolContext) -> Preview:
    changes = [{"label": "大纲", "after": clip(args.outline, 200)}]
    if args.title:
        changes.append({"label": "标题", "after": args.title})
    if args.source_ids:
        changes.append({"label": "可用素材", "after": "、".join(args.source_ids)})
    return Preview("新建创作任务（不会自动生成文案或构建）", changes)


async def create_job(args: CreateJob, ctx: ToolContext) -> ToolOutput:
    body = {"text": args.outline, **({"title": args.title} if args.title else {})}
    if args.source_ids:
        body["options"] = {"source_ids": args.source_ids}
    job = check(await ctx.client.post("/api/studio/jobs", body), "创建任务")
    return ToolOutput({"job_id": job["id"], "title": job["title"]},
                      text=f"已创建任务 {job['id']}「{job['title']}」。",
                      mutation=mutation(job["id"], created=True), focus=studio_focus(job["id"], section="script"))


# ------------------------------------------------------------ 方案
class CreatePlan(StrictModel):
    job_id: str | None = JobId
    name: str = Field(min_length=1, max_length=60)
    copy_from: str | None = Field(default=None, description="复制已有方案（方案 ID），包含其分镜与选用")
    from_final: str | None = Field(default=None, description="从某个成片版本恢复方案（成片版本 ID 或前缀）")


async def preview_create_plan(args: CreatePlan, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id)
    if args.copy_from and args.copy_from not in view.w["plans"]:
        raise ToolError(f"要复制的方案不存在：{args.copy_from}", code="missing_plan")
    source = (f"复制方案 {view.w['plans'][args.copy_from]['name']}" if args.copy_from else
              f"从成片 {short(args.from_final)} 恢复" if args.from_final else "沿用当前方案的文案与参数，重新拆分镜")
    return Preview(f"在任务 {view.job['id']} 新建方案「{args.name}」并设为当前方案", [{"label": "来源", "after": source}],
                   focus=studio_focus(view.job["id"], section="plan"))


async def create_plan(args: CreatePlan, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id)
    body = {"name": args.name}
    if args.copy_from:
        body["copy_from"] = args.copy_from
    if args.from_final:
        found = resolve_version(view.w["versions"], args.from_final, stage="compose")
        if found["status"] != "ok":
            raise ToolError(f"无法确定成片版本 {args.from_final}：{found['status']}", code="unresolved_version", value=found)
        body["from_final"] = found["version_id"]
    data = check(await ctx.client.post(f"/api/studio/jobs/{view.job['id']}/plans", body), "新建方案")
    pid = data["active_plan"]
    return ToolOutput({"plan_id": pid}, text=f"已新建方案「{args.name}」({pid}) 并设为当前方案。",
                      mutation=mutation(view.job["id"], pid), focus=studio_focus(view.job["id"], pid, section="plan"))


class ActivatePlan(StrictModel):
    job_id: str | None = JobId
    plan_id: str = Field(description="要切换到的方案 ID")


async def preview_activate_plan(args: ActivatePlan, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id, args.plan_id)
    current = view.w["plans"][view.w["active_plan"]]["name"]
    return Preview("切换当前方案", [{"label": "方案", "before": current, "after": view.plan["name"]}],
                   focus=studio_focus(view.job["id"], section="plan"))


async def activate_plan(args: ActivatePlan, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{args.plan_id}/activate"), "切换方案")
    return ToolOutput({"plan_id": args.plan_id}, text=f"已切换到方案 {args.plan_id}。", mutation=mutation(job, args.plan_id),
                      focus=studio_focus(job, args.plan_id, section="plan"))


class PlanOptionChanges(StrictModel):
    voice: str | None = Field(default=None, description="方案音色（voice_id）；设置后 voice_mode 自动为 custom")
    voice_mode: Literal["inherit", "custom"] | None = Field(
        default=None, description="inherit=跟随设置页默认音色；custom=使用 voice")
    recall_limit: int | None = Field(default=None, ge=1, le=50, description="每个分镜召回候选数量")
    audio_mode: Literal["tts_only", "ducking"] | None = Field(default=None, description="tts_only=只保留配音；ducking=保留原声并压低")
    burn: bool | None = Field(default=None, description="是否把字幕烧录进画面")
    atempo: float | None = Field(default=None, ge=0.75, le=1.25, description="配音变速系数")
    alignment: Literal["auto", "freeze", "strict"] | None = Field(default=None, description="选片对齐策略")
    duck_gain: float | None = Field(default=None, gt=0, le=1, description="ducking 模式下原声音量")


class UpdatePlanOptions(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    changes: PlanOptionChanges | None = Field(default=None, description="只填需要修改的方案参数")
    name: str | None = Field(default=None, min_length=1, max_length=60, description="方案新名称")

    @model_validator(mode="after")
    def something(self):
        if self.name is None and (self.changes is None or not self.changes.model_dump(exclude_none=True)):
            raise ValueError("至少修改一项方案参数或名称")
        return self


async def plan_options_body(args: UpdatePlanOptions, ctx: ToolContext):
    from mindex.studio.studio_workspace import PlanOptions

    view = await load_view(ctx, args.job_id, args.plan_id)
    before = dict(view.plan["options"])
    updates = args.changes.model_dump(exclude_none=True) if args.changes else {}
    if "voice" in updates and "voice_mode" not in updates:
        updates["voice_mode"] = "custom"
    after = {**before, **updates}
    if after.get("voice_mode") == "inherit":
        after["voice"] = None
    try:
        after = PlanOptions.model_validate(after).model_dump()
    except ValidationError as exc:
        raise ToolError(f"方案参数无效：{exc.errors()[0]['msg']}", code="invalid_options") from exc
    body = {"revision": view.revision}
    if updates:
        body["options"] = after
    if args.name:
        body["name"] = args.name
    return view, before, after, body


async def preview_update_plan_options(args: UpdatePlanOptions, ctx: ToolContext) -> Preview:
    view, before, after, body = await plan_options_body(args, ctx)
    planned(ctx, "plan_options", (view.job["id"], view.plan_id, body))
    changes = [{"label": key, "before": before.get(key), "after": after.get(key)}
               for key in after if before.get(key) != after.get(key)]
    if args.name:
        changes.append({"label": "名称", "before": view.plan["name"], "after": args.name})
    warnings = []
    if any(c["label"] in ("voice", "voice_mode") for c in changes):
        warnings.append("音色变化后，下次构建会重新配音并重建选片、渲染与成片")
    return Preview(f"修改方案「{view.plan['name']}」参数", changes, warnings=warnings,
                   focus=studio_focus(view.job["id"], view.plan_id, section="plan"))


async def update_plan_options(args: UpdatePlanOptions, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "plan_options")
    if stored is None:
        view, _, _, body = await plan_options_body(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "修改方案参数")
    return ToolOutput({"options": data["plans"][pid]["options"], "revision": data["revision"]},
                      text=f"已更新方案参数：{data['plans'][pid]['options']}", mutation=mutation(job, pid),
                      focus=studio_focus(job, pid, section="plan"))


# ------------------------------------------------------------ 文案
class EditScript(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    text: str = Field(min_length=1, max_length=20000, description="完整的新文案（旁白正文）")


async def preview_edit_script(args: EditScript, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id, args.plan_id)
    planned(ctx, "script", (view.job["id"], view.plan_id, {"revision": view.revision, "script": args.text}))
    return Preview("保存为新的手动文案版本；已有分镜会标记为过期，下次构建会重新拆分镜",
                   [{"label": "文案", "before": clip(view.script(), 300) or "（无）", "after": clip(args.text, 300)}],
                   focus=studio_focus(view.job["id"], view.plan_id, section="script"))


async def edit_script(args: EditScript, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "script")
    if stored is None:
        view = await load_view(ctx, args.job_id, args.plan_id)
        stored = (view.job["id"], view.plan_id, {"revision": view.revision, "script": args.text})
    job, pid, body = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "保存文案")
    return ToolOutput({"revision": data["revision"], "script_stale": data.get("script_stale")},
                      text="已保存新文案。" + ("现有分镜已过期，构建时会重新拆分镜。" if data.get("script_stale") else ""),
                      mutation=mutation(job, pid), focus=studio_focus(job, pid, section="script"))


# ------------------------------------------------------------ 分镜
class ShotFields(StrictModel):
    text: str | None = Field(default=None, min_length=1, max_length=200, description="旁白（配音与字幕）")
    query: str | None = Field(default=None, min_length=1, max_length=400, description="召回查询，2–5 个中文词")
    estimated_seconds: float | None = Field(default=None, gt=0, le=60)


class ShotOperation(StrictModel):
    op: Literal["update", "insert", "delete", "move", "merge", "split"] = Field(
        description="update 改字段；insert 在 after 之后插入新镜（after 省略=开头）；delete 删除；"
                    "move 移到 to_index（从 1 开始）；merge 合并 shots 中相邻的分镜；split 按 parts 拆成多镜")
    shot: str | None = Field(default=None, description="目标分镜：完整 ID、短 ID 或 #序号（update/delete/move/split）")
    shots: list[str] | None = Field(default=None, min_length=2, description="merge：要合并的相邻分镜，按任意顺序")
    after: str | None = Field(default=None, description="insert：插在这个分镜之后")
    to_index: int | None = Field(default=None, ge=1, description="move：目标位置（从 1 开始）")
    fields: ShotFields | None = Field(default=None, description="update/insert：要设置的字段")
    parts: list[ShotFields] | None = Field(default=None, min_length=2,
                                           description="split：拆分后的各镜，旁白按顺序拼接须等于原旁白")


class EditShots(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    operations: list[ShotOperation] = Field(min_length=1, max_length=50, description="按顺序执行的分镜结构操作")


def recall_is_default(recall: dict | None) -> bool:
    if not recall:
        return True
    filters = recall.get("filters") or {}
    return (
        recall.get("mode", "text") == "text" and not recall.get("person_ids")
        and recall.get("person_mode", "auto") == "auto" and recall.get("person_relation", "all") == "all"
        and not filters.get("source_id") and not filters.get("source_ids") and not filters.get("min_duration")
        and not filters.get("max_duration") and not filters.get("scene") and not filters.get("emotion")
        and not filters.get("shot_size")
    )


def merge_into(previous: dict, current: dict) -> None:
    """与 studio.js 的 merge 逐字段一致。"""
    previous["text"] += current["text"]
    previous["query"] += f"；{current['query']}"
    previous["estimated_seconds"] += current["estimated_seconds"]
    if recall_is_default(previous.get("recall")) and current.get("recall"):
        previous["recall"] = current["recall"]


def plan_shots(view: PlanView, operations: list[ShotOperation]) -> tuple[list[dict], list[dict]]:
    order = view.order
    shots = copy.deepcopy(view.shots())
    labels = {sid: f"#{i} {short(sid)}" for i, sid in enumerate(order, 1)}
    changes = []
    touched: list[str] = []

    def find(ref: str) -> str:
        found = resolve_shot(order, ref)
        if found["status"] == "ambiguous":
            raise ToolError(f"分镜 {ref} 有歧义，候选："
                            + "、".join(f"#{c['index']} {c['shot_id']}" for c in found["candidates"]),
                            code="ambiguous_shot", value=found)
        if found["status"] != "ok":
            raise ToolError(f"找不到分镜 {ref}", code="missing_shot", value=found)
        if found["shot_id"] not in touched:
            touched.append(found["shot_id"])
        return found["shot_id"]

    def position(sid: str) -> int:
        for index, shot in enumerate(shots):
            if shot.get("id") == sid:
                return index
        raise ToolError(f"分镜 {labels.get(sid, sid)} 已在前面的操作中被删除或合并", code="missing_shot")

    for op in operations:
        if op.op == "update":
            sid = find(op.shot or "")
            values = op.fields.model_dump(exclude_none=True) if op.fields else {}
            if not values:
                raise ToolError("update 需要 fields", code="invalid_operation")
            shot = shots[position(sid)]
            for key, value in values.items():
                changes.append({"label": f"修改 {labels[sid]} 的{ {'text': '旁白', 'query': '查询', 'estimated_seconds': '时长'}[key]}",
                                "before": shot[key], "after": value})
                shot[key] = value
        elif op.op == "insert":
            values = op.fields.model_dump(exclude_none=True) if op.fields else {}
            if not values.get("text") or not values.get("query"):
                raise ToolError("insert 需要 fields.text 与 fields.query", code="invalid_operation")
            values.setdefault("estimated_seconds", max(1.0, round(len(values["text"]) / 4, 2)))
            index = position(find(op.after)) + 1 if op.after else 0
            shots.insert(index, values)
            changes.append({"label": f"在第 {index + 1} 位插入新分镜", "after": values["text"]})
        elif op.op == "delete":
            sid = find(op.shot or "")
            shot = shots.pop(position(sid))
            changes.append({"label": f"删除 {labels[sid]}", "before": shot["text"]})
        elif op.op == "move":
            sid = find(op.shot or "")
            if not op.to_index:
                raise ToolError("move 需要 to_index", code="invalid_operation")
            shot = shots.pop(position(sid))
            target = min(op.to_index, len(shots) + 1) - 1
            shots.insert(target, shot)
            changes.append({"label": f"移动 {labels[sid]}", "after": f"第 {target + 1} 位"})
        elif op.op == "merge":
            ids = [find(ref) for ref in op.shots or []]
            if len(set(ids)) != len(ids):
                raise ToolError("merge 中有重复的分镜", code="invalid_operation")
            indexes = sorted(position(sid) for sid in ids)
            if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
                raise ToolError("只能合并相邻的分镜；请先用 move 调整顺序，或向用户确认要合并哪些分镜",
                                code="not_adjacent")
            before = [shots[i]["text"] for i in indexes]
            first = shots[indexes[0]]
            for index in indexes[1:]:
                merge_into(first, shots[index])
            for index in reversed(indexes[1:]):
                shots.pop(index)
            changes.append({"label": "合并 " + " + ".join(labels[sid] for sid in sorted(ids, key=order.index)),
                            "before": " | ".join(before), "after": first["text"]})
        elif op.op == "split":
            sid = find(op.shot or "")
            parts = [p.model_dump(exclude_none=True) for p in op.parts or []]
            index = position(sid)
            original = shots[index]
            joined = "".join(p.get("text", "") for p in parts)
            if "".join(joined.split()) != "".join(original["text"].split()):
                raise ToolError("split 的各段旁白按顺序拼接后必须等于原旁白", code="invalid_operation")
            new = []
            for number, part in enumerate(parts):
                shot = {k: v for k, v in original.items() if k != "id"} if number else dict(original)
                shot.update(text=part["text"], query=part.get("query") or original["query"],
                            estimated_seconds=part.get("estimated_seconds") or max(1.0, round(len(part["text"]) / 4, 2)))
                new.append(shot)
            shots[index:index + 1] = new
            changes.append({"label": f"拆分 {labels[sid]}", "before": original["text"],
                            "after": " | ".join(p["text"] for p in new)})
    if not shots:
        raise ToolError("操作后没有任何分镜；至少保留一个分镜", code="invalid_operation")
    from mindex.studio.studio_models import ShotBody

    for number, shot in enumerate(shots, 1):
        try:
            ShotBody.model_validate(shot)
        except ValidationError as exc:
            problem = exc.errors()[0]
            raise ToolError(f"操作后第 {number} 个分镜无效（{'.'.join(map(str, problem['loc']))}：{problem['msg']}）",
                            code="invalid_shot") from exc
    return shots, changes, touched


async def preview_edit_shots(args: EditShots, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id, args.plan_id)
    shots, changes, touched = plan_shots(view, args.operations)
    planned(ctx, "shots", (view.job["id"], view.plan_id, {"revision": view.revision, "shots": shots}, len(view.order),
                           view.order, touched))
    return Preview(
        f"分镜 {len(view.order)} → {len(shots)} 个；未改动的分镜保留现有产物，受影响分镜的召回、配音、选片、渲染及成片"
        "会在下次构建时重建",
        changes,
        focus=studio_focus(view.job["id"], view.plan_id, shots=touched or None, section=None if touched else "storyboard"),
    )


async def edit_shots(args: EditShots, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "shots")
    if stored is None:
        view = await load_view(ctx, args.job_id, args.plan_id)
        shots, _, touched = plan_shots(view, args.operations)
        stored = (view.job["id"], view.plan_id, {"revision": view.revision, "shots": shots}, len(view.order),
                  view.order, touched)
    job, pid, body, before, old_order, touched = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "修改分镜")
    after = PlanView(data, pid)
    from mindex.agent.tools.common import board_text

    return ToolOutput(
        {"revision": after.revision, "shots": after.shot_rows()},
        text=f"已修改分镜：{before} → {len(after.order)} 个。如需生成成片，可先 get_preflight 查看要重建的节点，"
             f"再 run_build。\n" + board_text(after),
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, shots=edited_shots(old_order, after.order, touched)[:20] or None,
                           stage="content", section="storyboard"),
    )


def edited_shots(old_order: list[str], new_order: list[str], touched: list[str]) -> list[str]:
    """执行后要高亮的分镜：仍存在的被操作分镜（如合并保留的那一镜）加上新出现的分镜，按新顺序。"""
    wanted = set(touched) | (set(new_order) - set(old_order))
    return [sid for sid in new_order if sid in wanted]


# ------------------------------------------------------------ 版本选用
class SelectVersion(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    stage: Literal["script", "content", "recall", "tts", "select", "render", "compose"]
    version: str = Field(description="版本 ID 或其前缀（界面显示前 8 位）")
    shot: str | None = Field(default=None, description="分镜级阶段需要：完整 ID、短 ID 或 #序号")


async def select_body(args: SelectVersion, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    shot_id = None
    if args.stage not in ("script", "compose"):
        if not args.shot:
            raise ToolError(f"{STAGE_LABELS[args.stage]}阶段需要指定分镜", code="missing_shot")
        found = resolve_shot(view.order, args.shot)
        if found["status"] != "ok":
            raise ToolError(f"无法确定分镜 {args.shot}：{found['status']}", code="unresolved_shot", value=found)
        shot_id = found["shot_id"]
    version = resolve_version(view.w["versions"], args.version, stage=args.stage, shot_id=shot_id)
    if version["status"] != "ok":
        raise ToolError(f"无法确定版本 {args.version}：{version['status']}", code="unresolved_version", value=version)
    body = {"revision": view.revision, "version_id": version["version_id"], "stage": args.stage, "shot_id": shot_id}
    return view, body


async def preview_select_version(args: SelectVersion, ctx: ToolContext) -> Preview:
    view, body = await select_body(args, ctx)
    planned(ctx, "select", (view.job["id"], view.plan_id, body))
    if body["stage"] == "script":
        current = view.plan.get("script")
    elif body["stage"] == "compose":
        current = view.plan.get("final")
    else:
        current = view.plan["shots"][body["shot_id"]].get(body["stage"])
    item = view.w["versions"][body["version_id"]]
    return Preview(
        f"选用{STAGE_LABELS[body['stage']]}版本" + (f"（分镜 {short(body['shot_id'])}）" if body["shot_id"] else ""),
        [{"label": "版本", "before": short(current) or "（无）", "after": f"{short(body['version_id'])} · {item['status']}"}],
        warnings=["选用文案版本会把分镜工作台还原为该文案对应的最近一次构建结果"] if body["stage"] == "script" else [],
        focus=version_focus(view.job["id"], view.plan_id, body),
    )


def version_focus(job, pid, body) -> dict:
    if body["stage"] == "script":
        return studio_focus(job, pid, section="script")
    if body["stage"] == "compose":
        return studio_focus(job, pid, section="final")
    return studio_focus(job, pid, shots=[body["shot_id"]], stage=body["stage"])


async def select_version(args: SelectVersion, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "select")
    if stored is None:
        view, body = await select_body(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    data = check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/select-version", body), "选用版本")
    from mindex.agent.tools.media import version_media

    chosen = data["versions"].get(body["version_id"]) if isinstance(data, dict) else None
    media = version_media(chosen, f"{STAGE_LABELS[body['stage']]} {short(body['version_id'])}",
                          collapsed=body["stage"] != "compose")
    return ToolOutput({"version_id": body["version_id"]}, text=f"已选用版本 {short(body['version_id'])}。",
                      mutation=mutation(job, pid), focus=version_focus(job, pid, body), media=media)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("create_job", "studio", "新建创作任务（视频大纲）。不会自动生成文案或构建。", CreateJob, "write",
                 create_job, preview_create_job),
        ToolSpec("create_plan", "studio", "在任务里新建方案（可复制已有方案或从成片恢复），并设为当前方案。", CreatePlan,
                 "write", create_plan, preview_create_plan),
        ToolSpec("activate_plan", "studio", "切换任务的当前方案。", ActivatePlan, "write", activate_plan,
                 preview_activate_plan),
        ToolSpec("update_plan_options", "studio",
                 "修改方案参数（音色、召回数量、音轨模式、字幕烧录、变速、对齐策略、原声音量）或方案名称。只传要改的字段。",
                 UpdatePlanOptions, "write", update_plan_options, preview_update_plan_options),
        ToolSpec("edit_script", "studio", "用完整新文案替换方案文案（保存为手动版本）；分镜随之过期，构建时重新拆分镜。",
                 EditScript, "write", edit_script, preview_edit_script),
        ToolSpec("edit_shots", "studio",
                 "批量修改分镜结构：update/insert/delete/move/merge/split。合并规则与创作页一致（旁白拼接、查询以「；」连接、"
                 "时长相加）。只能合并相邻分镜。未改动分镜保留产物，只有受影响分镜需要重建。",
                 EditShots, "write", edit_shots, preview_edit_shots),
        ToolSpec("select_version", "studio", "选用某个历史版本作为当前方案的产物（文案、分镜内容、召回、配音、选片、渲染或成片）。",
                 SelectVersion, "write", select_version, preview_select_version),
    ]
