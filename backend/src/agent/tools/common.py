"""工具共用：回环结果检查、工作台读取与分镜表格。"""

from __future__ import annotations

from mindex.agent.ids import short
from mindex.agent.registry import ToolContext, ToolError

STAGES = ("recall", "tts", "select", "render")
STAGE_LABELS = {"script": "文案", "split": "拆分镜", "recall": "召回", "tts": "配音", "music": "背景音乐",
                "draw": "生成图片", "generate": "生成画面",
                "animate": "生成动画", "select": "选片", "render": "渲染", "compose": "合成", "content": "分镜内容"}
# 只属于部分来源的逐镜阶段：分镜表在四列之外另起一行显示。
EXTRA_STAGES = {"aigc": "generate", "animation": "animate", "aigc_image": "draw"}


def shot_stage_names(content: dict) -> tuple[str, ...]:
    """分镜表展示的阶段：素材召回四列；aigc 多一个生成画面，aigc_image 多一个生成图片，animation 多一个生成动画；
    背景音乐提示点（生成/翻唱）分镜另有背景音乐阶段。"""
    extra = EXTRA_STAGES.get(((content or {}).get("source") or {}).get("kind"))
    stages = ("recall", "tts", extra, "select", "render") if extra else STAGES
    music = (content or {}).get("music") or {}
    if music.get("kind") in ("generate", "cover"):
        stages = (*stages, "music")
    return stages


def source_line(source: dict) -> str:
    """分镜表里画面来源的一行摘要（不截断提示词以外的字段）。"""
    if source["kind"] == "aigc":
        options = "、".join(f"{k}={v}" for k, v in source.items() if k not in ("kind", "prompt") and v is not None)
        prompt = clip(source.get("prompt"), 40) if source.get("prompt") else "（缺省用画面意象/查询）"
        return f"AI 生成视频 提示词 {prompt}" + (f"（{options}）" if options else "")
    if source["kind"] == "aigc_image":
        options = "、".join(f"{k}={v}" for k, v in source.items() if k not in ("kind", "prompt") and v is not None)
        prompt = clip(source.get("prompt"), 40) if source.get("prompt") else "（缺省用画面意象/查询）"
        return f"AI 生成图片 提示词 {prompt}" + (f"（{options}）" if options else "")
    if source["kind"] == "animation":
        return "AI 动画 画面说明 " + (clip(source.get("brief"), 40) if source.get("brief") else "（缺省用画面意象/旁白）")
    options = "、".join(f"{k}={v}" for k, v in source.items() if k not in ("kind", "media_id"))
    return f"{'上传片段' if source['kind'] == 'clip' else '静态图片'} {source['media_id'][:8]}（{options}）"


def check(result: dict, what: str = "请求") -> object:
    if result["ok"]:
        return result["data"]
    status = result["status"]
    hint = ""
    if status == 409:
        hint = "（资源忙或数据已变化：稍后重试，或先重新读取工作台再操作）"
    elif status == 422 and "revision" in str(result.get("error", "")).lower():
        hint = "（方案已被修改：请重新读取工作台后重试）"
    raise ToolError(f"{what}失败（HTTP {status}）：{result['error']}{hint}", code=f"http_{status}",
                    value={"status": status, "error": result["error"]})


def clip(text: str | None, limit: int = 60) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


