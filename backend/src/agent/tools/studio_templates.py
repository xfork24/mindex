"""Studio 文案模板工具：列出、读取、复制、修改、删除（``/api/studio/templates``）。

- 内置模板只读；用户模板由复制产生，可修改、删除。修改与删除的服务端拒绝（403 内置只读、409 被方案引用）
  原样转述。
- 修改影响生成结果的字段（类型、提示词、字数上限）后，引用该模板的方案在创作页显示「模板已变化」，
  已有文案不会自动重写，需要时用 run_node 重新生成文案。
- ``delete_script_template`` 只删除模板记录（metadata 库一行），按删除策略为 danger。
- 模板是全局数据：改动事件为 ``{domain: "studio", templates: true}``，创作页据此重新读取模板目录与当前任务。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check, clip
from mindex.shared.config import StrictModel

TemplateId = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="文案模板 ID（list_script_templates 列出）")
KINDS = {"outline": "按大纲", "rewrite": "改写参考文稿"}
FOCUS = {"page": "studio", "section": "script"}
MUTATION = {"domain": "studio", "templates": True}
# 修改后进入 script 签名（内容哈希）的字段；其余字段只影响展示。
CONTENT_KEYS = ("kind", "instructions", "max_chars")
CHANGE_LABELS = {"name": "名称", "category": "分类", "kind": "类型", "description": "说明", "instructions": "提示词",
                 "max_chars": "字数上限"}
# 预览中提示词的截断长度。
PREVIEW_CHARS = 200


def origin(item: dict) -> str:
    return "内置" if item.get("builtin") else "用户"


def max_chars_text(value) -> str:
    return f"{value} 字" if value else "跟随 studio.max_input_chars"


async def fetch_template(ctx: ToolContext, template_id: str) -> dict:
    return check(await ctx.client.get(f"/api/studio/templates/{template_id}"), "读取文案模板")


class ListScriptTemplates(StrictModel):
    include_instructions: bool = Field(default=False, description="true=同时返回每个模板的完整提示词")


async def list_script_templates(args: ListScriptTemplates, ctx: ToolContext) -> ToolOutput:
    items = check(await ctx.client.get("/api/studio/templates"), "列出文案模板")["items"]
    keys = ("id", "name", "category", "kind", "description", "builtin", "base_id", "version", "constraints")
    rows = [{k: item.get(k) for k in keys} | ({"instructions": item["instructions"]} if args.include_instructions else {})
            for item in items]
    text = "\n".join(
        f"{r['id']} | {r['name']} | {r['category']} | {KINDS.get(r['kind'], r['kind'])} | {origin(r)} | "
        f"{r['description']}"
        for r in rows
    )
    return ToolOutput({"items": rows},
                      text=f"共 {len(rows)} 个文案模板（ID | 名称 | 分类 | 类型 | 来源 | 说明）：\n{text}\n"
                           "用 update_plan_options 的 template_id 为方案选择模板；已有文案不会自动重写。",
                      focus=FOCUS)


class GetScriptTemplate(StrictModel):
    template_id: str = TemplateId


async def get_script_template(args: GetScriptTemplate, ctx: ToolContext) -> ToolOutput:
    item = await fetch_template(ctx, args.template_id)
    constraints = item.get("constraints") or {}
    lines = [
        f"文案模板 {item['id']}「{item['name']}」：分类 {item['category']}，类型 {KINDS.get(item['kind'], item['kind'])}，"
        f"{origin(item)}模板，版本 {item.get('version')}，字数上限 {max_chars_text(constraints.get('max_chars'))}",
    ]
    if item.get("base_id"):
        lines.append(f"复制自内置模板：{item['base_id']}")
    lines.append(f"说明：{item.get('description') or '（无）'}")
    lines.append("提示词（完整）：\n" + item["instructions"])
    if item.get("builtin"):
        lines.append("内置模板只读；要修改请先用 copy_script_template 复制为用户模板。")
    return ToolOutput(item, text="\n".join(lines), focus=FOCUS)


class CopyScriptTemplate(StrictModel):
    template_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", description="要复制的来源模板 ID（内置或用户模板）")
    name: str = Field(min_length=1, max_length=40, description="新模板名称")

    @model_validator(mode="after")
    def named(self):
        if not self.name.strip():
            raise ValueError("模板名称不能为空")
        return self


async def preview_copy_script_template(args: CopyScriptTemplate, ctx: ToolContext) -> Preview:
    item = await fetch_template(ctx, args.template_id)
    return Preview(
        "复制文案模板为新的用户模板",
        [{"label": "来源模板",
          "after": f"{item['name']}（{item['id']}，{origin(item)}，{KINDS.get(item['kind'], item['kind'])}）"},
         {"label": "新模板名称", "after": args.name.strip()}],
        warnings=["复制不会改变任何方案所选的模板；需要时再用 update_plan_options 选用新模板"],
        focus=FOCUS,
    )


async def copy_script_template(args: CopyScriptTemplate, ctx: ToolContext) -> ToolOutput:
    item = check(await ctx.client.post(f"/api/studio/templates/{args.template_id}/copy", {"name": args.name.strip()}),
                 "复制文案模板")
    return ToolOutput(item, text=f"已复制为用户模板 {item['id']}「{item['name']}」；可用 update_script_template 修改。",
                      mutation=MUTATION, focus=FOCUS)


class UpdateScriptTemplate(StrictModel):
    template_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
                             description="要修改的用户模板 ID（user- 开头；内置模板只读，请先复制）")
    name: str | None = Field(default=None, min_length=1, max_length=40, description="新名称")
    category: str | None = Field(default=None, min_length=1, max_length=40, description="新分类")
    kind: Literal["outline", "rewrite"] | None = Field(
        default=None, description="outline=按大纲创作；rewrite=改写参考文稿（方案须登记参考文稿）")
    description: str | None = Field(default=None, max_length=200, description="新说明")
    instructions: str | None = Field(default=None, min_length=1, max_length=6000, description="新的完整提示词")
    max_chars: int | None = Field(default=None, ge=50, le=20000,
                                  description="文案字数上限；显式传 null 恢复跟随 studio.max_input_chars")

    @model_validator(mode="after")
    def something(self):
        if not self.changes():
            raise ValueError("至少修改一项：name、category、kind、description、instructions 或 max_chars")
        return self

    def changes(self) -> dict:
        """只包含调用方给出的字段（max_chars 显式 null 也算修改）；其余字段 null 视为未给出。"""
        given = self.model_fields_set - {"template_id"}
        return {k: getattr(self, k) for k in CHANGE_LABELS
                if k in given and (getattr(self, k) is not None or k == "max_chars")}


def current_value(item: dict, key: str):
    return (item.get("constraints") or {}).get("max_chars") if key == "max_chars" else item.get(key)


def shown(key: str, value) -> str:
    if key == "instructions":
        return clip(value, PREVIEW_CHARS) + (f"（共 {len(value)} 字）" if value and len(value) > PREVIEW_CHARS else "")
    if key == "kind":
        return KINDS.get(value, value)
    if key == "max_chars":
        return max_chars_text(value)
    return value if value else "（空）"


async def preview_update_script_template(args: UpdateScriptTemplate, ctx: ToolContext) -> Preview:
    item = await fetch_template(ctx, args.template_id)
    if item.get("builtin"):
        raise ToolError("内置文案模板只读，请先用 copy_script_template 复制为自己的模板再修改", code="template_readonly")
    changed = {k: v for k, v in args.changes().items() if current_value(item, k) != v}
    if not changed:
        raise ToolError("给出的字段与模板当前内容相同，没有需要修改的内容", code="no_changes")
    rows = [{"label": CHANGE_LABELS[k], "before": shown(k, current_value(item, k)), "after": shown(k, v)}
            for k, v in changed.items()]
    if set(changed) & set(CONTENT_KEYS):
        warnings = ["使用该模板的方案会在创作页显示「模板已变化」；已有文案不会自动重写，需要时用 run_node 重新生成文案"]
    else:
        warnings = ["只修改名称、分类或说明，不影响已有文案，也不会显示「模板已变化」"]
    return Preview(f"修改用户文案模板 {item['id']}「{item['name']}」", rows, warnings=warnings, focus=FOCUS)


async def update_script_template(args: UpdateScriptTemplate, ctx: ToolContext) -> ToolOutput:
    result = await ctx.client.put(f"/api/studio/templates/{args.template_id}", args.changes())
    if not result["ok"] and result["status"] == 403:
        raise ToolError(f"修改文案模板失败：{result['error']}", code="http_403", value=result)
    item = check(result, "修改文案模板")
    note = ("引用它的方案会显示「模板已变化」，已有文案不会自动重写" if set(args.changes()) & set(CONTENT_KEYS)
            else "已有文案不受影响")
    return ToolOutput(item, text=f"已修改文案模板 {item['id']}「{item['name']}」（版本 {item.get('version')}）；{note}。",
                      mutation=MUTATION, focus=FOCUS)


class DeleteScriptTemplate(StrictModel):
    template_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
                             description="要删除的用户模板 ID（内置模板不能删除；被方案引用时服务端拒绝）")


async def preview_delete_script_template(args: DeleteScriptTemplate, ctx: ToolContext) -> Preview:
    item = await fetch_template(ctx, args.template_id)
    if item.get("builtin"):
        raise ToolError("内置文案模板只读，不能删除", code="template_readonly")
    return Preview(
        "删除用户文案模板（只删除模板记录；已用它生成的文案版本保留）",
        [{"label": "文案模板", "before": f"{item['name']}（{item['id']}，{item['category']}）", "after": "（删除）"}],
        warnings=["正被任一方案选用时服务端会拒绝删除；需先为这些方案换用其他模板"],
        focus=FOCUS,
    )


async def delete_script_template(args: DeleteScriptTemplate, ctx: ToolContext) -> ToolOutput:
    result = await ctx.client.delete(f"/api/studio/templates/{args.template_id}")
    if not result["ok"] and result["status"] in (403, 409):
        # 403 内置只读、409 被方案引用：原样转述服务端原因，不附加“稍后重试”。
        raise ToolError(f"删除文案模板失败：{result['error']}", code=f"http_{result['status']}", value=result)
    check(result, "删除文案模板")
    return ToolOutput({"deleted": args.template_id}, text=f"已删除文案模板 {args.template_id}。",
                      mutation=MUTATION, focus=FOCUS)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_script_templates", "studio",
                 "列出文案模板（内置六类与用户模板）：ID、名称、分类、类型（outline 按大纲 / rewrite 改写参考文稿）、说明；"
                 "include_instructions=true 时附完整提示词。为方案选模板前先调用它。",
                 ListScriptTemplates, "read", list_script_templates),
        ToolSpec("get_script_template", "studio",
                 "读取单个文案模板的完整信息与完整提示词（不截断）。修改模板前先调用它。",
                 GetScriptTemplate, "read", get_script_template),
        ToolSpec("copy_script_template", "studio",
                 "把内置或用户模板复制为新的用户模板（内置模板只读，要定制须先复制）。不改变方案所选模板。",
                 CopyScriptTemplate, "write", copy_script_template, preview_copy_script_template),
        ToolSpec("update_script_template", "studio",
                 "修改用户模板，只传要改的字段（name/category/kind/description/instructions/max_chars）。"
                 "改类型、提示词或字数上限后，使用它的方案显示「模板已变化」，已有文案不会自动重写。",
                 UpdateScriptTemplate, "write", update_script_template, preview_update_script_template),
        ToolSpec("delete_script_template", "studio",
                 "删除用户模板（只删模板记录）。内置模板不能删除；正被方案选用时拒绝，需先换用其他模板。",
                 DeleteScriptTemplate, "danger", delete_script_template, preview_delete_script_template),
    ]
