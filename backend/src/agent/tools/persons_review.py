"""人物领域的审核与发布工具：出镜审核、人物发布、检索组合管理与演员表导入。

- 全部经回环调用人物库页面与设置页「索引版本」同一套接口，校验、锁与中文错误与页面一致。
- 审核与演员表确认先在本地按页面同一规则预检（必须带角色、演员与作品角色关系），
  服务端仍是最终裁决；服务端拒绝时原样转述其中文原因。
- 发布与切换检索组合会改变当前生效的检索结果，为 danger 级；删除检索组合只删除记录，同样为 danger 级，
  当前生效的组合受保护。
- 不提供删除演员表导入图片的工具（会删除物理文件）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mindex.agent.ids import short
from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check
from mindex.agent.tools.index import MUTATION_DOMAIN as RELEASE_DOMAIN
from mindex.agent.tools.index import index_focus, load_versions, resolve_index
from mindex.agent.tools.library_tasks import attachment_meta, attachment_rows
from mindex.agent.tools.media import clean as clean_media
from mindex.agent.tools.media import item as media_item
from mindex.agent.tools.persons import (
    PERSONS,
    find_entity,
    hinted,
    label,
    load_catalogue,
    names,
    persons_focus,
)
from mindex.shared.config import StrictModel

MIN_PREFIX = 6
STATUS_LABELS = {"accepted": "已接受", "candidate": "待确认", "rejected": "已排除"}
ACTIVE_RELEASE_PROTECTED = "这是当前生效的检索组合；请先恢复到其他组合再删除"
CAST_HINTS = {
    422: "条目未通过校验或照片框质检：可调整该条目的照片框、去掉照片框，或改为关联已有演员",
    503: "人物服务不可用：照片框质检需要人物服务，请先在设置页检查人物模型",
}


# ---------- 公共 ----------

def pair_name(lookup: dict[str, str], actor_id: str | None, role_id: str | None) -> str:
    actor = lookup.get(actor_id, actor_id) if actor_id else "未知演员"
    return f"{actor}·{lookup.get(role_id, role_id)}" if role_id else actor


def source_of(data: dict, source_id: str) -> dict:
    source = next((s for s in data.get("sources", []) if s["id"] == source_id), None)
    if source is None:
        raise ToolError(f"找不到素材 {source_id}；可用 list_sources 查找", code="missing_source")
    return source


def checked_pair(data: dict, work_id: str | None, actor_id: str, role_id: str) -> None:
    """与服务端 shot_review 同一规则：演员、作品角色存在，角色属于素材作品且该演员出演该角色。"""
    actor, role = find_entity(data, actor_id), find_entity(data, role_id)
    if actor["kind"] != "actor":
        raise ToolError(f"{label(actor)}不是演员：actor_id 必须是演员 ID", code="invalid_selection")
    if role["kind"] != "role":
        raise ToolError(f"{label(role)}不是角色：出镜审核必须指定角色（role_id）", code="invalid_selection")
    if work_id not in (role.get("work_ids") or []) or actor_id not in (role.get("actor_ids") or []):
        lookup = names(data)
        raise ToolError(
            f"演员与作品角色关系不符：{label(role)}"
            + ("不属于该素材的作品" if work_id not in (role.get("work_ids") or []) else "")
            + ("" if actor_id in (role.get("actor_ids") or []) else f"，出演演员中没有{label(actor)}")
            + f"（出演演员：{'、'.join(lookup.get(a, a) for a in role.get('actor_ids') or []) or '无'}）。"
            "可先用 save_person 调整角色的出演演员或所属作品",
            code="invalid_selection")


# ---------- list_review_shots ----------

class ListReviewShots(StrictModel):
    source_id: str | None = Field(default=None, description="只看某个素材")
    status: Literal["candidate", "accepted", "rejected"] | None = Field(
        default=None, description="candidate 待确认、accepted 已接受、rejected 已排除（整镜排除）")
    actor_id: str | None = Field(default=None, description="按身份筛选：演员 ID（与 role_id 组成演员·角色）")
    role_id: str | None = Field(default=None, description="按身份筛选：角色 ID；只给角色且只有一名出演演员时自动补全演员")
    no_candidate: bool = Field(default=False, description="只看没有任何候选身份的分镜")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def exclusive(self):
        if self.no_candidate and (self.actor_id or self.role_id):
            raise ValueError("no_candidate 不能与 actor_id/role_id 同时使用")
        return self


def shot_status(counts: dict) -> str:
    if counts.get("accepted"):
        return "accepted"
    if counts.get("candidate") or not counts.get("rejected"):
        return "candidate"
    return "rejected"


def identity_filter(args: ListReviewShots, data: dict) -> tuple[str | None, str | None]:
    if not args.actor_id and not args.role_id:
        return None, None
    lookup = names(data)
    if args.role_id:
        role = find_entity(data, args.role_id)
        if role["kind"] != "role":
            raise ToolError(f"{label(role)}不是角色", code="invalid_arguments")
        actors = role.get("actor_ids") or []
        if args.actor_id:
            return args.actor_id, args.role_id
        if len(actors) != 1:
            raise ToolError(f"{label(role)}有 {len(actors)} 名出演演员，请同时给出 actor_id："
                            + ("、".join(f"{lookup.get(a, a)}({a})" for a in actors) or "无"), code="invalid_arguments")
        return actors[0], args.role_id
    actor = find_entity(data, args.actor_id)
    if actor["kind"] != "actor":
        raise ToolError(f"{label(actor)}不是演员", code="invalid_arguments")
    roles = [e for e in data["entities"] if e["kind"] == "role" and actor["id"] in (e.get("actor_ids") or [])]
    if len(roles) != 1:
        # 审核分类以「演员·角色」为单位，只按演员筛选会漏掉其他角色。
        raise ToolError(f"{label(actor)}饰演 {len(roles)} 个角色，请同时给出 role_id："
                        + ("、".join(f"{r['name']}({r['id']})" for r in roles) or "无"), code="invalid_arguments")
    return actor["id"], roles[0]["id"]


def shot_line(shot: dict, lookup: dict[str, str], sources: dict[str, str]) -> str:
    status = shot_status(shot.get("counts") or {})
    parts = [f"{shot['shot_id']} | {sources.get(shot['source_id'], shot['source_id'])} "
             f"{shot['start']:.1f}–{shot['end']:.1f}s | {STATUS_LABELS[status]}"]
    if shot.get("confirmed_people"):
        parts.append("已确认 " + "、".join(pair_name(lookup, p["actor_id"], p["role_id"])
                                         for p in shot["confirmed_people"]))
    candidate = shot.get("candidate")
    if candidate and status == "candidate":
        score = candidate.get("score")
        parts.append(f"首位候选 {pair_name(lookup, candidate['actor_id'], candidate['role_id'])}"
                     + (f" {score:.3f}" if isinstance(score, (int, float)) else ""))
    recognized = [p for p in shot.get("recognized_people") or []
                  if (p["actor_id"], p["role_id"]) != ((candidate or {}).get("actor_id"), (candidate or {}).get("role_id"))]
    if recognized and status == "candidate":
        parts.append("其他识别 " + "、".join(pair_name(lookup, p["actor_id"], p["role_id"]) for p in recognized[:5]))
    return " | ".join(parts)


async def list_review_shots(args: ListReviewShots, ctx: ToolContext) -> ToolOutput:
    data = await load_catalogue(ctx)
    if args.source_id:
        source_of(data, args.source_id)
    actor_id, role_id = identity_filter(args, data)
    result = check(await ctx.client.get(
        "/api/persons/review/shots", source_id=args.source_id, status=args.status, actor_id=actor_id,
        role_id=role_id, no_candidate=args.no_candidate or None, compact=True, offset=args.offset,
        limit=args.limit), "读取出镜审核分镜")
    lookup = names(data)
    sources = {s["id"]: s["name"] for s in data.get("sources", [])}
    items = result["items"]
    lines = [f"共 {result['total']} 个分镜（本页 {args.offset + 1 if items else 0}–{args.offset + len(items)}；"
             "分镜ID | 素材 区间 | 状态 | 身份）："]
    lines += [shot_line(shot, lookup, sources) for shot in items] or ["（没有符合条件的分镜）"]
    if result["total"] > args.offset + len(items):
        lines.append(f"（还有更多，可用 offset={args.offset + len(items)} 翻页）")
    groups = [f for f in result.get("filters") or [] if f.get("count")]
    if groups and not actor_id and not args.no_candidate:
        lines.append("按身份分布：" + "；".join(
            f"{pair_name(lookup, g['actor_id'], g['role_id']) if g['actor_id'] else '无候选'} {g['count']}"
            for g in groups[:20]))
    lines.append("（审核用 review_shot：selections 列出镜头中出现的演员·角色，空列表表示整镜排除）")
    value = {"items": [{k: shot.get(k) for k in ("shot_id", "source_id", "start", "end", "counts", "candidate",
                                                   "confirmed_people", "recognized_people", "image_url")}
                       | {"status": shot_status(shot.get("counts") or {})} for shot in items],
             "total": result["total"], "offset": args.offset, "filters": groups}
    media = [media_item("image", shot["image_url"],
                        f"{sources.get(shot['source_id'], shot['source_id'])} {shot['start']:.1f}s", collapsed=False)
             for shot in items]
    return ToolOutput(value, text="\n".join(lines), meta={"kind": "review_shots", "total": result["total"]},
                      focus=persons_focus("processing", source_id=args.source_id), media=clean_media(media))


# ---------- review_shot ----------

class Selection(StrictModel):
    actor_id: str = Field(min_length=1, description="演员 ID")
    role_id: str = Field(min_length=1, description="该演员在本素材作品中饰演的角色 ID（必填）")


class ReviewShot(StrictModel):
    source_id: str = Field(min_length=1, description="素材 ID")
    shot_id: str = Field(min_length=1, description="分镜 ID（list_review_shots 返回的 shot_id）")
    selections: list[Selection] = Field(
        max_length=50, description="镜头中出现的全部演员·角色（整体替换）；空列表表示整镜排除")


async def review_target(args: ReviewShot, ctx: ToolContext) -> tuple[dict, dict, list[Selection]]:
    data = await load_catalogue(ctx)
    source = source_of(data, args.source_id)
    unique = list({(s.actor_id, s.role_id): s for s in args.selections}.values())
    for selection in unique:
        checked_pair(data, source.get("work_id"), selection.actor_id, selection.role_id)
    return data, source, unique


async def preview_review_shot(args: ReviewShot, ctx: ToolContext) -> Preview:
    data, source, selections = await review_target(args, ctx)
    lookup = names(data)
    current = await ctx.client.get(f"/api/persons/review/shots/{args.shot_id}", source_id=args.source_id)
    if current["ok"]:
        shot = current["data"]
        before_status = STATUS_LABELS[shot_status(shot.get("counts") or {})]
        before = "、".join(pair_name(lookup, p["actor_id"], p["role_id"]) for p in shot.get("confirmed_people") or [])
        before = f"{before_status}" + (f"：{before}" if before else "")
        where = f"{source['name']} {shot['start']:.1f}–{shot['end']:.1f}s"
    elif current["status"] == 404:
        before, where = "尚无人物处理结果", source["name"]
    else:
        check(current, "读取出镜审核分镜")
    after = ("整镜排除" if not selections else
             "已接受：" + "、".join(pair_name(lookup, s.actor_id, s.role_id) for s in selections))
    warnings = []
    if selections:
        warnings.append("勾选的每个身份生成一条覆盖整镜的人工证据；该分镜其余证据驳回留档")
    else:
        warnings.append("整镜排除：该分镜全部证据驳回留档，人物检索不再命中该镜，重新处理人物也不会恢复")
    warnings.append("审核结果要在 publish_persons 发布后才进入人物检索")
    return Preview(f"审核分镜 {args.shot_id}（{where}）", [{"label": "出镜身份", "before": before, "after": after}],
                   warnings=warnings, focus=persons_focus("processing", source_id=args.source_id))


async def review_shot(args: ReviewShot, ctx: ToolContext) -> ToolOutput:
    data, _, selections = await review_target(args, ctx)
    body = {"source_id": args.source_id, "shot_id": args.shot_id,
            "selections": [s.model_dump() for s in selections]}
    result = check(await ctx.client.post("/api/persons/review/shot", body), "提交出镜审核")
    lookup = names(data)
    text = (f"已整镜排除分镜 {args.shot_id}（驳回 {result.get('rejected', 0)} 条证据）。" if not selections else
            f"已确认分镜 {args.shot_id} 出现："
            + "、".join(pair_name(lookup, s.actor_id, s.role_id) for s in selections)
            + f"（驳回其余 {result.get('rejected', 0)} 条证据）。")
    return ToolOutput(result, text=text + "发布人物检索后生效（publish_persons）。",
                      mutation={**PERSONS, "source_id": args.source_id, "shot_id": args.shot_id},
                      focus=persons_focus("processing", source_id=args.source_id))


# ---------- 检索组合 ----------

async def load_releases(ctx: ToolContext) -> dict:
    return check(await ctx.client.get("/api/persons/releases"), "读取检索组合")


def resolve_release(releases: list[dict], ref: str) -> dict:
    token = str(ref).strip().lower()
    for release in releases:
        if release["id"].lower() == token:
            return release
    if len(token) < MIN_PREFIX:
        raise ToolError(f"检索组合 ID 前缀至少 {MIN_PREFIX} 位：{ref}；可先用 list_search_releases 查看",
                        code="invalid_arguments")
    hits = [r for r in releases if r["id"].lower().startswith(token)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ToolError(f"检索组合前缀 {ref} 有歧义：" + "、".join(r["id"] for r in hits[:10]),
                        code="ambiguous_release", value={"candidates": [r["id"] for r in hits[:10]]})
    raise ToolError(f"检索组合不存在（或不在最近 30 条中）：{ref}；可先用 list_search_releases 查看",
                    code="missing_release")


def active_release(data: dict) -> str | None:
    return (data.get("active") or {}).get("release_id")


class ListReleases(StrictModel):
    pass


async def list_search_releases(args: ListReleases, ctx: ToolContext) -> ToolOutput:
    data = await load_releases(ctx)
    active = data.get("active") or {}
    lines = [f"当前生效：检索组合 {short(active.get('release_id')) or '无'}，内容索引 {short(active.get('content_id')) or '无'}，"
             f"人物版本 {short(active.get('person_id')) or '未发布'}",
             f"最近 {len(data['releases'])} 个检索组合（短ID | 内容索引 | 人物版本 | 创建时间）："]
    lines += [f"{short(r['id'])} | {short(r['content_id'])} | {short(r.get('person_id')) or '未发布'} | "
              f"{r.get('created_at')}{' | 【当前生效】' if r['id'] == active.get('release_id') else ''}"
              for r in data["releases"]] or ["（还没有检索组合；可用 publish_persons 发布）"]
    rows = [{**r, "short_id": short(r["id"]), "active": r["id"] == active.get("release_id")} for r in data["releases"]]
    return ToolOutput({"active": data.get("active"), "releases": rows}, text="\n".join(lines), focus=index_focus())


class ChangeRelease(StrictModel):
    release_id: str = Field(min_length=1, description="检索组合 ID 或至少 6 位前缀（list_search_releases）")
    action: Literal["activate", "delete"] = Field(description="activate 恢复为当前生效；delete 删除组合记录")


async def release_target(args: ChangeRelease, ctx: ToolContext) -> tuple[dict, dict]:
    data = await load_releases(ctx)
    return data, resolve_release(data["releases"], args.release_id)


async def preview_change_release(args: ChangeRelease, ctx: ToolContext) -> Preview:
    data, release = await release_target(args, ctx)
    active = data.get("active") or {}
    is_active = release["id"] == active.get("release_id")
    person = short(release.get("person_id")) or "未发布"
    if args.action == "delete":
        if is_active:
            raise ToolError(f"不能删除检索组合 {release['id']}：{ACTIVE_RELEASE_PROTECTED}",
                            code="release_active", value={"release_id": release["id"]})
        return Preview(f"删除检索组合 {release['id']}",
                       [{"label": "检索组合", "before": f"{short(release['id'])}（内容索引 {short(release['content_id'])}"
                                                       f" + 人物版本 {person}）", "after": "删除"}],
                       warnings=["只删除组合记录；其人物快照若不再被其他组合引用会一并删除，内容索引与文件不受影响",
                                 "删除后无法再恢复到这个组合，除非重新发布人物检索"], focus=index_focus())
    warnings = []
    if is_active:
        warnings.append("该组合已是当前生效组合；恢复只会重新校验并保持不变")
    if release["content_id"] != active.get("content_id"):
        versions = await load_versions(ctx)
        target = next((v for v in versions["versions"] if v["id"] == release["content_id"]), None)
        status = target["status"] if target else "不存在"
        warnings.append(f"会同时把活动内容索引切换为 {short(release['content_id'])}（状态 {status}）；"
                        "不会重新校验嵌入空间，需要完整校验时改用 activate_index")
        if status != "ready":
            warnings.append("该内容索引未就绪，服务端会拒绝恢复")
    warnings.append("恢复立即影响检索结果；创作召回版本记录了检索组合，切换后下次构建会据此判定召回是否需要重建")
    changes = [{"label": "检索组合", "before": short(active.get("release_id")) or "无", "after": short(release["id"])},
               {"label": "内容索引", "before": short(active.get("content_id")) or "无",
                "after": short(release["content_id"])},
               {"label": "人物版本", "before": short(active.get("person_id")) or "未发布", "after": person}]
    return Preview(f"把当前生效的检索组合恢复为 {release['id']}", changes, warnings=warnings, focus=index_focus())


async def change_search_release(args: ChangeRelease, ctx: ToolContext) -> ToolOutput:
    _, release = await release_target(args, ctx)
    # 是否为当前生效组合、内容索引是否就绪等以服务端为准：拒绝时原样转述其中文原因。
    if args.action == "delete":
        result = check(await ctx.client.delete(f"/api/persons/releases/{release['id']}"), "删除检索组合")
        text = f"已删除检索组合 {release['id']}。"
    else:
        result = check(await ctx.client.post(f"/api/persons/releases/{release['id']}/activate"), "恢复检索组合")
        text = (f"已把当前生效的检索组合恢复为 {release['id']}（内容索引 {short(release['content_id'])}，"
                f"人物版本 {short(release.get('person_id')) or '未发布'}）。")
    return ToolOutput(result, text=text, mutation={"domain": RELEASE_DOMAIN, "release_id": release["id"],
                                                   **({"deleted": True} if args.action == "delete" else {})},
                      focus=index_focus())


# ---------- publish_persons ----------

class PublishPersons(StrictModel):
    content_id: str | None = Field(default=None,
                                   description="内容索引 ID 或至少 6 位前缀；省略使用当前活动内容索引")


async def publish_target(args: PublishPersons, ctx: ToolContext) -> dict | None:
    if args.content_id is None:
        return None
    return resolve_index((await load_versions(ctx))["versions"], args.content_id)


async def preview_publish_persons(args: PublishPersons, ctx: ToolContext) -> Preview:
    target = await publish_target(args, ctx)
    data = await load_releases(ctx)
    active = data.get("active") or {}
    warnings = []
    content = target["id"] if target else active.get("content_id")
    if target is None and not content:
        warnings.append("当前没有活动内容索引，服务端会拒绝发布（请先发布内容索引）")
    if target is not None and target["status"] != "ready":
        warnings.append(f"内容索引 {short(target['id'])} 状态为 {target['status']}，服务端会拒绝发布")
    pending = await ctx.client.get("/api/persons/review/shots", status="candidate", compact=True, limit=1)
    if pending["ok"] and pending["data"]["total"]:
        warnings.append(f"仍有 {pending['data']['total']} 个待确认分镜；只有已接受的出镜证据会进入人物检索")
    warnings.append("发布会生成当前人物库快照并立即切换当前生效的检索组合；"
                    "创作召回版本记录了检索组合，下次构建会据此判定召回是否需要重建")
    if content and content != active.get("content_id"):
        warnings.append(f"会同时把活动内容索引切换为 {short(content)}")
    changes = [{"label": "检索组合", "before": short(active.get("release_id")) or "无",
                "after": "新组合：内容索引 + 当前人物库快照（内容未变时沿用已有组合）"},
               {"label": "内容索引", "before": short(active.get("content_id")) or "无", "after": short(content) or "无"}]
    return Preview("发布人物检索并切换当前生效的检索组合", changes, warnings=warnings, focus=index_focus())


async def publish_persons(args: PublishPersons, ctx: ToolContext) -> ToolOutput:
    target = await publish_target(args, ctx)
    # 内容索引未就绪、仍有未完成片段、处理锁被占用等前置条件以服务端为准，原样转述。
    result = check(await ctx.client.post("/api/persons/publish",
                                         {"content_id": target["id"]} if target else {}), "发布人物检索")
    text = (f"已发布人物检索：当前生效检索组合 {result['release_id']}（内容索引 {short(result['content_id'])}，"
            f"人物版本 {short(result['person_version'])}）。")
    return ToolOutput(result, text=text, mutation={"domain": RELEASE_DOMAIN, "release_id": result["release_id"]},
                      focus=index_focus())


# ---------- 演员表导入 ----------

class ImportCast(StrictModel):
    attachments: list[str] = Field(min_length=1, max_length=12,
                                   description="演员表截图附件 attachment://…（1–12 张）")
    parse: bool = Field(default=True, description="创建草稿后是否调用分析模型自动拆分条目")

    @model_validator(mode="after")
    def refs(self):
        if any(not a.startswith("attachment://") for a in self.attachments):
            raise ValueError("attachments 只接受附件引用 attachment://…")
        if len(set(self.attachments)) != len(self.attachments):
            raise ValueError("attachments 有重复引用")
        return self


async def preview_import_cast(args: ImportCast, ctx: ToolContext) -> Preview:
    changes, warnings = [], []
    for index, ref in enumerate(args.attachments, 1):
        meta = await attachment_meta(ctx, ref)
        rows = attachment_rows(meta)
        changes.append({"label": f"截图 {index}", "after": " / ".join(r["after"] for r in rows)})
        ctype = str(meta.get("content_type") or "")
        if ctype and not ctype.startswith("image/"):
            warnings.append(f"截图 {index} 类型为 {ctype}，可能不是图片，服务端会拒绝")
    warnings.append("只创建导入草稿" + ("并调用分析模型自动拆分条目" if args.parse else "")
                    + "；不会写入人物库，需核对后用 confirm_cast_import 确认")
    return Preview(f"从 {len(args.attachments)} 张演员表截图创建导入草稿", changes, warnings=warnings,
                   focus=persons_focus("cast"))


def entry_line(index: int, entry: dict, numbers: dict[str, int]) -> str:
    actor = entry.get("actor_name") or entry.get("actor_id") or "（未命名）"
    if entry.get("foreign_name"):
        actor += f"（{entry['foreign_name']}）"
    role = entry.get("role_name") or entry.get("role_id") or "无角色"
    box = f"照片框 {entry['bbox']}" if entry.get("bbox") else "无照片框"
    return f"#{index} {actor} → {role} | 原图 {numbers.get(entry.get('image_id'), '?')} | {box}"


def draft_text(draft: dict) -> list[str]:
    numbers = {im["id"]: n for n, im in enumerate(draft["images"], 1)}
    lines = [f"演员表草稿 {draft['id']}（{draft['status']}）：{len(draft['images'])} 张原图，"
             f"{len(draft.get('entries') or [])} 个条目"]
    lines += [entry_line(n, e, numbers) for n, e in enumerate(draft.get("entries") or [], 1)]
    failed = [numbers.get(i, "?") for i, s in (draft.get("parse_state") or {}).items() if s.get("status") == "failed"]
    if failed:
        lines.append("自动拆分失败的原图：" + "、".join(str(n) for n in failed))
    if draft.get("warnings"):
        lines.append("警告：" + "；".join(str(w) for w in draft["warnings"][:20]))
    return lines


def draft_media(draft: dict) -> list[dict]:
    return clean_media(media_item("image", f"{im['url']}?thumbnail=true", f"演员表原图 {n}", collapsed=False)
                       for n, im in enumerate(draft["images"], 1))


async def import_cast(args: ImportCast, ctx: ToolContext) -> ToolOutput:
    draft = hinted(await ctx.client.upload("/api/persons/imports", files={"files": list(args.attachments)}),
                   "创建演员表草稿", {})
    if args.parse:
        draft = check(await ctx.client.post(f"/api/persons/imports/{draft['id']}/parse"), "自动拆分演员表")
    lines = draft_text(draft)
    lines.append("请核对条目（演员名、角色名、照片框），再用 confirm_cast_import(import_id, work_id) 确认；"
                 "可用 entries 整体替换或 exclude 去掉条目。")
    value = {"import_id": draft["id"], "status": draft["status"], "entries": draft.get("entries") or [],
             "warnings": draft.get("warnings") or [],
             "images": [{"id": im["id"], "url": im["url"], "width": im.get("width"), "height": im.get("height")}
                        for im in draft["images"]]}
    return ToolOutput(value, text="\n".join(lines), mutation={**PERSONS, "import_id": draft["id"]},
                      focus=persons_focus("cast", import_id=draft["id"]), media=draft_media(draft))


class CastEntryArgs(StrictModel):
    image_id: str = Field(min_length=1, description="条目所在原图 ID（import_cast 返回的 images[].id）")
    actor_name: str = Field(default="", max_length=200, description="演员名；关联已有演员时可留空")
    foreign_name: str = Field(default="", max_length=200, description="外文名（作为演员别名）")
    role_name: str = Field(default="", max_length=200, description="角色名；空串表示无角色")
    actor_id: str | None = Field(default=None, description="关联已有演员 ID（不再按姓名匹配）")
    role_id: str | None = Field(default=None, description="关联已有角色 ID")
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4,
                                     description="原图像素坐标照片框 [x1,y1,x2,y2]；给出时裁剪为演员参考图并质检")

    @model_validator(mode="after")
    def identity(self):
        if not self.actor_name.strip() and not self.actor_id:
            raise ValueError("请填写演员名或关联已有演员")
        return self


class ConfirmCast(StrictModel):
    import_id: str = Field(min_length=1, description="演员表草稿 ID（import_cast 返回）")
    work_id: str = Field(min_length=1, description="导入到的作品 ID（list_persons(kind=work)）")
    entries: list[CastEntryArgs] | None = Field(default=None, max_length=200,
                                                description="整体替换草稿条目；省略则使用草稿当前条目")
    exclude: list[int] = Field(default_factory=list, max_length=200,
                               description="从草稿条目中去掉的条目序号（从 1 开始；不能与 entries 同用）")

    @model_validator(mode="after")
    def exclusive(self):
        if self.entries is not None and self.exclude:
            raise ValueError("entries 与 exclude 不能同时使用")
        return self


async def cast_plan(args: ConfirmCast, ctx: ToolContext) -> tuple[dict, dict, dict, list[dict]]:
    draft = check(await ctx.client.get(f"/api/persons/imports/{args.import_id}"), "读取演员表草稿")
    if draft["status"] != "draft":
        raise ToolError("演员表已确认；请在人物库追加或修改参考图（add_person_reference）", code="import_confirmed")
    data = await load_catalogue(ctx)
    work = find_entity(data, args.work_id)
    if work["kind"] != "work":
        raise ToolError(f"{label(work)}不是作品：演员表只能导入到作品", code="invalid_arguments")
    if args.entries is not None:
        entries = [e.model_dump() for e in args.entries]
    else:
        drafted = draft.get("entries") or []
        wrong = [n for n in args.exclude if not 1 <= n <= len(drafted)]
        if wrong:
            raise ToolError(f"条目序号超出范围：{wrong}（草稿共 {len(drafted)} 个条目）", code="invalid_arguments")
        entries = []
        fields = set(CastEntryArgs.model_fields)
        for n, entry in enumerate(drafted, 1):
            if n in set(args.exclude):
                continue
            try:
                # 草稿条目可能带旧字段（页面同样丢弃）；只提交确认接口认识的字段。
                entries.append(CastEntryArgs.model_validate({k: v for k, v in entry.items() if k in fields}).model_dump())
            except ValueError as exc:
                raise ToolError(f"草稿条目 #{n} 无效：{exc}；请用 entries 修正或用 exclude 去掉该条目",
                                code="invalid_arguments") from exc
    if not entries:
        raise ToolError("没有可确认的条目：请先用 import_cast 自动拆分，或通过 entries 给出条目", code="invalid_arguments")
    images = {im["id"] for im in draft["images"]}
    for n, entry in enumerate(entries, 1):
        if entry["image_id"] not in images:
            raise ToolError(f"条目 {n} 的原图 {entry['image_id']} 不属于此草稿", code="invalid_arguments")
        for key, kind in (("actor_id", "actor"), ("role_id", "role")):
            if entry.get(key) and find_entity(data, entry[key])["kind"] != kind:
                raise ToolError(f"条目 {n} 的 {key} 必须是{'演员' if kind == 'actor' else '角色'} ID",
                                code="invalid_arguments")
    return draft, data, work, entries


def entry_effect(entry: dict, data: dict, work_id: str) -> tuple[str, str, list[str]]:
    """按服务端确认规则预估：演员按姓名或外文别名复用，角色按作品内同名复用。"""
    lookup, warnings = names(data), []
    if entry.get("actor_id"):
        actor = f"关联「{lookup.get(entry['actor_id'], entry['actor_id'])}」"
    else:
        matches = [e for e in data["entities"] if e["kind"] == "actor" and (
            e["name"] == entry["actor_name"].strip()
            or (entry.get("foreign_name") and entry["foreign_name"].strip() in (e.get("aliases") or [])))]
        actor = f"复用「{matches[0]['name']}」" if len(matches) == 1 else f"新建「{entry['actor_name']}」"
        if len(matches) > 1:
            actor = f"「{entry['actor_name']}」有 {len(matches)} 个同名演员"
            warnings.append(f"演员「{entry['actor_name']}」有歧义，服务端会拒绝：请给该条目指定 actor_id")
    if entry.get("role_id"):
        role = f"关联「{lookup.get(entry['role_id'], entry['role_id'])}」"
    elif entry.get("role_name", "").strip():
        exists = any(e["kind"] == "role" and e["name"] == entry["role_name"].strip()
                     and work_id in (e.get("work_ids") or []) for e in data["entities"])
        role = f"{'复用' if exists else '新建'}「{entry['role_name'].strip()}」"
    else:
        role = "无角色"
    return actor, role, warnings


async def preview_confirm_cast(args: ConfirmCast, ctx: ToolContext) -> Preview:
    draft, data, work, entries = await cast_plan(args, ctx)
    numbers = {im["id"]: n for n, im in enumerate(draft["images"], 1)}
    changes, warnings = [], []
    for n, entry in enumerate(entries, 1):
        actor, role, extra = entry_effect(entry, data, work["id"])
        warnings += extra
        changes.append({"label": f"条目 {n}", "after": f"演员 {actor} · 角色 {role} · "
                        + (f"原图 {numbers.get(entry['image_id'], '?')} 裁剪参考图" if entry.get("bbox") else "无参考图")})
    if any(e.get("bbox") for e in entries):
        warnings.append("带照片框的条目会裁剪为演员参考图并调用人物服务质检；任一条目不合格整批拒绝")
    warnings.append("同名演员+角色且未关联已有身份的重复条目只保留一个；确认后草稿不可再修改")
    if args.exclude:
        warnings.append("已去掉草稿条目：" + "、".join(f"#{n}" for n in sorted(set(args.exclude))))
    return Preview(f"确认演员表 {draft['id']}：{len(entries)} 个条目导入作品「{work['name']}」", changes,
                   warnings=warnings, focus=persons_focus("cast", import_id=draft["id"]))


async def confirm_cast_import(args: ConfirmCast, ctx: ToolContext) -> ToolOutput:
    draft, _, work, entries = await cast_plan(args, ctx)
    result = hinted(await ctx.client.post(f"/api/persons/imports/{draft['id']}/confirm",
                                          {"work_id": work["id"], "entries": entries}), "确认演员表", CAST_HINTS)
    lookup = names(await load_catalogue(ctx))
    confirmed = result.get("confirmed") or []
    lines = [f"已确认演员表 {draft['id']}，导入作品「{work['name']}」{len(confirmed)} 个条目："]
    lines += [f"- {pair_name(lookup, c['actor_id'], c.get('role_id'))}"
              + (f"（参考图 {c['reference_id']}）" if c.get("reference_id") else "") for c in confirmed]
    if result.get("skipped_duplicates"):
        lines.append("跳过重复条目：" + "、".join(f"{d['actor_name']}·{d['role_name'] or '无角色'}"
                                             for d in result["skipped_duplicates"]))
    if result.get("ignored_existing"):
        lines.append(f"{result['ignored_existing']} 张照片与已有参考图相同，沿用已有参考图。")
    lines.append("之后可对相关素材 start_source_task(work=persons) 处理人物，再审核并发布。")
    return ToolOutput(result, text="\n".join(lines), mutation={**PERSONS, "import_id": draft["id"]},
                      focus=persons_focus("catalogue"))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_review_shots", "persons",
                 "列出人物出镜审核分镜：可按素材、状态（待确认/已接受/已排除）、演员·角色、是否无候选筛选并分页，"
                 "返回首位候选、已确认身份与证据图。",
                 ListReviewShots, "read", list_review_shots),
        ToolSpec("review_shot", "persons",
                 "审核一个分镜：selections 给出镜头中出现的全部演员·角色（每项必须带角色，且演员须出演该作品角色）；"
                 "空列表表示整镜排除。",
                 ReviewShot, "write", review_shot, preview_review_shot),
        ToolSpec("publish_persons", "persons",
                 "发布人物检索：把当前人物库与已接受的出镜证据生成快照，并切换当前生效的检索组合。",
                 PublishPersons, "danger", publish_persons, preview_publish_persons),
        ToolSpec("list_search_releases", "persons",
                 "列出最近的检索组合（内容索引 + 人物版本）与当前生效组合。",
                 ListReleases, "read", list_search_releases),
        ToolSpec("change_search_release", "persons",
                 "切换或删除检索组合：activate 恢复为当前生效（同时切换其内容索引），delete 删除组合记录"
                 "（当前生效组合不可删除）。",
                 ChangeRelease, "danger", change_search_release, preview_change_release),
        ToolSpec("import_cast", "persons",
                 "用 1–12 张演员表截图附件（attachment://）创建导入草稿并自动拆分，返回条目（演员、角色、照片框）与警告；"
                 "不写入人物库。",
                 ImportCast, "write", import_cast, preview_import_cast),
        ToolSpec("confirm_cast_import", "persons",
                 "确认演员表草稿导入到某个作品：按姓名复用或新建演员与角色，带照片框的条目裁剪为参考图（会质检）；"
                 "可用 entries 整体替换条目或 exclude 去掉条目。",
                 ConfirmCast, "write", confirm_cast_import, preview_confirm_cast),
    ]
