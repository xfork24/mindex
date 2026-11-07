"""Studio 写操作工具（风险级别 write，执行前展示预览并经用户确认）。

所有改动都经既有 REST 接口提交，带 ``revision`` 乐观锁：预览时读取的 revision 会在确认后原样提交，
若确认期间工作台被改动（例如用户在页面上编辑），服务端拒绝并返回可恢复错误，助手需重新读取后再试。

``edit_shots`` 的合并规则与创作页一致（studio-editor.js 的 changeStructure）：旁白直接拼接、查询以「；」连接、
预计时长相加；保留前一镜的召回设置，前一镜为默认文字召回时继承被合并镜的设置；画面意象跳过空值后以「；」连接，
截到 120 字。

分镜召回字段（``recall`` / ``granularity`` / ``recall_limit``）由 ``edit_shots`` 设置，``clear``
恢复为跟随方案；
人物、素材范围、查询文件等引用由服务端 ``validate_recall_references`` 校验，拒绝原因原样转述。
``attach_query_media`` 在一次确认内先上传查询文件、再把分镜改为图片/视频召回；``purpose=source`` 时上传为
画面文件，并把分镜的画面来源（``source``）改为上传片段 clip 或静态图片 image。``edit_shots`` 的 ``source``
字段与 ``clear=["source"]`` 设置/恢复画面来源，文件引用由服务端 ``validate_source_references`` 校验。
"""

from __future__ import annotations

import copy
import json
from typing import Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from agent.ids import resolve_shot, resolve_version, short
from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import STAGE_LABELS, PlanView, board_text, check, clip, load_view
from agent.tools.library_tasks import attachment_meta, size_text
from agent.tools.media import version_media
from agent.tools.search import upload_check
from shared.config import StrictModel
from studio.models import VERSION_STAGES, BrandOverrides, ShotBody
from studio.studio import (
    VISUAL_LIMIT,
    ShotMusic,
    ShotOverlay,
    ShotRecall,
    ShotSource,
    ShotTransition,
    StudioError,
    normalize_highlights,
    visual_error,
)

VersionStage = Literal[VERSION_STAGES]  # type: ignore[valid-type]  # 由阶段注册表生成

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
    source_group_ids: list[str] = Field(
        default_factory=list, description="素材组取并集，与手动素材取交集；空组无候选"
    )


async def preview_create_job(args: CreateJob, ctx: ToolContext) -> Preview:
    changes = [{"label": "大纲", "after": clip(args.outline, 200)}]
    if args.title:
        changes.append({"label": "标题", "after": args.title})
    if args.source_ids:
        changes.append({"label": "可用素材", "after": "、".join(args.source_ids)})
    if args.source_group_ids:
        changes.append({"label": "素材组", "after": "、".join(args.source_group_ids)})
    return Preview("新建创作任务（不会自动生成文案或构建）", changes)


async def create_job(args: CreateJob, ctx: ToolContext) -> ToolOutput:
    body = {
        "language": ctx.page.get("language", "zh"),
        "text": args.outline,
        **({"title": args.title} if args.title else {}),
    }
    if args.source_ids or args.source_group_ids:
        body["options"] = {"source_ids": args.source_ids, "source_group_ids": args.source_group_ids}
    # 任务元信息的「原始输入」记录用户在对话里的原话；大纲是助手整理后的文字。MCP 调用没有会话，省略即同大纲。
    original = ctx.extra.get("user_input")
    original = original() if callable(original) else None
    if original:
        body["original_input"] = original[:20000]
    job = check(await ctx.client.post("/api/studio/jobs", body), "创建任务")
    return ToolOutput(
        {"job_id": job["id"], "title": job["title"]},
        text=f"已创建任务 {job['id']}「{job['title']}」。",
        mutation=mutation(job["id"], created=True),
        focus=studio_focus(job["id"], section="script"),
    )


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
    source = (
        f"复制方案 {view.w['plans'][args.copy_from]['name']}"
        if args.copy_from
        else f"从成片 {short(args.from_final)} 恢复"
        if args.from_final
        else "沿用当前方案的文案与参数，重新拆分镜"
    )
    return Preview(
        f"在任务 {view.job['id']} 新建方案「{args.name}」并设为当前方案",
        [{"label": "来源", "after": source}],
        focus=studio_focus(view.job["id"], section="plan"),
    )


async def create_plan(args: CreatePlan, ctx: ToolContext) -> ToolOutput:
    view = await load_view(ctx, args.job_id)
    body = {"name": args.name}
    if args.copy_from:
        body["copy_from"] = args.copy_from
    if args.from_final:
        found = resolve_version(view.w["versions"], args.from_final, stage="compose")
        if found["status"] != "ok":
            raise ToolError(
                f"无法确定成片版本 {args.from_final}：{found['status']}",
                code="unresolved_version",
                value=found,
            )
        body["from_final"] = found["version_id"]
    data = check(await ctx.client.post(f"/api/studio/jobs/{view.job['id']}/plans", body), "新建方案")
    pid = data["active_plan"]
    return ToolOutput(
        {"plan_id": pid},
        text=f"已新建方案「{args.name}」({pid}) 并设为当前方案。",
        mutation=mutation(view.job["id"], pid),
        focus=studio_focus(view.job["id"], pid, section="plan"),
    )


class ActivatePlan(StrictModel):
    job_id: str | None = JobId
    plan_id: str = Field(description="要切换到的方案 ID")


async def preview_activate_plan(args: ActivatePlan, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id, args.plan_id)
    current = view.w["plans"][view.w["active_plan"]]["name"]
    return Preview(
        "切换当前方案",
        [{"label": "方案", "before": current, "after": view.plan["name"]}],
        focus=studio_focus(view.job["id"], section="plan"),
    )


async def activate_plan(args: ActivatePlan, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    check(await ctx.client.post(f"/api/studio/jobs/{job}/plans/{args.plan_id}/activate"), "切换方案")
    return ToolOutput(
        {"plan_id": args.plan_id},
        text=f"已切换到方案 {args.plan_id}。",
        mutation=mutation(job, args.plan_id),
        focus=studio_focus(job, args.plan_id, section="plan"),
    )


class PlanOptionChanges(StrictModel):
    voice: str | None = Field(
        default=None, description="方案音色（voice_id）；设置后 voice_mode 自动为 custom"
    )
    voice_mode: Literal["inherit", "custom"] | None = Field(
        default=None, description="inherit=跟随设置页默认音色；custom=使用 voice"
    )
    recall_limit: int | None = Field(default=None, ge=1, le=50, description="每个分镜召回候选数量")
    audio_mode: Literal["tts_only", "ducking"] | None = Field(
        default=None, description="tts_only=只保留配音；ducking=保留原声并压低"
    )
    burn: bool | None = Field(default=None, description="是否把字幕烧录进画面")
    atempo: float | None = Field(default=None, ge=0.75, le=1.25, description="配音变速系数")
    alignment: Literal["auto", "freeze", "strict"] | None = Field(default=None, description="选片对齐策略")
    duck_gain: float | None = Field(default=None, gt=0, le=1, description="ducking 模式下原声音量")
    template_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
        description="文案模板 ID（list_script_templates 列出）；只影响之后生成的文案",
    )
    canvas: Literal["16:9", "9:16", "1:1"] | None = Field(
        default=None,
        description="成片画幅：16:9 横屏、9:16 竖屏、1:1 方形；分辨率来自配置 studio.render.canvases",
    )
    fit: Literal["pad", "crop"] | None = Field(
        default=None, description="画面适配：pad=完整保留并补黑边；crop=铺满画幅并居中裁切"
    )
    subtitle_style: Literal["default", "classic", "outline", "box", "highlight"] | None = Field(
        default=None,
        description="烧录字幕的样式模板（需要 burn=true）：classic 经典、outline 粗描边、box 底条、"
        "highlight 描边加高亮词变色；default=恢复配置的字体字号（旧版样式）",
    )
    brand_id: str | None = Field(
        default=None,
        pattern=r"^(none|[a-z0-9][a-z0-9-]{0,63})$",
        description="品牌模板 ID（list_brands 列出）：统一字幕样式、文字层样式、片头片尾与水印；"
        "none=不使用品牌",
    )
    brand_overrides: BrandOverrides | None = Field(
        default=None,
        description="对所选品牌模板的方案覆盖（整体替换）：subtitle/overlay/intro/outro/watermark "
        "替换模板中的同名项，"
        "disable 关掉片头/片尾/水印；传空对象 {} 清除覆盖",
    )
    beat_align: float | None = Field(
        default=None,
        ge=0,
        le=1.0,
        description="节拍对齐容差（秒，0.04–1）：成片合成时把背景音乐段落内的切点推迟到"
        "该范围内最近的节拍；0=关闭",
    )


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
    # 延迟导入：保持工作区阶段处理器的注册顺序。
    from studio.workspace import PlanOptions

    view = await load_view(ctx, args.job_id, args.plan_id)
    before = dict(view.plan["options"])
    updates = args.changes.model_dump(exclude_none=True) if args.changes else {}
    if "voice" in updates and "voice_mode" not in updates:
        updates["voice_mode"] = "custom"
    if updates.get("subtitle_style") == "default":
        updates["subtitle_style"] = None
    if updates.get("brand_id") == "none":
        updates["brand_id"] = None
    if "brand_overrides" in updates and BrandOverrides.model_validate(updates["brand_overrides"]).empty:
        updates["brand_overrides"] = None
    if "beat_align" in updates:
        updates["beat_align"] = {"tolerance": updates["beat_align"]} if updates["beat_align"] else None
    after = {**before, **updates}
    if after.get("voice_mode") == "inherit":
        after["voice"] = None
    try:
        after = PlanOptions.model_validate(after).model_dump()
    except ValidationError as exc:
        raise ToolError(f"方案参数无效：{exc.errors()[0]['msg']}", code="invalid_options") from exc
    if updates.get("brand_id"):
        brand = check(await ctx.client.get(f"/api/studio/brands/{updates['brand_id']}"), "读取品牌模板")
        ctx.extra.setdefault("brand_names", {})[brand["id"]] = brand["name"]
    if "template_id" in updates:
        # 先确认模板存在，预览里给出名称；不存在时服务端同样会拒绝。
        template = check(
            await ctx.client.get(f"/api/studio/templates/{updates['template_id']}"), "读取文案模板"
        )
        ctx.extra.setdefault("template_names", {})[template["id"]] = template["name"]
    body = {"revision": view.revision}
    if updates:
        body["options"] = after
    if args.name:
        body["name"] = args.name
    return view, before, after, body


