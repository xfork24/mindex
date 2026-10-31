"""媒体组：多组取并集，与调用方已有范围取交集。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from mindex.shared.config import StrictModel
from mindex.shared.media_groups import Groups

GroupType = Literal["source", "music"]


class GroupBody(StrictModel):
    type: GroupType
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=4000)


class GroupPatch(StrictModel):
    type: GroupType | None = None
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


class ResolveBody(StrictModel):
    source_ids: list[str] = Field(default_factory=list, max_length=1000)
    source_group_ids: list[str] = Field(default_factory=list, max_length=200)


class MembersBody(StrictModel):
    member_ids: list[str] = Field(max_length=1000)


class MembershipBody(StrictModel):
    group_ids: list[str] = Field(max_length=200)


def groups_router(store):
    router = APIRouter(prefix="/api/admin", tags=["组管理"])
    groups = Groups(store)

    def invoke(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/groups")
    def list_groups(type: GroupType):
        return {"items": groups.list(type)}

    @router.post("/groups", status_code=201)
    def create(body: GroupBody):
        return invoke(groups.save, body.type, body.name, body.description)

    @router.post("/groups/resolve")
    def resolve_scope(body: ResolveBody):
        from mindex.shared.types import Filters

        values = invoke(Filters(**body.model_dump()).effective_sources, store)
        if values == set():
            raise HTTPException(422, "所选组与素材范围没有成员，请重新选择")
        return {"source_ids": sorted(values) if values is not None else None}

    @router.get("/groups/{group_id}")
    def detail(group_id: str, page: int = Query(1, ge=1), page_size: int = Query(48, ge=1, le=100)):
        return invoke(groups.members, group_id, page, page_size)

    @router.patch("/groups/{group_id}")
    def update(group_id: str, body: GroupPatch):
        current = invoke(groups.get, group_id)
        return invoke(
            groups.save,
            body.type or current["type"],
            body.name if body.name is not None else current["name"],
            body.description if body.description is not None else current["description"],
            group_id,
        )

    @router.delete("/groups/{group_id}")
    def delete(group_id: str):
        return invoke(groups.delete, group_id)

    @router.post("/groups/{group_id}/members")
    def add(group_id: str, body: MembersBody):
        return invoke(groups.change_members, group_id, body.member_ids)

    @router.post("/groups/{group_id}/members/remove")
    def remove(group_id: str, body: MembersBody):
        return invoke(groups.change_members, group_id, body.member_ids, True)

    @router.put("/group-memberships/{kind}/{member_id}")
    def memberships(kind: GroupType, member_id: str, body: MembershipBody):
        return {"groups": invoke(groups.set_groups, kind, member_id, body.group_ids)}

    return router
