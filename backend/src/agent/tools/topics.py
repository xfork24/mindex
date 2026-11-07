"""选题挖掘工具（领域 ``topics``，``/api/topics``）：领域档案、数据源、挖掘、选题卡片、建任务、档案同步、
素材采集清单与定时状态。全部经回环调用 REST，不复制业务逻辑；MCP 同名导出。

- 页面为创作页子页面 ``/studio/topics``；``focus`` 用 ``{"page": "topics", "profile_id", "card_id",
"section"}``，
  建出创作任务后定位到创作页。
- 改动事件为 ``{"domain": "topics", "profile_id": ...}``，选题页据此刷新。
- 删除档案或数据源只删记录（metadata 库），按删除策略为 danger。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools import media as tool_media
from agent.tools.common import check, clip
from shared.config import StrictModel
from topics.common import ID_PATTERN
from topics.models import CoverageSettings, Schedule, Weights

# 参数名带 topic_ 前缀，避免与设置页模型档案的 profile_id、素材的 source_id 混淆。
ProfileId = Field(pattern=ID_PATTERN, description="领域档案 ID（tp_ 开头，list_topic_profiles 列出）")
PROFILE_LABELS = {
    "source_ids": "素材范围",
    "source_group_ids": "素材组",
    "name": "名称",
    "domain": "领域",
    "subtopics": "子方向",
    "audience": "目标受众",
    "positioning": "账号定位",
    "taboos": "禁区",
    "keywords": "关键词",
    "language": "搜索语言",
    "region": "搜索地区",
    "canvas": "默认画幅",
    "weights": "打分权重",
    "schedule": "每日定时",
    "coverage": "素材覆盖核验",
}


def focus(profile_id: str | None = None, section: str = "profiles", **extra) -> dict:
    value = {"page": "topics", "section": section}
    if profile_id:
        value["profile_id"] = profile_id
    return {**value, **{k: v for k, v in extra.items() if v}}


def mutation(profile_id: str | None) -> dict:
    return {"domain": "topics", "profile_id": profile_id}


def shown(value) -> str:
    if isinstance(value, list):
        return "、".join(str(v) for v in value) if value else "（空）"
    if isinstance(value, dict):
        return "，".join(f"{k}={v}" for k, v in value.items())
    return clip(str(value), 120) if value not in (None, "") else "（空）"


def profile_line(item: dict) -> str:
    schedule = item.get("schedule") or {}
    timer = (
        f"每天 {schedule.get('time')} 前 {schedule.get('count')} 名写大纲"
        if schedule.get("enabled")
        else "未开启定时"
    )
    subtopics = "、".join(item.get("subtopics") or []) or "—"
    sources = "、".join(item.get("source_ids") or []) or "不限"
    groups = "、".join(item.get("source_group_ids") or []) or "不限"
    return (
        f"{item['id']} | {item['name']} | {item['domain']}（{subtopics}）| {timer} | 素材：{sources} | "
        f"素材组：{groups}"
    )


async def fetch_profile(ctx: ToolContext, profile_id: str) -> dict:
    return check(await ctx.client.get(f"/api/topics/profiles/{profile_id}"), "读取领域档案")


# ---------------------------------------------------------------- 领域档案


class Empty(StrictModel):
    pass


async def list_topic_profiles(args: Empty, ctx: ToolContext) -> ToolOutput:
    items = check(await ctx.client.get("/api/topics/profiles"), "列出领域档案")["items"]
    if not items:
        text = "还没有领域档案；用 save_topic_profile 新建（name 与 domain 必填）。"
    else:
        text = f"共 {len(items)} 份领域档案（ID | 名称 | 领域（子方向）| 定时）：\n" + "\n".join(
            map(profile_line, items)
        )
    return ToolOutput({"items": items}, text=text, focus=focus(ctx.page.get("profile_id")))


class SaveTopicProfile(StrictModel):
    topic_profile_id: str | None = Field(
        default=None,
        pattern=ID_PATTERN,
        description="要修改的领域档案 ID；省略表示新建（此时 name 与 domain 必填）",
    )
    name: str | None = Field(default=None, min_length=1, max_length=40, description="档案名称")
    domain: str | None = Field(
        default=None, min_length=1, max_length=40, description="领域，如 金融、科技、历史"
    )
    subtopics: list[str] | None = Field(default=None, max_length=20, description="子方向（整体替换）")
    audience: str | None = Field(default=None, max_length=500, description="目标受众")
    positioning: str | None = Field(default=None, max_length=500, description="账号定位与表达风格")
    taboos: list[str] | None = Field(
        default=None,
        max_length=30,
        description="禁区（整体替换）；也会作为写作约束进入从选题创建的任务的文案提示词",
    )
    keywords: list[str] | None = Field(
        default=None, max_length=30, description="检索与搜索关键词（整体替换）"
    )
    language: str | None = Field(
        default=None,
        min_length=2,
        max_length=16,
        description="旧档案兼容字段；默认全球搜索不采用此限制，定向搜索可配置数据源 search_lang",
    )
    region: str | None = Field(
        default=None,
        min_length=2,
        max_length=8,
        description="旧档案兼容字段；默认全球搜索不采用此限制，定向搜索可配置数据源 country",
    )
    source_ids: list[str] | None = Field(
        default=None, max_length=200, description="素材库 ID 多选；空列表不限，与素材组并集取交集"
    )
    source_group_ids: list[str] | None = Field(
        default=None, max_length=200, description="素材组 ID 多选取并集；与素材范围取交集，空组无候选"
    )
    canvas: Literal["16:9", "9:16", "1:1"] | None = Field(
        default=None, description="从选题建任务时的默认画幅"
    )
    weights: Weights | None = Field(default=None, description="五项打分权重，和为 1（需整体给出）")
    coverage: CoverageSettings | None = Field(
        default=None, description="素材候选粗筛阈值、数量与最小时长；需整体给出，空字段继承全局配置"
    )
    schedule: Schedule | None = Field(
        default=None, description="每日定时：enabled、time（HH:MM）、count（前 N 名写大纲）"
    )

    def changes(self) -> dict:
        data = self.model_dump(exclude_unset=True, exclude={"topic_profile_id"})
        return {k: v for k, v in data.items() if v is not None}

    @model_validator(mode="after")
    def required(self):
        if not self.topic_profile_id and not (self.name and self.domain):
            raise ValueError("新建档案需要 name 与 domain")
        if self.topic_profile_id and not self.changes():
            raise ValueError("至少修改一个字段")
        return self


async def preview_save_topic_profile(args: SaveTopicProfile, ctx: ToolContext) -> Preview:
    changes = args.changes()
    if not args.topic_profile_id:
        rows = [{"label": PROFILE_LABELS[k], "after": shown(v)} for k, v in changes.items()]
        return Preview("新建领域档案", rows, focus=focus())
    item = await fetch_profile(ctx, args.topic_profile_id)
    rows = [
        {"label": PROFILE_LABELS[k], "before": shown(item.get(k)), "after": shown(v)}
        for k, v in changes.items()
        if item.get(k) != v
    ]
    if not rows:
        raise ToolError("给出的字段与档案当前内容相同，没有需要修改的内容", code="no_changes")
    warnings = []
    if {"domain", "subtopics", "audience", "positioning", "taboos"} & set(changes):
        warnings.append(
            "从该档案创建的创作任务会提示「档案已更新」，需要时用 sync_job_profile 同步；不会自动重写文案"
        )
    return Preview(
        f"修改领域档案「{item['name']}」", rows, warnings=warnings, focus=focus(args.topic_profile_id)
    )


async def save_topic_profile(args: SaveTopicProfile, ctx: ToolContext) -> ToolOutput:
    if args.topic_profile_id:
        item = check(
            await ctx.client.patch(f"/api/topics/profiles/{args.topic_profile_id}", args.changes()),
            "修改领域档案",
        )
        verb = "已修改"
    else:
        item = check(await ctx.client.post("/api/topics/profiles", args.changes()), "新建领域档案")
        verb = "已新建"
    return ToolOutput(
        item,
        text=f"{verb}领域档案：{profile_line(item)}",
        mutation=mutation(item["id"]),
        focus=focus(item["id"]),
    )


class DeleteTopicProfile(StrictModel):
    topic_profile_id: str = ProfileId


async def preview_delete_topic_profile(args: DeleteTopicProfile, ctx: ToolContext) -> Preview:
    item = await fetch_profile(ctx, args.topic_profile_id)
    return Preview(
        "删除领域档案（只删除记录）",
        [{"label": "领域档案", "before": profile_line(item), "after": "（删除）"}],
        warnings=[
            "同时删除该档案的数据源、候选条目、选题卡片、素材采集清单与挖掘记录；已建的创作任务不受影响",
            "有挖掘任务在运行时服务端会拒绝删除",
        ],
        focus=focus(args.topic_profile_id),
    )


async def delete_topic_profile(args: DeleteTopicProfile, ctx: ToolContext) -> ToolOutput:
    result = check(await ctx.client.delete(f"/api/topics/profiles/{args.topic_profile_id}"), "删除领域档案")
    return ToolOutput(
        result,
        text=f"已删除领域档案 {args.topic_profile_id}。",
        mutation=mutation(args.topic_profile_id),
        focus=focus(),
    )


# ---------------------------------------------------------------- 数据源与抓取

SourceId = Field(pattern=ID_PATTERN, description="数据源 ID（ts_ 开头，list_topic_sources 列出）")
KIND_TEXT = (
    "kind 与 config：rss {url}；hotlist {url, items_path, fields:{title,desc,url,hot,time}} 或 "
    "DailyHotApi 预设 {preset:'dailyhot', base_url, boards:[...]}；brave {endpoint:'news'|'web', "
    "freshness:'pd'|'pw'|'pm', queries:[...]}（需要 secret=API Key）；calendar {url, days} 或用 "
    "import_topic_calendar 导入；local {library:true}（本地素材库，无网络请求）"
)


def source_line(item: dict) -> str:
    state = "启用" if item["enabled"] else "停用"
    last = (
        f"最近抓取 {item['last_fetch_at']}，{item['last_count']} 条"
        if item.get("last_fetch_at")
        else "尚未抓取"
    )
    error = f"；最近错误：{item['last_error']}" if item.get("last_error") else ""
    key = f"；密钥 {item['secret_mask']}" if item.get("has_secret") else ""
    return f"{item['id']} | {item['name']} | {item['kind_label']} | {state} | {last}{error}{key}"


class ListTopicSources(StrictModel):
    topic_profile_id: str = ProfileId


async def list_topic_sources(args: ListTopicSources, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get(f"/api/topics/profiles/{args.topic_profile_id}/sources"), "列出数据源")
    items = data["items"]
    kinds = "、".join(f"{k['kind']}（{k['label']}）" for k in data.get("kinds", []))
    text = (
        f"共 {len(items)} 个数据源（ID | 名称 | 类型 | 状态 | 最近抓取）：\n"
        + "\n".join(map(source_line, items))
        if items
        else "该档案还没有数据源；用 save_topic_source 添加。"
    ) + f"\n可用类型：{kinds}。"
    return ToolOutput(data, text=text, focus=focus(args.topic_profile_id, "sources"))


class SaveTopicSource(StrictModel):
    topic_source_id: str | None = Field(
        default=None, pattern=ID_PATTERN, description="要修改的数据源 ID；省略表示在 topic_profile_id 下新建"
    )
    topic_profile_id: str | None = Field(default=None, pattern=ID_PATTERN, description="新建时所属的领域档案")
    kind: Literal["rss", "hotlist", "brave", "calendar", "local"] | None = Field(
        default=None, description="新建时必填；修改时不能改类型"
    )
    name: str | None = Field(default=None, min_length=1, max_length=40, description="数据源名称")
    enabled: bool | None = Field(default=None, description="启用或停用")
    config: dict | None = Field(default=None, description="来源配置（整体给出）；" + KIND_TEXT)
    secret: str | None = Field(
        default=None,
        max_length=500,
        description="API Key 等密钥（只写不回显）；用安全表单收集的 secret:// 引用；空串表示清除",
    )

    @model_validator(mode="after")
    def required(self):
        if self.topic_source_id:
            if self.kind is not None:
                raise ValueError("修改数据源时不能改类型；需要换类型请新建")
            if all(v is None for v in (self.name, self.enabled, self.config, self.secret)):
                raise ValueError("至少修改一项：name、enabled、config 或 secret")
        elif not (self.topic_profile_id and self.kind and self.name):
            raise ValueError("新建数据源需要 topic_profile_id、kind 与 name")
        return self


def third_party_warning(kind: str | None) -> list[str]:
    if kind in ("hotlist", "brave", "calendar", "rss"):
        return ["抓取时会请求你配置的第三方地址；Brave 搜索会把档案关键词发给 Brave"]
    return []


async def preview_save_topic_source(args: SaveTopicSource, ctx: ToolContext) -> Preview:
    shown_config = shown(args.config) if args.config is not None else None
    if not args.topic_source_id:
        rows = [{"label": "类型", "after": args.kind}, {"label": "名称", "after": args.name}]
        if shown_config:
            rows.append({"label": "配置", "after": shown_config})
        if args.secret:
            rows.append({"label": "密钥", "after": "（已提供，保存后只显示掩码）"})
        return Preview(
            "新建数据源",
            rows,
            warnings=third_party_warning(args.kind),
            focus=focus(args.topic_profile_id, "sources"),
        )
    item = check(await ctx.client.get(f"/api/topics/sources/{args.topic_source_id}"), "读取数据源")
    rows = []
    if args.name is not None and args.name != item["name"]:
        rows.append({"label": "名称", "before": item["name"], "after": args.name})
    if args.enabled is not None and args.enabled != item["enabled"]:
        rows.append(
            {
                "label": "状态",
                "before": "启用" if item["enabled"] else "停用",
                "after": "启用" if args.enabled else "停用",
            }
        )
    if shown_config and args.config != item["config"]:
        rows.append({"label": "配置", "before": shown(item["config"]), "after": shown_config})
    if args.secret is not None:
        rows.append(
            {
                "label": "密钥",
                "before": item.get("secret_mask") or "（无）",
                "after": "（清除）" if args.secret == "" else "（替换，保存后只显示掩码）",
            }
        )
    if not rows:
        raise ToolError("给出的内容与数据源当前配置相同，没有需要修改的内容", code="no_changes")
    return Preview(
        f"修改数据源「{item['name']}」（{item['kind_label']}）",
        rows,
        warnings=third_party_warning(item["kind"]) if args.config is not None else [],
        focus=focus(item["profile_id"], "sources"),
    )


async def save_topic_source(args: SaveTopicSource, ctx: ToolContext) -> ToolOutput:
    if args.topic_source_id:
        body = {
            k: v
            for k, v in (
                ("name", args.name),
                ("enabled", args.enabled),
                ("config", args.config),
                ("secret", args.secret),
            )
            if v is not None
        }
        item = check(
            await ctx.client.patch(f"/api/topics/sources/{args.topic_source_id}", body), "修改数据源"
        )
        verb = "已修改"
    else:
        body = {"kind": args.kind, "name": args.name, "config": args.config or {}}
        if args.enabled is not None:
            body["enabled"] = args.enabled
        if args.secret:
            body["secret"] = args.secret
        item = check(
            await ctx.client.post(f"/api/topics/profiles/{args.topic_profile_id}/sources", body), "新建数据源"
        )
        verb = "已新建"
    return ToolOutput(
        item,
        text=f"{verb}数据源：{source_line(item)}\n可用 test_topic_source 试抓。",
        mutation=mutation(item["profile_id"]),
        focus=focus(item["profile_id"], "sources"),
    )


class DeleteTopicSource(StrictModel):
    topic_source_id: str = SourceId


async def preview_delete_topic_source(args: DeleteTopicSource, ctx: ToolContext) -> Preview:
    item = check(await ctx.client.get(f"/api/topics/sources/{args.topic_source_id}"), "读取数据源")
    return Preview(
        "删除数据源（只删除记录；已抓到的候选条目保留，随过期清理）",
        [{"label": "数据源", "before": source_line(item), "after": "（删除）"}],
        focus=focus(item["profile_id"], "sources"),
    )


async def delete_topic_source(args: DeleteTopicSource, ctx: ToolContext) -> ToolOutput:
    item = check(await ctx.client.get(f"/api/topics/sources/{args.topic_source_id}"), "读取数据源")
    result = check(await ctx.client.delete(f"/api/topics/sources/{args.topic_source_id}"), "删除数据源")
    return ToolOutput(
        result,
        text=f"已删除数据源「{item['name']}」。",
        mutation=mutation(item["profile_id"]),
        focus=focus(item["profile_id"], "sources"),
    )


class TestTopicSource(StrictModel):
    topic_source_id: str = SourceId


def item_line(item: dict) -> str:
    when = item.get("event_at") or item.get("published_at") or ""
    hot = f" 热度 {item['hot']:g}" if item.get("hot") is not None else ""
    link = f" {item['url']}" if item.get("url") else ""
    return f"- {item['title']}（{when}{hot}）{link}" + (
        f"\n  {clip(item['summary'], 120)}" if item.get("summary") else ""
    )


async def test_topic_source(args: TestTopicSource, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.post(f"/api/topics/sources/{args.topic_source_id}/test"), "试抓数据源")
    if not data["ok"]:
        text = f"试抓失败：{data['error']}"
    else:
        text = f"试抓成功，共 {data['count']} 条（未入库），前 {len(data['items'])} 条：\n" + "\n".join(
            map(item_line, data["items"])
        )
    return ToolOutput(data, text=text, focus=focus(ctx.page.get("profile_id"), "sources"))


class FetchTopicItems(StrictModel):
    topic_profile_id: str = ProfileId
    topic_source_ids: list[str] | None = Field(
        default=None, max_length=50, description="只抓这些数据源；省略抓全部启用的来源"
    )


async def preview_fetch_topic_items(args: FetchTopicItems, ctx: ToolContext) -> Preview:
    item = await fetch_profile(ctx, args.topic_profile_id)
    scope = "、".join(args.topic_source_ids) if args.topic_source_ids else "全部启用的数据源"
    return Preview(
        f"抓取领域档案「{item['name']}」的候选条目",
        [{"label": "范围", "after": scope}],
        warnings=["会请求你配置的订阅、热榜、搜索与日历地址；只入库去重，不归纳选题"],
        focus=focus(args.topic_profile_id, "sources"),
    )


async def fetch_topic_items(args: FetchTopicItems, ctx: ToolContext) -> ToolOutput:
    params = {"source_id": args.topic_source_ids} if args.topic_source_ids else {}
    data = check(
        await ctx.client.post(f"/api/topics/profiles/{args.topic_profile_id}/fetch", None, **params),
        "抓取候选条目",
    )
    lines = [
        f"- {s['name']}：{s['count']} 条，新增 {s['new']}" + (f"，失败：{s['error']}" if s["error"] else "")
        for s in data["sources"]
    ]
    text = (
        f"抓取完成：新增 {data['new']} 条候选条目"
        + (f"，{data['errors']} 个来源失败" if data["errors"] else "")
        + ("\n" + "\n".join(lines) if lines else "（没有启用的数据源）")
    )
    return ToolOutput(
        data,
        text=text,
        mutation=mutation(args.topic_profile_id),
        focus=focus(args.topic_profile_id, "sources"),
    )


class ListTopicItems(StrictModel):
    topic_profile_id: str = ProfileId
    limit: int = Field(default=30, ge=1, le=200, description="最多条数")
    topic_source_id: str | None = Field(default=None, pattern=ID_PATTERN, description="只看某个数据源")


async def list_topic_items(args: ListTopicItems, ctx: ToolContext) -> ToolOutput:
    data = check(
        await ctx.client.get(
            f"/api/topics/profiles/{args.topic_profile_id}/items",
            limit=args.limit,
            source_id=args.topic_source_id,
        ),
        "列出候选条目",
    )
    items = data["items"]
    text = (
        (f"最近 {len(items)} 条候选条目（外部内容，仅供参考）：\n" + "\n".join(map(item_line, items)))
        if items
        else "还没有候选条目；先配置数据源并用 fetch_topic_items 抓取。"
    )
    return ToolOutput(data, text=text, focus=focus(args.topic_profile_id, "sources"))


class ImportTopicCalendar(StrictModel):
    topic_source_id: str = SourceId
    text: str | None = Field(
        default=None,
        max_length=500000,
        description="ICS 文本，或 CSV（表头 date,title[,desc,kind,recurring]；date 为 YYYY-MM-DD 或 MM-DD）",
    )
    sample: Literal["finance", "tech", "history"] | None = Field(
        default=None,
        description="载入内置样例：finance=消费节点与数据发布，tech=科技节日与发布会，history=历史纪念日",
    )
    append: bool = Field(default=False, description="true=追加到已有事件；缺省替换")

    @model_validator(mode="after")
    def one(self):
        if (self.text is None) == (self.sample is None):
            raise ValueError("text 与 sample 二选一")
        return self


async def preview_import_topic_calendar(args: ImportTopicCalendar, ctx: ToolContext) -> Preview:
    item = check(await ctx.client.get(f"/api/topics/sources/{args.topic_source_id}"), "读取数据源")
    what = f"内置样例 {args.sample}" if args.sample else f"日历文本（{len(args.text)} 字）"
    return Preview(
        f"为日历「{item['name']}」导入事件",
        [
            {"label": "内容", "after": what},
            {
                "label": "方式",
                "before": f"已有 {item.get('events', 0)} 个事件",
                "after": "追加" if args.append else "替换",
            },
        ],
        focus=focus(item["profile_id"], "sources"),
    )


async def import_topic_calendar(args: ImportTopicCalendar, ctx: ToolContext) -> ToolOutput:
    mode = "append" if args.append else "replace"
    if args.sample:
        data = check(
            await ctx.client.post(
                f"/api/topics/sources/{args.topic_source_id}/calendar-sample/{args.sample}", None, mode=mode
            ),
            "导入日历样例",
        )
    else:
        data = check(
            await ctx.client.post(
                f"/api/topics/sources/{args.topic_source_id}/calendar", {"text": args.text, "mode": mode}
            ),
            "导入日历",
        )
    errors = data.get("errors") or []
    text = (
        f"已导入 {data['imported']} 个日历事件（现有 {data['total']} 个）"
        + ("；跳过：" + "；".join(errors[:5]) if errors else "")
        + "。挖掘时只取今天起的近期事件。"
    )
    item = check(await ctx.client.get(f"/api/topics/sources/{args.topic_source_id}"), "读取数据源")
    return ToolOutput(
        data, text=text, mutation=mutation(item["profile_id"]), focus=focus(item["profile_id"], "sources")
    )


# ---------------------------------------------------------------- 挖掘任务

CardId = Field(pattern=ID_PATTERN, description="选题卡片 ID（tc_ 开头，list_topics 列出）")
RunId = Field(pattern=r"^[a-f0-9]{32}$", description="挖掘任务 ID")
STAGES = {
    "fetch": "抓取",
    "embed": "向量",
    "cluster": "聚类",
    "summarize": "归纳",
    "score": "打分",
    "outline": "大纲",
    "done": "完成",
}
SCORE_LABELS = {
    "timeliness": "时效",
    "audience": "受众",
    "coverage": "覆盖",
    "novelty": "去重",
    "cost": "成本",
}


def run_line(run: dict) -> str:
    progress = run.get("progress") or {}
    stage = STAGES.get(progress.get("stage"), progress.get("stage") or "")
    result = run.get("result") or {}
    tail = (
        f"，产出 {result['cards']} 张卡片、{result['outlined']} 张大纲"
        if result.get("cards") is not None
        else ""
    )
    error = f"，错误：{run['error']}" if run.get("error") else ""
    return (
        f"{run['id']} | {run['status']} | {run.get('trigger')} | {stage} "
        f"{progress.get('message') or ''}{tail}{error}"
    )


class DiscoverTopics(StrictModel):
    topic_profile_id: str = ProfileId
    skip_fetch: bool = Field(default=False, description="true=不重新抓取，只用已有的候选条目")
    outline_count: int | None = Field(
        default=None, ge=0, le=50, description="为前 N 名写大纲；缺省取档案定时设置的条数"
    )


async def preview_discover_topics(args: DiscoverTopics, ctx: ToolContext) -> Preview:
    item = await fetch_profile(ctx, args.topic_profile_id)
    count = item["schedule"]["count"] if args.outline_count is None else args.outline_count
    rows = [
        {"label": "领域档案", "after": f"{item['name']}（{item['domain']}）"},
        {"label": "抓取", "after": "跳过，只用已有条目" if args.skip_fetch else "先抓取全部启用的数据源"},
        {"label": "写大纲", "after": f"前 {count} 名"},
    ]
    return Preview(
        "开始选题挖掘（后台任务）",
        rows,
        warnings=[
            "会调用嵌入服务与文案模型（消耗模型额度），并请求已配置的第三方数据源；同一时间只运行一个挖掘任务"
        ],
        focus=focus(args.topic_profile_id, "runs"),
    )


async def discover_topics(args: DiscoverTopics, ctx: ToolContext) -> ToolOutput:
    body = {"skip_fetch": args.skip_fetch, "outline_count": args.outline_count}
    run = check(
        await ctx.client.post(f"/api/topics/profiles/{args.topic_profile_id}/runs", body), "开始选题挖掘"
    )
    return ToolOutput(
        run,
        text=f"已开始挖掘任务 {run['id']}；完成后会收到通知，也可用 get_topic_run 查看进度，"
        "完成后用 list_topics 查看选题。",
        mutation=mutation(args.topic_profile_id),
        focus=focus(args.topic_profile_id, "runs"),
        watch_task={"kind": "topics", "task_id": run["id"]},
    )


class GetTopicRun(StrictModel):
    run_id: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{32}$", description="挖掘任务 ID；省略时列出档案最近的任务"
    )
    topic_profile_id: str | None = Field(default=None, pattern=ID_PATTERN, description="省略 run_id 时必填")

    @model_validator(mode="after")
    def target(self):
        if not self.run_id and not self.topic_profile_id:
            raise ValueError("需要 run_id 或 topic_profile_id")
        return self


async def get_topic_run(args: GetTopicRun, ctx: ToolContext) -> ToolOutput:
    if args.run_id:
        run = check(await ctx.client.get(f"/api/topics/runs/{args.run_id}"), "读取挖掘任务")
        warnings = (run.get("result") or {}).get("warnings") or []
        text = run_line(run) + ("\n告警：\n" + "\n".join(f"- {w}" for w in warnings) if warnings else "")
        return ToolOutput(run, text=text, focus=focus(run["profile_id"], "runs"))
    data = check(
        await ctx.client.get(f"/api/topics/profiles/{args.topic_profile_id}/runs", limit=10), "列出挖掘任务"
    )
    items = data["items"]
    text = (
        ("最近的挖掘任务（ID | 状态 | 触发 | 进度）：\n" + "\n".join(map(run_line, items)))
        if items
        else "还没有挖掘任务。"
    )
    return ToolOutput(data, text=text, focus=focus(args.topic_profile_id, "runs"))


class ManageTopicRun(StrictModel):
    run_id: str = RunId
    action: Literal["cancel", "retry"] = Field(
        description="cancel=取消排队中或运行中的任务；retry=重试失败、中断或已取消的任务"
    )


async def preview_manage_topic_run(args: ManageTopicRun, ctx: ToolContext) -> Preview:
    run = check(await ctx.client.get(f"/api/topics/runs/{args.run_id}"), "读取挖掘任务")
    label = "取消" if args.action == "cancel" else "重试"
    return Preview(
        f"{label}挖掘任务",
        [{"label": "任务", "before": run_line(run), "after": label}],
        focus=focus(run["profile_id"], "runs"),
    )


async def manage_topic_run(args: ManageTopicRun, ctx: ToolContext) -> ToolOutput:
    run = check(await ctx.client.post(f"/api/topics/runs/{args.run_id}/{args.action}"), "管理挖掘任务")
    extra = {"watch_task": {"kind": "topics", "task_id": run["id"]}} if args.action == "retry" else {}
    return ToolOutput(
        run,
        text=f"已{'请求取消' if args.action == 'cancel' else '重新开始'}挖掘任务：{run_line(run)}",
        mutation=mutation(run["profile_id"]),
        focus=focus(run["profile_id"], "runs"),
        **extra,
    )


# ---------------------------------------------------------------- 选题卡片


def score_text(scores: dict) -> str:
    return "、".join(
        f"{label} {scores[key]:.2f}" for key, label in SCORE_LABELS.items() if scores.get(key) is not None
    )


def card_line(card: dict) -> str:
    coverage = card.get("coverage") or {}
    cover = f"覆盖 {coverage.get('hits')}/{coverage.get('total')}" if coverage.get("total") else "覆盖 —"
    outline = "有大纲" if card.get("outline") else "无大纲"
    return f"{card['id']} | {card['total']:.2f} | {card['title']} | {cover} | {outline} | {card['status']}"


class ListTopics(StrictModel):
    topic_profile_id: str = ProfileId
    status: Literal["new", "adopted", "dismissed", "all"] = Field(default="new", description="按状态筛选")
    limit: int = Field(default=20, ge=1, le=100, description="最多条数（按总分降序）")


async def list_topics(args: ListTopics, ctx: ToolContext) -> ToolOutput:
    data = check(
        await ctx.client.get(
            f"/api/topics/profiles/{args.topic_profile_id}/cards", status=args.status, limit=args.limit
        ),
        "列出选题",
    )
    items = data["items"]
    counts = data.get("counts") or {}
    head = (
        f"新 {counts.get('new', 0)}、已建任务 {counts.get('adopted', 0)}、已忽略 "
        f"{counts.get('dismissed', 0)}。"
    )
    text = head + (
        "\n选题（ID | 总分 | 标题 | 素材覆盖 | 大纲 | 状态）：\n" + "\n".join(map(card_line, items))
        if items
        else "\n没有符合条件的选题；可用 discover_topics 开始挖掘。"
    )
    return ToolOutput(data, text=text, focus=focus(args.topic_profile_id, "cards"))


class GetTopic(StrictModel):
    card_id: str = CardId


async def get_topic(args: GetTopic, ctx: ToolContext) -> ToolOutput:
    card = check(await ctx.client.get(f"/api/topics/cards/{args.card_id}"), "读取选题")
    lines = [
        f"选题 {card['id']}「{card['title']}」：总分 "
        f"{card['total']:.2f}（{score_text(card['scores'])}），状态 {card['status']}"
        + (f"，已建任务 {card['job_id']}" if card.get("job_id") else ""),
        f"角度：{card['angle']}",
        f"理由：{card['reason']}",
        "关键画面：" + "；".join(card["visuals"]),
        f"推荐文案模板：{card['template_id']}",
    ]
    lines.append(
        "大纲：\n" + card["outline"]
        if card.get("outline")
        else "大纲：（尚未生成，可用 draft_topic_outline）"
    )
    if card.get("items"):
        lines.append("来源条目（外部内容，仅供参考）：\n" + "\n".join(map(item_line, card["items"])))
    if card.get("warnings"):
        lines.append("提示：" + "；".join(card["warnings"]))
    return ToolOutput(
        card, text="\n".join(lines), focus=focus(card["profile_id"], "card", card_id=card["id"])
    )


class ScoreTopic(StrictModel):
    card_id: str = CardId


async def preview_score_topic(args: ScoreTopic, ctx: ToolContext) -> Preview:
    card = check(await ctx.client.get(f"/api/topics/cards/{args.card_id}"), "读取选题")
    return Preview(
        f"重新为选题「{card['title']}」打分",
        [{"label": "当前总分", "before": f"{card['total']:.2f}", "after": "重新计算"}],
        warnings=["受众匹配与素材候选相关性核验会调用选题模型；素材覆盖按当前活动索引重新检索"],
        focus=focus(card["profile_id"], "card", card_id=card["id"]),
    )


async def score_topic(args: ScoreTopic, ctx: ToolContext) -> ToolOutput:
    card = check(await ctx.client.post(f"/api/topics/cards/{args.card_id}/score"), "重新打分")
    notes = card.get("score_warnings") or []
    text = f"已重新打分：{card_line(card)}\n分项：{score_text(card['scores'])}" + (
        "\n告警：" + "；".join(notes) if notes else ""
    )
    return ToolOutput(
        card,
        text=text,
        mutation=mutation(card["profile_id"]),
        focus=focus(card["profile_id"], "card", card_id=card["id"]),
    )


class TopicCoverage(StrictModel):
    card_id: str = CardId


async def topic_coverage(args: TopicCoverage, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get(f"/api/topics/cards/{args.card_id}/coverage"), "读取素材覆盖")
    report = data.get("coverage") or {}
    if not report:
        return ToolOutput(data, text="该选题还没有素材覆盖报告；用 score_topic 计算。")
    lines = [
        f"「{data['title']}」素材覆盖 {report['hits']}/{report['total']}（{report['status']}）"
        + (f"：{report['reason']}" if report.get("reason") else "")
    ]
    for row in report.get("visuals") or []:
        if row["hit"]:
            clip_line = "；".join(
                f"{c['segment_id']}（{c.get('source_name') or c['source_id']} "
                f"{c['start']:.1f}–{c['end']:.1f}s，"
                f"相似度 {c['similarity'] if c['similarity'] is not None else '—'}）"
                for c in row["clips"][:3]
            )
            lines.append(f"- 命中：{row['visual']} → {clip_line}")
        else:
            lines.append(f"- 缺口或待核验：{row['visual']}")
        for candidate in row.get("candidates", []):
            decision = candidate.get("verification") or {}
            lines.append(
                f"  候选 {candidate['segment_id']}：{decision.get('verdict', 'uncertain')}；"
                f"{decision.get('reason', '')}"
                + (f"；依据：{decision['quote']}" if decision.get("quote") else "")
            )

    media = tool_media.clean(
        [
            tool_media.item("image", c["thumbnail_url"], row["visual"])
            for row in report.get("visuals") or []
            if row["hit"]
            for c in row["clips"][:1]
            if c.get("thumbnail_url")
        ]
    )
    return ToolOutput(
        data, text="\n".join(lines), focus=focus(None, "card", card_id=args.card_id), media=media or None
    )


class DraftTopicOutline(StrictModel):
    card_id: str = CardId


async def preview_draft_topic_outline(args: DraftTopicOutline, ctx: ToolContext) -> Preview:
    card = check(await ctx.client.get(f"/api/topics/cards/{args.card_id}"), "读取选题")
    warnings = ["会调用文案模型"]
    if card.get("outline_origin") == "manual":
        warnings.append("会覆盖手工修改过的大纲")
    return Preview(
        f"为选题「{card['title']}」生成大纲与推荐模板",
        [{"label": "大纲", "before": clip(card.get("outline"), 80) or "（无）", "after": "重新生成"}],
        warnings=warnings,
        focus=focus(card["profile_id"], "card", card_id=card["id"]),
    )


async def draft_topic_outline(args: DraftTopicOutline, ctx: ToolContext) -> ToolOutput:
    card = check(await ctx.client.post(f"/api/topics/cards/{args.card_id}/outline"), "生成大纲")
    return ToolOutput(
        card,
        text=f"已生成大纲（推荐模板 {card['template_id']}）：\n{card['outline']}",
        mutation=mutation(card["profile_id"]),
        focus=focus(card["profile_id"], "card", card_id=card["id"]),
    )


class UpdateTopic(StrictModel):
    card_id: str = CardId
    title: str | None = Field(default=None, min_length=1, max_length=80, description="新标题")
    outline: str | None = Field(default=None, max_length=20000, description="新的完整大纲；空串表示清空")
    template_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
        description="推荐文案模板 ID（list_script_templates 列出）",
    )
    status: Literal["new", "dismissed"] | None = Field(default=None, description="dismissed=忽略；new=恢复")

    def changes(self) -> dict:
        return {
            k: getattr(self, k)
            for k in ("title", "outline", "template_id", "status")
            if getattr(self, k) is not None
        }

    @model_validator(mode="after")
    def something(self):
        if not self.changes():
            raise ValueError("至少修改一项：title、outline、template_id 或 status")
        return self


async def preview_update_topic(args: UpdateTopic, ctx: ToolContext) -> Preview:
    card = check(await ctx.client.get(f"/api/topics/cards/{args.card_id}"), "读取选题")
    labels = {"title": "标题", "outline": "大纲", "template_id": "推荐模板", "status": "状态"}
    rows = [
        {
            "label": labels[k],
            "before": clip(str(card.get(k) or "（无）"), 80),
            "after": clip(v, 80) or "（清空）",
        }
        for k, v in args.changes().items()
    ]
    return Preview(
        f"修改选题「{card['title']}」", rows, focus=focus(card["profile_id"], "card", card_id=card["id"])
    )


async def update_topic(args: UpdateTopic, ctx: ToolContext) -> ToolOutput:
    card = check(await ctx.client.patch(f"/api/topics/cards/{args.card_id}", args.changes()), "修改选题")
    return ToolOutput(
        card,
        text=f"已修改选题：{card_line(card)}",
        mutation=mutation(card["profile_id"]),
        focus=focus(card["profile_id"], "card", card_id=card["id"]),
    )


# ---------------------------------------------------------------- 建创作任务与任务档案


class CreateJobFromTopic(StrictModel):
    card_id: str = CardId
    force: bool = Field(default=False, description="选题已建过任务时仍再建一份")


async def preview_create_job_from_topic(args: CreateJobFromTopic, ctx: ToolContext) -> Preview:
    card = check(await ctx.client.get(f"/api/topics/cards/{args.card_id}"), "读取选题")
    profile = await fetch_profile(ctx, card["profile_id"])
    rows = [
        {"label": "选题", "after": card["title"]},
        {
            "label": "大纲",
            "after": clip(card.get("outline"), 120) or "（没有大纲，用标题、角度与关键画面代替）",
        },
        {"label": "文案模板", "after": card["template_id"]},
        {"label": "画幅", "after": profile["canvas"]},
        {"label": "账号档案", "after": f"{profile['name']}（受众、定位与禁区进入文案提示词）"},
    ]
    warnings = []
    if card["status"] == "adopted":
        warnings.append(
            f"该选题已建过任务 {card['job_id']}" + ("；将再建一份" if args.force else "；需要 force=true")
        )
    return Preview(
        "从选题创建创作任务",
        rows,
        warnings=warnings,
        focus=focus(card["profile_id"], "card", card_id=card["id"]),
    )


async def create_job_from_topic(args: CreateJobFromTopic, ctx: ToolContext) -> ToolOutput:
    data = check(
        await ctx.client.post(f"/api/topics/cards/{args.card_id}/job", {"force": args.force}), "从选题建任务"
    )
    job = data["job"]
    return ToolOutput(
        data,
        text=f"已创建创作任务 {job['id']}「{job['title']}」：已选文案模板 {data['card']['template_id']}，"
        "来源资料已登记为参考文稿，账号档案作为写作约束。可用 run_build 或 run_node 生成文案与成片。",
        mutation={"domain": "studio", "job_id": job["id"]},
        focus={"page": "studio", "job_id": job["id"]},
    )


class JobProfileArgs(StrictModel):
    job_id: str | None = Field(default=None, description="创作任务 ID；省略时用当前页面的任务")


def job_profile_text(data: dict) -> str:
    if not data.get("topic"):
        return "该任务不是从选题创建的，没有账号档案快照。"
    lines = [
        f"来自选题「{data['topic'].get('title')}」，档案「{data.get('profile_name')}」，快照同步于 "
        f"{data.get('synced_at')}。"
    ]
    if not data["profile_exists"]:
        lines.append("原领域档案已删除；任务继续使用快照。")
    elif data["outdated"]:
        lines.append("原档案已更新：可用 sync_job_profile 同步（同步后文案会提示可重新生成，不自动重写）。")
    else:
        lines.append("快照与当前档案一致。")
    snapshot = data.get("snapshot") or {}
    if snapshot.get("taboos"):
        lines.append("禁区：" + "；".join(snapshot["taboos"]))
    return "\n".join(lines)


async def get_job_profile(args: JobProfileArgs, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    data = check(await ctx.client.get(f"/api/topics/jobs/{job}/profile"), "读取任务档案")
    return ToolOutput(data, text=job_profile_text(data), focus={"page": "studio", "job_id": job})


async def preview_sync_job_profile(args: JobProfileArgs, ctx: ToolContext) -> Preview:
    job = ctx.job_id(args.job_id)
    data = check(await ctx.client.get(f"/api/topics/jobs/{job}/profile"), "读取任务档案")
    if not data.get("topic"):
        raise ToolError("该任务不是从选题创建的，没有账号档案快照", code="no_profile")
    return Preview(
        "用当前领域档案更新任务的档案快照",
        [
            {
                "label": "档案",
                "before": f"快照 {str(data['hash'])[:10]}",
                "after": f"当前 {str(data['current_hash'])[:10]}",
            }
        ],
        warnings=["已有文案会提示可重新生成，不会自动重写"],
        focus={"page": "studio", "job_id": job},
    )


async def sync_job_profile(args: JobProfileArgs, ctx: ToolContext) -> ToolOutput:
    job = ctx.job_id(args.job_id)
    data = check(await ctx.client.post(f"/api/topics/jobs/{job}/profile/sync"), "同步任务档案")
    return ToolOutput(
        data,
        text="已同步账号档案。\n" + job_profile_text(data),
        mutation={"domain": "studio", "job_id": job},
        focus={"page": "studio", "job_id": job},
    )


# ---------------------------------------------------------------- 素材采集清单


class TopicGaps(StrictModel):
    topic_profile_id: str = ProfileId
    status: Literal["open", "done", "all"] = Field(
        default="open", description="open=待采集；done=已采集；all=全部"
    )


async def topic_gaps(args: TopicGaps, ctx: ToolContext) -> ToolOutput:
    data = check(
        await ctx.client.get(f"/api/topics/profiles/{args.topic_profile_id}/gaps", status=args.status),
        "读取素材采集清单",
    )
    items = data["items"]
    lines = [
        f"- {g['id']} | {g['visual']} | 出现 {g['count']} 次 | "
        f"{'已采集' if g['status'] == 'done' else '待采集'} | "
        + "、".join(c["title"] or c["id"] for c in g["cards"][:3])
        for g in items
    ]
    text = (
        ("素材采集清单（ID | 画面 | 出现次数 | 状态 | 相关选题）：\n" + "\n".join(lines))
        if items
        else "素材采集清单为空。"
    )
    text += f"\n导出 CSV：/api/topics/profiles/{args.topic_profile_id}/gaps.csv"
    return ToolOutput(data, text=text, focus=focus(args.topic_profile_id, "gaps"))


class UpdateTopicGap(StrictModel):
    gap_id: str = Field(pattern=ID_PATTERN, description="缺口 ID（tg_ 开头，topic_gaps 列出）")
    status: Literal["open", "done"] = Field(description="done=已采集；open=重新标记为待采集")


async def preview_update_topic_gap(args: UpdateTopicGap, ctx: ToolContext) -> Preview:
    label = "已采集" if args.status == "done" else "待采集"
    return Preview(
        f"把素材缺口标记为{label}",
        [{"label": "缺口", "before": args.gap_id, "after": label}],
        focus=focus(ctx.page.get("profile_id"), "gaps"),
    )


async def update_topic_gap(args: UpdateTopicGap, ctx: ToolContext) -> ToolOutput:
    gap = check(
        await ctx.client.patch(f"/api/topics/gaps/{args.gap_id}", {"status": args.status}), "更新素材缺口"
    )
    return ToolOutput(
        gap,
        text=f"已标记「{gap['visual']}」为{'已采集' if gap['status'] == 'done' else '待采集'}。",
        mutation=mutation(gap["profile_id"]),
        focus=focus(gap["profile_id"], "gaps"),
    )


# ---------------------------------------------------------------- 定时


async def get_topic_schedule(args: Empty, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get("/api/topics/schedule"), "读取定时状态")
    lines = []
    if not data["scheduler"]:
        lines.append(
            "当前服务没有运行定时调度（topics.scheduler.enabled 为 false，或不是 mindex serve 启动的服务）。"
        )
    for item in data["items"]:
        if not item["enabled"]:
            lines.append(f"- {item['name']}：未开启每日定时")
            continue
        last = item.get("last_run") or {}
        lines.append(
            f"- {item['name']}：每天 {item['time']}，前 {item['count']} 名写大纲；下次 "
            f"{item['next_run'] or '—'}；"
            f"今日运行 {item['today']['runs']} 次、产出 {item['today']['cards']} 张"
            + (f"；最近一次 {last.get('status')}" if last else "")
        )
    lines.append(
        "定时在 mindex serve（含桌面版托盘后台）运行期间生效，启动时补跑当天错过的定时；用 "
        "save_topic_profile 的 "
        "schedule 修改。"
    )
    return ToolOutput(data, text="\n".join(lines), focus=focus(ctx.page.get("profile_id"), "schedule"))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "list_topic_profiles",
            "topics",
            "列出选题挖掘的领域档案：ID、名称、领域与子方向、每日定时。其他选题工具都需要档案 ID。",
            Empty,
            "read",
            list_topic_profiles,
        ),
        ToolSpec(
            "save_topic_profile",
            "topics",
            "新建（省略 topic_profile_id，name 与 domain "
            "必填）或修改领域档案（只传要改的字段，列表整体替换）。"
            "字段：领域、子方向、受众、账号定位、禁区、关键词、搜索语言/地区、默认画幅、打分权重、每日定时。",
            SaveTopicProfile,
            "write",
            save_topic_profile,
            preview_save_topic_profile,
        ),
        ToolSpec(
            "delete_topic_profile",
            "topics",
            "删除领域档案及其数据源、选题卡片、采集清单与挖掘记录（只删记录；已建的创作任务不受影响）。",
            DeleteTopicProfile,
            "danger",
            delete_topic_profile,
            preview_delete_topic_profile,
        ),
        ToolSpec(
            "list_topic_sources",
            "topics",
            "列出领域档案的数据源：类型、启用状态、最近抓取条数与错误、密钥掩码，以及可用的数据源类型。",
            ListTopicSources,
            "read",
            list_topic_sources,
        ),
        ToolSpec(
            "save_topic_source",
            "topics",
            "新建（topic_profile_id + kind + name）或修改数据源（topic_source_id，只传要改的项）。"
            + KIND_TEXT
            + "。只请求用户配置的地址，不内置爬虫。",
            SaveTopicSource,
            "write",
            save_topic_source,
            preview_save_topic_source,
        ),
        ToolSpec(
            "delete_topic_source",
            "topics",
            "删除数据源记录（已抓到的候选条目保留，随过期清理）。",
            DeleteTopicSource,
            "danger",
            delete_topic_source,
            preview_delete_topic_source,
        ),
        ToolSpec(
            "test_topic_source",
            "topics",
            "试抓一个数据源并返回前 20 条，不入库；用于检查地址、字段映射或密钥。",
            TestTopicSource,
            "read",
            test_topic_source,
        ),
        ToolSpec(
            "import_topic_calendar",
            "topics",
            "为日历数据源导入事件：ICS/CSV "
            "文本，或内置样例（finance/tech/history）；每年重复的纪念日会附周年数。",
            ImportTopicCalendar,
            "write",
            import_topic_calendar,
            preview_import_topic_calendar,
        ),
        ToolSpec(
            "fetch_topic_items",
            "topics",
            "立即抓取档案的数据源（或指定来源）并入库去重，返回每个来源的条数与错误；不归纳选题。",
            FetchTopicItems,
            "write",
            fetch_topic_items,
            preview_fetch_topic_items,
        ),
        ToolSpec(
            "list_topic_items",
            "topics",
            "列出档案最近抓到的候选条目（标题、摘要、链接、时间、热度）。",
            ListTopicItems,
            "read",
            list_topic_items,
        ),
        ToolSpec(
            "discover_topics",
            "topics",
            "开始一次选题挖掘（后台任务）：抓取数据源 → 聚类 → 模型归纳选题卡片 → 打分与素材覆盖 → 前 N "
            "名写大纲。"
            "会调用嵌入服务与文案模型；结束时会收到通知。",
            DiscoverTopics,
            "danger",
            discover_topics,
            preview_discover_topics,
        ),
        ToolSpec(
            "get_topic_run",
            "topics",
            "查看挖掘任务的状态、进度、产出与告警；省略 run_id 时列出档案最近的任务。",
            GetTopicRun,
            "read",
            get_topic_run,
        ),
        ToolSpec(
            "manage_topic_run",
            "topics",
            "取消排队中或运行中的挖掘任务，或重试失败、中断、已取消的任务。",
            ManageTopicRun,
            "write",
            manage_topic_run,
            preview_manage_topic_run,
        ),
        ToolSpec(
            "list_topics",
            "topics",
            "按总分降序列出选题卡片：总分、标题、素材覆盖、是否有大纲、状态（new/adopted/dismissed）。",
            ListTopics,
            "read",
            list_topics,
        ),
        ToolSpec(
            "get_topic",
            "topics",
            "读取一张选题卡片：分项分数、角度、理由、关键画面、推荐模板、大纲与来源条目。",
            GetTopic,
            "read",
            get_topic,
        ),
        ToolSpec(
            "score_topic",
            "topics",
            "重新为一张选题打分；受众匹配与素材候选相关性核验调用选题模型，覆盖按领域档案配置重新检索。",
            ScoreTopic,
            "write",
            score_topic,
            preview_score_topic,
        ),
        ToolSpec(
            "topic_coverage",
            "topics",
            "查看素材覆盖报告：核验通过的命中片段、候选的相关性结论、理由与原文依据，以及缺口。",
            TopicCoverage,
            "read",
            topic_coverage,
        ),
        ToolSpec(
            "draft_topic_outline",
            "topics",
            "为选题生成（或重新生成）视频大纲与推荐文案模板。",
            DraftTopicOutline,
            "write",
            draft_topic_outline,
            preview_draft_topic_outline,
        ),
        ToolSpec(
            "update_topic",
            "topics",
            "修改选题的标题、大纲、推荐模板，或忽略/恢复（已建任务的选题不能改状态）。",
            UpdateTopic,
            "write",
            update_topic,
            preview_update_topic,
        ),
        ToolSpec(
            "create_job_from_topic",
            "topics",
            (
                "把选题建成创作任务：大纲、推荐文案模板、档案默认画幅、"
                "来源资料（参考文稿）与账号档案快照都预先设好。"
            ),
            CreateJobFromTopic,
            "write",
            create_job_from_topic,
            preview_create_job_from_topic,
        ),
        ToolSpec(
            "get_job_profile",
            "topics",
            "读取从选题创建的创作任务的账号档案快照，以及原档案是否已更新。",
            JobProfileArgs,
            "read",
            get_job_profile,
        ),
        ToolSpec(
            "sync_job_profile",
            "topics",
            "用当前领域档案更新任务的档案快照（已有文案只提示可重新生成，不自动重写）。",
            JobProfileArgs,
            "write",
            sync_job_profile,
            preview_sync_job_profile,
        ),
        ToolSpec(
            "topic_gaps",
            "topics",
            "列出素材采集清单：选题覆盖报告里缺少的画面，按出现次数排序，附相关选题与 CSV 导出地址。",
            TopicGaps,
            "read",
            topic_gaps,
        ),
        ToolSpec(
            "update_topic_gap",
            "topics",
            "把素材缺口标记为已采集（done）或重新标记为待采集（open）。",
            UpdateTopicGap,
            "write",
            update_topic_gap,
            preview_update_topic_gap,
        ),
        ToolSpec(
            "get_topic_schedule",
            "topics",
            "查看每日定时挖掘：各档案的时刻与条数、下次运行时间、今日运行与产出、调度是否在运行。",
            Empty,
            "read",
            get_topic_schedule,
        ),
    ]