async def preview_update_plan_options(args: UpdatePlanOptions, ctx: ToolContext) -> Preview:
    view, before, after, body = await plan_options_body(args, ctx)
    planned(ctx, "plan_options", (view.job["id"], view.plan_id, body))
    changes = [
        {"label": key, "before": before.get(key), "after": after.get(key)}
        for key in after
        if before.get(key) != after.get(key)
    ]
    if args.name:
        changes.append({"label": "名称", "before": view.plan["name"], "after": args.name})
    warnings = []
    if any(c["label"] in ("voice", "voice_mode") for c in changes):
        warnings.append("音色变化后，下次构建会重新配音并重建选片、渲染与成片")
    for change in changes:
        if change["label"] == "template_id":
            change["after"] = (
                f"{change['after']}（{ctx.extra.get('template_names', {}).get(change['after'], '')}）"
            )
            change["before"] = change["before"] or "base"
            warnings.append("更换文案模板不会重写已有文案；需要按新模板写时再重新生成文案")
    if any(c["label"] in ("canvas", "fit") for c in changes):
        warnings.append("画幅或适配方式变化后，下次构建会重建全部分镜渲染与成片；召回、配音与选片照常复用")
    for change in changes:
        if change["label"] == "brand_id" and change["after"]:
            change["after"] = (
                f"{change['after']}（{ctx.extra.get('brand_names', {}).get(change['after'], '')}）"
            )
    if any(c["label"] in ("brand_id", "brand_overrides") for c in changes):
        warnings.append("品牌模板或覆盖变化后，下次构建只重新合成成片；分镜产物照常复用")
    if any(c["label"] == "beat_align" for c in changes):
        warnings.append(
            "节拍对齐只作用于有背景音乐的段落：开启或改容差会重建非末镜的分镜渲染（多留余量）与成片"
            if after.get("beat_align")
            else "关闭节拍对齐后分镜渲染与成片恢复原切点（重建一次）"
        )
    if any(c["label"] == "subtitle_style" for c in changes):
        warnings.append(
            "字幕样式只在烧录字幕（burn=true）时生效；变化后只重新合成成片"
            if after.get("burn")
            else "字幕样式只在烧录字幕（burn=true）时生效，当前为软字幕，成片不受影响"
        )
    return Preview(
        f"修改方案「{view.plan['name']}」参数",
        changes,
        warnings=warnings,
        focus=studio_focus(view.job["id"], view.plan_id, section="plan"),
    )


async def update_plan_options(args: UpdatePlanOptions, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "plan_options")
    if stored is None:
        view, _, _, body = await plan_options_body(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "修改方案参数")
    return ToolOutput(
        {"options": data["plans"][pid]["options"], "revision": data["revision"]},
        text=f"已更新方案参数：{data['plans'][pid]['options']}",
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, section="plan"),
    )


# ------------------------------------------------------------ 文案
class EditScript(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    text: str = Field(min_length=1, max_length=20000, description="完整的新文案（旁白正文）")


async def preview_edit_script(args: EditScript, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id, args.plan_id)
    planned(ctx, "script", (view.job["id"], view.plan_id, {"revision": view.revision, "script": args.text}))
    return Preview(
        "保存为新的手动文案版本；已有分镜会标记为过期，下次构建会重新拆分镜",
        [{"label": "文案", "before": clip(view.script(), 300) or "（无）", "after": clip(args.text, 300)}],
        focus=studio_focus(view.job["id"], view.plan_id, section="script"),
    )


async def edit_script(args: EditScript, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "script")
    if stored is None:
        view = await load_view(ctx, args.job_id, args.plan_id)
        stored = (view.job["id"], view.plan_id, {"revision": view.revision, "script": args.text})
    job, pid, body = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "保存文案")
    return ToolOutput(
        {"revision": data["revision"], "script_stale": data.get("script_stale")},
        text="已保存新文案。" + ("现有分镜已过期，构建时会重新拆分镜。" if data.get("script_stale") else ""),
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, section="script"),
    )


# ------------------------------------------------------------ 分镜
class RecallFilterArgs(StrictModel):
    """与 ShotRecall.filters（检索页筛选）字段一致；未给出的条件不过滤。"""

    source_id: str | None = Field(default=None, description="旧版单素材限定；优先使用 source_ids")
    source_ids: list[str] = Field(
        default_factory=list, description="只在这些素材里召回（须在任务素材范围内）"
    )
    source_group_ids: list[str] = Field(
        default_factory=list, description="素材组取并集，与任务和手动素材取交集"
    )
    min_duration: float = Field(default=0, ge=0, description="片段最短时长（秒）")
    max_duration: float | None = Field(default=None, gt=0, description="片段最长时长（秒）")
    scene: str | None = Field(default=None, max_length=100, description="场景标签")
    emotion: str | None = Field(default=None, max_length=100, description="情绪标签")
    shot_size: str | None = Field(default=None, max_length=100, description="景别标签")


class ShotRecallArgs(StrictModel):
    """分镜召回设置（与 ShotRecall 一致），整体替换该分镜原有的召回设置；未给出的字段取默认值。"""

    mode: Literal["text", "image", "video"] = Field(
        default="text", description="text=文字召回（可带人物）；image=以图找视频；video=以视频找相似"
    )
    person_ids: list[str] = Field(
        default_factory=list,
        max_length=30,
        description="要求出现的人物（演员或角色 ID，不能是作品）；仅文字召回可用",
    )
    person_mode: Literal["auto", "onscreen", "mentioned"] = Field(
        default="auto", description="人物匹配方式：auto=自动；onscreen=画面出镜；mentioned=对白提及"
    )
    person_relation: Literal["all", "any"] = Field(
        default="all", description="多个人物时：all=同时出现；any=任一出现"
    )
    filters: RecallFilterArgs = Field(default_factory=RecallFilterArgs)
    media_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="查询文件 ID（image/video 召回必填）；用户给的是附件时请改用 attach_query_media",
    )
    media_kind: Literal["image", "video"] | None = Field(
        default=None, description="查询文件类型；缺省等于 mode"
    )


class MediaRefArgs(StrictModel):
    kind: Literal["image", "video", "audio"] = Field(description="参考素材类型")
    media_id: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="已上传参考素材的 media_id（attach_query_media 上传）"
    )


class ShotSourceArgs(StrictModel):
    """分镜画面来源（与 Shot.source 一致），整体替换；恢复素材召回用 kind=recall 或 clear。"""

    kind: Literal["recall", "clip", "image", "aigc_image", "aigc", "animation", "tool"] = Field(
        description="recall=素材召回（缺省）；clip=上传的视频片段；image=静态图片（推拉/平移）；"
        "aigc_image=AI 生成图片：图片生成模型按提示词生成一张图片（可带主体参考图 reference_id 图生图），"
        "再按静态图片推拉/平移（需在设置页激活图片生成档案）；"
        "aigc=AI 生成视频：视频生成服务按提示词生成画面（需在设置页激活视频生成档案）；"
        "animation=由动画生成模型按画面说明写 HTML/JS 动画再逐帧录制（数据图表、文字动效、示意动画）；"
        "tool=调用登记的第三方工具（list_tool_sources 列出）生成视频或图片"
    )
    media_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="clip/image 必填：已上传画面文件的 media_id；用户给的是附件时请改用 "
        "attach_query_media（purpose=source）",
    )
    prompt: str | None = Field(
        default=None,
        max_length=1500,
        description="aigc/aigc_image：生成提示词（视频 ≤1000 字、图片 ≤1500 字）；"
        "缺省用画面意象，再缺省用召回查询",
    )
    negative: str | None = Field(
        default=None, max_length=500, description="aigc：反向提示词（不希望出现的内容）"
    )
    image_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="aigc：首帧参考图的 media_id（经 query-media purpose=source 上传的图片）；"
        "用户给的是图片附件时，先把来源设为 aigc，再用 attach_query_media（purpose=source）",
    )
    seed: int | None = Field(
        default=None, ge=0, le=2147483647, description="aigc/aigc_image：随机种子，固定后便于复现"
    )
    mode: Literal["text", "first_frame", "last_frame", "first_last", "reference"] | None = Field(
        default=None,
        description="aigc 生成方式：text 文生视频；first_frame 首帧图生视频（image_id）；last_frame "
        "尾帧图生视频"
        "（last_image_id）；first_last 首尾帧生视频；reference 主体参考生视频（references）。"
        "缺省：有 image_id 为 first_frame，否则 text。尾帧/首尾帧/主体参考只有 MiniMax H3 支持",
    )
    last_image_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", description="aigc：尾帧图片 media_id"
    )
    references: list[MediaRefArgs] | None = Field(
        default=None,
        max_length=12,
        description="aigc 主体参考：参考图片（≤9）/视频（≤3，2–15 秒）/音频（≤3，不能只有音频）",
    )
    model: Literal["MiniMax-H3", "MiniMax-H3-Highspeed", "MiniMax-H3-Lite", "MiniMax-H3-Max"] | None = Field(
        default=None, description="aigc：MiniMax H3 模型档位；缺省用设置页档案的模型"
    )
    resolution: Literal["480P", "768P", "2K"] | None = Field(
        default=None, description="aigc：分辨率（H3 为 768P/2K；Lite/Max 为 480P/768P）；缺省 768P"
    )
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] | None = Field(
        default=None, description="aigc：宽高比；缺省按方案画幅（图生视频固定 adaptive）"
    )
    duration: int | None = Field(
        default=None, ge=4, le=15, description="aigc：生成时长（秒，H3 为 4–15）；缺省按配音时长"
    )
    reference_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", description="aigc_image：主体参考图 media_id（图生图）"
    )
    prompt_optimizer: bool | None = Field(default=None, description="aigc_image：是否开启提示词自动优化")
    brief: str | None = Field(
        default=None,
        max_length=2000,
        description="animation：画面说明（≤2000 字，写清要展示的数据、文字、元素与节奏）；"
        "缺省用画面意象，再缺省用旁白",
    )
    start: float | None = Field(default=None, ge=0, description="clip：片段内的起点（秒），缺省从头开始")
    motion: Literal["zoom-in", "zoom-out", "pan-left", "pan-right", "none"] | None = Field(
        default=None, description="image/aigc_image：运动方式，缺省 zoom-in"
    )
    strength: float | None = Field(
        default=None, ge=0, le=1, description="image/aigc_image：运动强度 0–1，缺省 0.3"
    )
    tool_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,39}$",
        description="tool 必填：工具源 ID（list_tool_sources 列出）",
    )
    tool: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.:/-]{1,128}$", description="tool 必填：白名单中的工具名"
    )
    arguments: dict | None = Field(
        default=None,
        description="tool：传给工具的 JSON 参数（≤8000 字符）；字符串值里的 "
        "{{text}}/{{visual}}/{{query}} 替换为本镜旁白、画面意象与查询",
    )
    uploads: list[MediaRefArgs] | None = Field(
        default=None,
        max_length=4,
        description="tool：随调用上传的素材（需要该工具允许上传），先用 attach_query_media 上传",
    )
    output: Literal["video", "image"] | None = Field(
        default=None, description="tool：产物类型，须与工具登记一致，缺省 video"
    )


