"""文案模板 API：``/api/studio/templates``。内置模板只读；用户模板由复制产生，可修改、删除。

挂在创作路由上并共用 studio 修改锁：删除时扫描方案引用，与方案修改互斥，避免删掉刚被选用的模板。
``POST /templates/{id}/preview`` 用模板（可附未保存的修改）按示例大纲试写一次文案：不落库、不涉及任务。
"""

from __future__ import annotations

import time

from fastapi import HTTPException
from pydantic import Field

from models import adapters
from shared.config import StrictModel
from shared.types import ScriptTemplateKind
from studio import script_templates, studio
from studio.script_templates import TemplateChanges, TemplateInUse, TemplateReadOnly

PREVIEW_MAX_REFERENCES = 5


class CopyTemplateBody(StrictModel):
    name: str = Field(min_length=1, max_length=40)


class PreviewTemplateBody(StrictModel):
    """试写参数。kind/instructions/max_chars 只填要临时覆盖的字段（编辑中尚未保存的内容）；
    max_chars 显式传 null 表示跟随 studio.max_input_chars。大纲字数受 studio.max_input_chars 限制，
    参考文稿总字数受 studio.max_reference_chars 限制。"""

    outline: str = Field(default="", max_length=20000)
    references: list[str] = Field(default_factory=list, max_length=PREVIEW_MAX_REFERENCES)
    kind: ScriptTemplateKind | None = None
    instructions: str | None = Field(default=None, min_length=1, max_length=6000)
    max_chars: int | None = Field(default=None, ge=50, le=20000)


def template_routes(router, base, store, mutation_lock, *, current, writer=None):
    """current() 返回最新配置；writer 为注入的文案模型（测试用），缺省时按当前配置创建。"""

    @router.get("/templates")
    def list_templates():
        return {"items": script_templates.list_templates(store), "default": script_templates.DEFAULT_TEMPLATE}

    @router.get("/templates/{template_id}")
    def get_template(template_id: str):
        return script_templates.template_view(store, template_id)

    @router.post("/templates/{template_id}/copy", status_code=201)
    def copy_template(template_id: str, body: CopyTemplateBody):
        if not body.name.strip():
            raise studio.StudioError("模板名称不能为空")
        return script_templates.copy_template(store, template_id, body.name.strip(), now=studio.now())

    @router.put("/templates/{template_id}")
    def update_template(template_id: str, body: TemplateChanges):
        try:
            return script_templates.update_template(store, template_id, body, now=studio.now())
        except TemplateReadOnly as exc:
            raise HTTPException(403, str(exc)) from exc

    @router.post("/templates/{template_id}/preview")
    def preview_template(template_id: str, body: PreviewTemplateBody):
        # 同步路由：在线程池中阻塞等待文案模型（超时按 studio.writer 档案）。模型失败统一映射为 503。
        config = current()
        overrides = body.model_dump(include={"kind", "instructions", "max_chars"}, exclude_unset=True)
        template = script_templates.apply_changes(
            script_templates.get_template(store, template_id), overrides
        )
        outline = body.outline.strip()
        if len(outline) > config.studio.max_input_chars:
            raise studio.StudioError(f"大纲超过 {config.studio.max_input_chars} 字上限，请精简后再试")
        texts = [text for text in body.references if text.strip()]
        if sum(len(text) for text in texts) > config.studio.max_reference_chars:
            raise studio.StudioError(
                f"参考文稿总字数超过 {config.studio.max_reference_chars} 字上限，请删减后再试"
            )
        limit = config.studio.max_input_chars
        if template.constraints.max_chars:
            limit = min(limit, template.constraints.max_chars)
        started = time.monotonic()
        text = (writer or adapters.make_writer(config)).write(outline, template=template, references=texts)
        return {
            "text": text,
            "chars": len(text),
            "limit": limit,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "template": {
                **script_templates.reference(template),
                "name": template.name,
                "kind": template.kind,
            },
        }

    @router.delete("/templates/{template_id}")
    def delete_template(template_id: str):
        with mutation_lock():
            try:
                script_templates.delete_template(base, store, template_id)
            except TemplateReadOnly as exc:
                raise HTTPException(403, str(exc)) from exc
            except TemplateInUse as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"deleted": template_id}
