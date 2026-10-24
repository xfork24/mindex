"""Studio 写操作工具（风险级别 write，执行前展示预览并经用户确认）。

所有改动都经既有 REST 接口提交，带 ``revision`` 乐观锁：预览时读取的 revision 会在确认后原样提交，
若确认期间工作台被改动（例如用户在页面上编辑），服务端拒绝并返回可恢复错误，助手需重新读取后再试。

``edit_shots`` 的合并规则与创作页一致（studio.js changeStructure）：旁白直接拼接、查询以「；」连接、
预计时长相加；保留前一镜的召回设置，前一镜为默认文字召回时继承被合并镜的设置；画面意象跳过空值后以「；」连接，
截到 120 字。

分镜召回字段（``recall`` / ``granularity`` / ``recall_limit``）由 ``edit_shots`` 设置，``clear`` 恢复为跟随方案；
人物、素材范围、查询文件等引用由服务端 ``validate_recall_references`` 校验，拒绝原因原样转述。
``attach_query_media`` 在一次确认内先上传查询文件、再把分镜改为图片/视频召回。
"""

from __future__ import annotations

import copy
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from mindex.agent.ids import resolve_shot, resolve_version, short
from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import STAGE_LABELS, PlanView, check, clip, load_view
from mindex.agent.tools.library_tasks import attachment_meta, size_text
from mindex.agent.tools.search import upload_check
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
class RecallFilterArgs(StrictModel):
    """与 ShotRecall.filters（检索页筛选）字段一致；未给出的条件不过滤。"""

    source_id: str | None = Field(default=None, description="旧版单素材限定；优先使用 source_ids")
    source_ids: list[str] = Field(default_factory=list, description="只在这些素材里召回（须在任务素材范围内）")
    min_duration: float = Field(default=0, ge=0, description="片段最短时长（秒）")
    max_duration: float | None = Field(default=None, gt=0, description="片段最长时长（秒）")
    scene: str | None = Field(default=None, max_length=100, description="场景标签")
    emotion: str | None = Field(default=None, max_length=100, description="情绪标签")
    shot_size: str | None = Field(default=None, max_length=100, description="景别标签")


class ShotRecallArgs(StrictModel):
    """分镜召回设置（与 ShotRecall 一致），整体替换该分镜原有的召回设置；未给出的字段取默认值。"""

    mode: Literal["text", "image", "video"] = Field(
        default="text", description="text=文字召回（可带人物）；image=以图找视频；video=以视频找相似")
    person_ids: list[str] = Field(default_factory=list, max_length=30,
                                  description="要求出现的人物（演员或角色 ID，不能是作品）；仅文字召回可用")
    person_mode: Literal["auto", "onscreen", "mentioned"] = Field(
        default="auto", description="人物匹配方式：auto=自动；onscreen=画面出镜；mentioned=对白提及")
    person_relation: Literal["all", "any"] = Field(default="all", description="多个人物时：all=同时出现；any=任一出现")
    filters: RecallFilterArgs = Field(default_factory=RecallFilterArgs)
    media_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$",
                                 description="查询文件 ID（image/video 召回必填）；用户给的是附件时请改用 attach_query_media")
    media_kind: Literal["image", "video"] | None = Field(default=None, description="查询文件类型；缺省等于 mode")


class ShotFields(StrictModel):
    visual: str | None = Field(default=None, description="画面意象：一句中文，不超过 120 字；空字符串表示清空")
    text: str | None = Field(default=None, min_length=1, max_length=200, description="旁白（配音与字幕）")
    query: str | None = Field(default=None, min_length=1, max_length=400, description="召回查询，2–5 个中文词")
    estimated_seconds: float | None = Field(default=None, gt=0, le=60)
    recall: ShotRecallArgs | None = Field(default=None, description="分镜召回设置（整体替换）；恢复默认文字召回请用 clear")
    granularity: Literal["shot", "moment", "scene"] | None = Field(
        default=None, description="本分镜召回粒度：shot=镜头；moment=精细片段；scene=场景；恢复跟随服务配置请用 clear")
    recall_limit: int | None = Field(default=None, ge=1, le=50,
                                     description="本分镜召回候选数量；恢复跟随方案请用 clear")