class ShotMusicArgs(StrictModel):
    """背景音乐提示点（与 Shot.music 一致），整体替换：从该分镜起换成新的背景音乐或停止，之后的分镜顺延。"""

    kind: Literal["generate", "cover", "library", "none"] = Field(
        description="generate=音乐生成模型按描述生成（缺省纯音乐）；cover=按参考音频 audio_id 翻唱；"
        "library=选用曲库曲目 track_id（recommend_music / list_music_tracks "
        "给出）；none=从该分镜起停止背景音乐"
    )
    prompt: str | None = Field(
        default=None,
        max_length=2000,
        description="音乐描述（风格、情绪、场景，例如「轻快电子，明亮，旅行 vlog」）；cover 时为 10–300 "
        "字的目标风格",
    )
    lyrics: str | None = Field(
        default=None,
        max_length=3500,
        description="歌词（可用 [Verse]/[Chorus] 等结构标签）；仅 instrumental=false 时使用，可先用 "
        "generate_lyrics 生成",
    )
    instrumental: bool | None = Field(default=None, description="是否纯音乐（无人声），缺省 true")
    audio_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="cover：参考音频 media_id（attach_query_media 上传音频）",
    )
    track_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="library：曲库曲目 ID（recommend_music / list_music_tracks 给出）",
    )
    volume: float | None = Field(
        default=None, ge=0, le=1, description="混音音量 0–1，缺省 0.25；配音出现时自动压低"
    )


class TransitionEdgeArgs(StrictModel):
    kind: Literal["fade", "white", "zoom", "slide"] = Field(
        description="fade=黑场淡入/淡出；white=白场；zoom=缩放并淡入/淡出；slide=从右滑入/向左滑出"
    )
    duration: float | None = Field(
        default=None, ge=0.1, le=2, description="时长（秒），缺省 0.5；超过镜头一半时自动截短"
    )


class OverlapTransitionArgs(StrictModel):
    kind: (
        Literal[
            "fade",
            "dissolve",
            "fadeblack",
            "fadewhite",
            "wipeleft",
            "wiperight",
            "slideleft",
            "slideright",
            "circleopen",
            "circleclose",
            "smoothleft",
            "smoothright",
        ]
        | None
    ) = Field(default=None, description="过渡样式（ffmpeg xfade），缺省 fade 交叉淡化")
    duration: float | None = Field(default=None, ge=0.1, le=2, description="时长（秒），缺省 0.5")


class CardItemArgs(StrictModel):
    label: str = Field(min_length=1, max_length=20, description="标签")
    value: str = Field(min_length=1, max_length=20, description="数值（强调色显示）")


class ShotOverlayArgs(StrictModel):
    """文字层（与 Shot.overlays 的元素一致）。"""

    engine: Literal["ass", "html"] | None = Field(
        default=None,
        description="ass=字幕引擎烧录（轻量，缺省）；html=透明 HTML 动画叠加（效果更丰富，需要动画录制组件）",
    )
    kind: Literal["title", "lower_third", "data_card", "custom"] = Field(
        description=(
            "title=标题卡（居中）；lower_third=下三分之一信息条（姓名、职位等）；"
            "data_card=数据卡片（右上角）；custom=自定义 HTML 动画叠加（仅 html，按 brief 生成）"
        )
    )
    title: str | None = Field(default=None, max_length=60, description="标题（title/lower_third 必填）")
    subtitle: str | None = Field(default=None, max_length=80, description="副标题 / 第二行")
    items: list[CardItemArgs] | None = Field(default=None, max_length=6, description="data_card 的数据行")
    brief: str | None = Field(default=None, max_length=1000, description="custom：叠加层画面说明")
    start: float | None = Field(default=None, ge=0, le=60, description="相对本镜开头的开始秒，缺省从头")
    end: float | None = Field(default=None, gt=0, le=60, description="相对本镜开头的结束秒，缺省到尾")
    effect: Literal["none", "fade", "slide"] | None = Field(default=None, description="入场效果，缺省 fade")


class ShotTransitionArgs(StrictModel):
    """分镜转场（与 Shot.transition 一致），整体替换。镜内转场不改变时长；重叠转场不改变成片时间线
    （本镜多渲染尾部画面，过渡从切点开始、在下一镜开头完成）。"""

    enter: TransitionEdgeArgs | None = Field(default=None, description="本镜开头的镜内转场")
    leave: TransitionEdgeArgs | None = Field(
        default=None, description="本镜结尾的镜内转场（设置了 next 时被取代）"
    )
    next: OverlapTransitionArgs | None = Field(
        default=None, description="本镜到下一镜的重叠转场（最后一镜忽略；下一镜的 enter 被取代）"
    )


class ShotFields(StrictModel):
    visual: str | None = Field(
        default=None, description="画面意象：一句中文，不超过 120 字；空字符串表示清空"
    )
    text: str | None = Field(default=None, min_length=1, max_length=200, description="旁白（配音与字幕）")
    query: str | None = Field(
        default=None, min_length=1, max_length=400, description="召回查询，2–5 个中文词"
    )
    estimated_seconds: float | None = Field(default=None, gt=0, le=60)
    recall: ShotRecallArgs | None = Field(
        default=None, description="分镜召回设置（整体替换）；恢复默认文字召回请用 clear"
    )
    granularity: Literal["shot", "moment", "scene"] | None = Field(
        default=None,
        description="本分镜召回粒度：shot=镜头；moment=精细片段；scene=场景；恢复跟随服务配置请用 clear",
    )
    recall_limit: int | None = Field(
        default=None, ge=1, le=50, description="本分镜召回候选数量；恢复跟随方案请用 clear"
    )
    source: ShotSourceArgs | None = Field(
        default=None, description="画面来源（整体替换）；恢复素材召回请用 clear"
    )
    music: ShotMusicArgs | None = Field(
        default=None,
        description="背景音乐提示点（整体替换）：从该分镜起换成新的背景音乐（generate/cover）或停止（none），"
        "之后的分镜顺延；去掉提示点（改为顺延前面的背景音乐）请用 clear",
    )
    transition: ShotTransitionArgs | None = Field(
        default=None,
        description="转场（整体替换）：enter/leave 镜内转场，next 到下一镜的重叠转场；恢复硬切请用 clear",
    )
    highlights: list[str] | None = Field(
        default=None,
        max_length=10,
        description="字幕高亮词（整体替换，≤10 个、每个 ≤20 字）：烧录字幕时这些词变色；清空请用 clear",
    )
    overlays: list[ShotOverlayArgs] | None = Field(
        default=None,
        max_length=4,
        description="文字层（整体替换，≤4 个）：标题卡、信息条、数据卡片或自定义 HTML 叠加；"
        "ass 文字层即使软字幕也会烧录进画面；清空请用 clear",
    )


ClearableField = Literal[
    "recall",
    "granularity",
    "recall_limit",
    "visual",
    "source",
    "music",
    "transition",
    "highlights",
    "overlays",
]


class ShotOperation(StrictModel):
    op: Literal["update", "insert", "delete", "move", "merge", "split"] = Field(
        description="update 改字段；insert 在 after 之后插入新镜（after 省略=开头）；delete 删除；"
        "move 移到 to_index（从 1 开始）；merge 合并 shots 中相邻的分镜；split 按 parts 拆成多镜"
    )
    shot: str | None = Field(
        default=None, description="目标分镜：完整 ID、短 ID 或 #序号（update/delete/move/split）"
    )
    shots: list[str] | None = Field(
        default=None, min_length=2, description="merge：要合并的相邻分镜，按任意顺序"
    )
    after: str | None = Field(default=None, description="insert：插在这个分镜之后")
    to_index: int | None = Field(default=None, ge=1, description="move：目标位置（从 1 开始）")
    fields: ShotFields | None = Field(default=None, description="update/insert：要设置的字段")
    clear: list[ClearableField] | None = Field(
        default=None,
        description=(
            "update：恢复为跟随方案或清空的字段（recall=默认文字召回，granularity=跟随服务配置，"
            "recall_limit=跟随方案，visual=清空画面意象，source=恢复素材召回，music=去掉背景音乐提示点、"
            "顺延前面，transition=恢复硬切，highlights=清空高亮词，overlays=去掉文字层）；不能与 fields "
            "中的同名字段同时给出"
        ),
    )
    parts: list[ShotFields] | None = Field(
        default=None,
        min_length=2,
        description="split：拆分后的各镜；各段旁白须由 get_shot_details 返回的完整旁白"
        "按顺序切分，按顺序拼接（忽略空白）须等于原旁白",
    )


