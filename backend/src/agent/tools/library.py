"""素材库、人物库与检索的只读工具（第一期不提供这些领域的写操作）。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mindex.agent.registry import ToolContext, ToolOutput, ToolSpec
from mindex.agent.tools.common import check, clip
from mindex.agent.tools.media import item as media_item
from mindex.agent.tools.media import segment_media
from mindex.shared.config import StrictModel


class SearchMedia(StrictModel):
    text: str = Field(min_length=1, max_length=400, description="中文检索描述，例如：城市 夜景 灯光")
    granularity: Literal["shot", "moment"] = Field(default="moment", description="shot=镜头；moment=精细片段")
    limit: int = Field(default=8, ge=1, le=20)
    source_ids: list[str] | None = Field(default=None, description="只在这些素材里检索")
    person_ids: list[str] | None = Field(default=None, description="要求出现的人物（演员或角色 ID）")


async def search_media(args: SearchMedia, ctx: ToolContext) -> ToolOutput:
    body = {"text": args.text, "granularity": args.granularity, "limit": args.limit}
    if args.source_ids:
        body["filters"] = {"source_ids": args.source_ids}
    if args.person_ids:
        body["person_ids"] = args.person_ids
    data = check(await ctx.client.post("/api/search/text", body), "检索")
    rows = []
    for rank, item in enumerate(data.get("results", []), 1):
        annotation = item.get("annotation") or {}
        rows.append({
            "rank": rank, "segment_id": item["id"], "source_id": item.get("source_id"),
            "source_name": item.get("source_name"), "start": item.get("start"), "end": item.get("end"),
            "description": clip(annotation.get("description") or item.get("description"), 80),
            "score": round(float(item.get("score", 0)), 4),
        })
    media = [segment_media({**r, "thumbnail_url": raw.get("thumbnail_url")},
                           f"#{r['rank']} {r['source_name']} {r['start']:.1f}–{r['end']:.1f}s")
             for r, raw in zip(rows, data.get("results", [])) if r["start"] is not None]
    lines = [f"#{r['rank']} {r['source_name']} {r['start']:.1f}–{r['end']:.1f}s 片段 {r['segment_id']} {r['description']}"
             for r in rows if r["start"] is not None]
    warnings = data.get("warnings") or []
    text = ("\n".join(lines) or "（没有检索结果）") + ("\n注意：" + "；".join(warnings) if warnings else "")
    return ToolOutput({"results": rows, "warnings": warnings, "degraded": data.get("degraded", False)}, text=text,
                      meta={"kind": "search", "count": len(rows)},
                      focus={"page": "search", "query": {"text": args.text, "granularity": args.granularity}},
                      media=media)


class ListSources(StrictModel):
    q: str = Field(default="", max_length=200, description="按名称过滤")
    limit: int = Field(default=30, ge=1, le=100)


async def list_sources(args: ListSources, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get("/api/admin/sources", q=args.q, page=1, page_size=args.limit), "列出素材")
    rows = [{"id": s["id"], "name": s["name"], "duration": (s.get("media") or {}).get("duration"),
             "prepared": s["prepared"], "shots": s.get("shot_count"), "hidden": s.get("hidden")}
            for s in data["items"]]
    text = f"共 {data['total']} 个素材：\n" + "\n".join(
        f"{r['id']} | {r['name']} | {r['duration'] or 0:.0f}s | 镜头 {r['shots']}{' | 隐藏' if r['hidden'] else ''}"
        for r in rows)
    focus = {"page": "admin"}
    if len(rows) == 1:
        focus["source_id"] = rows[0]["id"]
    media = []
    for row in rows[:6]:
        poster = None
        shots = await ctx.client.get(f"/api/admin/sources/{row['id']}/segments", kind="shot", page=1, page_size=1)
        if shots["ok"] and shots["data"].get("items"):
            poster = f"/api/admin/segments/{shots['data']['items'][0]['id']}/thumbnail"
        media.append(media_item("video", f"/api/admin/sources/{row['id']}/playlist", row["name"], poster=poster,
                          playlist=True, collapsed=poster is None))
    return ToolOutput({"items": rows, "total": data["total"]}, text=text, focus=focus, media=media)


class ListPersons(StrictModel):
    kind: Literal["actor", "role", "work"] | None = Field(default=None, description="actor 演员、role 角色、work 作品")
    q: str = Field(default="", max_length=100, description="按名称过滤")


async def list_persons(args: ListPersons, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get("/api/persons"), "读取人物库")
    rows = [{"id": e["id"], "kind": e["kind"], "name": e["name"]}
            for e in data["entities"]
            if (args.kind is None or e["kind"] == args.kind) and args.q.lower() in e["name"].lower()]
    labels = {"actor": "演员", "role": "角色", "work": "作品"}
    text = "\n".join(f"{r['id']} | {labels.get(r['kind'], r['kind'])} | {r['name']}" for r in rows) or "（人物库为空）"
    names = {r["id"]: r["name"] for r in rows}
    media = []
    for reference in data.get("references", []):
        owner = reference.get("role_id") or reference.get("actor_id")
        if owner in names and len(media) < 12:
            media.append(media_item("image", reference["url"], names[owner], collapsed=False))
    return ToolOutput({"items": rows}, text=text, focus={"page": "persons"}, media=media)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("search_media", "search", "按文字描述检索素材片段（与检索页相同的多渠道检索），返回素材、时间区间与描述。",
                 SearchMedia, "read", search_media),
        ToolSpec("list_sources", "library", "列出素材库中的素材（ID、名称、时长、镜头数）。", ListSources, "read",
                 list_sources),
        ToolSpec("list_persons", "library", "列出人物库中的演员、角色与作品。", ListPersons, "read", list_persons),
    ]
