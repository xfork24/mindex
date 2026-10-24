"""检索领域工具：文字/图片/视频检索、找相似、区间抽帧与系统状态。

全部为 read 级，经回环客户端调用检索页同一套接口；结果附带检索页定位与片段缩略图媒体。
"""

from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import Field, model_validator

from mindex.agent.registry import ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check, clip
from mindex.agent.tools.media import item, segment_media
from mindex.shared.config import StrictModel

ATTACHMENT_PREFIX = "attachment://"
MAX_FRAMES = 12
# search_by_media 交给模型核对的结果缩略图数。
SEARCH_IMAGE_RESULTS = 4
Granularity = Literal["shot", "moment", "scene"]
GRANULARITY_FIELD = Field(default="moment", description="shot=镜头；moment=精细片段；scene=场景")


class SearchFilters(StrictModel):
    """与检索页筛选一致；未给出的条件不过滤。"""

    min_duration: float | None = Field(default=None, ge=0, description="片段最短时长（秒）")
    max_duration: float | None = Field(default=None, gt=0, description="片段最长时长（秒）")
    scene: str | None = Field(default=None, max_length=100, description="场景标签")
    emotion: str | None = Field(default=None, max_length=100, description="情绪标签")
    shot_size: str | None = Field(default=None, max_length=100, description="景别标签")


def filters_body(filters: SearchFilters | None, source_ids: list[str] | None) -> dict:
    body = filters.model_dump(exclude_none=True) if filters else {}
    if source_ids:
        body["source_ids"] = source_ids
    return body


class SearchMedia(StrictModel):
    text: str = Field(default="", max_length=400, description="中文检索描述，例如：城市 夜景 灯光")
    granularity: Granularity = GRANULARITY_FIELD
    limit: int = Field(default=8, ge=1, le=20)
    source_ids: list[str] | None = Field(default=None, description="只在这些素材里检索")
    person_ids: list[str] | None = Field(default=None, max_length=30, description="要求出现的人物（演员或角色 ID）")
    person_mode: Literal["auto", "onscreen", "mentioned"] | None = Field(
        default=None, description="人物匹配方式：auto=自动；onscreen=画面出镜；mentioned=对白提及")
    person_relation: Literal["all", "any"] | None = Field(
        default=None, description="多个人物时：all=同时出现；any=任一出现")
    channels: list[Literal["visual", "dialogue", "analysis"]] | None = Field(
        default=None, min_length=1, description="召回渠道：visual=画面；dialogue=对白；analysis=分析描述；缺省为全部")
    filters: SearchFilters | None = None

    @model_validator(mode="after")
    def nonblank(self):
        self.text = self.text.strip()
        if not self.text and not self.person_ids:
            raise ValueError("text 与 person_ids 至少给出一个")
        return self


def result_rows(data: dict) -> tuple[list[dict], list[dict], list[str]]:
    """检索结果 → (行, 媒体, 文本行)；与 /api/search/text 结构一致的结果都可复用。"""
    rows, media, lines = [], [], []
    for rank, raw in enumerate(data.get("results", []), 1):
        annotation = raw.get("annotation") or {}
        row = {
            "rank": rank, "segment_id": raw["id"], "source_id": raw.get("source_id"),
            "source_name": raw.get("source_name"), "kind": raw.get("kind"),
            "start": raw.get("start"), "end": raw.get("end"),
            "description": clip(annotation.get("description") or raw.get("description"), 80),
            "score": round(float(raw.get("score", 0)), 4),
        }
        rows.append(row)
        if row["start"] is None or row["end"] is None:
            continue
        label = f"#{rank} {row['source_name']} {row['start']:.1f}–{row['end']:.1f}s"
        media.append(segment_media({**row, "thumbnail_url": raw.get("thumbnail_url")}, label))
        lines.append(f"{label} 片段 {row['segment_id']} {row['description']}")
    return rows, [m for m in media if m], lines


def result_text(lines: list[str], data: dict) -> str:
    """结果文本：如实说明降级与警告（Req 6.2）。"""
    text = "\n".join(lines) or "（没有检索结果）"
    warnings = data.get("warnings") or []
    if data.get("degraded"):
        text += "\n注意：本次检索已降级，结果可能不完整。"
    if warnings:
        text += "\n警告：" + "；".join(warnings)
    return text


