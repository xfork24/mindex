"""``navigate``：在用户的浏览器里切换页面并定位（只影响界面，不修改数据）。

其他读写工具执行后已经会自动定位到相关位置；只有用户明确要求「打开某页」「带我去看某处」时才需要调用它。
分镜可以用短 ID、完整 ID 或 #序号，解析有歧义时报错并列出候选。依赖浏览器面板执行，不导出到 MCP。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agent.ids import resolve_shot, short
from agent.registry import ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import load_view
from shared.config import StrictModel

PAGES = {
    "groups": "组管理",
    "music": "音乐库",
    "studio": "创作",
    "settings": "设置",
    "search": "视频检索",
    "admin": "素材管理",
    "persons": "人物库",
    "topics": "选题挖掘",
}
STUDIO_SECTIONS = ("jobs", "script", "plan", "storyboard", "log", "final", "artifacts")
SETTINGS_SECTIONS = ("models", "processing", "tuning", "voices", "releases")
TOPICS_SECTIONS = ("profiles", "sources", "runs", "cards", "card", "gaps", "schedule")


class Navigate(StrictModel):
    page: Literal["studio", "settings", "search", "admin", "persons", "topics", "groups", "music"]
    group_id: str | None = Field(default=None, description="组管理页：当前素材组或音乐组 ID")
    job_id: str | None = Field(
        default=None, description="创作页：要打开的任务；省略时用当前页面上下文里的任务"
    )
    plan_id: str | None = None
    shots: list[str] | None = Field(
        default=None, max_length=20, description="创作页：要定位的分镜（短 ID、完整 ID 或 #序号）"
    )
    stage: (
        Literal["content", "recall", "tts", "music", "draw", "generate", "animate", "select", "render"] | None
    ) = Field(default=None, description="创作页：展开分镜的哪个阶段详情")
    section: (
        Literal[
            "jobs",
            "script",
            "plan",
            "storyboard",
            "log",
            "final",
            "artifacts",
            "models",
            "processing",
            "tuning",
            "voices",
            "releases",
            "profiles",
            "sources",
            "runs",
            "cards",
            "card",
            "gaps",
            "schedule",
        ]
        | None
    ) = Field(
        default=None,
        description="创作页区块：jobs/script/plan/storyboard/log/final/artifacts；"
        "设置页区块：models/processing/tuning/voices/releases；"
        "选题挖掘页区块：profiles/sources/runs/cards/card/gaps/schedule",
    )
    role: (
        Literal[
            "analyzer",
            "writer",
            "generator",
            "embedding",
            "asr",
            "tts",
            "image_generator",
            "video_generator",
            "music_generator",
            "animator",
            "person",
            "agent",
        ]
        | None
    ) = Field(default=None, description="设置页：展开的模型角色")
    profile_id: int | None = Field(default=None, description="设置页：高亮的配置档案")
    query: str | None = Field(default=None, max_length=400, description="检索页：填入并执行的检索文字")
    granularity: Literal["shot", "moment"] | None = None
    source_id: str | None = Field(default=None, description="素材管理页：打开的素材")
    segment_id: str | None = Field(default=None, description="素材管理页：定位的镜头或片段")
    pane: str | None = Field(default=None, description="人物库页：切换到的分页，如 cast、processing")
    topic_profile_id: str | None = Field(default=None, description="选题挖掘页：切换到的领域档案")
    card_id: str | None = Field(default=None, description="选题挖掘页：打开的选题卡片")


async def navigate(args: Navigate, ctx: ToolContext) -> ToolOutput:
    focus: dict = {"page": args.page}
    label = PAGES[args.page]
    if args.section:
        allowed = {"studio": STUDIO_SECTIONS, "settings": SETTINGS_SECTIONS, "topics": TOPICS_SECTIONS}.get(
            args.page, ()
        )
        if args.section not in allowed:
            raise ToolError(f"{label}页没有区块 {args.section}", code="invalid_arguments")
        focus["section"] = args.section
    if args.page == "studio":
        job = args.job_id or ctx.page.get("job_id")
        if args.shots or args.plan_id or args.stage:
            view = await load_view(ctx, job, args.plan_id)
            focus.update(job_id=view.job["id"], plan_id=view.plan_id)
            label += f"「{view.job['title']}」"
            if args.shots:
                ids = []
                for ref in args.shots:
                    found = resolve_shot(view.order, ref)
                    if found["status"] == "ambiguous":
                        raise ToolError(
                            f"分镜 {ref} 有歧义，候选："
                            + "、".join(f"#{c['index']} {c['shot_id']}" for c in found["candidates"]),
                            code="ambiguous_shot",
                            value=found,
                        )
                    if found["status"] != "ok":
                        raise ToolError(f"找不到分镜 {ref}", code="missing_shot", value=found)
                    ids.append(found["shot_id"])
                focus["shots"] = ids
                label += " 分镜 " + "、".join(f"#{view.order.index(s) + 1} {short(s)}" for s in ids)
            if args.stage:
                focus["stage"] = args.stage
        elif job:
            focus["job_id"] = job
    elif args.page == "settings":
        if args.role:
            focus["role"] = args.role
        if args.profile_id is not None:
            focus["profile_id"] = args.profile_id
    elif args.page == "search" and args.query:
        focus["query"] = {"text": args.query, "granularity": args.granularity or "moment"}
    elif args.page == "admin":
        if args.segment_id and not args.source_id:
            raise ToolError("定位片段需要同时给出 source_id", code="invalid_arguments")
        focus.update({k: v for k, v in (("source_id", args.source_id), ("segment_id", args.segment_id)) if v})
    elif args.page == "groups" and args.group_id:
        focus["group_id"] = args.group_id
    elif args.page == "persons" and args.pane:
        focus["pane"] = args.pane
    elif args.page == "topics":
        focus.update(
            {k: v for k, v in (("profile_id", args.topic_profile_id), ("card_id", args.card_id)) if v}
        )
    return ToolOutput(
        focus,
        text=f"已在用户页面上打开：{label}" + (f"（{args.section}）" if args.section else ""),
        focus=focus,
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "navigate",
            "meta",
            "在用户浏览器里切换页面并定位（创作任务/分镜/区块、设置角色/档案、检索词、素材、选题档案/卡片）。只改变界面，不修改数据；"
            "其他工具已会自动定位，仅在用户要求打开或查看某处时调用。",
            Navigate,
            "read",
            navigate,
            mcp=False,
        ),
    ]