class EditShots(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    operations: list[ShotOperation] = Field(
        min_length=1, max_length=50, description="按顺序执行的分镜结构操作"
    )


def recall_is_default(recall: dict | None) -> bool:
    if not recall:
        return True
    filters = recall.get("filters") or {}
    return (
        recall.get("mode", "text") == "text"
        and not recall.get("person_ids")
        and recall.get("person_mode", "auto") == "auto"
        and recall.get("person_relation", "all") == "all"
        and not filters.get("source_id")
        and not filters.get("source_ids")
        and not filters.get("source_group_ids")
        and not filters.get("min_duration")
        and not filters.get("max_duration")
        and not filters.get("scene")
        and not filters.get("emotion")
        and not filters.get("shot_size")
    )


LABELS = {
    "visual": "画面意象",
    "text": "旁白",
    "query": "查询",
    "estimated_seconds": "时长",
    "recall": "召回设置",
    "granularity": "召回粒度",
    "recall_limit": "召回数量",
    "source": "画面来源",
    "music": "背景音乐",
    "transition": "转场",
    "highlights": "字幕高亮词",
    "overlays": "文字层",
}
RECALL_KEYS = ("recall", "granularity", "recall_limit")
INHERITED_KEYS = (*RECALL_KEYS, "source")  # split 时未提供即继承原镜的字段
MODE_LABELS = {"text": "文字召回", "image": "图片召回", "video": "视频召回"}
GRANULARITY_LABELS = {"shot": "镜头", "moment": "精细片段", "scene": "场景"}
PERSON_MODE_LABELS = {"auto": "自动", "onscreen": "画面出镜", "mentioned": "对白提及"}
RECALL_REBUILD = "召回、选片、渲染及成片会在下次构建时重建"
SOURCE_LABELS = {
    "recall": "素材召回",
    "clip": "上传片段",
    "image": "静态图片",
    "aigc_image": "AI 生成图片",
    "aigc": "AI 生成视频",
    "animation": "AI 动画",
    "tool": "第三方工具",
}
MUSIC_LABELS = {"generate": "生成背景音乐", "cover": "翻唱", "library": "曲库音乐", "none": "停止背景音乐"}
VIDEO_MODE_LABELS = {
    "text": "文生视频",
    "first_frame": "首帧图生视频",
    "last_frame": "尾帧图生视频",
    "first_last": "首尾帧生视频",
    "reference": "主体参考生视频",
}
MOTION_LABELS = {
    "zoom-in": "推近",
    "zoom-out": "拉远",
    "pan-left": "向左平移",
    "pan-right": "向右平移",
    "none": "静止",
}
# 改为某种来源后下次构建要重建的阶段（配音只依赖旁白，来源变化不重配）。
SOURCE_REBUILD = {
    "recall": "召回、选片、渲染及成片会在下次构建时重建",
    "clip": "片段选窗、渲染及成片会在下次构建时重建（不需要召回与索引）",
    "image": "图片画面渲染及成片会在下次构建时重建（不需要召回、选片与索引）",
    "aigc_image": "生成图片（调用图片生成服务，可能产生费用）、图片画面渲染及成片会在下次构建时重建"
    "（不需要召回、选片与索引）",
    "aigc"
    "": "生成画面（调用视频生成服务，可能产生费用）、选窗、渲染及成片会在下次构建时重建（不需要召回与索引）",
    "animation"
    "": "生成动画（调用动画生成模型写源码）、录制渲染及成片会在下次构建时重建（不需要召回、选片与索引）",
    "tool": "调用第三方工具（参数与允许上传的素材会发送给该服务）、选窗或图片渲染及成片会在下次构建时重建"
    "（不需要召回与索引）",
}


def validation_message(exc: ValidationError) -> str:
    message = exc.errors()[0]["msg"]
    return message.removeprefix("Value error, ")


def normalize_recall(value: ShotRecallArgs | dict) -> dict:
    """召回参数 → ShotRecall 的完整规范形状（与工作台存储一致）；image/video 缺省 media_kind 取 mode。"""

    data = value.model_dump() if isinstance(value, ShotRecallArgs) else dict(value)
    if data.get("mode", "text") != "text" and data.get("media_kind") is None:
        data["media_kind"] = data["mode"]
    try:
        return ShotRecall.model_validate(data).model_dump()
    except ValidationError as exc:
        raise ToolError(f"召回设置无效：{validation_message(exc)}", code="invalid_recall") from exc


def normalize_source(value: ShotSourceArgs | dict | None) -> dict | None:
    """来源参数 → Shot.source 的规范形状；recall 返回 None（与工作台存储一致：素材召回不落键）。"""

    if value is None:
        return None
    data = value.model_dump(exclude_none=True) if isinstance(value, ShotSourceArgs) else dict(value)
    if data.get("kind") == "recall":
        return None
    if data.get("kind") not in ("aigc", "aigc_image", "animation", "tool") and not data.get("media_id"):
        raise ToolError(
            "clip/image 来源需要 media_id；用户给的附件请用 attach_query_media（purpose=source）上传",
            code="invalid_source",
        )
    allowed = {
        "clip": {"kind", "media_id", "start"},
        "image": {"kind", "media_id", "motion", "strength"},
        "aigc": {
            "kind",
            "prompt",
            "negative",
            "image_id",
            "seed",
            "mode",
            "last_image_id",
            "references",
            "model",
            "resolution",
            "ratio",
            "duration",
        },
        "aigc_image": {"kind", "prompt", "reference_id", "seed", "prompt_optimizer", "motion", "strength"},
        "animation": {"kind", "brief"},
        "tool": {"kind", "tool_id", "tool", "arguments", "uploads", "output", "motion", "strength"},
    }
    extra = set(data) - allowed[data["kind"]]
    if extra:
        raise ToolError(
            f"{SOURCE_LABELS[data['kind']]}来源不支持字段：{'、'.join(sorted(extra))}", code="invalid_source"
        )
    try:
        return TypeAdapter(ShotSource).validate_python(data).model_dump()
    except ValidationError as exc:
        raise ToolError(f"画面来源无效：{validation_message(exc)}", code="invalid_source") from exc


def source_kind(source: dict | None) -> str:
    return (source or {}).get("kind") or "recall"


def normalize_music(value: ShotMusicArgs | dict | None) -> dict | None:
    """背景音乐参数 → Shot.music 的规范形状。"""

    if value is None:
        return None
    data = value.model_dump(exclude_none=True) if isinstance(value, ShotMusicArgs) else dict(value)
    try:
        return ShotMusic.model_validate(data).model_dump()
    except ValidationError as exc:
        raise ToolError(f"背景音乐设置无效：{validation_message(exc)}", code="invalid_music") from exc


def normalize_transition(value: ShotTransitionArgs | dict | None) -> dict | None:
    """转场参数 → Shot.transition 的规范形状；三项皆空返回 None（硬切不落键）。"""

    if value is None:
        return None
    data = value.model_dump(exclude_none=True) if isinstance(value, ShotTransitionArgs) else dict(value)
    try:
        parsed = ShotTransition.model_validate(data)
    except ValidationError as exc:
        raise ToolError(f"转场设置无效：{validation_message(exc)}", code="invalid_transition") from exc
    return None if parsed.empty else parsed.model_dump()


def music_text(music: dict | None, media: str | None = None) -> str:
    if not music:
        return "无提示点（顺延前面分镜的背景音乐）"
    kind = music["kind"]
    if kind == "none":
        return "从此分镜起停止背景音乐"
    parts = [MUSIC_LABELS[kind]]
    if music.get("prompt"):
        parts.append(f"描述「{clip(music['prompt'], 60)}」")
    if kind == "generate":
        parts.append(
            "纯音乐"
            if music.get("instrumental", True)
            else "带人声" + ("（含歌词）" if music.get("lyrics") else "（自动写词）")
        )
    if music.get("audio_id"):
        parts.append(f"参考音频 {media or music['audio_id'][:8]}")
    if music.get("track_id"):
        parts.append(f"曲目 {music['track_id'][:8]}")
    parts.append(f"音量 {music.get('volume', 0.25):g}")
    return "，".join(parts)


def source_text(source: dict | None, media: str | None = None) -> str:
    """画面来源的一行中文摘要；``media`` 覆盖文件显示名（默认取 media_id 前 8 位）。"""
    kind = source_kind(source)
    if kind == "recall":
        return "素材召回（默认）"
    if kind == "aigc":
        prompt = (
            f"提示词「{clip(source['prompt'], 60)}」"
            if source.get("prompt")
            else "提示词缺省（画面意象/查询）"
        )
        image = source.get("image_id")
        extra = [
            f"参考图 {media or image[:8]}" if image else "",
            f"种子 {source['seed']}" if source.get("seed") is not None else "",
            f"反向提示词「{clip(source['negative'], 30)}」" if source.get("negative") else "",
        ]
        mode = source.get("mode") or ("first_frame" if image else "text")
        extra += [
            f"尾帧 {source['last_image_id'][:8]}" if source.get("last_image_id") else "",
            f"参考素材 {len(source['references'])} 个" if source.get("references") else "",
            *[
                f"{label} {source[key]}"
                for key, label in (
                    ("model", "模型"),
                    ("resolution", "分辨率"),
                    ("ratio", "比例"),
                    ("duration", "时长"),
                )
                if source.get(key)
            ],
        ]
        return f"AI 生成视频（{VIDEO_MODE_LABELS[mode]}），" + "，".join(
            [prompt, *[item for item in extra if item]]
        )
    if kind == "aigc_image":
        prompt = (
            f"提示词「{clip(source['prompt'], 60)}」"
            if source.get("prompt")
            else "提示词缺省（画面意象/查询）"
        )
        extra = [
            f"主体参考图 {media or source['reference_id'][:8]}" if source.get("reference_id") else "",
            f"种子 {source['seed']}" if source.get("seed") is not None else "",
            f"{MOTION_LABELS.get(source.get('motion', 'zoom-in'))}，强度 {source.get('strength', 0.3):g}",
        ]
        return "AI 生成图片，" + "，".join([prompt, *[item for item in extra if item]])
    if kind == "animation":
        brief = (
            f"画面说明「{clip(source['brief'], 60)}」"
            if source.get("brief")
            else "画面说明缺省（画面意象/旁白）"
        )
        return f"AI 动画，{brief}"
    if kind == "tool":
        extra = [
            f"参数 {clip(json.dumps(source.get('arguments') or {}, ensure_ascii=False), 60)}",
            f"上传素材 {len(source['uploads'])} 个" if source.get("uploads") else "",
        ]
        return (
            f"第三方工具 {source['tool_id']} / "
            f"{source['tool']}（产物{'图片' if source.get('output') == 'image' else '视频'}），"
            + "，".join(item for item in extra if item)
        )
    name = media or source["media_id"][:8]
    if kind == "clip":
        return f"上传片段 {name}，起点 {source.get('start') or 0:g} 秒"
    return (
        f"静态图片 {name}，{MOTION_LABELS.get(source.get('motion', 'zoom-in'))}，"
        f"强度 {source.get('strength', 0.3):g}"
    )


def shot_values(fields: ShotFields | None) -> dict:
    """ShotFields → 要写入分镜的值；recall 规范化为完整 ShotRecall 形状，source 规范化为 Shot.source。"""
    if fields is None:
        return {}
    values = fields.model_dump(exclude_none=True)
    if fields.recall is not None:
        values["recall"] = normalize_recall(fields.recall)
    if fields.source is not None:
        values["source"] = normalize_source(fields.source)
    if fields.music is not None:
        values["music"] = normalize_music(fields.music)
    if fields.transition is not None:
        values["transition"] = normalize_transition(fields.transition)
    if fields.overlays is not None:
        values["overlays"] = normalize_overlays(fields.overlays)
    if fields.highlights is not None:
        try:
            values["highlights"] = normalize_highlights(fields.highlights)
        except ValueError as exc:
            raise ToolError(f"字幕高亮词无效：{exc}", code="invalid_highlights") from exc
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
        parts.append(
            f"人物 "
            f"{'、'.join(persons)}（{PERSON_MODE_LABELS.get(recall.get('person_mode', 'auto'))}，"
            f"{relation}）"
        )
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
    if key == "source":
        return source_text(value)
    if key == "music":
        return music_text(value)
    if key == "transition":
        # 延迟导入：保持工作区阶段处理器的注册顺序。
        from studio import workspace as ws

        return ws.transitions.describe(value)
    if key == "highlights":
        return "、".join(value) if value else "无"
    if key == "overlays":
        return overlays_text(value)
    return value


OVERLAY_LABELS = {"title": "标题卡", "lower_third": "信息条", "data_card": "数据卡片", "custom": "自定义叠加"}


def overlays_text(value: list[dict] | None) -> str:
    if not value:
        return "无"
    parts = []
    for layer in value:
        name = OVERLAY_LABELS[layer["kind"]] + ("（HTML）" if layer.get("engine") == "html" else "")
        detail = layer.get("title") or clip(layer.get("brief") or "", 30)
        if layer.get("items"):
            detail += " " + "、".join(f"{item['label']} {item['value']}" for item in layer["items"])
        parts.append(f"{name}「{detail.strip()}」")
    return "；".join(parts)


def normalize_overlays(value) -> list[dict] | None:

    if not value:
        return None
    data = [
        item.model_dump(exclude_none=True) if isinstance(item, ShotOverlayArgs) else dict(item)
        for item in value
    ]
    try:
        return [layer.model_dump() for layer in TypeAdapter(list[ShotOverlay]).validate_python(data)]
    except ValidationError as exc:
        raise ToolError(f"文字层无效：{validation_message(exc)}", code="invalid_overlays") from exc


def recall_warnings(view: PlanView, shots: list[dict]) -> list[str]:
    """召回相关字段变化的已有分镜 → 级联重建提示（Req 14.2）。"""
    before = {s.get("id"): s for s in view.shots()}
    changed = [
        s["id"]
        for s in shots
        if s.get("id") in before and any(before[s["id"]].get(k) != s.get(k) for k in RECALL_KEYS)
    ]
    if not changed:
        return []
    labels = "、".join(f"#{view.order.index(sid) + 1} {short(sid)}" for sid in changed)
    return [f"{labels} 的召回设置已变化：{RECALL_REBUILD}"]


def music_warnings(view: PlanView, shots: list[dict]) -> list[str]:
    before = {s.get("id"): s for s in view.shots()}
    found = []
    for shot in shots:
        sid = shot.get("id")
        if sid in before and before[sid].get("music") != shot.get("music"):
            music = shot.get("music")
            if music and music["kind"] == "library":
                found.append(
                    f"#{view.order.index(sid) + 1} {short(sid)} "
                    f"改用曲库音乐：下次构建复制曲目（不调用生成服务），"
                    "之后的分镜顺延这段音乐，成片重新合成"
                )
            elif music and music["kind"] in ("generate", "cover"):
                found.append(
                    f"#{view.order.index(sid) + 1} {short(sid)} 的背景音乐将变化：下次构建会调用音乐生成服务"
                    "（可能产生费用），之后的分镜顺延这段音乐，成片重新合成"
                )
            else:
                found.append(
                    f"#{view.order.index(sid) + 1} {short(sid)} 的背景音乐提示点已变化：成片会重新合成"
                )
    return found


def source_warnings(view: PlanView, shots: list[dict]) -> list[str]:
    """画面来源变化的已有分镜 → 按新来源说明下次构建重建哪些阶段。"""
    before = {s.get("id"): s for s in view.shots()}
    found = []
    for shot in shots:
        sid = shot.get("id")
        if sid in before and before[sid].get("source") != shot.get("source"):
            kind = source_kind(shot.get("source"))
            found.append(
                f"#{view.order.index(sid) + 1} {short(sid)} 的画面来源改为{SOURCE_LABELS[kind]}："
                f"{SOURCE_REBUILD[kind]}"
            )
    return found


def set_visual(shot: dict, value: str | None) -> None:
    """写入去除首尾空白后的 visual；空值删除该键（与 Shot 的空 visual 不落键一致）。"""
    visual = (value or "").strip()
    if visual:
        shot["visual"] = visual
    else:
        shot.pop("visual", None)


def join_visual(a: str | None, b: str | None) -> str | None:
    """与 studio-editor.js 的 joinVisual 一致：跳过空值、以「；」拼接、按字符截到 VISUAL_LIMIT；
    全空返回 None。
    """

    parts = [v.strip() for v in (a, b) if v and v.strip()]
    return "；".join(parts)[:VISUAL_LIMIT] or None


def merge_into(previous: dict, current: dict) -> None:
    """与 studio-editor.js changeStructure 中的 merge 逐字段一致。"""
    previous["text"] += current["text"]
    previous["query"] += f"；{current['query']}"
    previous["estimated_seconds"] += current["estimated_seconds"]
    if recall_is_default(previous.get("recall")) and current.get("recall"):
        previous["recall"] = current["recall"]
    set_visual(previous, join_visual(previous.get("visual"), current.get("visual")))
    if not previous.get("music") and current.get("music"):
        previous["music"] = current["music"]
    merge_transition(previous, current)
    words = list(dict.fromkeys([*(previous.get("highlights") or []), *(current.get("highlights") or [])]))[
        :10
    ]
    if words:
        previous["highlights"] = words
    if not previous.get("overlays") and current.get("overlays"):  # 文字层：保留上一镜的，没有时继承被合并镜的
        previous["overlays"] = current["overlays"]


def merge_transition(previous: dict, current: dict) -> None:
    """合并：保留前一镜的入场转场，出场与重叠转场取后一镜（与 studio-editor.js 一致）。"""
    first, last = previous.get("transition") or {}, current.get("transition") or {}
    merged = {
        key: value
        for key, value in (
            ("enter", first.get("enter")),
            ("leave", last.get("leave")),
            ("next", last.get("next")),
        )
        if value
    }
    if merged:
        previous["transition"] = merged
    else:
        previous.pop("transition", None)


def split_transition(original: dict | None, number: int, count: int) -> dict | None:
    """拆分：入场转场留在第一段，出场与重叠转场留在最后一段，中间各段硬切。"""
    original = original or {}
    kept = {}
    if number == 0 and original.get("enter"):
        kept["enter"] = original["enter"]
    if number == count - 1:
        kept.update({key: original[key] for key in ("leave", "next") if original.get(key)})
    return kept or None


def plan_shots(view: PlanView, operations: list[ShotOperation]) -> tuple[list[dict], list[dict]]:
    order = view.order
    shots = copy.deepcopy(view.shots())
    labels = {sid: f"#{i} {short(sid)}" for i, sid in enumerate(order, 1)}
    changes = []
    touched: list[str] = []

    def find(ref: str) -> str:
        found = resolve_shot(order, ref)
        if found["status"] == "ambiguous":
            raise ToolError(
                f"分镜 {ref} 有歧义，候选："
                + "、".join(f"#{c['index']} {c['shot_id']}" for c in found["candidates"]),
                code="ambiguous_shot",
                value=found,
            )
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
                raise ToolError(
                    "clear 与 fields 不能同时给出同一字段：" + "、".join(LABELS[k] for k in both),
                    code="invalid_operation",
                )
            shot = shots[position(sid)]
            for key in clear:
                before = shot.get(key)
                if key in ("visual", "source", "music", "transition", "highlights", "overlays"):
                    shot.pop(key, None)
                else:
                    shot[key] = None
                changes.append(
                    {
                        "label": f"{'清空' if key == 'visual' else '恢复'} {labels[sid]} 的{LABELS[key]}",
                        "before": field_text(key, before),
                        "after": field_text(key, shot.get(key)),
                    }
                )
            for key, value in values.items():
                if key == "visual":
                    before = shot.get("visual")
                    set_visual(shot, value)
                    after = shot.get("visual")
                    changes.append(
                        {
                            "label": f"{'清空' if after is None else '修改'} {labels[sid]} 的{LABELS[key]}",
                            "before": before,
                            "after": after,
                        }
                    )
                    continue
                changes.append(
                    {
                        "label": f"修改 {labels[sid]} 的{LABELS[key]}",
                        "before": field_text(key, shot.get(key)),
                        "after": field_text(key, value),
                    }
                )
                if key == "source" and value is None:
                    shot.pop("source", None)  # 素材召回不落键，与已存内容一致
                elif key in ("transition", "highlights", "overlays") and value is None:
                    shot.pop(key, None)  # 硬切 / 无高亮词不落键
                else:
                    shot[key] = value
        elif op.op == "insert":
            values = shot_values(op.fields)
            if not values.get("text") or not values.get("query"):
                raise ToolError("insert 需要 fields.text 与 fields.query", code="invalid_operation")
            values.setdefault("estimated_seconds", max(1.0, round(len(values["text"]) / 4, 2)))
            set_visual(values, values.get("visual"))
            if values.get("source", ...) is None:
                values.pop("source", None)
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
                raise ToolError(
                    "只能合并相邻的分镜；请先用 move 调整顺序，或向用户确认要合并哪些分镜",
                    code="not_adjacent",
                )
            before = [shots[i]["text"] for i in indexes]
            first = shots[indexes[0]]
            for index in indexes[1:]:
                merge_into(first, shots[index])
            for index in reversed(indexes[1:]):
                shots.pop(index)
            changes.append(
                {
                    "label": "合并 " + " + ".join(labels[sid] for sid in sorted(ids, key=order.index)),
                    "before": " | ".join(before),
                    "after": first["text"],
                }
            )
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
                shot.update(
                    text=part["text"],
                    query=part.get("query") or original["query"],
                    estimated_seconds=part.get("estimated_seconds")
                    or max(1.0, round(len(part["text"]) / 4, 2)),
                )
                if "visual" in part:  # 未提供 visual 的段继承原镜（已随 original 复制）
                    set_visual(shot, part["visual"])
                for key in INHERITED_KEYS:  # 未提供的召回字段与画面来源同样继承原镜
                    if key in part:
                        shot[key] = part[key]
                # 背景音乐提示点只留在第一段（之后的段顺延）；各段显式给出时以给出的为准。
                if number and "music" not in part:
                    shot.pop("music", None)
                elif "music" in part:
                    shot["music"] = part["music"]
                if "transition" in part:
                    shot["transition"] = part["transition"]
                else:
                    shot["transition"] = split_transition(original.get("transition"), number, len(parts))
                if not shot.get("transition"):
                    shot.pop("transition", None)
                if number and "overlays" not in part:  # 文字层只留在第一段
                    shot.pop("overlays", None)
                elif "overlays" in part:
                    shot["overlays"] = part["overlays"]
                if not shot.get("overlays"):
                    shot.pop("overlays", None)
                if shot.get("source", ...) is None:
                    shot.pop("source", None)
                new.append(shot)
            shots[index : index + 1] = new
            changes.append(
                {
                    "label": f"拆分 {labels[sid]}",
                    "before": original["text"],
                    "after": " | ".join(p["text"] for p in new),
                }
            )
    if not shots:
        raise ToolError("操作后没有任何分镜；至少保留一个分镜", code="invalid_operation")

    problem = visual_error(shots)
    if problem:
        raise ToolError(problem, code="invalid_shot")
    for number, shot in enumerate(shots, 1):
        try:
            ShotBody.model_validate(shot)
        except ValidationError as exc:
            problem = exc.errors()[0]
            raise ToolError(
                f"操作后第 {number} 个分镜无效（{'.'.join(map(str, problem['loc']))}：{problem['msg']}）",
                code="invalid_shot",
            ) from exc
    return shots, changes, touched


async def preview_edit_shots(args: EditShots, ctx: ToolContext) -> Preview:
    view = await load_view(ctx, args.job_id, args.plan_id)
    shots, changes, touched = plan_shots(view, args.operations)
    planned(
        ctx,
        "shots",
        (
            view.job["id"],
            view.plan_id,
            {"revision": view.revision, "shots": shots},
            len(view.order),
            view.order,
            touched,
        ),
    )
    return Preview(
        f"分镜 {len(view.order)} → {len(shots)} "
        f"个；未改动的分镜保留现有产物，受影响分镜的召回、配音、选片、渲染及成片"
        "会在下次构建时重建",
        changes,
        warnings=recall_warnings(view, shots) + source_warnings(view, shots) + music_warnings(view, shots),
        focus=studio_focus(
            view.job["id"], view.plan_id, shots=touched or None, section=None if touched else "storyboard"
        ),
    )


async def edit_shots(args: EditShots, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "shots")
    if stored is None:
        view = await load_view(ctx, args.job_id, args.plan_id)
        shots, _, touched = plan_shots(view, args.operations)
        stored = (
            view.job["id"],
            view.plan_id,
            {"revision": view.revision, "shots": shots},
            len(view.order),
            view.order,
            touched,
        )
    job, pid, body, before, old_order, touched = stored
    data = check(await ctx.client.patch(f"/api/studio/jobs/{job}/plans/{pid}", body), "修改分镜")
    after = PlanView(data, pid)

    return ToolOutput(
        {"revision": after.revision, "shots": after.shot_rows()},
        text=f"已修改分镜：{before} → {len(after.order)} 个。如需生成成片，可先 get_preflight "
        f"查看要重建的节点，"
        f"再 run_build。\n" + board_text(after),
        mutation=mutation(job, pid),
        focus=studio_focus(
            job,
            pid,
            shots=edited_shots(old_order, after.order, touched)[:20] or None,
            stage="content",
            section="storyboard",
        ),
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
    return Preview(
        f"修改任务 {job} 的标题（手动命名后不再自动生成标题）",
        [{"label": "标题", "before": title or "（未命名）", "after": args.title}],
        focus=studio_focus(job),
    )


async def rename_job(args: RenameJob, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    state = check(await ctx.client.patch(f"/api/studio/jobs/{job}/title", {"title": args.title}), "修改标题")
    return ToolOutput(
        {"job_id": job, "title": state["title"]},
        text=f"已把任务 {job} 的标题改为「{state['title']}」。",
        mutation=mutation(job),
        focus=studio_focus(job),
    )


# ------------------------------------------------------------ 查询文件召回 / 画面来源文件
class AttachQueryMedia(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    shot: str = Field(description="目标分镜：完整 ID、短 ID 或 #序号")
    attachment: str = Field(pattern=r"^attachment://", description="图片或视频附件引用 attachment://…")
    kind: Literal["image", "video", "audio"] | None = Field(
        default=None, description="文件类型；缺省按附件的内容类型推断"
    )
    purpose: Literal["query", "source"] = Field(
        default="query",
        description="query=作为召回查询（以图/视频找素材）；source=直接作为该分镜的画面"
        "（视频→上传片段 clip，图片→静态图片 image，不再召回；"
        "分镜已是 aigc 来源时图片作为首帧参考图，aigc_image 来源时作为主体参考图；"
        "音频只能用 source：作为该分镜背景音乐翻唱的参考音频或 aigc 的参考音频）",
    )
    role: Literal["first_frame", "last_frame", "reference", "cover"] | None = Field(
        default=None,
        description="purpose=source 时附件的用途：first_frame/last_frame=aigc 首帧/尾帧图；reference=aigc "
        "主体参考素材"
        "（图片/视频/音频）；cover=背景音乐翻唱参考音频（分镜须已有 cover "
        "提示点或会新建一个）。缺省按来源推断",
    )


KIND_LABELS = {"image": "图片", "video": "视频", "audio": "音频"}


def media_kind(meta: dict, explicit: str | None) -> Literal["image", "video"]:
    if explicit:
        return explicit
    ctype = str(meta.get("content_type") or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("audio/"):
        return "audio"
    raise ToolError(
        f"无法从内容类型 {ctype or '未知'} 判断附件是图片还是视频：请显式指定 kind=image 或 video",
        code="invalid_arguments",
    )


def query_recall(current: dict | None, kind: str, media_id: str) -> dict:
    """image/video 召回：保留原有素材范围与时长等筛选，去掉人物条件（图片/视频召回不支持人物）。"""
    filters = (current or {}).get("filters") or {}
    return normalize_recall({"mode": kind, "media_id": media_id, "media_kind": kind, "filters": filters})


def media_source(current: dict | None, kind: str, media_id: str, role: str | None = None) -> dict:
    """附件作为画面来源：视频 → clip（从头开始），图片 → image（同为图片来源时保留原运动设置）；
    分镜已是 aigc 来源时，图片作为首帧参考图（role 可改为尾帧或主体参考，音频/视频也可作主体参考）；
    aigc_image 来源时，图片作为主体参考图。"""
    if source_kind(current) == "aigc" and (
        role in ("first_frame", "last_frame", "reference") or kind == "image"
    ):
        if role == "reference" or kind != "image":
            refs = [*(current.get("references") or []), {"kind": kind, "media_id": media_id}]
            return normalize_source({**current, "references": refs, "mode": "reference"})
        if role == "last_frame":
            mode = "first_last" if current.get("image_id") else "last_frame"
            return normalize_source({**current, "last_image_id": media_id, "mode": mode})
        mode = current.get("mode")
        if mode in ("last_frame",):
            mode = "first_last"
        return normalize_source({**current, "image_id": media_id, **({"mode": mode} if mode else {})})
    if kind == "image" and source_kind(current) == "aigc_image":
        return normalize_source({**current, "reference_id": media_id})
    if kind == "audio":
        raise ToolError(
            "音频不能直接作为画面；请用 role=cover 作为背景音乐翻唱参考，或先把来源设为 aigc 再作为主体参考",
            code="invalid_arguments",
        )
    if kind == "video":
        return normalize_source({"kind": "clip", "media_id": media_id})
    previous = current if source_kind(current) == "image" else {}
    return normalize_source(
        {
            "kind": "image",
            "media_id": media_id,
            **{k: previous[k] for k in ("motion", "strength") if k in previous},
        }
    )


def cover_music(current: dict | None, media_id: str) -> dict:
    """附件作为翻唱参考音频：保留已有 cover/generate 提示点的描述与音量，描述不足 10 字时补一个缺省风格。"""
    previous = current if current and current.get("kind") != "none" else {}
    prompt = previous.get("prompt") or ""
    if len(prompt) < 10:
        prompt = (prompt + " 温暖流行风格翻唱，适合作为视频背景音乐").strip()
    return normalize_music(
        {
            "kind": "cover",
            "prompt": prompt[:300],
            "audio_id": media_id,
            "volume": previous.get("volume", 0.25),
            "instrumental": True,
        }
    )


async def query_media_plan(args: AttachQueryMedia, ctx: ToolContext) -> dict:
    view = await load_view(ctx, args.job_id, args.plan_id)
    found = resolve_shot(view.order, args.shot)
    if found["status"] != "ok":
        raise ToolError(f"无法确定分镜 {args.shot}：{found['status']}", code="unresolved_shot", value=found)
    meta = await attachment_meta(ctx, args.attachment)
    kind = media_kind(meta, args.kind)
    if kind == "audio" and args.purpose != "source":
        raise ToolError(
            "音频只能用 purpose=source 上传（翻唱参考或 aigc 主体参考）", code="invalid_arguments"
        )
    sid = found["shot_id"]
    return {
        "job": view.job["id"],
        "plan": view.plan_id,
        "revision": view.revision,
        "shots": view.shots(),
        "shot_id": sid,
        "index": view.order.index(sid),
        "kind": kind,
        "meta": meta,
        "purpose": args.purpose,
    }


async def preview_attach_query_media(args: AttachQueryMedia, ctx: ToolContext) -> Preview:
    plan = await query_media_plan(args, ctx)
    planned(ctx, "query_media", plan)
    sid, kind, meta = plan["shot_id"], plan["kind"], plan["meta"]
    label = f"#{plan['index'] + 1} {short(sid)}"
    name = meta.get("filename") or args.attachment
    upload = {
        "label": "上传画面文件" if args.purpose == "source" else "上传查询文件",
        "after": f"{name}（{KIND_LABELS[kind]}，{size_text(meta.get('bytes'))}）",
    }
    if args.purpose == "source" and args.role == "cover":
        current = plan["shots"][plan["index"]].get("music")
        after = cover_music(current, "0" * 64)
        return Preview(
            f"上传音频，并把分镜 {label} 设为背景音乐翻唱提示点（一次确认完成两步）",
            [
                upload,
                {
                    "label": f"修改 {label} 的背景音乐",
                    "before": music_text(current),
                    "after": music_text(after, media=name),
                },
            ],
            warnings=["下次构建会调用音乐生成服务翻唱（可能产生费用），之后的分镜顺延这段音乐"],
            focus=studio_focus(plan["job"], plan["plan"], shots=[sid]),
        )
    if args.purpose == "source":
        current = plan["shots"][plan["index"]].get("source")
        after = media_source(current, kind, "0" * 64, args.role)
        target = SOURCE_LABELS[after["kind"]]
        return Preview(
            f"上传{KIND_LABELS[kind]}，并把分镜 {label} 的画面来源改为{target}（一次确认完成两步）",
            [
                upload,
                {
                    "label": f"修改 {label} 的画面来源",
                    "before": source_text(current),
                    "after": source_text(after, media=name),
                },
            ],
            warnings=[f"{label} 的画面来源将变化：{SOURCE_REBUILD[after['kind']]}"],
            focus=studio_focus(plan["job"], plan["plan"], shots=[sid]),
        )
    current = plan["shots"][plan["index"]].get("recall")
    after = recall_text(query_recall(current, kind, "0" * 64), media=name)
    warnings = [f"{label} 的召回设置将变化：{RECALL_REBUILD}"]
    if (current or {}).get("person_ids"):
        warnings.append("图片/视频召回不支持人物设置：原有人物条件会被移除")
    if source_kind(plan["shots"][plan["index"]].get("source")) != "recall":
        warnings.append(f"{label} 的画面来源不是素材召回：召回设置要在恢复素材召回后才生效")
    return Preview(
        f"上传{KIND_LABELS[kind]}查询文件，并把分镜 {label} 改为{MODE_LABELS[kind]}（一次确认完成两步）",
        [upload, {"label": f"修改 {label} 的召回设置", "before": recall_text(current), "after": after}],
        warnings=warnings,
        focus=studio_focus(plan["job"], plan["plan"], shots=[sid]),
    )


async def attach_query_media(args: AttachQueryMedia, ctx: ToolContext) -> ToolOutput:
    plan = planned(ctx, "query_media") or await query_media_plan(args, ctx)
    job, pid, sid, kind = plan["job"], plan["plan"], plan["shot_id"], plan["kind"]
    source = plan.get("purpose", "query") == "source"
    uploaded = upload_check(
        await ctx.client.upload(
            f"/api/studio/jobs/{job}/plans/{pid}/query-media",
            files={"file": args.attachment},
            data={"kind": kind, "purpose": "source" if source else "query"},
        ),
        "上传画面文件" if source else "上传查询文件",
    )
    media_id = uploaded["media_id"]
    shots = copy.deepcopy(plan["shots"])
    target = shots[plan["index"]]
    if source and args.role == "cover":
        target["music"] = cover_music(target.get("music"), media_id)
        data = check(
            await ctx.client.patch(
                f"/api/studio/jobs/{job}/plans/{pid}", {"revision": plan["revision"], "shots": shots}
            ),
            "修改背景音乐",
        )
        after = PlanView(data, pid)
        return ToolOutput(
            {
                "media_id": media_id,
                "kind": kind,
                "shot_id": sid,
                "revision": after.revision,
                "music": target["music"],
            },
            text=f"已上传音频（media_id={media_id[:8]}），分镜 #{plan['index'] + 1} {short(sid)} "
            f"已设为背景音乐翻唱提示点。",
            mutation=mutation(job, pid),
            focus=studio_focus(job, pid, shots=[sid], stage="tts"),
        )
    if source:
        target["source"] = media_source(target.get("source"), kind, media_id, args.role)
    else:
        target["recall"] = query_recall(target.get("recall"), kind, media_id)
    field = "画面来源" if source else "召回设置"
    try:
        data = check(
            await ctx.client.patch(
                f"/api/studio/jobs/{job}/plans/{pid}", {"revision": plan["revision"], "shots": shots}
            ),
            f"修改{field}",
        )
    except ToolError as exc:
        raise ToolError(
            f"{exc}（文件已上传，media_id={media_id}；可重新读取工作台后用 edit_shots 设置{field}）",
            code=exc.code,
            value={**(exc.value or {}), "media_id": media_id},
        ) from exc
    after = PlanView(data, pid)
    label = f"#{plan['index'] + 1} {short(sid)}"
    if source:
        return ToolOutput(
            {
                "media_id": media_id,
                "kind": kind,
                "shot_id": sid,
                "revision": after.revision,
                "source": target["source"],
                "preview_url": uploaded.get("preview_url"),
            },
            text=f"已上传{KIND_LABELS[kind]}（media_id={media_id[:8]}），分镜 {label} 的画面来源已改为"
            f"{SOURCE_LABELS[target['source']['kind']]}；{SOURCE_REBUILD[target['source']['kind']]}。",
            mutation=mutation(job, pid),
            focus=studio_focus(job, pid, shots=[sid], stage="content"),
        )
    return ToolOutput(
        {
            "media_id": media_id,
            "kind": kind,
            "shot_id": sid,
            "revision": after.revision,
            "recall": target["recall"],
            "preview_url": uploaded.get("preview_url"),
        },
        text=f"已上传{KIND_LABELS[kind]}查询文件（media_id={media_id[:8]}），分镜 {label} "
        f"已改为{MODE_LABELS[kind]}；下次构建时重新召回。",
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, shots=[sid], stage="recall"),
    )


# ------------------------------------------------------------ 参考文稿
class AddReferenceText(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    attachment: str | None = Field(
        default=None,
        pattern=r"^attachment://",
        description="UTF-8 文本附件（.txt/.md）引用 attachment://…；与 text 二选一",
    )
    text: str | None = Field(
        default=None, min_length=1, max_length=200000, description="直接粘贴的参考文稿正文"
    )
    name: str | None = Field(
        default=None, min_length=1, max_length=200, description="显示名称；缺省用附件文件名"
    )

    @model_validator(mode="after")
    def one_source(self):
        if (self.attachment is None) == (self.text is None):
            raise ValueError("attachment 与 text 必须且只能提供一个")
        return self


async def reference_plan(args: AddReferenceText, ctx: ToolContext) -> dict:
    # 延迟导入：保持工作区阶段处理器的注册顺序。
    from studio import workspace

    view = await load_view(ctx, args.job_id, args.plan_id)
    if args.attachment:
        meta = await attachment_meta(ctx, args.attachment)
        name = args.name or (meta.get("filename") or "参考文稿").rsplit(".", 1)[0]
        try:
            raw, _ = await ctx.client.fetch_bytes(
                f"/api/agent/attachments/{args.attachment[len('attachment://') :]}/content"
            )
        except Exception:  # noqa: BLE001 — 取不到内容时只在预览里给出大小，上传端点仍会校验
            raw = None
    else:
        meta, name, raw = None, args.name or "粘贴的参考文稿", args.text
    chars = None
    if raw is not None:
        try:
            chars = len(workspace.references.normalize(raw))
        except StudioError as exc:
            raise ToolError(str(exc), code="invalid_reference") from exc
    existing = view.plan["options"].get("references") or []
    return {
        "job": view.job["id"],
        "plan": view.plan_id,
        "revision": view.revision,
        "name": name,
        "chars": chars,
        "meta": meta,
        "total": sum(item["chars"] for item in existing),
        "limit": view.w.get("reference_limit"),
    }


async def preview_add_reference_text(args: AddReferenceText, ctx: ToolContext) -> Preview:
    plan = await reference_plan(args, ctx)
    planned(ctx, "reference", plan)
    size = (
        f"{plan['chars']} 字" if plan["chars"] is not None else size_text((plan["meta"] or {}).get("bytes"))
    )
    warnings = ["参考文稿只供文案创作参考：不会自动重写已有文案，需要时请显式重新生成文案"]
    if plan["limit"] and plan["chars"] is not None and plan["total"] + plan["chars"] > plan["limit"]:
        warnings.append(
            f"登记后共 {plan['total'] + plan['chars']} 字，超过 {plan['limit']} 字上限，服务端会拒绝"
        )
    return Preview(
        "为方案登记一份参考文稿",
        [
            {"label": "参考文稿", "after": f"{plan['name']}（{size}）"},
            {
                "label": "参考文稿总字数",
                "before": plan["total"],
                "after": plan["total"] + plan["chars"] if plan["chars"] is not None else "上传后统计",
            },
        ],
        warnings=warnings,
        focus=studio_focus(plan["job"], plan["plan"], section="script"),
    )


async def add_reference_text(args: AddReferenceText, ctx: ToolContext) -> ToolOutput:
    plan = planned(ctx, "reference") or await reference_plan(args, ctx)
    job, pid = plan["job"], plan["plan"]
    path = f"/api/studio/jobs/{job}/plans/{pid}/references"
    if args.attachment:
        data = upload_check(
            await ctx.client.upload(
                path,
                files={"file": args.attachment},
                data={"revision": plan["revision"], "name": plan["name"]},
            ),
            "登记参考文稿",
        )
    else:
        data = check(
            await ctx.client.post(
                path, {"revision": plan["revision"], "name": plan["name"], "text": args.text}
            ),
            "登记参考文稿",
        )
    item = data["reference"]
    items = data["plans"][pid]["options"].get("references") or []
    return ToolOutput(
        {"reference": item, "references": items, "revision": data["revision"]},
        text=f"已登记参考文稿「{item['name']}」（{item['chars']} 字，id={item['id'][:8]}），方案共 "
        f"{len(items)} 份。"
        "已有文案不会自动重写，需要时请重新生成文案。",
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, section="script"),
    )


class RemoveReferenceText(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    reference: str = Field(min_length=1, description="参考文稿 id（或至少 4 位前缀）或名称")


def find_reference(view: PlanView, ref: str) -> dict:
    items = view.plan["options"].get("references") or []
    found = (
        [i for i in items if i["id"] == ref]
        or [i for i in items if i["name"] == ref]
        or ([i for i in items if i["id"].startswith(ref)] if len(ref) >= 4 else [])
    )
    if len(found) != 1:
        listing = "；".join(f"{i['name']}({i['id'][:8]})" for i in items) or "（无）"
        raise ToolError(
            f"无法确定参考文稿 {ref}：{'有歧义' if found else '不存在'}；当前登记：{listing}",
            code="unresolved_reference",
        )
    return found[0]


async def remove_reference_plan(args: RemoveReferenceText, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    return view, find_reference(view, args.reference)


async def preview_remove_reference_text(args: RemoveReferenceText, ctx: ToolContext) -> Preview:
    view, item = await remove_reference_plan(args, ctx)
    planned(ctx, "remove_reference", (view.job["id"], view.plan_id, view.revision, item))
    return Preview(
        "移除方案登记的参考文稿（文件保留在任务目录）",
        [{"label": "参考文稿", "before": f"{item['name']}（{item['chars']} 字）", "after": "（移除）"}],
        warnings=["已有文案不会自动重写；需要按剩余参考文稿重写时请重新生成文案"],
        focus=studio_focus(view.job["id"], view.plan_id, section="script"),
    )


async def remove_reference_text(args: RemoveReferenceText, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "remove_reference")
    if stored is None:
        view, item = await remove_reference_plan(args, ctx)
        stored = (view.job["id"], view.plan_id, view.revision, item)
    job, pid, revision, item = stored
    data = check(
        await ctx.client.delete(
            f"/api/studio/jobs/{job}/plans/{pid}/references/{item['id']}", revision=revision
        ),
        "移除参考文稿",
    )
    return ToolOutput(
        {
            "removed": item["id"],
            "references": data["plans"][pid]["options"].get("references") or [],
            "revision": data["revision"],
        },
        text=f"已移除参考文稿「{item['name']}」。",
        mutation=mutation(job, pid),
        focus=studio_focus(job, pid, section="script"),
    )


# ------------------------------------------------------------ 版本选用
class SelectVersion(StrictModel):
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    stage: VersionStage = Field(
        description="阶段：script 文案、content 分镜内容、各逐镜阶段，或成片阶段（compose 成片等）"
    )
    version: str = Field(description="版本 ID 或其前缀（界面显示前 8 位）")
    shot: str | None = Field(default=None, description="分镜级阶段需要：完整 ID、短 ID 或 #序号")
    variant: str | None = Field(
        default=None,
        max_length=40,
        description="有变体的成片阶段（如某个画幅的导出、某个平台的封面）需要：变体名",
    )


def final_stages() -> tuple[str, ...]:
    # 延迟导入：保持工作区阶段处理器的注册顺序。
    from studio import workspace as ws

    return ws.stages.names(scope="final")


def selected_version(plan: dict, stage: str, sid: str | None):
    # 延迟导入：保持工作区阶段处理器的注册顺序。
    from studio import workspace as ws

    if stage == "script":
        return plan.get("script")
    if stage in final_stages():
        return ws.stages.handler(stage).selected(plan, sid)
    return plan["shots"][sid].get(stage)


async def select_body(args: SelectVersion, ctx: ToolContext):
    view = await load_view(ctx, args.job_id, args.plan_id)
    shot_id = None
    if args.stage in final_stages():
        shot_id = args.variant
    elif args.stage != "script":
        if not args.shot:
            raise ToolError(f"{STAGE_LABELS[args.stage]}阶段需要指定分镜", code="missing_shot")
        found = resolve_shot(view.order, args.shot)
        if found["status"] != "ok":
            raise ToolError(
                f"无法确定分镜 {args.shot}：{found['status']}", code="unresolved_shot", value=found
            )
        shot_id = found["shot_id"]
    version = resolve_version(view.w["versions"], args.version, stage=args.stage, shot_id=shot_id)
    if version["status"] != "ok":
        raise ToolError(
            f"无法确定版本 {args.version}：{version['status']}", code="unresolved_version", value=version
        )
    body = {
        "revision": view.revision,
        "version_id": version["version_id"],
        "stage": args.stage,
        "shot_id": shot_id,
    }
    return view, body


async def preview_select_version(args: SelectVersion, ctx: ToolContext) -> Preview:
    view, body = await select_body(args, ctx)
    planned(ctx, "select", (view.job["id"], view.plan_id, body))
    current = selected_version(view.plan, body["stage"], body["shot_id"])
    item = view.w["versions"][body["version_id"]]
    final = body["stage"] in final_stages()
    where = (
        (f"（{body['shot_id']}）" if final else f"（分镜 {short(body['shot_id'])}）")
        if body["shot_id"]
        else ""
    )
    return Preview(
        f"选用{STAGE_LABELS.get(body['stage'], body['stage'])}版本" + where,
        [
            {
                "label": "版本",
                "before": short(current) or "（无）",
                "after": f"{short(body['version_id'])} · {item['status']}",
            }
        ],
        warnings=["选用文案版本会把分镜工作台还原为该文案对应的最近一次构建结果"]
        if body["stage"] == "script"
        else [],
        focus=version_focus(view.job["id"], view.plan_id, body),
    )


def version_focus(job, pid, body) -> dict:
    if body["stage"] == "script":
        return studio_focus(job, pid, section="script")
    if body["stage"] in final_stages():
        return studio_focus(job, pid, section="final")
    return studio_focus(job, pid, shots=[body["shot_id"]], stage=body["stage"])


async def select_version(args: SelectVersion, ctx: ToolContext) -> ToolOutput:
    stored = planned(ctx, "select")
    if stored is None:
        view, body = await select_body(args, ctx)
        stored = (view.job["id"], view.plan_id, body)
    job, pid, body = stored
    data = check(
        await ctx.client.post(f"/api/studio/jobs/{job}/plans/{pid}/select-version", body), "选用版本"
    )

    chosen = data["versions"].get(body["version_id"]) if isinstance(data, dict) else None
    media = version_media(
        chosen,
        f"{STAGE_LABELS[body['stage']]} {short(body['version_id'])}",
        collapsed=body["stage"] != "compose",
    )
    return ToolOutput(
        {"version_id": body["version_id"]},
        text=f"已选用版本 {short(body['version_id'])}。",
        mutation=mutation(job, pid),
        focus=version_focus(job, pid, body),
        media=media,
    )


# ------------------------------------------------------------ 背景音乐歌词
class GenerateLyrics(StrictModel):
    job_id: str | None = JobId
    prompt: str | None = Field(
        default=None, max_length=2000, description="歌曲主题、风格或修改方向；为空时随机生成"
    )
    mode: Literal["write_full_song", "edit"] = Field(
        default="write_full_song", description="write_full_song=写完整歌词；edit=按 prompt 修改/续写 lyrics"
    )
    lyrics: str | None = Field(default=None, max_length=3500, description="edit 模式下的现有歌词")
    title: str | None = Field(default=None, max_length=100, description="歌名；给出时保持不变")


async def preview_generate_lyrics(args: GenerateLyrics, ctx: ToolContext) -> Preview:
    job = ctx.job_id(args.job_id)
    changes = [
        {"label": "方式", "after": "改写已有歌词" if args.mode == "edit" else "写完整歌词"},
        {"label": "主题/指令", "after": clip(args.prompt, 120) or "（随机）"},
    ]
    if args.title:
        changes.append({"label": "歌名", "after": args.title})
    return Preview(
        "调用音乐生成服务的歌词生成（只返回歌词，不修改分镜）",
        changes,
        warnings=["会调用外部音乐生成服务，可能产生少量费用；本地 ComfyUI 音乐档案不支持歌词生成"],
        focus=studio_focus(job),
    )


async def generate_lyrics(args: GenerateLyrics, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    body = args.model_dump(exclude_none=True, exclude={"job_id"})
    data = check(await ctx.client.post(f"/api/studio/jobs/{job}/music/lyrics", body), "生成歌词")
    return ToolOutput(
        data,
        text=f"歌名：{data['title'] or '（无）'}\n风格：{data['style_tags'] or '（无）'}\n{data['lyrics']}\n"
        "（歌词尚未写入分镜：需要时用 edit_shots 把它填入背景音乐提示点的 lyrics，并把 instrumental 设为 "
        "false）",
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "create_job",
            "studio",
            "新建创作任务（视频大纲）。不会自动生成文案或构建。",
            CreateJob,
            "write",
            create_job,
            preview_create_job,
        ),
        ToolSpec(
            "create_plan",
            "studio",
            "在任务里新建方案（可复制已有方案或从成片恢复），并设为当前方案。",
            CreatePlan,
            "write",
            create_plan,
            preview_create_plan,
        ),
        ToolSpec(
            "activate_plan",
            "studio",
            "切换任务的当前方案。",
            ActivatePlan,
            "write",
            activate_plan,
            preview_activate_plan,
        ),
        ToolSpec(
            "update_plan_options",
            "studio",
            "修改方案参数（音色、召回数量、音轨模式、字幕烧录、变速、对齐策略、原声音量、文案模板 "
            "template_id、"
            "画幅 canvas 与适配 fit、字幕样式 subtitle_style、品牌 brand_id/brand_overrides、节拍对齐 "
            "beat_align）"
            "或方案名称。只传要改的字段。",
            UpdatePlanOptions,
            "write",
            update_plan_options,
            preview_update_plan_options,
        ),
        ToolSpec(
            "edit_script",
            "studio",
            "用完整新文案替换方案文案（保存为手动版本）；分镜随之过期，构建时重新拆分镜。",
            EditScript,
            "write",
            edit_script,
            preview_edit_script,
        ),
        ToolSpec(
            "edit_shots",
            "studio",
            (
                "批量修改分镜结构：update/insert/delete/move/merge/split。合并规则与创作页一致（旁白拼接、"
                "查询以「；」连接、时长相加、画面意象以「；」连接并截到 120 字）。只能合并相邻分镜。"
                "未改动分镜保留产物，只有受影响分镜需要重建。split 的各段旁白须由 get_shot_details "
                "返回的完整旁白按顺序切分，各段按顺序拼接（忽略空白）须等于原旁白。update/insert "
                "可设置分镜召回：recall（文字+人物/限定素材/图片或视频召回，整体替换）、granularity、"
                "recall_limit；source 设置画面来源（recall 素材召回 / clip 已上传片段 + 起点 start / image "
                "已上传图片 + motion、strength / aigc_image AI 生成图片 / aigc AI 生成视频（mode 选文生、"
                "首帧、尾帧、首尾帧、主体参考，以及 H3 的 model、resolution、ratio、duration）/ animation "
                "AI 动画）；music 设置背景音乐提示点（从该分镜起生成/翻唱/停止背景音乐，之后的分镜顺延，"
                "默认没有背景音乐）；transition 设置转场（enter/leave 镜内转场fade/white/zoom/slide，next "
                "到下一镜的重叠转场，默认硬切）；update 的 clear 把这些字段恢复为跟随方案（visual 为清空、"
                "source 恢复素材召回、music 去掉提示点、transition 恢复硬切）；highlights "
                "设置字幕高亮词（烧录字幕时变色，字幕样式用 update_plan_options 的 subtitle_style）；"
                "overlays 设置文字层（标题卡 / 信息条 / 数据卡片，engine=ass 烧录或 html 透明动画叠加）。"
                "用户给的图片/视频附件请用 attach_query_media。"
            ),
            EditShots,
            "write",
            edit_shots,
            preview_edit_shots,
        ),
        ToolSpec(
            "rename_job",
            "studio",
            "修改创作任务标题（手动命名后不再自动生成标题）。",
            RenameJob,
            "write",
            rename_job,
            preview_rename_job,
        ),
        ToolSpec(
            "attach_query_media",
            "studio",
            (
                "把图片、视频或音频附件用于某个分镜，只需一次确认。purpose=query（缺省）：上传为查询文件并"
                "把该分镜改为图片/视频召回（保留原素材范围与时长筛选，去掉人物条件）；"
                "purpose=source：上传为画面文件并把该分镜的画面来源改为上传片段（视频）或静态图片（图片），"
                "不再召回素材；分镜是 aigc 时作为首帧/尾帧/主体参考（role），是 aigc_image "
                "时作为主体参考图；role=cover 的音频作为背景音乐翻唱参考。"
            ),
            AttachQueryMedia,
            "write",
            attach_query_media,
            preview_attach_query_media,
        ),
        ToolSpec(
            "add_reference_text",
            "studio",
            "为方案登记一份参考文稿（UTF-8 文本附件 attachment://… 或直接粘贴的正文），供文案创作作为"
            "“仅供参考”的独立段落；访谈改写模板必须至少有一份。不会自动重写已有文案。",
            AddReferenceText,
            "write",
            add_reference_text,
            preview_add_reference_text,
        ),
        ToolSpec(
            "remove_reference_text",
            "studio",
            "移除方案登记的参考文稿（只删登记，文件保留）。",
            RemoveReferenceText,
            "danger",
            remove_reference_text,
            preview_remove_reference_text,
        ),
        ToolSpec(
            "generate_lyrics",
            "studio_detail",
            "调用音乐生成服务的歌词生成（MiniMax "
            "lyrics_generation）写一首歌词或修改/续写已有歌词，只返回结果、不改分镜；"
            "可能产生少量费用。用于带人声的背景音乐：生成后用 edit_shots 写入背景音乐提示点。",
            GenerateLyrics,
            "write",
            generate_lyrics,
            preview_generate_lyrics,
        ),
        ToolSpec(
            "select_version",
            "studio",
            "选用某个历史版本作为当前方案的产物（文案、分镜内容、召回、配音、选片、渲染或成片）。",
            SelectVersion,
            "write",
            select_version,
            preview_select_version,
        ),
    ]