def search_output(data: dict, lines: list[str], rows: list[dict], media: list[dict], focus: dict,
                  **extra) -> ToolOutput:
    value = {"results": rows, "warnings": data.get("warnings") or [], "degraded": bool(data.get("degraded")),
             **extra}
    if data.get("channels") is not None:
        value["channels"] = data["channels"]
    return ToolOutput(value, text=result_text(lines, data), meta={"kind": "search", "count": len(rows)},
                      focus=focus, media=media)


async def search_media(args: SearchMedia, ctx: ToolContext) -> ToolOutput:
    body: dict = {"text": args.text, "granularity": args.granularity, "limit": args.limit}
    filters = filters_body(args.filters, args.source_ids)
    if filters:
        body["filters"] = filters
    for key in ("person_ids", "person_mode", "person_relation", "channels"):
        value = getattr(args, key)
        if value:
            body[key] = value
    data = check(await ctx.client.post("/api/search/text", body), "检索")
    rows, media, lines = result_rows(data)
    return search_output(data, lines, rows, media,
                         {"page": "search", "query": {"text": args.text, "granularity": args.granularity}})


class SearchByMedia(StrictModel):
    attachment: str = Field(pattern=r"^attachment://", description="图片或视频附件引用 attachment://…")
    kind: Literal["image", "video"] | None = Field(
        default=None, description="附件类型；缺省按附件的内容类型推断")
    granularity: Granularity = GRANULARITY_FIELD
    limit: int = Field(default=8, ge=1, le=20)
    source_ids: list[str] | None = Field(default=None, description="只在这些素材里检索")
    filters: SearchFilters | None = None


def upload_check(result: dict, what: str) -> object:
    """multipart 回环结果检查：附件引用无效时转为 ``attachment_invalid``。"""
    if not result["ok"] and result.get("code") == "attachment_invalid":
        raise ToolError(f"附件不可用：{result['error']}", code="attachment_invalid",
                        value={"status": result["status"], "error": result["error"]})
    return check(result, what)


async def attachment_kind(ctx: ToolContext, ref: str) -> Literal["image", "video"]:
    token = ref[len(ATTACHMENT_PREFIX):]
    result = await ctx.client.get(f"/api/agent/attachments/{token}")
    if not result["ok"]:
        raise ToolError(f"附件不可用：{result['error']}", code="attachment_invalid",
                        value={"status": result["status"], "error": result["error"]})
    ctype = str((result["data"] or {}).get("content_type") or "").lower()
    if ctype.startswith("image/"):
        return "image"
    if ctype.startswith("video/"):
        return "video"
    raise ToolError(f"无法从内容类型 {ctype or '未知'} 判断附件是图片还是视频：请显式指定 kind=image 或 video",
                    code="invalid_arguments")


async def search_by_media(args: SearchByMedia, ctx: ToolContext) -> ToolOutput:
    kind = args.kind or await attachment_kind(ctx, args.attachment)
    data = upload_check(await ctx.client.upload(
        f"/api/search/{kind}", files={"file": args.attachment},
        data={"granularity": args.granularity, "limit": str(args.limit),
              "filters": json.dumps(filters_body(args.filters, args.source_ids), ensure_ascii=False)},
    ), "以图搜索" if kind == "image" else "以视频搜索")
    rows, media, lines = result_rows(data)
    output = search_output(data, lines, rows, media, {"page": "search"}, kind=kind)
    # 视觉输入：以图搜索时附上查询图片本身，再加前几条结果的缩略图，便于模型核对是否相似。
    posters = [m["poster"] for m in media if m.get("poster")][:SEARCH_IMAGE_RESULTS]
    output.images = ([args.attachment] if kind == "image" else []) + posters
    return output


class FindSimilar(StrictModel):
    segment_id: str = Field(min_length=1, description="种子片段 ID（来自检索结果的 segment_id）")
    granularity: Granularity | None = Field(default=None, description="结果粒度；缺省沿用种子片段的粒度")
    limit: int = Field(default=8, ge=1, le=20)
    source_ids: list[str] | None = Field(default=None, description="只在这些素材里找")
    filters: SearchFilters | None = None


SIMILAR_HINTS = {
    404: "种子片段不在当前索引中或尚未完成：请使用当前检索结果里的 segment_id",
    422: "种子片段没有画面表示（例如只有对白渠道），无法按画面找相似",
    409: "种子片段的画面向量在向量库中缺失：可用 check_index 检查索引一致性",
}


