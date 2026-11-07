"""文案模板：内置模板（``studio/templates/*.yaml``，只读）与用户模板（metadata 库 ``script_templates`` 表）。

- 模板模型 ``ScriptTemplate`` 定义在 ``shared/types.py``（strict，拒绝未知字段），Writer 直接接收它。
- ``content_hash`` 只覆盖影响生成结果的字段（kind、instructions、constraints），进入 script 阶段签名；
  改名称、分类、说明不会让已有文案显示为过期。
- 用户模板只能由复制产生（ID 以 ``user-`` 开头），可修改、删除；被任一创作任务的方案引用时拒绝删除。
- 方案通过 ``options.template_id`` 选择模板，缺省（含没有该键的旧方案）为 ``base``。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import Field, ValidationError

from admin.curation import EntityNotFound
from shared.config import StrictModel, fingerprint
from shared.types import ScriptTemplate, ScriptTemplateConstraints, ScriptTemplateKind

BUILTIN_DIR = Path(__file__).resolve().parent / "templates"
BUILTIN_ORDER = ("base", "history", "hot", "film", "science", "interview")
DEFAULT_TEMPLATE = "base"
USER_PREFIX = "user-"


class TemplateReadOnly(ValueError):
    """内置模板只读（API 返回 403）。"""


class TemplateInUse(ValueError):
    """用户模板被方案引用（API 返回 409）。"""


class TemplateChanges(StrictModel):
    """修改用户模板：只填需要修改的字段；max_chars 传 null 表示跟随 studio.max_input_chars。"""

    name: str | None = Field(default=None, min_length=1, max_length=40)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    kind: ScriptTemplateKind | None = None
    description: str | None = Field(default=None, max_length=200)
    instructions: str | None = Field(default=None, min_length=1, max_length=6000)
    max_chars: int | None = Field(default=None, ge=50, le=20000)


def _load(path: Path) -> ScriptTemplate:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        template = ScriptTemplate.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise RuntimeError(f"内置文案模板 {path.name} 无效：{exc}") from exc
    if template.id != path.stem:
        raise RuntimeError(f"内置文案模板 {path.name} 的 id 必须与文件名一致")
    if template.id.startswith(USER_PREFIX):
        raise RuntimeError(f"内置文案模板 id 不能以 {USER_PREFIX} 开头：{template.id}")
    return template


@lru_cache(maxsize=1)
def builtins() -> dict[str, ScriptTemplate]:
    """按固定顺序加载内置模板；文件缺失、字段非法或多出文件都视为打包错误。"""
    found = {path.stem: _load(path) for path in sorted(BUILTIN_DIR.glob("*.yaml"))}
    if set(found) != set(BUILTIN_ORDER):
        raise RuntimeError(f"内置文案模板不完整：{sorted(set(found) ^ set(BUILTIN_ORDER))}")
    return {key: found[key] for key in BUILTIN_ORDER}


def content_hash(template: ScriptTemplate) -> str:
    """影响生成结果的模板内容指纹（进入 script 阶段签名）。"""
    return fingerprint(
        {
            "kind": template.kind,
            "instructions": template.instructions,
            "constraints": template.constraints.model_dump(),
        }
    )


def reference(template: ScriptTemplate) -> dict:
    """script 签名与文案版本记录使用的模板引用。"""
    return {"id": template.id, "hash": content_hash(template)}


def _row_template(row: dict) -> ScriptTemplate:
    body = json.loads(row["body"])
    return ScriptTemplate.model_validate(
        {
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "kind": row["kind"],
            **body,
        }
    )


def _view(template: ScriptTemplate, row: dict | None = None) -> dict:
    return {
        **template.model_dump(),
        "hash": content_hash(template),
        "builtin": row is None,
        "base_id": row["base_id"] if row else None,
        "created_at": row["created_at"] if row else None,
        "updated_at": row["updated_at"] if row else None,
    }


def _user_row(store, template_id: str) -> dict | None:
    return store.one("SELECT * FROM script_templates WHERE id=?", (template_id,))


def get_template(store, template_id: str | None) -> ScriptTemplate:
    template_id = template_id or DEFAULT_TEMPLATE
    found = builtins().get(template_id)
    if found is not None:
        return found
    row = _user_row(store, template_id) if template_id.startswith(USER_PREFIX) else None
    if row is None:
        raise EntityNotFound(f"找不到文案模板：{template_id}")
    return _row_template(row)


def exists(store, template_id: str) -> bool:
    try:
        get_template(store, template_id)
    except EntityNotFound:
        return False
    return True


def template_view(store, template_id: str) -> dict:
    template = get_template(store, template_id)
    return _view(template, None if template.id in builtins() else _user_row(store, template.id))


def list_templates(store) -> list[dict]:
    """内置模板在前（固定顺序），用户模板按创建时间排列。"""
    items = [_view(template) for template in builtins().values()]
    for row in store.rows("SELECT * FROM script_templates ORDER BY created_at, id"):
        items.append(_view(_row_template(row), row))
    return items


def _body(template: ScriptTemplate) -> str:
    return json.dumps(
        {
            "version": template.version,
            "description": template.description,
            "instructions": template.instructions,
            "constraints": template.constraints.model_dump(),
        },
        ensure_ascii=False,
    )


def copy_template(store, from_id: str, name: str, *, now: str) -> dict:
    """复制内置或用户模板为新的用户模板；base_id 记录最初来源的内置模板。"""
    source = get_template(store, from_id)
    row = _user_row(store, source.id)
    base_id = (row["base_id"] or source.id) if row else source.id
    template = source.model_copy(update={"id": USER_PREFIX + uuid4().hex[:12], "name": name, "version": 1})
    template = ScriptTemplate.model_validate(template.model_dump())  # 名称按模型规则重新校验
    store.execute(
        "INSERT INTO script_templates(id,name,category,kind,base_id,body,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (template.id, template.name, template.category, template.kind, base_id, _body(template), now, now),
    )
    return template_view(store, template.id)


def _require_user(store, template_id: str) -> dict:
    if template_id in builtins():
        raise TemplateReadOnly("内置文案模板只读，请先复制为自己的模板再修改")
    row = _user_row(store, template_id) if template_id.startswith(USER_PREFIX) else None
    if row is None:
        raise EntityNotFound(f"找不到文案模板：{template_id}")
    return row


def apply_changes(current: ScriptTemplate, values: dict) -> ScriptTemplate:
    """把部分字段修改（TemplateChanges 的 exclude_unset 结果）合并到模板上并重新校验；不落库、不改版本号。"""
    update = {k: v for k, v in values.items() if k != "max_chars"}
    if "max_chars" in values:
        update["constraints"] = ScriptTemplateConstraints(max_chars=values["max_chars"]).model_dump()
    return ScriptTemplate.model_validate({**current.model_dump(), **update})


def update_template(store, template_id: str, changes: TemplateChanges, *, now: str) -> dict:
    row = _require_user(store, template_id)
    current = _row_template(row)
    template = apply_changes(current, changes.model_dump(exclude_unset=True))
    if content_hash(template) != content_hash(current):
        template = template.model_copy(update={"version": current.version + 1})
    store.execute(
        "UPDATE script_templates SET name=?,category=?,kind=?,body=?,updated_at=? WHERE id=?",
        (template.name, template.category, template.kind, _body(template), now, template_id),
    )
    return template_view(store, template_id)


def references(config, template_id: str) -> list[dict]:
    """引用某模板的方案（扫描全部创作任务的 workspace.json，只读，不触发迁移）。"""
    from studio import studio

    root = studio.studio_root(config)
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/workspace.json")):
        try:
            w = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for plan in (w.get("plans") or {}).values():
            if (plan.get("options") or {}).get("template_id", DEFAULT_TEMPLATE) == template_id:
                found.append(
                    {"job_id": path.parent.name, "plan_id": plan.get("id"), "plan_name": plan.get("name")}
                )
    return found


def delete_template(config, store, template_id: str) -> None:
    """删除用户模板；被任一方案引用时拒绝。调用方须持有 studio 修改锁，避免与方案修改竞争。"""
    _require_user(store, template_id)
    used = references(config, template_id)
    if used:
        first = used[0]
        raise TemplateInUse(
            f"文案模板正被 {len(used)} 个方案引用（如任务 {first['job_id']} "
            f"的方案「{first['plan_name']}」），"
            "请先为这些方案换用其他模板"
        )
    store.execute("DELETE FROM script_templates WHERE id=?", (template_id,))