ClearableField = Literal["recall", "granularity", "recall_limit", "visual"]


class ShotOperation(StrictModel):
    op: Literal["update", "insert", "delete", "move", "merge", "split"] = Field(
        description="update 改字段；insert 在 after 之后插入新镜（after 省略=开头）；delete 删除；"
                    "move 移到 to_index（从 1 开始）；merge 合并 shots 中相邻的分镜；split 按 parts 拆成多镜")
    shot: str | None = Field(default=None, description="目标分镜：完整 ID、短 ID 或 #序号（update/delete/move/split）")
    shots: list[str] | None = Field(default=None, min_length=2, description="merge：要合并的相邻分镜，按任意顺序")
    after: str | None = Field(default=None, description="insert：插在这个分镜之后")
    to_index: int | None = Field(default=None, ge=1, description="move：目标位置（从 1 开始）")
    fields: ShotFields | None = Field(default=None, description="update/insert：要设置的字段")
    clear: list[ClearableField] | None = Field(
        default=None, description="update：恢复为跟随方案或清空的字段（recall=默认文字召回，granularity=跟随服务配置，"
                                  "recall_limit=跟随方案，visual=清空画面意象）；不能与 fields 中的同名字段同时给出")
    parts: list[ShotFields] | None = Field(default=None, min_length=2,
                                           description="split：拆分后的各镜；各段旁白须由 get_shot_details 返回的完整旁白"
                                                       "按顺序切分，按顺序拼接（忽略空白）须等于原旁白")


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


LABELS = {"visual": "画面意象", "text": "旁白", "query": "查询", "estimated_seconds": "时长",
          "recall": "召回设置", "granularity": "召回粒度", "recall_limit": "召回数量"}
RECALL_KEYS = ("recall", "granularity", "recall_limit")
MODE_LABELS = {"text": "文字召回", "image": "图片召回", "video": "视频召回"}
GRANULARITY_LABELS = {"shot": "镜头", "moment": "精细片段", "scene": "场景"}
PERSON_MODE_LABELS = {"auto": "自动", "onscreen": "画面出镜", "mentioned": "对白提及"}
RECALL_REBUILD = "召回、选片、渲染及成片会在下次构建时重建"


def validation_message(exc: ValidationError) -> str:
    message = exc.errors()[0]["msg"]
    return message.removeprefix("Value error, ")


def normalize_recall(value: ShotRecallArgs | dict) -> dict:
    """召回参数 → ShotRecall 的完整规范形状（与工作台存储一致）；image/video 缺省 media_kind 取 mode。"""
    from mindex.studio.studio import ShotRecall

    data = value.model_dump() if isinstance(value, ShotRecallArgs) else dict(value)
    if data.get("mode", "text") != "text" and data.get("media_kind") is None:
        data["media_kind"] = data["mode"]
    try:
        return ShotRecall.model_validate(data).model_dump()
    except ValidationError as exc:
        raise ToolError(f"召回设置无效：{validation_message(exc)}", code="invalid_recall") from exc


def shot_values(fields: ShotFields | None) -> dict:
    """ShotFields → 要写入分镜的值；recall 规范化为完整 ShotRecall 形状。"""
    if fields is None:
        return {}
    values = fields.model_dump(exclude_none=True)
    if fields.recall is not None:
        values["recall"] = normalize_recall(fields.recall)
    return values


def recall_text(recall: dict | None, media: str | None = None) -> str:
    """召回设置的一行中文摘要（预览用）；``media`` 覆盖查询文件的显示名（默认取 media_id 前 8 位）。"""
    if not recall:
        return "文字召回（默认）"
    mode = recall.get("mode", "text")
    parts = [MODE_LABELS.get(mode, mode)]
    if mode != "text" and (media or recall.get("media_id")):
        parts.append(f"查询文件 {media or recall['media_id'][:8]}")
    persons = recall.get("person_ids") or []
    if persons:
        relation = "全部出现" if recall.get("person_relation", "all") == "all" else "任一出现"
        parts.append(f"人物 {'、'.join(persons)}（{PERSON_MODE_LABELS.get(recall.get('person_mode', 'auto'))}，{relation}）")
    filters = recall.get("filters") or {}
    sources = filters.get("source_ids") or ([filters["source_id"]] if filters.get("source_id") else [])
    if sources:
        parts.append("限定素材 " + "、".join(sources))
    if filters.get("min_duration"):
        parts.append(f"时长 ≥ {filters['min_duration']:g} 秒")
    if filters.get("max_duration"):
        parts.append(f"时长 ≤ {filters['max_duration']:g} 秒")
    for key, label in (("scene", "场景"), ("emotion", "情绪"), ("shot_size", "景别")):
        if filters.get(key):
            parts.append(f"{label} {filters[key]}")
    return "；".join(parts)


