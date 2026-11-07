"""素材组与音乐组工具，共享页面 REST 与既有 library 权限；自动导出到 MCP。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agent.registry import Preview, ToolContext, ToolOutput, ToolSpec
from agent.tools.common import check
from shared.config import StrictModel

RULES = "多选组成员取并集，与手动素材、任务范围及其他筛选取交集；不选组不限，空组无候选；删除组不删除媒体。"


class ListGroups(StrictModel):
    type: Literal["source", "music"]


class GroupId(StrictModel):
    group_id: str = Field(min_length=1)


class GetGroup(GroupId):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=48, ge=1, le=100)


class CreateGroup(ListGroups):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)


class UpdateGroup(GroupId):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class GroupMembers(GroupId):
    member_ids: list[str] = Field(max_length=1000)


class SetGroups(ListGroups):
    member_id: str = Field(min_length=1)
    group_ids: list[str] = Field(max_length=200)


class EffectiveScope(StrictModel):
    source_ids: list[str] = Field(default_factory=list)
    source_group_ids: list[str] = Field(default_factory=list)


class JobScope(StrictModel):
    job_id: str = Field(min_length=1)
    source_ids: list[str] | None = None
    source_group_ids: list[str] = Field(default_factory=list)


def output(data, *, group_id=None, write=False):
    return ToolOutput(
        data,
        focus={"page": "groups", **({"group_id": group_id} if group_id else {})},
        mutation={"domain": "library", "groups": True} if write else None,
    )


async def list_groups(args: ListGroups, ctx: ToolContext):
    return output(check(await ctx.client.get("/api/admin/groups", type=args.type), "列举组"))


async def get_group(args: GetGroup, ctx: ToolContext):
    return output(
        check(
            await ctx.client.get(
                f"/api/admin/groups/{args.group_id}", page=args.page, page_size=args.page_size
            ),
            "查看组",
        ),
        group_id=args.group_id,
    )


async def create_group(args: CreateGroup, ctx: ToolContext):
    data = check(await ctx.client.post("/api/admin/groups", args.model_dump()), "创建组")
    return output(data, group_id=data["id"], write=True)


async def update_group(args: UpdateGroup, ctx: ToolContext):
    data = check(
        await ctx.client.patch(
            f"/api/admin/groups/{args.group_id}", args.model_dump(exclude={"group_id"}, exclude_none=True)
        ),
        "编辑组",
    )
    return output(data, group_id=args.group_id, write=True)


async def delete_group(args: GroupId, ctx: ToolContext):
    return output(check(await ctx.client.delete(f"/api/admin/groups/{args.group_id}"), "删除组"), write=True)


async def add_group_members(args: GroupMembers, ctx: ToolContext):
    data = check(
        await ctx.client.post(f"/api/admin/groups/{args.group_id}/members", {"member_ids": args.member_ids}),
        "添加组成员",
    )
    return output(data, group_id=args.group_id, write=True)


async def remove_group_members(args: GroupMembers, ctx: ToolContext):
    data = check(
        await ctx.client.post(
            f"/api/admin/groups/{args.group_id}/members/remove", {"member_ids": args.member_ids}
        ),
        "移除组成员",
    )
    return output(data, group_id=args.group_id, write=True)


async def set_member_groups(args: SetGroups, ctx: ToolContext):
    data = check(
        await ctx.client.put(
            f"/api/admin/group-memberships/{args.type}/{args.member_id}", {"group_ids": args.group_ids}
        ),
        "更新所属组",
    )
    return output(data, write=True)


async def effective_scope(args: EffectiveScope, ctx: ToolContext):
    return ToolOutput(
        check(await ctx.client.post("/api/admin/groups/resolve", args.model_dump()), "查询有效素材范围")
    )


async def set_job_scope(args: JobScope, ctx: ToolContext):
    data = check(
        await ctx.client.put(
            f"/api/studio/jobs/{args.job_id}/material-scope", args.model_dump(exclude={"job_id"})
        ),
        "更新任务素材范围",
    )
    return ToolOutput(
        data,
        focus={"page": "studio", "job_id": args.job_id},
        mutation={"domain": "studio", "job_id": args.job_id},
    )


async def preview(args, ctx):
    labels = {
        "type": "组类型",
        "group_id": "组 ID",
        "name": "组名称",
        "description": "说明",
        "member_ids": "成员 ID",
        "member_id": "成员 ID",
        "group_ids": "所属组",
        "job_id": "任务 ID",
        "source_ids": "素材 ID",
        "source_group_ids": "素材组 ID",
    }
    changes = [
        {"label": labels.get(key, key), "after": value}
        for key, value in args.model_dump(exclude_none=True).items()
    ]
    return Preview("修改媒体组织关系或任务范围", changes=changes, value=args.model_dump(), warnings=[RULES])


def specs():
    return [
        ToolSpec(
            name,
            "library" if name != "set_job_material_scope" else "studio_detail",
            description + "。" + RULES,
            model,
            risk,
            execute,
            preview if risk != "read" else None,
        )
        for name, description, model, risk, execute in [
            ("list_groups", "按类型列举素材组或音乐组", ListGroups, "read", list_groups),
            ("get_group", "查看组信息、成员数和分页成员", GetGroup, "read", get_group),
            ("create_group", "创建空组", CreateGroup, "write", create_group),
            ("update_group", "编辑组名称与说明", UpdateGroup, "write", update_group),
            ("delete_group", "删除组；引用此组的任务需重新选择", GroupId, "danger", delete_group),
            ("add_group_members", "批量添加已存在的同类型成员", GroupMembers, "write", add_group_members),
            ("remove_group_members", "批量移除组成员", GroupMembers, "write", remove_group_members),
            ("set_member_groups", "设置单项素材或音乐的全部所属组", SetGroups, "write", set_member_groups),
            (
                "resolve_material_scope",
                "查询组与素材交集后的有效范围，空范围明确返回错误",
                EffectiveScope,
                "read",
                effective_scope,
            ),
            (
                "set_job_material_scope",
                "编辑任务素材与素材组范围，下一次召回生效",
                JobScope,
                "write",
                set_job_scope,
            ),
        ]
    ]