class PlanView:
    def __init__(self, workspace: dict, plan_id: str | None = None):
        self.w = workspace
        pid = plan_id or workspace["active_plan"]
        if pid not in workspace["plans"]:
            names = ", ".join(f"{p['name']}({k})" for k, p in workspace["plans"].items())
            raise ToolError(f"方案不存在：{pid}；可用方案：{names}", code="missing_plan")
        self.plan = workspace["plans"][pid]
        self.plan_id = pid
        self.job = workspace["job"]

    @property
    def active(self) -> bool:
        return self.plan_id == self.w["active_plan"]

    @property
    def revision(self) -> str:
        if self.active and self.w.get("revision"):
            return self.w["revision"]
        from mindex.studio.workspace import revision

        return revision(self.plan)

    @property
    def order(self) -> list[str]:
        board = self.plan.get("board")
        return list(self.w["versions"][board]["data"]["order"]) if board else []

    def version(self, vid):
        return self.w["versions"].get(vid) if vid else None

    def content(self, sid) -> dict:
        return self.version(self.plan["shots"][sid]["content"])["data"]

    def shots(self) -> list[dict]:
        """当前方案分镜内容（ShotBody 形状），用于整份提交 PATCH。"""
        return [dict(self.content(sid)) for sid in self.order]

    def script(self) -> str | None:
        item = self.version(self.plan.get("script"))
        return item["data"]["text"] if item else None

    def stage_state(self, sid, stage) -> dict:
        vid = self.plan["shots"].get(sid, {}).get(stage)
        item = self.version(vid)
        conflicts = (self.w.get("conflicts") or {}).get(f"{stage}:{sid}") or [] if self.active else []
        if not item:
            return {"status": "missing"}
        state = {"status": item["status"], "version": short(vid)}
        if conflicts:
            state["stale"] = conflicts
        return state

    def shot_rows(self) -> list[dict]:
        rows = []
        for index, sid in enumerate(self.order, 1):
            content = self.content(sid)
            row = {
                "index": index,
                "short_id": short(sid),
                "shot_id": sid,
                "visual": content.get("visual") or None,
                "text": content["text"],
                "query": content["query"],
                "estimated_seconds": content["estimated_seconds"],
                "stages": {stage: self.stage_state(sid, stage) for stage in shot_stage_names(content)},
            }
            if content.get("recall"):
                row["recall"] = content["recall"]
            if content.get("source"):
                row["source"] = content["source"]
            if content.get("music"):
                row["music"] = content["music"]
            if sid in self.plan.get("ranges", {}):
                row["manual_range"] = self.plan["ranges"][sid]
            rows.append(row)
        return rows


async def load_view(ctx: ToolContext, job_id: str | None, plan_id: str | None = None) -> PlanView:
    job = ctx.job_id(job_id)
    data = check(await ctx.client.get(f"/api/studio/jobs/{job}/workspace", compact="true"), "读取工作台")
    return PlanView(data, plan_id or (ctx.page.get("plan_id") if ctx.page.get("job_id") == job else None))


def stage_mark(state: dict) -> str:
    if state["status"] == "missing":
        return "—"
    mark = {"complete": "✓", "failed": "✗"}.get(state["status"], state["status"])
    return mark + ("(过期)" if state.get("stale") else "")


def board_text(view: PlanView, rows: list[dict] | None = None, limit: int = 60) -> str:
    rows = view.shot_rows() if rows is None else rows
    if not rows:
        return "（当前方案还没有分镜）"
    lines = ["# | 短ID | 完整ID | 旁白 | 查询 | 秒 | 召回 配音 选片 渲染"]
    for row in rows:
        marks = " ".join(stage_mark(row["stages"][s]) for s in STAGES)
        lines.append(
            f"{row['index']} | {row['short_id']} | {row['shot_id']} | {clip(row['text'], limit)} | "
            f"{clip(row['query'], 30)} | {row['estimated_seconds']:g} | {marks}"
        )
        if row.get("visual"):
            lines.append(f"  画面意象：{clip(row['visual'], limit)}")
        if row.get("source"):
            lines.append(f"  画面来源：{source_line(row['source'])}")
            if "generate" in row["stages"]:
                lines.append(f"  生成画面：{stage_mark(row['stages']['generate'])}")
            if "animate" in row["stages"]:
                lines.append(f"  生成动画：{stage_mark(row['stages']['animate'])}")
            if "draw" in row["stages"]:
                lines.append(f"  生成图片：{stage_mark(row['stages']['draw'])}")
        if row.get("music"):
            music = row["music"]
            label = {"generate": "生成", "cover": "翻唱", "none": "停止"}[music["kind"]]
            text = f"  背景音乐提示点：{label}"
            if music.get("prompt"):
                text += f" 描述 {clip(music['prompt'], 40)}"
            if "music" in row["stages"]:
                text += f"（{stage_mark(row['stages']['music'])}）"
            lines.append(text + "；之后的分镜顺延")
    lines.append(f"（旁白与画面意象截断到 {limit} 字、查询截断到 30 字；完整内容请用 get_shot_details 读取）")
    return "\n".join(lines)