def field_text(key: str, value):
    """预览中字段值的展示形式；召回相关字段的空值显示为“跟随”语义。"""
    if key == "recall":
        return recall_text(value)
    if key == "granularity":
        return GRANULARITY_LABELS.get(value, value) if value else "跟随服务配置"
    if key == "recall_limit":
        return value if value else "跟随方案"
    return value


def recall_warnings(view: PlanView, shots: list[dict]) -> list[str]:
    """召回相关字段变化的已有分镜 → 级联重建提示（Req 14.2）。"""
    before = {s.get("id"): s for s in view.shots()}
    changed = [s["id"] for s in shots if s.get("id") in before
               and any(before[s["id"]].get(k) != s.get(k) for k in RECALL_KEYS)]
    if not changed:
        return []
    labels = "、".join(f"#{view.order.index(sid) + 1} {short(sid)}" for sid in changed)
    return [f"{labels} 的召回设置已变化：{RECALL_REBUILD}"]


def set_visual(shot: dict, value: str | None) -> None:
    """写入去除首尾空白后的 visual；空值删除该键（与 Shot 的空 visual 不落键一致）。"""
    visual = (value or "").strip()
    if visual:
        shot["visual"] = visual
    else:
        shot.pop("visual", None)


def join_visual(a: str | None, b: str | None) -> str | None:
    """与 studio.js 的 joinVisual 一致：跳过空值、以「；」拼接、按字符截到 VISUAL_LIMIT；全空返回 None。"""
    from mindex.studio.studio import VISUAL_LIMIT

    parts = [v.strip() for v in (a, b) if v and v.strip()]
    return "；".join(parts)[:VISUAL_LIMIT] or None


def merge_into(previous: dict, current: dict) -> None:
    """与 studio.js 的 merge 逐字段一致。"""
    previous["text"] += current["text"]
    previous["query"] += f"；{current['query']}"
    previous["estimated_seconds"] += current["estimated_seconds"]
    if recall_is_default(previous.get("recall")) and current.get("recall"):
        previous["recall"] = current["recall"]
    set_visual(previous, join_visual(previous.get("visual"), current.get("visual")))


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
        if op.clear and op.op != "update":
            raise ToolError("clear 只能用于 update", code="invalid_operation")
        if op.op == "update":
            sid = find(op.shot or "")
            values = shot_values(op.fields)
            clear = list(dict.fromkeys(op.clear or []))
            if not values and not clear:
                raise ToolError("update 需要 fields 或 clear", code="invalid_operation")
            both = [key for key in clear if key in values]
            if both:
                raise ToolError("clear 与 fields 不能同时给出同一字段：" + "、".join(LABELS[k] for k in both),
                                code="invalid_operation")
            shot = shots[position(sid)]
            for key in clear:
                before = shot.get(key)
                if key == "visual":
                    shot.pop("visual", None)
                else:
                    shot[key] = None
                changes.append({"label": f"{'清空' if key == 'visual' else '恢复'} {labels[sid]} 的{LABELS[key]}",
                                "before": field_text(key, before), "after": field_text(key, shot.get(key))})
            for key, value in values.items():
                if key == "visual":
                    before = shot.get("visual")
                    set_visual(shot, value)
                    after = shot.get("visual")
                    changes.append({"label": f"{'清空' if after is None else '修改'} {labels[sid]} 的{LABELS[key]}",
                                    "before": before, "after": after})
                    continue
                changes.append({"label": f"修改 {labels[sid]} 的{LABELS[key]}",
                                "before": field_text(key, shot.get(key)), "after": field_text(key, value)})
                shot[key] = value
        elif op.op == "insert":
            values = shot_values(op.fields)
            if not values.get("text") or not values.get("query"):
                raise ToolError("insert 需要 fields.text 与 fields.query", code="invalid_operation")
            values.setdefault("estimated_seconds", max(1.0, round(len(values["text"]) / 4, 2)))
            set_visual(values, values.get("visual"))
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
            parts = [shot_values(p) for p in op.parts or []]
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
                if "visual" in part:  # 未提供 visual 的段继承原镜（已随 original 复制）
                    set_visual(shot, part["visual"])
                for key in RECALL_KEYS:  # 未提供的召回字段同样继承原镜
                    if key in part:
                        shot[key] = part[key]
                new.append(shot)
            shots[index:index + 1] = new
            changes.append({"label": f"拆分 {labels[sid]}", "before": original["text"],
                            "after": " | ".join(p["text"] for p in new)})
    if not shots:
        raise ToolError("操作后没有任何分镜；至少保留一个分镜", code="invalid_operation")
    from mindex.studio.studio import visual_error
    from mindex.studio.studio_models import ShotBody

    problem = visual_error(shots)
    if problem:
        raise ToolError(problem, code="invalid_shot")
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
        warnings=recall_warnings(view, shots),
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


