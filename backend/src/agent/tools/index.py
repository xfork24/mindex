"""索引领域工具：内容索引版本列表、一致性检查、激活与回退。

- 全部经回环调用 ``/api/indexes``；激活的锁、一致性检查、空间与预处理校验都在服务端完成，
  与 ``mindex indexes activate`` 一致。
- ``index_id`` 接受完整 ID 或至少 6 位的唯一前缀（界面与列表显示前 8 位）；有歧义时列出候选，不猜测。
- 激活预览只做名义比对（目标索引记录的嵌入模型名 vs 当前嵌入身份），完整身份与能力门槛由服务端校验。
"""

from __future__ import annotations

from pydantic import Field

from mindex.agent.ids import short
from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check
from mindex.shared.config import StrictModel

MIN_PREFIX = 6
MAX_ISSUES = 20
# 激活会切换检索组合：设置页「索引版本」区块与它读取同一来源，刷新设置页即可同步。
MUTATION_DOMAIN = "settings"


def index_focus() -> dict:
    return {"page": "settings", "section": "releases"}


async def load_versions(ctx: ToolContext) -> dict:
    return check(await ctx.client.get("/api/indexes"), "读取索引版本")


def active_id(data: dict) -> str | None:
    return (data.get("active") or {}).get("content_id")


