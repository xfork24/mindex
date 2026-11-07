"""素材领域工具：素材与片段浏览、整理信息与素材报告（任务与上传见 library_tasks.py）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import check, clip
from agent.tools.media import clean
from agent.tools.media import item as media_item
from shared.config import StrictModel

# 素材管理页暂未订阅 mindex:agent-mutation；按领域名派发，供页面后续接入刷新。
LIBRARY = {"domain": "library"}
WORK_NAMES = {"shots": "分镜", "dialogue": "对白", "analysis": "分析", "persons": "人物"}
WORK_STATUS = {
    "complete": "已完成",
    "partial": "部分完成",
    "empty": "无内容",
    "no_audio": "无音轨",
    "disabled": "未启用",
    "not_processed": "未处理",
    "failed": "失败",
    "processing": "处理中",
    "pending_alignment": "等待对齐",
}
TASK_STATUS = {
    "queued": "排队中",
    "running": "运行中",
    "complete": "已完成",
    "failed": "失败",
    "interrupted": "已中断",
    "cancelled": "已取消",
}
# 片段详情各部分的文本预算（字符）；整体再受 registry.MAX_TEXT 约束。
UTTERANCE_LIMIT = 40
TEXT_BUDGET = 3000
MAX_THUMBNAILS = 12


def admin_focus(source_id: str | None = None, segment_id: str | None = None) -> dict:
    focus = {"page": "admin"}
    if source_id:
        focus["source_id"] = source_id
        if segment_id:
            focus["segment_id"] = segment_id
    return focus


def seconds(value) -> str:
    return f"{float(value or 0):.1f}s"


def source_media(source_id: str, title: str, poster: str | None) -> list[dict]:
    return clean(
        [
            media_item(
                "video",
                f"/api/admin/sources/{source_id}/playlist",
                title,
                poster=poster,
                playlist=True,
                collapsed=poster is None,
            )
        ]
    )


async def first_thumbnail(ctx: ToolContext, source_id: str) -> str | None:
    shots = await ctx.client.get(f"/api/admin/sources/{source_id}/segments", kind="shot", page=1, page_size=1)
    if shots["ok"] and shots["data"].get("items"):
        return shots["data"]["items"][0]["thumbnail_url"]
    return None


class ListSources(StrictModel):
    source_group_ids: list[str] = Field(
        default_factory=list, description="多组取并集，再与搜索条件取交集；空组无候选"
    )
    q: str = Field(default="", max_length=200, description="按名称过滤")
    limit: int = Field(default=30, ge=1, le=100)


async def list_sources(args: ListSources, ctx: ToolContext) -> ToolOutput:
    data = check(
        await ctx.client.get(
            "/api/admin/sources",
            q=args.q,
            page=1,
            page_size=args.limit,
            source_group_ids=args.source_group_ids or None,
        ),
        "列出素材",
    )
    rows = [
        {
            "id": s["id"],
            "name": s["name"],
            "duration": (s.get("media") or {}).get("duration"),
            "prepared": s["prepared"],
            "shots": s.get("shot_count"),
            "hidden": s.get("hidden"),
        }
        for s in data["items"]
    ]
    text = f"共 {data['total']} 个素材：\n" + "\n".join(
        f"{r['id']} | {r['name']} | {r['duration'] or 0:.0f}s | 镜头 "
        f"{r['shots']}{' | 隐藏' if r['hidden'] else ''}"
        for r in rows
    )
    focus = {"page": "admin"}
    if len(rows) == 1:
        focus["source_id"] = rows[0]["id"]
    media = []
    for row in rows[:6]:
        poster = None
        shots = await ctx.client.get(
            f"/api/admin/sources/{row['id']}/segments", kind="shot", page=1, page_size=1
        )
        if shots["ok"] and shots["data"].get("items"):
            poster = f"/api/admin/segments/{shots['data']['items'][0]['id']}/thumbnail"
        media.append(
            media_item(
                "video",
                f"/api/admin/sources/{row['id']}/playlist",
                row["name"],
                poster=poster,
                playlist=True,
                collapsed=poster is None,
            )
        )
    return ToolOutput({"items": rows, "total": data["total"]}, text=text, focus=focus, media=media)


# ---------- get_source ----------


class GetSource(StrictModel):
    source_id: str = Field(min_length=1, max_length=200, description="素材 ID（list_sources 获取）")


def task_brief(task: dict | None) -> str | None:
    if not task:
        return None
    text = f"{task['id']}（{TASK_STATUS.get(task['status'], task['status'])}"
    stage = (task.get("progress") or {}).get("stage") if isinstance(task.get("progress"), dict) else None
    if stage and task["status"] in ("queued", "running"):
        text += f"，阶段 {stage}"
    if task.get("error") and task["status"] in ("failed", "interrupted"):
        text += f"，{clip(task['error'], 80)}"
    return text + "）"


def work_result(name: str, value: dict) -> str:
    result = value.get("result") or {}
    if name == "shots":
        return (
            f"镜头 {result.get('completed_shots', 0)}/{result.get('shots', 0)}，精细片段 "
            f"{result.get('moments', 0)}"
            + (f"，缺失文件 {result['missing_artifacts']}" if result.get("missing_artifacts") else "")
        )
    if name == "dialogue":
        origin = {"asr": "语音识别", "legacy_asr": "语音识别", "subtitle": "字幕"}.get(
            result.get("source"), result.get("source")
        )
        text = f"对白 {result.get('utterances', 0)} 句" + (f"，来源 {origin}" if origin else "")
        if result.get("total_chunks"):
            text += f"，进度 {result.get('completed_chunks') or 0}/{result['total_chunks']} 段"
        if result.get("error"):
            text += f"，错误：{clip(result['error'], 80)}"
        return text
    if name == "analysis":
        text = f"已分析 {result.get('analyzed', 0)}/{result.get('total', 0)}"
        if result.get("skipped"):
            text += f"，跳过 {result['skipped']}"
        if result.get("scenes"):
            text += f"，场景 {result['scenes']}"
        return text
    return (
        f"覆盖镜头 {result.get('covered_shots', 0)}/{result.get('total_shots', 0)}，"
        f"确认人物 {result.get('people', 0)}"
    )


def work_line(name: str, value: dict) -> str:
    parts = [
        f"{WORK_NAMES[name]}：{WORK_STATUS.get(value.get('status'), value.get('status'))}",
        work_result(name, value),
    ]
    search = value.get("search")
    if isinstance(search, dict) and search.get("expected_segments"):
        parts.append(
            f"检索已发布 {search.get('indexed_segments', 0)}/{search['expected_segments']}"
            + ("（最新）" if search.get("current") else "（待发布）")
        )
    scene = value.get("scene_search")
    if isinstance(scene, dict) and scene.get("expected_segments"):
        parts.append(
            f"场景检索已发布 {scene.get('indexed_segments', 0)}/{scene['expected_segments']}"
            + ("（最新）" if scene.get("current") else "（待发布）")
        )
    for label, key in (
        ("最近任务", "task"),
        ("最近发布", "publish_task"),
        ("最近场景发布", "scene_publish_task"),
    ):
        brief = task_brief(value.get(key))
        if brief:
            parts.append(f"{label} {brief}")
    if not value.get("can_start", True) and value.get("start_reason"):
        parts.append(f"不可启动：{value['start_reason']}")
    parts.append("可发布" if value.get("can_publish") else "不可发布")
    return "- " + "｜".join(parts)


def compact_work(value: dict) -> dict:
    keep = {}
    for key in ("status", "result", "can_start", "start_reason", "can_publish", "can_publish_scene"):
        if key in value:
            keep[key] = value[key]
    for key in ("task", "publish_task", "scene_publish_task"):
        task = value.get(key)
        keep[key] = (
            {k: task.get(k) for k in ("id", "status", "work", "error", "progress", "updated_at")}
            if task
            else None
        )
    for field in ("search", "scene_search"):
        if isinstance(value.get(field), dict):
            keep[field] = {
                k: value[field].get(k) for k in ("current", "expected_segments", "indexed_segments")
            }
    if value.get("setup"):
        keep["setup"] = value["setup"]
    return keep


async def get_source(args: GetSource, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get(f"/api/admin/sources/{args.source_id}", detail="work"), "读取素材")
    work = data.get("work") or {}
    flags = [label for flag, label in (("hidden", "已隐藏"), ("favorite", "已收藏")) if data.get(flag)]
    header = (
        f"素材 {data['id']}「{data['name']}」｜时长 {seconds((data.get('media') or {}).get('duration'))}｜"
        f"镜头 {data.get('shot_count', 0)}｜精细片段 {data.get('moment_count', 0)}"
        + ("｜" + "、".join(flags) if flags else "")
    )
    lines = [header]
    if data.get("groups"):
        lines.append("所属组：" + "、".join(f"{group['name']} ({group['id']})" for group in data["groups"]))
    if data.get("notes"):
        lines.append(f"备注：{clip(data['notes'], 200)}")
    lines += [work_line(name, work[name]) for name in WORK_NAMES if name in work]
    if data.get("work_refreshing"):
        lines.append("（工作状态正在后台刷新，覆盖与发布数据可能稍有滞后）")
    value = {
        "id": data["id"],
        "name": data["name"],
        "media": data.get("media"),
        "hidden": data.get("hidden"),
        "favorite": data.get("favorite"),
        "notes": data.get("notes"),
        "groups": data.get("groups", []),
        "work": {name: compact_work(work[name]) for name in WORK_NAMES if name in work},
    }
    poster = await first_thumbnail(ctx, data["id"])
    return ToolOutput(
        value,
        text="\n".join(lines),
        focus=admin_focus(data["id"]),
        media=source_media(data["id"], data["name"], poster),
    )


# ---------- list_segments ----------


class ListSegments(StrictModel):
    source_id: str = Field(min_length=1, max_length=200)
    kind: Literal["shot", "moment"] = Field(default="shot", description="shot 镜头；moment 精细片段")
    parent_id: str | None = Field(default=None, description="只列出该镜头下的精细片段（需 kind=moment）")
    visibility: Literal["visible", "hidden", "all"] = "visible"
    favorite_only: bool = False
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=50)


async def list_segments(args: ListSegments, ctx: ToolContext) -> ToolOutput:
    data = check(
        await ctx.client.get(
            f"/api/admin/sources/{args.source_id}/segments",
            kind=args.kind,
            parent_id=args.parent_id,
            visibility=args.visibility,
            favorite_only=args.favorite_only or None,
            page=args.page,
            page_size=args.page_size,
        ),
        "列出片段",
    )
    items = data["items"]
    rows = [
        {
            "id": s["id"],
            "name": s["name"],
            "start": s["start"],
            "end": s["end"],
            "hidden": s["effective_hidden"],
            "favorite": s["favorite"],
            "children": s.get("child_count", 0),
        }
        for s in items
    ]
    label = "镜头" if args.kind == "shot" else "精细片段"
    pages = max(1, -(-data["total"] // args.page_size))
    lines = [f"共 {data['total']} 个{label}，第 {args.page}/{pages} 页："]
    for row in rows:
        marks = [m for m, on in (("隐藏", row["hidden"]), ("收藏", row["favorite"])) if on]
        lines.append(
            f"{row['id']} | {clip(row['name'], 40)} | {row['start']:.2f}-{row['end']:.2f}s"
            + (f" | 子片段 {row['children']}" if args.kind == "shot" and row["children"] else "")
            + (" | " + "、".join(marks) if marks else "")
        )
    if not rows:
        lines.append("（本页没有结果）")
    media = clean(
        media_item(
            "video",
            f"/api/admin/segments/{s['id']}/playlist",
            s["name"],
            poster=s.get("thumbnail_url"),
            playlist=True,
            collapsed=False,
        )
        for s in items[:MAX_THUMBNAILS]
    )
    return ToolOutput(
        {"items": rows, "total": data["total"], "page": args.page, "page_size": args.page_size},
        text="\n".join(lines),
        focus=admin_focus(args.source_id),
        media=media,
    )


# ---------- get_segment_details ----------


class GetSegmentDetails(StrictModel):
    segment_id: str = Field(
        min_length=1, max_length=200, description="镜头或精细片段 ID（list_segments 获取）"
    )


class Budget:
    """按字符预算拼接多段文本；超出时标注已截断。"""

    def __init__(self, limit: int):
        self.left = limit
        self.truncated = False

    def take(self, text: str) -> str | None:
        if self.left <= 0:
            self.truncated = True
            return None
        if len(text) > self.left:
            self.truncated = True
            text = text[: self.left] + "…"
        self.left -= len(text)
        return text


def utterance_lines(utterances: list[dict], budget: Budget) -> list[str]:
    lines = []
    for index, u in enumerate(utterances):
        if index >= UTTERANCE_LIMIT:
            budget.truncated = True
            lines.append(f"  …其余 {len(utterances) - index} 句已截断")
            break
        line = budget.take(f"  [{u['start']:.1f}-{u['end']:.1f}s] {(u.get('text') or '').strip()}")
        if line is None:
            lines.append(f"  …其余 {len(utterances) - index} 句已截断")
            break
        lines.append(line)
    return lines


ANNOTATION_FIELDS = (
    ("actions", "动作"),
    ("people", "人物"),
    ("objects", "对象"),
    ("scene", "场景"),
    ("emotions", "情绪"),
    ("atmosphere", "氛围"),
    ("shot_size", "景别"),
    ("angle", "角度"),
    ("camera_motion", "镜头运动"),
)


async def get_segment_details(args: GetSegmentDetails, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get(f"/api/admin/segments/{args.segment_id}/details"), "读取片段详情")
    segment, asr, subtitles, llm, person = (
        data["segment"],
        data["asr"],
        data["subtitles"],
        data["llm"],
        data["person"],
    )
    budget = Budget(TEXT_BUDGET)
    lines = [
        f"{'镜头' if segment['kind'] == 'shot' else '精细片段'} {segment['id']}「{segment['name']}」"
        f"｜素材 "
        f"{segment['source_name']}（{segment['source_id']}）｜{segment['start']:.2f}-{segment['end']:.2f}"
        f"s" + ("｜已隐藏" if segment.get("effective_hidden") else "")
    ]
    lines.append(f"ASR：{asr.get('message') or asr.get('status')}")
    lines += utterance_lines(asr.get("utterances") or [], budget)
    tracks = subtitles.get("tracks") or []
    lines.append(
        f"字幕：{subtitles.get('message') or subtitles.get('status')}"
        + (f"（{len(tracks)} 条轨道）" if tracks else "")
    )
    for track in tracks:
        lines.append(
            f" 轨道 {track.get('index')} {track.get('title') or ''} [{track.get('language') or 'und'}]："
            f"{track.get('message') or ''}"
        )
        lines += utterance_lines(track.get("utterances") or [], budget)
    lines.append(f"LLM 分析：{llm.get('message') or llm.get('status')}")
    if llm.get("description"):
        text = budget.take(llm["description"].strip())
        if text:
            lines.append(f"  描述：{text}")
    for key, label in ANNOTATION_FIELDS:
        values = llm.get(key)
        if values:
            text = budget.take("；".join(str(v) for v in values))
            if text:
                lines.append(f"  {label}：{text}")
    counts = person.get("counts") or {}
    lines.append(
        f"人物：{person.get('message') or person.get('status')}｜已确认 {counts.get('accepted', 0)}，"
        f"待确认 {counts.get('candidate', 0)}，已驳回 {counts.get('rejected', 0)}"
    )
    people = person.get("people") or []
    if people:
        lines.append(
            "  "
            + "、".join(f"{p['name']}（{p.get('kind_label') or p['kind']}，{p['id']}）" for p in people[:20])
            + (f" 等 {len(people)} 人" if len(people) > 20 else "")
        )
    if budget.truncated:
        lines.append(f"（文本超出 {TEXT_BUDGET} 字预算，已截断；可在素材管理页查看完整内容）")
    value = {
        "segment": {
            k: segment.get(k)
            for k in (
                "id",
                "source_id",
                "kind",
                "name",
                "start",
                "end",
                "parent_id",
                "effective_hidden",
                "favorite",
                "notes",
            )
        },
        "asr": {"status": asr.get("status"), "utterances": len(asr.get("utterances") or [])},
        "subtitles": {"status": subtitles.get("status"), "tracks": len(tracks)},
        "llm": {"status": llm.get("status")},
        "person": {"status": person.get("status"), "counts": counts, "people": [p["id"] for p in people]},
        "truncated": budget.truncated,
    }
    media = clean(
        [
            media_item(
                "video",
                f"/api/admin/segments/{segment['id']}/playlist",
                segment["name"],
                poster=segment.get("thumbnail_url"),
                playlist=True,
                collapsed=False,
            )
        ]
    )
    return ToolOutput(
        value, text="\n".join(lines), focus=admin_focus(segment["source_id"], segment["id"]), media=media
    )


# ---------- get_source_report ----------


class GetSourceReport(StrictModel):
    source_id: str = Field(min_length=1, max_length=200)


MISSING_SHOWN = 5


async def get_source_report(args: GetSourceReport, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get(f"/api/admin/sources/{args.source_id}/report"), "生成素材报告")
    stats = data.get("shot_seconds")
    missing = data.get("missing_artifacts") or []
    materialized = data.get("materialized") or {}
    lines = [
        f"素材报告：{data['source_id']}「{data['name']}」｜时长 "
        f"{seconds((data.get('media') or {}).get('duration'))}"
        f"｜{'已完成分镜' if data.get('prepared') else '分镜未完成'}",
        f"计划镜头 {data['planned_shots']}，计划精细片段 "
        + (
            str(data["planned_moments"])
            if data.get("planned_moments") is not None
            else "未知（布局与当前配置不一致）"
        )
        + "；已落盘 "
        + ("、".join(f"{k} {v}" for k, v in sorted(materialized.items())) or "无"),
        "布局与当前配置"
        + ("一致" if data.get("configuration_matches") else "不一致（需要重新分镜才能使用当前配置）"),
    ]
    if stats:
        lines.append(
            f"镜头时长：最短 {stats['min']:.2f}s，中位 {stats['median']:.2f}s，P95 {stats['p95']:.2f}s，"
            f"最长 {stats['max']:.2f}s，短于 1 秒 {stats['under_one_second']} 个"
        )
    if missing:
        lines.append(
            f"缺失文件 {len(missing)} 个："
            + "；".join(f"{m['id']} {m['field']}" for m in missing[:MISSING_SHOWN])
            + (f" 等（其余 {len(missing) - MISSING_SHOWN} 个省略）" if len(missing) > MISSING_SHOWN else "")
        )
    else:
        lines.append("缺失文件：无")
    runs = data.get("runs") or []
    if runs:
        lines.append("最近运行：")
        lines += [
            f"  {r.get('stage')} {r.get('status')}"
            + (f"：{clip(r.get('message'), 80)}" if r.get("message") else "")
            for r in runs[:5]
        ]
    value = {
        k: data.get(k)
        for k in (
            "source_id",
            "name",
            "prepared",
            "planned_shots",
            "planned_moments",
            "materialized",
            "shot_seconds",
            "configuration_matches",
        )
    }
    value.update(
        missing_count=len(missing),
        missing=missing[:MISSING_SHOWN],
        runs=[{k: r.get(k) for k in ("stage", "status", "message")} for r in runs[:5]],
    )
    return ToolOutput(value, text="\n".join(lines), focus=admin_focus(data["source_id"]))


# ---------- update_curation ----------


class UpdateCuration(StrictModel):
    target: Literal["source", "segment"] = Field(description="source 素材；segment 镜头或精细片段")
    id: str = Field(min_length=1, max_length=200, description="素材 ID 或片段 ID")
    display_name: str | None = Field(default=None, max_length=200, description="显示名；空字符串表示恢复原名")
    notes: str | None = Field(default=None, max_length=4000, description="备注；空字符串表示清空")
    favorite: bool | None = None
    hidden: bool | None = Field(default=None, description="隐藏后不再出现在检索结果中")


FIELD_LABELS = {"display_name": "显示名", "notes": "备注", "favorite": "收藏", "hidden": "隐藏"}


def curation_changes(args: UpdateCuration) -> dict:
    changes = {k: getattr(args, k) for k in FIELD_LABELS if getattr(args, k) is not None}
    if not changes:
        raise ToolError(
            "请至少提供一个要修改的字段：display_name、notes、favorite、hidden", code="invalid_arguments"
        )
    return changes


async def current_record(args: UpdateCuration, ctx: ToolContext) -> dict:
    path = f"/api/admin/sources/{args.id}" if args.target == "source" else f"/api/admin/segments/{args.id}"
    params = {"detail": "summary"} if args.target == "source" else {}
    return check(
        await ctx.client.get(path, **params), "读取" + ("素材" if args.target == "source" else "片段")
    )


def shown(key: str, value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    return clip(value, 80) if value else "（空）"


async def preview_update_curation(args: UpdateCuration, ctx: ToolContext) -> Preview:
    changes = curation_changes(args)
    record = await current_record(args, ctx)
    before = {
        "display_name": record.get("custom_name"),
        "notes": record.get("notes"),
        "favorite": record.get("favorite"),
        "hidden": record.get("hidden"),
    }
    rows = [
        {"label": FIELD_LABELS[k], "before": shown(k, before[k]), "after": shown(k, v)}
        for k, v in changes.items()
    ]
    warnings = []
    if changes.get("hidden") is True and not before["hidden"]:
        warnings.append("隐藏后该内容不会出现在检索结果中")
        if args.target == "source":
            warnings.append("素材下所有镜头与片段一并隐藏")
        elif record.get("kind") == "shot":
            warnings.append("该镜头下的精细片段一并隐藏")
    if changes.get("hidden") is False and record.get("inherited_hidden"):
        warnings.append("所属素材或父镜头仍处于隐藏状态，取消本条隐藏后它在检索中仍不可见")
    label = "素材" if args.target == "source" else "片段"
    focus = admin_focus(args.id) if args.target == "source" else admin_focus(record["source_id"], args.id)
    return Preview(f"修改{label}「{record['name']}」的整理信息", rows, warnings=warnings, focus=focus)


async def update_curation(args: UpdateCuration, ctx: ToolContext) -> ToolOutput:
    changes = curation_changes(args)
    path = f"/api/admin/sources/{args.id}" if args.target == "source" else f"/api/admin/segments/{args.id}"
    data = check(await ctx.client.patch(path, changes), "修改整理信息")
    label = "素材" if args.target == "source" else "片段"
    summary = "，".join(f"{FIELD_LABELS[k]}={shown(k, v)}" for k, v in changes.items())
    if args.target == "source":
        focus, source_id = admin_focus(args.id), args.id
    else:
        focus, source_id = admin_focus(data["source_id"], args.id), data["source_id"]
    return ToolOutput(
        {"target": args.target, "id": args.id, "changed": changes, "name": data.get("name")},
        text=f"已修改{label}「{data.get('name')}」：{summary}。",
        mutation={**LIBRARY, "source_id": source_id, "target": args.target, "id": args.id},
        focus=focus,
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "list_sources",
            "library",
            "列出素材库中的素材（ID、名称、时长、镜头数）。",
            ListSources,
            "read",
            list_sources,
        ),
        ToolSpec(
            "get_source",
            "library",
            "读取单个素材：基本信息与分镜、对白、分析、人物四项工作的状态、最近任务与可发布性。",
            GetSource,
            "read",
            get_source,
        ),
        ToolSpec(
            "list_segments",
            "library",
            "分页列出素材下的镜头或精细片段（ID、名称、区间、隐藏/收藏），附缩略图。",
            ListSegments,
            "read",
            list_segments,
        ),
        ToolSpec(
            "get_segment_details",
            "library",
            "读取片段的 ASR、字幕、LLM 分析与人物结果（长文本按预算截断）。",
            GetSegmentDetails,
            "read",
            get_segment_details,
        ),
        ToolSpec(
            "get_source_report",
            "library",
            "生成素材报告：计划镜头数、时长统计、缺失文件明细、布局是否与当前配置一致、最近运行记录。",
            GetSourceReport,
            "read",
            get_source_report,
        ),
        ToolSpec(
            "update_curation",
            "library",
            (
                "修改素材或片段的整理信息（显示名、备注、收藏、隐藏），只提交给出的字段；"
                "隐藏会让内容不再出现在检索结果中。"
            ),
            UpdateCuration,
            "write",
            update_curation,
            preview=preview_update_curation,
        ),
    ]