# ------------------------------------------------------------ 任务标题
class RenameJob(StrictModel):
    job_id: str | None = JobId
    title: str = Field(min_length=1, max_length=60, description="新标题")

    @model_validator(mode="after")
    def nonblank(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("标题不能为空")
        return self


async def preview_rename_job(args: RenameJob, ctx: ToolContext) -> Preview:
    job = ctx.job_id(args.job_id)
    state = check(await ctx.client.get(f"/api/studio/jobs/{job}"), "读取任务")
    title = (state.get("job") or {}).get("title")
    return Preview(f"修改任务 {job} 的标题（手动命名后不再自动生成标题）",
                   [{"label": "标题", "before": title or "（未命名）", "after": args.title}], focus=studio_focus(job))


async def rename_job(args: RenameJob, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    state = check(await ctx.client.patch(f"/api/studio/jobs/{job}/title", {"title": args.title}), "修改标题")
    return ToolOutput({"job_id": job, "title": state["title"]}, text=f"已把任务 {job} 的标题改为「{state['title']}」。",
                      mutation=mutation(job), focus=studio_focus(job))


# ------------------------------------------------------------ 查询文件召回
class AttachQueryMedia(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    shot: str = Field(description="目标分镜：完整 ID、短 ID 或 #序号")
    attachment: str = Field(pattern=r"^attachment://", description="图片或视频附件引用 attachment://…")
    kind: Literal["image", "video"] | None = Field(default=None, description="查询文件类型；缺省按附件的内容类型推断")


KIND_LABELS = {"image": "图片", "video": "视频"}


def media_kind(meta: dict, explicit: str | None) -> Literal["image", "video"]:
    if explicit:
        return explicit
    ctype = str(meta.get("content_type") or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    raise ToolError(f"无法从内容类型 {ctype or '未知'} 判断附件是图片还是视频：请显式指定 kind=image 或 video",
                    code="invalid_arguments")


def query_recall(current: dict | None, kind: str, media_id: str) -> dict:
    """image/video 召回：保留原有素材范围与时长等筛选，去掉人物条件（图片/视频召回不支持人物）。"""
    filters = (current or {}).get("filters") or {}
    return normalize_recall({"mode": kind, "media_id": media_id, "media_kind": kind, "filters": filters})


async def query_media_plan(args: AttachQueryMedia, ctx: ToolContext) -> dict:
    view = await load_view(ctx, args.job_id, args.plan_id)
    found = resolve_shot(view.order, args.shot)
    if found["status"] != "ok":
        raise ToolError(f"无法确定分镜 {args.shot}：{found['status']}", code="unresolved_shot", value=found)
    meta = await attachment_meta(ctx, args.attachment)
    kind = media_kind(meta, args.kind)
    sid = found["shot_id"]
    return {"job": view.job["id"], "plan": view.plan_id, "revision": view.revision, "shots": view.shots(),
            "shot_id": sid, "index": view.order.index(sid), "kind": kind, "meta": meta}


async def preview_attach_query_media(args: AttachQueryMedia, ctx: ToolContext) -> Preview:
    plan = await query_media_plan(args, ctx)
    planned(ctx, "query_media", plan)
    sid, kind, meta = plan["shot_id"], plan["kind"], plan["meta"]
    label = f"#{plan['index'] + 1} {short(sid)}"
    current = plan["shots"][plan["index"]].get("recall")
    after = recall_text(query_recall(current, kind, "0" * 64), media=meta.get("filename") or args.attachment)
    warnings = [f"{label} 的召回设置将变化：{RECALL_REBUILD}"]
    if (current or {}).get("person_ids"):
        warnings.append("图片/视频召回不支持人物设置：原有人物条件会被移除")
    return Preview(
        f"上传{KIND_LABELS[kind]}查询文件，并把分镜 {label} 改为{MODE_LABELS[kind]}（一次确认完成两步）",
        [{"label": "上传查询文件", "after": f"{meta.get('filename') or args.attachment}（{KIND_LABELS[kind]}，"
                                            f"{size_text(meta.get('bytes'))}）"},
         {"label": f"修改 {label} 的召回设置", "before": recall_text(current), "after": after}],
        warnings=warnings,
        focus=studio_focus(plan["job"], plan["plan"], shots=[sid]),
    )


async def attach_query_media(args: AttachQueryMedia, ctx: ToolContext) -> ToolOutput:
    plan = planned(ctx, "query_media") or await query_media_plan(args, ctx)
    job, pid, sid, kind = plan["job"], plan["plan"], plan["shot_id"], plan["kind"]
    uploaded = upload_check(await ctx.client.upload(
        f"/api/studio/jobs/{job}/plans/{pid}/query-media", files={"file": args.attachment}, data={"kind": kind},
    ), "上传查询文件")
    media_id = uploaded["media_id"]
    shots = copy.deepcopy(plan["shots"])
    target = shots[plan["index"]]
    target["recall"] = query_recall(target.get("recall"), kind, media_id)
    try:
        data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}",
                                            {"revision": plan["revision"], "shots": shots}), "修改召回设置")
    except ToolError as exc:
        raise ToolError(f"{exc}（查询文件已上传，media_id={media_id}；可重新读取工作台后用 edit_shots 设置召回）",
                        code=exc.code, value={**(exc.value or {}), "media_id": media_id}) from exc
    after = PlanView(data, pid)
    return ToolOutput(
        {"media_id": media_id, "kind": kind, "shot_id": sid, "revision": after.revision,
         "recall": target["recall"], "preview_url": uploaded.get("preview_url")},
        text=f"已上传{KIND_LABELS[kind]}查询文件（media_id={media_id[:8]}），分镜 #{plan['index'] + 1} {short(sid)} "
             f"已改为{MODE_LABELS[kind]}；下次构建时重新召回。",
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, shots=[sid], stage="recall"),
    )


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
                 "时长相加、画面意象以「；」连接并截到 120 字）。只能合并相邻分镜。未改动分镜保留产物，只有受影响分镜需要重建。"
                 "split 的各段旁白须由 get_shot_details 返回的完整旁白按顺序切分，各段按顺序拼接（忽略空白）须等于原旁白。"
                 "update/insert 可设置分镜召回：recall（文字+人物/限定素材/图片或视频召回，整体替换）、granularity、"
                 "recall_limit；update 的 clear 把这些字段恢复为跟随方案（visual 为清空）。用户给的图片/视频附件请用 "
                 "attach_query_media。",
                 EditShots, "write", edit_shots, preview_edit_shots),
        ToolSpec("rename_job", "studio", "修改创作任务标题（手动命名后不再自动生成标题）。", RenameJob, "write",
                 rename_job, preview_rename_job),
        ToolSpec("attach_query_media", "studio",
                 "把图片或视频附件设为某个分镜的召回查询：先上传为查询文件，再把该分镜改为图片/视频召回"
                 "（保留原素材范围与时长筛选，去掉人物条件），只需一次确认。",
                 AttachQueryMedia, "write", attach_query_media, preview_attach_query_media),
        ToolSpec("select_version", "studio", "选用某个历史版本作为当前方案的产物（文案、分镜内容、召回、配音、选片、渲染或成片）。",
                 SelectVersion, "write", select_version, preview_select_version),
    ]