async def find_similar(args: FindSimilar, ctx: ToolContext) -> ToolOutput:
    body: dict = {"segment_id": args.segment_id, "limit": args.limit}
    if args.granularity:
        body["granularity"] = args.granularity
    filters = filters_body(args.filters, args.source_ids)
    if filters:
        body["filters"] = filters
    result = await ctx.client.post("/api/search/similar", body)
    if not result["ok"] and result["status"] in SIMILAR_HINTS:
        status = result["status"]
        raise ToolError(f"找相似失败（HTTP {status}）：{result['error']}（{SIMILAR_HINTS[status]}）",
                        code=f"http_{status}", value={"status": status, "error": result["error"]})
    data = check(result, "找相似")
    rows, media, lines = result_rows(data)
    output = search_output(data, lines, rows, media, {"page": "search"},
                           seed_segment_id=data.get("seed_segment_id", args.segment_id))
    output.text = f"与片段 {args.segment_id} 画面相似的片段：\n" + output.text
    return output


class SampleFrames(StrictModel):
    source_id: str | None = Field(default=None, description="素材 ID；与 segment_id 二选一")
    segment_id: str | None = Field(default=None, description="片段 ID；给出时使用该片段的时间区间")
    start: float | None = Field(default=None, ge=0, description="区间起点（秒，仅配合 source_id）；缺省为 0")
    end: float | None = Field(default=None, gt=0, description="区间终点（秒，仅配合 source_id）；缺省为素材结尾")
    count: int = Field(default=6, ge=1, le=MAX_FRAMES, description="帧数（1–12）")
    width: int = Field(default=768, ge=16, le=1024, description="帧宽度（像素）")

    @model_validator(mode="after")
    def target(self):
        if (self.source_id is None) == (self.segment_id is None):
            raise ValueError("source_id 与 segment_id 必须且只能给出一个")
        if self.segment_id is not None and (self.start is not None or self.end is not None):
            raise ValueError("使用 segment_id 时不要再给 start/end")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("end 必须不小于 start")
        return self


def frame_times(start: float, end: float, count: int) -> list[float]:
    """在 ``[start, end]`` 内均匀取 ``min(count, 12)`` 个时间点（含两端；只取 1 个时取中点），单调不减。"""
    count = max(1, min(int(count), MAX_FRAMES))
    if end < start:
        start, end = end, start
    if count == 1:
        return [start + (end - start) / 2]
    step = (end - start) / (count - 1)
    times = [start + i * step for i in range(count - 1)] + [end]
    # 浮点累积误差不能越出区间或破坏单调。
    result, last = [], start
    for t in times:
        t = min(max(t, last), end)
        result.append(t)
        last = t
    return result


def frame_url(source_id: str, t: float, width: int) -> str:
    # 向下取整到毫秒：保证 URL 中的时间不超过区间终点（终点可能正好是素材时长）。
    return f"/api/admin/sources/{source_id}/frame?t={math.floor(t * 1000) / 1000:.3f}&width={width}"


async def sample_frames(args: SampleFrames, ctx: ToolContext) -> ToolOutput:
    if args.segment_id:
        segment = check(await ctx.client.get(f"/api/admin/segments/{args.segment_id}"), "读取片段")
        source_id, start, end = segment["source_id"], float(segment["start"]), float(segment["end"])
        name = segment.get("source_name") or source_id
    else:
        source = check(await ctx.client.get(f"/api/admin/sources/{args.source_id}", detail="summary"), "读取素材")
        source_id, name = source["id"], source.get("name") or args.source_id
        duration = float((source.get("media") or {}).get("duration") or 0)
        start = args.start if args.start is not None else 0.0
        end = args.end if args.end is not None else duration
        if duration and (start > duration or end > duration):
            raise ToolError(f"区间超出素材时长（0–{duration:.3f} 秒）", code="invalid_arguments")
        if end < start:
            raise ToolError("end 必须不小于 start", code="invalid_arguments")
    times = frame_times(start, end, args.count)
    frames = [{"t": round(t, 3), "url": frame_url(source_id, t, args.width)} for t in times]
    media = [item("image", f["url"], f"{name} {f['t']:.1f}s", collapsed=False) for f in frames]
    images = [f["url"] for f in frames]
    # 引用同时放在结果值中（兼容既有调用方）；ToolOutput.images 供视觉开启时交给模型。
    value = {"source_id": source_id, "segment_id": args.segment_id, "start": start, "end": end,
             "timestamps": [f["t"] for f in frames], "frames": frames, "images": images}
    text = (f"{name} {start:.1f}–{end:.1f}s 均匀抽取 {len(frames)} 帧："
            + "、".join(f"{f['t']:.2f}s" for f in frames) + "（画面已在面板展示）")
    return ToolOutput(value, text=text, meta={"kind": "frames", "count": len(frames)},
                      focus={"page": "admin", "source_id": source_id}, media=[m for m in media if m],
                      images=images)