def resolve_index(versions: list[dict], ref: str) -> dict:
    token = str(ref).strip().lower()
    for version in versions:
        if version["id"].lower() == token:
            return version
    if len(token) < MIN_PREFIX:
        raise ToolError(f"索引版本 ID 前缀至少 {MIN_PREFIX} 位：{ref}；可先用 list_index_versions 查看",
                        code="invalid_arguments")
    hits = [v for v in versions if v["id"].lower().startswith(token)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ToolError(f"索引版本前缀 {ref} 有歧义：" + "、".join(v["id"] for v in hits[:10]),
                        code="ambiguous_index", value={"candidates": [v["id"] for v in hits[:10]]})
    raise ToolError(f"索引版本不存在：{ref}；可先用 list_index_versions 查看", code="missing_index")


def version_line(version: dict, active: str | None) -> str:
    return (f"{short(version['id'])} | {version['status']} | {version.get('mode') or '—'} | "
            f"格式{version.get('format')} | {version['created_at']} | {version.get('space_model') or '未记录'}"
            f"{' | 【活动】' if version['id'] == active else ''}")


class ListVersions(StrictModel):
    limit: int = Field(default=20, ge=1, le=100, description="最多列出多少个版本（按创建时间倒序）")


async def list_index_versions(args: ListVersions, ctx: ToolContext) -> ToolOutput:
    data = await load_versions(ctx)
    active = active_id(data)
    versions = data["versions"]
    rows = [{**v, "short_id": short(v["id"]), "active": v["id"] == active} for v in versions[:args.limit]]
    lines = [f"当前活动索引：{active or '无'}；检索组合：{(data.get('active') or {}).get('release_id') or '无'}",
             f"共 {len(versions)} 个版本（短ID | 状态 | 模式 | 格式 | 创建时间 | 空间模型）："]
    lines += [version_line(v, active) for v in rows] or ["（还没有索引版本）"]
    if len(versions) > args.limit:
        lines.append(f"（只列出最近 {args.limit} 个）")
    return ToolOutput({"active": data.get("active"), "versions": rows, "total": len(versions)},
                      text="\n".join(lines), focus=index_focus())


class CheckIndex(StrictModel):
    index_id: str | None = Field(default=None, description="索引版本 ID 或至少 6 位前缀；省略检查当前活动索引")


async def target_version(ctx: ToolContext, ref: str | None) -> tuple[dict, dict]:
    data = await load_versions(ctx)
    if ref is None:
        active = active_id(data)
        if not active:
            raise ToolError("当前没有活动索引；请指定 index_id", code="missing_index")
        ref = active
    return data, resolve_index(data["versions"], ref)


def issues_text(issues: list[str]) -> str:
    lines = [f"- {issue}" for issue in issues[:MAX_ISSUES]]
    if len(issues) > MAX_ISSUES:
        lines.append(f"（另有 {len(issues) - MAX_ISSUES} 条问题未列出）")
    return "\n".join(lines)


async def check_index(args: CheckIndex, ctx: ToolContext) -> ToolOutput:
    _, version = await target_version(ctx, args.index_id)
    result = check(await ctx.client.get(f"/api/indexes/{version['id']}/check"), "检查索引")
    issues = result["issues"]
    text = (f"索引 {version['id']} 一致性检查通过。" if result["ok"]
            else f"索引 {version['id']} 有 {len(issues)} 个问题：\n{issues_text(issues)}")
    return ToolOutput({"index_id": version["id"], "ok": result["ok"], "issues": issues[:MAX_ISSUES],
                       "issue_count": len(issues)}, text=text, focus=index_focus())


class ActivateIndex(StrictModel):
    index_id: str = Field(description="要发布或回退到的索引版本 ID，或至少 6 位前缀")


async def activation_target(args: ActivateIndex, ctx: ToolContext) -> tuple[dict, dict]:
    data, version = await target_version(ctx, args.index_id)
    if version["status"] != "ready":
        raise ToolError(f"索引版本 {version['id']} 状态为 {version['status']}，只有 ready 的版本可以激活",
                        code="index_not_ready")
    return data, version


async def space_warning(ctx: ToolContext, version: dict, is_active: bool) -> tuple[str, str | None]:
    """返回（空间比对说明, 警告或 None）。"""
    target = version.get("space_model")
    if not target:
        return "目标索引未记录嵌入空间（旧格式）", None
    status = await ctx.client.get("/api/settings/embedding-status")
    data = status["data"] if status["ok"] else {}
    identity = data.get("identity")
    if not identity:
        reason = data.get("error") or (status.get("error") if not status["ok"] else "嵌入服务未返回身份")
        return f"无法确认（{reason}）", f"无法确认当前嵌入空间：{reason}；空间不一致时激活会被拒绝"
    current = identity.get("model")
    matched = data.get("matches_index") if is_active and data.get("matches_index") is not None else current == target
    if matched:
        return f"一致（{current}）", None
    return (f"不一致：当前 {current}，目标 {target}",
            f"当前嵌入模型 {current} 与目标索引空间 {target} 不一致，激活会被拒绝；需先切换嵌入模型档案")


async def release_after(ctx: ToolContext, index_id: str) -> str:
    data = check(await ctx.client.get("/api/persons/releases"), "读取检索组合")
    release = next((r for r in data["releases"] if r["content_id"] == index_id), None)
    if release:
        person = release.get("person_id")
        return f"{short(release['id'])}（人物版本 {short(person) if person else '未发布'}）"
    person = (data.get("active") or {}).get("person_id")
    return f"新建组合：该索引 + 当前人物版本 {short(person) if person else '未发布'}"


async def preview_activate_index(args: ActivateIndex, ctx: ToolContext) -> Preview:
    data, version = await activation_target(args, ctx)
    current = active_id(data)
    is_active = version["id"] == current
    space, warning = await space_warning(ctx, version, is_active)
    release = await release_after(ctx, version["id"])
    current_release = (data.get("active") or {}).get("release_id")
    changes = [
        {"label": "内容索引", "before": short(current) or "无", "after": short(version["id"])},
        {"label": "模式/格式", "after": f"{version.get('mode') or '—'} / 格式{version.get('format')}"},
        {"label": "嵌入空间", "after": space},
        {"label": "检索组合", "before": short(current_release) or "无", "after": release},
    ]
    warnings = ["激活前服务端会再做一致性检查、空间与能力门槛校验和预处理配置校验，任一不通过都不会改变活动索引",
                "激活立即影响检索；创作召回版本记录了内容索引与检索组合，切换后下次构建会据此判定召回是否需要重建"]
    if is_active:
        warnings.insert(0, "该版本已是当前活动索引；激活只会重新校验，并切换到它最近的检索组合")
    if warning:
        warnings.insert(0, warning)
    return Preview(f"把内容索引切换到 {version['id']}（{version['created_at']} 创建）", changes, warnings=warnings,
                   value={"index_id": version["id"], "space": space, "release": release}, focus=index_focus())


async def activate_index(args: ActivateIndex, ctx: ToolContext) -> ToolOutput:
    _, version = await activation_target(args, ctx)
    result = await ctx.client.post(f"/api/indexes/{version['id']}/activate")
    if not result["ok"] and result["status"] == 422:
        detail = await ctx.client.get(f"/api/indexes/{version['id']}/check")
        issues = detail["data"]["issues"] if detail["ok"] else []
        if issues:
            raise ToolError(f"激活失败（HTTP 422）：{result['error']}，活动索引未改变：\n{issues_text(issues)}",
                            code="http_422", value={"status": 422, "error": result["error"],
                                                    "issues": issues[:MAX_ISSUES]})
    data = check(result, "激活索引")
    text = (f"已激活内容索引 {data['active']}，当前检索组合 {data.get('release_id') or '无'}；"
            "检索预热在后台进行。")
    return ToolOutput(data, text=text, mutation={"domain": MUTATION_DOMAIN, "index_id": data["active"]},
                      focus=index_focus())


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_index_versions", "index",
                 "列出内容索引版本（短ID、状态、模式、格式、创建时间、嵌入空间模型、是否活动）与当前检索组合。",
                 ListVersions, "read", list_index_versions),
        ToolSpec("check_index", "index", "对某个内容索引版本做一致性检查（向量与元数据、片段完成度、文件引用）；省略 index_id 检查活动索引。",
                 CheckIndex, "read", check_index),
        ToolSpec("activate_index", "index",
                 "发布或回退到指定内容索引版本（必须 ready、检查通过、嵌入空间与预处理配置一致），同时切换检索组合。",
                 ActivateIndex, "danger", activate_index, preview_activate_index),
    ]
