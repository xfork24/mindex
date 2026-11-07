"""人物领域工具：演员、角色、作品与参考图的读取和维护，以及识别演员。

- 全部经回环客户端调用人物库页面同一套接口（``/api/persons/...``），校验、锁与中文错误与页面一致。
- 只删除记录的操作（删除人物、移除参考图）为 danger 级；参考图文件留给 ``mindex clean`` 回收。
- 不提供删除演员表导入图片的工具（会删除物理文件）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from agent.tools.common import check
from agent.tools.media import clean as clean_media
from agent.tools.media import item as media_item
from agent.tools.search import ATTACHMENT_PREFIX, upload_check
from shared.config import StrictModel

PERSONS = {"domain": "persons"}
KIND_LABELS = {"actor": "演员", "role": "角色", "work": "作品"}
EntityKind = Literal["actor", "role", "work"]
ENTITY_FIELDS = ("kind", "name", "aliases", "work_ids", "actor_ids")
LINKED_EXAMPLES = 5
REFERENCE_HINTS = {
    422: "参考图未通过质检或配对无效：请换一张单人、正脸清晰的图片，或检查演员与角色的对应关系",
    503: "人物服务不可用或特征空间不一致：请先在设置页检查人物模型，再重试",
}


def persons_focus(pane: str = "catalogue", **extra) -> dict:
    return {"page": "persons", "pane": pane, **{k: v for k, v in extra.items() if v is not None}}


async def load_catalogue(ctx: ToolContext) -> dict:
    return check(await ctx.client.get("/api/persons", compact="true"), "读取人物库")


def find_entity(data: dict, identifier: str) -> dict:
    entity = next((e for e in data["entities"] if e["id"] == identifier), None)
    if entity is None:
        raise ToolError(
            f"找不到人物、作品或角色 {identifier}；可先用 list_persons 查找", code="missing_person"
        )
    return entity


def names(data: dict) -> dict[str, str]:
    return {e["id"]: e["name"] for e in data["entities"]}


def label(entity: dict) -> str:
    return f"{KIND_LABELS.get(entity['kind'], entity['kind'])}「{entity['name']}」"


def id_list(ids: list[str] | None, lookup: dict[str, str]) -> str:
    return "、".join(f"{lookup.get(i, '未知')}({i})" for i in ids or []) or "无"


def reference_owner(reference: dict, lookup: dict[str, str]) -> str:
    actor = lookup.get(reference.get("actor_id"), reference.get("actor_id"))
    if reference.get("role_id"):
        return f"角色「{lookup.get(reference['role_id'], reference['role_id'])}」（演员 {actor}）"
    return f"演员「{actor}」"


def hinted(result: dict, what: str, hints: dict[int, str]) -> object:
    """回环结果检查：给已知状态码附上处理建议，同时原样保留服务端中文原因。"""
    if not result["ok"] and result["status"] in hints:
        status = result["status"]
        raise ToolError(
            f"{what}失败（HTTP {status}）：{result['error']}（{hints[status]}）",
            code=f"http_{status}",
            value={"status": status, "error": result["error"]},
        )
    return upload_check(result, what)


class ListPersons(StrictModel):
    kind: EntityKind | None = Field(default=None, description="actor 演员、role 角色、work 作品")
    q: str = Field(default="", max_length=100, description="按名称过滤")


async def list_persons(args: ListPersons, ctx: ToolContext) -> ToolOutput:
    data = check(await ctx.client.get("/api/persons"), "读取人物库")
    rows = [
        {"id": e["id"], "kind": e["kind"], "name": e["name"]}
        for e in data["entities"]
        if (args.kind is None or e["kind"] == args.kind) and args.q.lower() in e["name"].lower()
    ]
    text = (
        "\n".join(f"{r['id']} | {KIND_LABELS.get(r['kind'], r['kind'])} | {r['name']}" for r in rows)
        or "（人物库为空）"
    )
    lookup = {r["id"]: r["name"] for r in rows}
    media = []
    for reference in data.get("references", []):
        owner = reference.get("role_id") or reference.get("actor_id")
        if owner in lookup and len(media) < 12:
            media.append(media_item("image", reference["url"], lookup[owner], collapsed=False))
    return ToolOutput({"items": rows}, text=text, focus={"page": "persons"}, media=media)


class GetPerson(StrictModel):
    person_id: str = Field(min_length=1, description="演员、角色或作品 ID（来自 list_persons）")


def owned_references(data: dict, entity: dict) -> list[dict]:
    key = {"actor": "actor_id", "role": "role_id"}.get(entity["kind"])
    if key is None:
        return []
    return [r for r in data.get("references", []) if r.get(key) == entity["id"]]


async def get_person(args: GetPerson, ctx: ToolContext) -> ToolOutput:
    data = await load_catalogue(ctx)
    entity = find_entity(data, args.person_id)
    lookup = names(data)
    lines = [f"{label(entity)} ID {entity['id']}"]
    if entity.get("aliases"):
        lines.append("别名：" + "、".join(entity["aliases"]))
    value: dict = {"entity": entity}
    if entity["kind"] == "role":
        lines.append("所属作品：" + id_list(entity.get("work_ids"), lookup))
        lines.append("出演演员：" + id_list(entity.get("actor_ids"), lookup))
    elif entity["kind"] == "actor":
        roles = [
            {"id": e["id"], "name": e["name"]}
            for e in data["entities"]
            if e["kind"] == "role" and entity["id"] in (e.get("actor_ids") or [])
        ]
        value["roles"] = roles
        lines.append("饰演角色：" + ("、".join(f"{r['name']}({r['id']})" for r in roles) or "无"))
    else:
        roles = [
            {"id": e["id"], "name": e["name"]}
            for e in data["entities"]
            if e["kind"] == "role" and entity["id"] in (e.get("work_ids") or [])
        ]
        sources = [
            {"id": s["id"], "name": s["name"]}
            for s in data.get("sources", [])
            if s.get("work_id") == entity["id"]
        ]
        value.update(roles=roles, sources=sources)
        lines.append("角色：" + ("、".join(f"{r['name']}({r['id']})" for r in roles) or "无"))
        lines.append("素材：" + ("、".join(f"{s['name']}({s['id']})" for s in sources) or "无"))
    references = owned_references(data, entity)
    value["references"] = [
        {
            "id": r["id"],
            "actor_id": r.get("actor_id"),
            "role_id": r.get("role_id"),
            "url": r["url"],
            "provenance": r.get("data") or {},
        }
        for r in references
    ]
    if entity["kind"] != "work":
        lines.append(
            f"参考图：{len(references)} 张"
            + (
                "（" + "、".join(r["id"] for r in references[:12]) + "）"
                if references
                else "；可用 add_person_reference 添加"
            )
        )
        linked = check(
            await ctx.client.get(f"/api/persons/entities/{entity['id']}/linked-shots", limit=LINKED_EXAMPLES),
            "读取关联镜头",
        )
        value["linked_shots"] = {"total": linked["total"], "items": linked["items"]}
        lines.append(
            f"关联镜头（已接受或待核对）：{linked['total']} 个"
            + "".join(
                f"\n  - {s['source_name']} {s['start']:.1f}–{s['end']:.1f}s（分镜 {s['shot_id']}）"
                for s in linked["items"]
            )
        )
    media = [
        media_item("image", r["url"], f"{entity['name']} 参考图", collapsed=False) for r in references[:12]
    ]
    return ToolOutput(value, text="\n".join(lines), focus=persons_focus(), media=clean_media(media))


class SavePerson(StrictModel):
    person_id: str | None = Field(default=None, description="修改已有实体时给出 ID；省略表示新建")
    kind: EntityKind | None = Field(
        default=None, description="新建时必填：actor 演员、role 角色、work 作品；修改时不可变更"
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] | None = Field(default=None, max_length=100, description="别名（整体替换）")
    work_ids: list[str] | None = Field(
        default=None, max_length=50, description="角色所属作品 ID（整体替换；角色至少一部）"
    )
    actor_ids: list[str] | None = Field(default=None, description="角色的出演演员 ID（整体替换）")

    @model_validator(mode="after")
    def target(self):
        if self.person_id is None and (self.kind is None or self.name is None):
            raise ValueError("新建时必须给出 kind 与 name")
        if self.person_id is not None and all(getattr(self, k) is None for k in ENTITY_FIELDS[1:]):
            raise ValueError("没有需要修改的字段")
        return self


async def person_body(args: SavePerson, ctx: ToolContext) -> tuple[dict, dict | None, dict]:
    data = await load_catalogue(ctx)
    old = find_entity(data, args.person_id) if args.person_id else None
    if old is not None and args.kind is not None and args.kind != old["kind"]:
        raise ToolError("不能变更实体类型", code="invalid_arguments")
    base = {k: old.get(k) for k in ENTITY_FIELDS} if old else {"aliases": [], "work_ids": [], "actor_ids": []}
    body = {**base, **args.model_dump(include=set(ENTITY_FIELDS), exclude_none=True)}
    body = {k: v for k, v in body.items() if v is not None}
    if body["kind"] == "role" and not body.get("work_ids"):
        raise ToolError(
            "角色至少归属一部作品：请给出 work_ids（作品 ID 可用 list_persons(kind=work) 查找）",
            code="invalid_arguments",
        )
    return data, old, body


async def preview_save_person(args: SavePerson, ctx: ToolContext) -> Preview:
    data, old, body = await person_body(args, ctx)
    lookup = names(data)
    labels = {"name": "名称", "aliases": "别名", "work_ids": "所属作品", "actor_ids": "出演演员"}

    def show(key, value):
        if key in ("work_ids", "actor_ids"):
            return id_list(value, lookup)
        return ("、".join(value) or "无") if isinstance(value, list) else value

    changes, warnings = [], []
    for key, text in labels.items():
        if key in ("work_ids", "actor_ids") and body["kind"] != "role":
            continue
        after = body.get(key)
        before = old.get(key) if old else None
        if old is None or before != after:
            changes.append(
                {"label": text, **({"before": show(key, before)} if old else {}), "after": show(key, after)}
            )
    if (
        old is not None
        and body["kind"] == "role"
        and set(old.get("work_ids") or []) - set(body.get("work_ids") or [])
    ):
        warnings.append("移除作品时，若该作品下仍有已接受的出镜证据，服务端会拒绝修改")
    summary = f"修改{label(old)}" if old else f"新建{KIND_LABELS[body['kind']]}「{body['name']}」"
    return Preview(summary, changes, warnings=warnings, focus=persons_focus())


async def save_person(args: SavePerson, ctx: ToolContext) -> ToolOutput:
    _, old, body = await person_body(args, ctx)
    if old:
        entity = check(await ctx.client.put(f"/api/persons/entities/{old['id']}", body), "修改人物")
    else:
        entity = check(await ctx.client.post("/api/persons/entities", body), "新建人物")
    verb = "修改" if old else "新建"
    return ToolOutput(
        {"entity": entity},
        text=f"已{verb}{label(entity)}，ID {entity['id']}。",
        mutation={**PERSONS, "person_id": entity["id"]},
        focus=persons_focus(),
    )


class DeletePerson(StrictModel):
    person_id: str = Field(min_length=1, description="要删除的演员或角色 ID（作品不可删除）")


def linked_text(linked: dict) -> str:
    examples = "；".join(
        f"{s['source_name']} {s['start']:.1f}–{s['end']:.1f}s（分镜 {s['shot_id']}）"
        for s in linked["items"][:LINKED_EXAMPLES]
    )
    return f"仍有 {linked['total']} 个未排除的关联镜头：{examples}"


async def deletion_target(args: DeletePerson, ctx: ToolContext) -> tuple[dict, dict, list[dict]]:
    data = await load_catalogue(ctx)
    entity = find_entity(data, args.person_id)
    if entity["kind"] == "work":
        raise ToolError(
            f"{label(entity)}不可删除：作品由素材自动生成或用于角色归属，不支持删除",
            code="work_not_deletable",
        )
    linked = check(
        await ctx.client.get(f"/api/persons/entities/{entity['id']}/linked-shots", limit=LINKED_EXAMPLES),
        "读取关联镜头",
    )
    if linked["total"]:
        raise ToolError(
            f"无法删除{label(entity)}：{linked_text(linked)}。请先在人物库「处理与审核」中核对"
            "（排除或改为其他身份）后再删除",
            code="person_has_linked_shots",
            value={"linked_shots": linked},
        )
    return data, entity, owned_references(data, entity)


async def preview_delete_person(args: DeletePerson, ctx: ToolContext) -> Preview:
    data, entity, references = await deletion_target(args, ctx)
    warnings = (
        [f"该身份的 {len(references)} 张参考图记录会一并删除（图片文件由 mindex clean 回收）"]
        if references
        else []
    )
    if entity["kind"] == "actor":
        roles = [
            e["name"]
            for e in data["entities"]
            if e["kind"] == "role" and entity["id"] in (e.get("actor_ids") or [])
        ]
        if roles:
            warnings.append("会从这些角色的出演演员中移除：" + "、".join(roles))
    warnings.append("审核留档与已发布的人物检索快照不受影响")
    return Preview(
        f"删除{label(entity)}",
        [{"label": KIND_LABELS[entity["kind"]], "before": entity["name"], "after": "删除"}],
        warnings=warnings,
        focus=persons_focus(),
    )


async def delete_person(args: DeletePerson, ctx: ToolContext) -> ToolOutput:
    _, entity, _ = await deletion_target(args, ctx)
    # 并发核对可能在预览后新增关联镜头：服务端 422 的中文原因（含关联镜头示例）原样转述。
    result = check(await ctx.client.delete(f"/api/persons/entities/{entity['id']}"), "删除人物")
    return ToolOutput(
        result,
        text=f"已删除{label(entity)}，同时移除 {result.get('removed_references', 0)} 张参考图记录。",
        mutation={**PERSONS, "person_id": entity["id"], "deleted": True},
        focus=persons_focus(),
    )


class AssignWork(StrictModel):
    source_id: str = Field(min_length=1, description="素材 ID")
    work_id: str = Field(min_length=1, description="作品 ID（list_persons(kind=work)）")


async def assignment(args: AssignWork, ctx: ToolContext) -> tuple[dict, dict, dict]:
    data = await load_catalogue(ctx)
    source = next((s for s in data.get("sources", []) if s["id"] == args.source_id), None)
    if source is None:
        raise ToolError(f"找不到素材 {args.source_id}；可用 list_sources 查找", code="missing_source")
    work = find_entity(data, args.work_id)
    if work["kind"] != "work":
        raise ToolError(f"{label(work)}不是作品：素材只能归属作品", code="invalid_arguments")
    return data, source, work


async def preview_assign_work(args: AssignWork, ctx: ToolContext) -> Preview:
    data, source, work = await assignment(args, ctx)
    before = names(data).get(source.get("work_id"), source.get("work_id"))
    warnings = []
    if source.get("work_id") != work["id"]:
        warnings.append("改变作品归属会清除该素材已有的人物出镜证据与覆盖记录，需要重新处理人物并审核")
    return Preview(
        f"把素材「{source['name']}」归属到作品「{work['name']}」",
        [{"label": "作品", "before": before, "after": work["name"]}],
        warnings=warnings,
        focus=persons_focus("processing"),
    )


async def assign_source_work(args: AssignWork, ctx: ToolContext) -> ToolOutput:
    _, source, work = await assignment(args, ctx)
    check(
        await ctx.client.put(f"/api/persons/sources/{source['id']}/work", {"work_id": work["id"]}),
        "设置作品归属",
    )
    return ToolOutput(
        {"source_id": source["id"], "work_id": work["id"]},
        text=f"已把素材「{source['name']}」归属到作品「{work['name']}」。如之前已处理过人物，"
        "请用 start_source_task(work=persons) 重新处理。",
        mutation={**PERSONS, "source_id": source["id"]},
        focus=persons_focus("processing"),
    )


class AddReference(StrictModel):
    actor_id: str | None = Field(default=None, description="演员 ID；角色有多名演员时必须指定")
    role_id: str | None = Field(default=None, description="角色 ID；给出时参考图同时归属该角色")
    attachment: str | None = Field(
        default=None,
        pattern=r"^attachment://",
        description="参考图附件 attachment://…（与 source_id 二选一）",
    )
    source_id: str | None = Field(default=None, description="从素材截图：素材 ID")
    timestamp: float | None = Field(default=None, ge=0, description="从素材截图：时间点（秒）")
    bbox: list[float] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        description="从素材截图：可选裁剪框 [x1,y1,x2,y2]（原始像素坐标）",
    )

    @model_validator(mode="after")
    def target(self):
        if not self.actor_id and not self.role_id:
            raise ValueError("参考图必须指定演员或角色")
        if (self.attachment is None) == (self.source_id is None):
            raise ValueError("attachment 与 source_id 必须且只能给出一个")
        if self.source_id is not None and self.timestamp is None:
            raise ValueError("从素材截图时必须给出 timestamp")
        if self.attachment is not None and (self.timestamp is not None or self.bbox is not None):
            raise ValueError("使用附件时不要给 timestamp/bbox")
        return self


async def reference_target(args: AddReference, ctx: ToolContext) -> str:
    data = await load_catalogue(ctx)
    lookup = names(data)
    for key, kind in (("actor_id", "actor"), ("role_id", "role")):
        identifier = getattr(args, key)
        if identifier and find_entity(data, identifier)["kind"] != kind:
            raise ToolError(f"{key} 必须是{KIND_LABELS[kind]} ID", code="invalid_arguments")
    return reference_owner(
        {"actor_id": args.actor_id or "（角色的出演演员）", "role_id": args.role_id}, lookup
    )


async def preview_add_reference(args: AddReference, ctx: ToolContext) -> Preview:
    owner = await reference_target(args, ctx)
    if args.attachment:
        source = f"附件 {args.attachment}"
    else:
        source = f"素材 {args.source_id} 的 {args.timestamp:.2f}s 画面" + (
            f"，裁剪 {args.bbox}" if args.bbox else ""
        )
    return Preview(
        f"为{owner}添加参考图",
        [{"label": "来源", "after": source}, {"label": "归属", "after": owner}],
        warnings=["添加时会调用人物服务做质检（单人、正脸清晰）；不合格会被拒绝"],
        focus=persons_focus(),
    )


async def add_person_reference(args: AddReference, ctx: ToolContext) -> ToolOutput:
    owner = await reference_target(args, ctx)
    if args.attachment:
        result = await ctx.client.upload(
            "/api/persons/references",
            files={"file": args.attachment},
            data={"actor_id": args.actor_id, "role_id": args.role_id},
        )
    else:
        body = {
            "actor_id": args.actor_id,
            "role_id": args.role_id,
            "source_id": args.source_id,
            "timestamp": args.timestamp,
            "bbox": args.bbox,
        }
        result = await ctx.client.post(
            "/api/persons/references/from-source", {k: v for k, v in body.items() if v is not None}
        )
    reference = hinted(result, "添加参考图", REFERENCE_HINTS)
    media = [media_item("image", reference["url"], f"{owner} 参考图", collapsed=False)]
    return ToolOutput(
        {"reference": reference},
        text=f"已为{owner}添加参考图 {reference['id']}（已通过质检）。",
        mutation={**PERSONS, "reference_id": reference["id"]},
        focus=persons_focus(),
        media=clean_media(media),
    )


class RemoveReference(StrictModel):
    reference_id: str = Field(min_length=1, description="参考图 ID（get_person 返回的 references[].id）")


async def removal_target(args: RemoveReference, ctx: ToolContext) -> tuple[dict, str]:
    data = await load_catalogue(ctx)
    reference = next((r for r in data.get("references", []) if r["id"] == args.reference_id), None)
    if reference is None:
        raise ToolError(
            f"找不到参考图 {args.reference_id}；可用 get_person 查看某个人物的参考图",
            code="missing_reference",
        )
    return reference, reference_owner(reference, names(data))


async def preview_remove_reference(args: RemoveReference, ctx: ToolContext) -> Preview:
    reference, owner = await removal_target(args, ctx)
    return Preview(
        f"移除{owner}的参考图 {reference['id']}",
        [{"label": "参考图", "before": reference["url"], "after": "移除"}],
        warnings=["只删除参考图记录，图片文件由 mindex clean 回收；之后的人物处理不再用它比对"],
        focus=persons_focus(),
    )


async def remove_person_reference(args: RemoveReference, ctx: ToolContext) -> ToolOutput:
    reference, owner = await removal_target(args, ctx)
    check(await ctx.client.delete(f"/api/persons/references/{reference['id']}"), "移除参考图")
    return ToolOutput(
        {"removed": reference["id"]},
        text=f"已移除{owner}的参考图 {reference['id']}。",
        mutation={**PERSONS, "reference_id": reference["id"], "deleted": True},
        focus=persons_focus(),
    )


class IdentifyPerson(StrictModel):
    attachment: str = Field(pattern=r"^attachment://", description="待识别的图片附件 attachment://…")
    top_k: int = Field(default=5, ge=1, le=20, description="每张人脸按角色、按演员各返回的候选数")


IDENTIFY_HINTS = {503: "人物服务不可用或特征空间与参考图缓存不一致：请先在设置页检查人物模型"}


def candidate_text(candidates: list[dict]) -> str:
    return (
        "、".join(
            f"{c['name'] or c['id']}({c['id']}) {c['score']:.3f}{'✓' if c['accepted'] else ''}"
            for c in candidates
        )
        or "无"
    )


async def identify_person(args: IdentifyPerson, ctx: ToolContext) -> ToolOutput:
    data = hinted(
        await ctx.client.upload(
            "/api/persons/identify", files={"file": args.attachment}, data={"top_k": str(args.top_k)}
        ),
        "识别演员",
        IDENTIFY_HINTS,
    )
    faces = data.get("faces") or []
    lines = [
        f"检出 {len(faces)} 张人脸（参考图 {data.get('references_total', 0)} 张，"
        f"其中 {data.get('uncovered_references', 0)} 张没有当前特征缓存、未参与比对）："
    ]
    upload_url = f"/api/agent/attachments/{args.attachment[len(ATTACHMENT_PREFIX) :]}/content"
    media = [media_item("image", upload_url, "待识别图片", collapsed=False)]
    for index, face in enumerate(faces, 1):
        accepted = face.get("accepted_role")
        lines.append(
            f"人脸 {index} 框 {face.get('face_bbox')} 质量 {face.get('face_quality', 0):.2f}："
            + (
                f"判定为角色「{accepted['name']}」({accepted['id']}) {accepted['score']:.3f}"
                if accepted
                else "未达到自动确认条件"
            )
        )
        lines.append(f"  角色候选：{candidate_text(face.get('roles') or [])}")
        lines.append(f"  演员候选：{candidate_text(face.get('actors') or [])}")
        for candidate in (face.get("roles") or [])[:1] + (face.get("actors") or [])[:1]:
            media.append(
                media_item(
                    "image",
                    candidate["reference_url"],
                    f"人脸 {index} 候选 {candidate['name'] or candidate['id']}",
                    collapsed=False,
                )
            )
    if not faces:
        lines.append("（没有检出带特征的人脸；可换一张人脸更清晰的图片）")
    if data.get("uncovered_references"):
        lines.append("提示：未覆盖的参考图需在重新处理人物后才参与识别。")
    lines.append("（✓ 表示达到自动确认条件；参考图与上传图片已在面板展示）")
    media = clean_media(media)
    # 引用同时放在结果值中（兼容既有调用方）；ToolOutput.images 供视觉开启时交给模型：上传图与获胜参考图。
    images = [args.attachment] + [m["url"] for m in media[1:]]
    value = {**data, "images": images}
    return ToolOutput(
        value,
        text="\n".join(lines),
        meta={"kind": "identify", "faces": len(faces)},
        focus=persons_focus(),
        media=media,
        images=images,
    )


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "list_persons", "persons", "列出人物库中的演员、角色与作品。", ListPersons, "read", list_persons
        ),
        ToolSpec(
            "get_person",
            "persons",
            "读取一个演员、角色或作品的详情：别名、作品与演员关系、参考图，以及关联镜头数量与示例。",
            GetPerson,
            "read",
            get_person,
        ),
        ToolSpec(
            "save_person",
            "persons",
            "新建或修改演员、角色、作品（修改时只传要改的字段，列表字段整体替换；角色至少归属一部作品）。",
            SavePerson,
            "write",
            save_person,
            preview_save_person,
        ),
        ToolSpec(
            "delete_person",
            "persons",
            "删除演员或角色（连同其参考图记录）；作品不可删除；仍有未排除关联镜头时会被拒绝。",
            DeletePerson,
            "danger",
            delete_person,
            preview_delete_person,
        ),
        ToolSpec(
            "assign_source_work",
            "persons",
            "设置素材所属作品；改变归属会清除该素材已有的人物证据，需要重新处理人物。",
            AssignWork,
            "write",
            assign_source_work,
            preview_assign_work,
        ),
        ToolSpec(
            "add_person_reference",
            "persons",
            (
                "为演员或角色添加参考图：用图片附件（attachment://），或从素材某个时间点截图（可裁剪）；"
                "会做质检。"
            ),
            AddReference,
            "write",
            add_person_reference,
            preview_add_reference,
        ),
        ToolSpec(
            "remove_person_reference",
            "persons",
            "移除一张参考图（只删除记录）。",
            RemoveReference,
            "danger",
            remove_person_reference,
            preview_remove_reference,
        ),
        ToolSpec(
            "identify_person",
            "persons",
            (
                "识别图片附件（attachment://）中的人：对每张人脸按角色与按演员给出候选、"
                "相似度与是否达到确认条件。"
            ),
            IdentifyPerson,
            "read",
            identify_person,
        ),
    ]