class SystemStatus(StrictModel):
    include_embedding: bool = Field(default=False, description="同时检查当前嵌入身份是否与活动索引匹配（较慢）")


async def get_system_status(args: SystemStatus, ctx: ToolContext) -> ToolOutput:
    # 渠道覆盖只在完整状态中计算（summary 模式下为空），因此不加 summary 参数；health 探测由 include_embedding 单独完成。
    data = check(await ctx.client.get("/api/status"), "读取系统状态")
    active = data.get("active_index") or None
    index = None
    if active:
        spec = active.get("spec") or {}
        index = {"id": active.get("id"), "status": active.get("status"), "mode": spec.get("mode"),
                 "format": spec.get("format", 1), "created_at": active.get("created_at"),
                 "search_release_id": active.get("search_release_id")}
    counts = {row["kind"]: row["count"] for row in data.get("segment_counts") or []}
    coverage = {name: {"indexed": c.get("indexed_segments", 0), "total": c.get("total_segments", 0),
                       "coverage": round(float(c.get("coverage", 0)), 4)}
                for name, c in (data.get("channel_coverage") or {}).items()}
    value = {"active_index": index, "source_count": len(data.get("sources") or []), "segment_counts": counts,
             "channel_coverage": coverage}
    lines = [f"活动索引：{index['id']}（{index['status']}，模式 {index['mode']}）" if index else "活动索引：无（尚未发布）",
             f"素材数：{value['source_count']}（不含隐藏素材）",
             "片段数：" + ("、".join(f"{k} {v}" for k, v in sorted(counts.items())) or "0")]
    labels = {"visual": "画面", "dialogue": "对白", "analysis": "分析"}
    if coverage:
        lines.append("渠道覆盖：" + "、".join(
            f"{labels.get(k, k)} {c['indexed']}/{c['total']}（{c['coverage']:.0%}）" for k, c in coverage.items()))
    if args.include_embedding:
        result = await ctx.client.get("/api/settings/embedding-status")
        embedding = result["data"] if result["ok"] else {"error": result["error"], "matches_index": None}
        value["embedding"] = embedding
        if embedding.get("matches_index") is None and not embedding.get("error"):
            lines.append("嵌入身份：没有活动索引，无需匹配")
        elif embedding.get("matches_index"):
            lines.append("嵌入身份：与活动索引一致，检索可用")
        else:
            reason = f"（{clip(embedding['error'], 120)}）" if embedding.get("error") else ""
            lines.append(f"嵌入身份：与活动索引不一致或不可用{reason}；画面检索可能降级，可用 list_index_versions 查看版本")
    return ToolOutput(value, text="\n".join(lines), meta={"kind": "status"}, focus={"page": "search"})


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("search_media", "search",
                 "按文字描述和/或人物检索素材片段（与检索页相同的多渠道检索），可指定粒度、渠道、人物匹配方式与筛选条件，"
                 "返回素材、时间区间、片段 ID 与描述。",
                 SearchMedia, "read", search_media),
        ToolSpec("search_by_media", "search",
                 "以图搜视频或以视频搜相似：用用户上传的图片/视频附件（attachment://…）检索素材片段。",
                 SearchByMedia, "read", search_by_media),
        ToolSpec("find_similar", "search",
                 "用库内已有片段（检索结果的 segment_id）找画面相似的其他片段，不需要重新上传；自动排除种子及同源重叠片段。",
                 FindSimilar, "read", find_similar),
        ToolSpec("sample_frames", "search",
                 "在素材区间或某个片段内均匀抽取 1–12 帧画面展示给用户（source_id 加可选 start/end，或 segment_id）。",
                 SampleFrames, "read", sample_frames),
        ToolSpec("get_system_status", "search",
                 "查看检索系统是否就绪：活动索引、渠道覆盖、素材与片段数量；可选检查嵌入身份是否与活动索引匹配。",
                 SystemStatus, "read", get_system_status),
    ]
