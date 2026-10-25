"""文案阶段（方案级）：按方案选择的文案模板（``options.template_id``，缺省 base）由 writer 模型生成文案。

签名参数含 writer 配置、提示词版本与模板引用 ``{"id", "hash"}``，有参考文稿时再加排序后的参考文稿
摘要 ``references``（没有参考文稿的方案参数不变）；模板内容变化只让已有文案显示"模板已变化"提示
（``template_changed``），构建时已有文案仍直接复用，从不自动重写。

参考文稿（``workspace/references.py``）作为"仅供参考"的独立段落交给 writer：rewrite 模板允许大纲为空
但至少需要一份参考文稿；outline 模板把参考文稿当补充材料，大纲与参考文稿至少有其一。

测试改提示词版本时替换本模块的 ``WRITER_PROMPT_VERSION``。
"""

from __future__ import annotations

import copy

from mindex.models import adapters
from mindex.models.writer import WRITER_PROMPT_VERSION
from mindex.shared.storage import Store
from mindex.studio import script_templates, studio
from mindex.studio.material_digest import safe_digest
from mindex.studio.studio_artifacts import sanitized

from .. import references
from ..stages import StageHandler, register

REWRITE_NEEDS_REFERENCES = "访谈改写模板需要参考文稿"


def template_id(p) -> str:
    """方案选择的模板；没有该键的旧方案等价于通用解说模板。"""
    return (p.get("options") or {}).get("template_id") or script_templates.DEFAULT_TEMPLATE


def plan_template(config, p, store=None):
    return script_templates.get_template(store or Store(config.data_dir), template_id(p))


def template_changed(config, w, p, *, store=None) -> bool:
    """当前文案是按别的模板（或旧提示词）生成的：只提示可重新生成，不自动重写。

    手动保存与旧任务迁移的文案不来自模板，不提示；所选模板已不存在时也提示。"""
    vid = p.get("script")
    item = w["versions"].get(vid) if vid else None
    if not item or item.get("data", {}).get("origin") != "generated":
        return False
    try:
        current = script_templates.reference(plan_template(config, p, store))
    except studio.EntityNotFound:
        return True
    recorded = item["data"].get("template") or {}
    return (
        {"id": recorded.get("id"), "hash": recorded.get("hash")} != current
        or item.get("parameters", {}).get("prompt_version") != WRITER_PROMPT_VERSION
    )


class ScriptHandler(StageHandler):
    name, label, scope = "script", "生成文案", "plan"
    reusable = False  # 写文案是显式的方案级操作，从不切换指针复用
    column = False
    uses_store = True  # 用户模板在 metadata 库

    def settings(self, config, p, *, store=None, display=False):
        result = {
            "writer": sanitized(config.studio.writer.model_dump()),
            "prompt_version": WRITER_PROMPT_VERSION,
            "template": script_templates.reference(plan_template(config, p, store)),
        }
        if ids := references.reference_ids(p):
            result["references"] = ids  # 只在有参考文稿时加键，旧签名逐字节不变
        return result

    def specification(self, config, job_id, w, p, sid, params, resources):
        return {"outline": studio.load_job(config, job_id)["outline"]}, {}, params

    def plan_node(self, config, job_id, w, p):
        # 已有文案时直接复用，绝不在构建中悄悄重写；只在缺少文案时先生成。
        if p.get("script"):
            return None
        outline = studio.load_job(config, job_id).get("outline", "").strip()
        has_references = bool(references.references(p))
        try:
            template = plan_template(config, p)
        except studio.EntityNotFound as exc:
            template, reason = None, str(exc)
        if template is None:
            ready = False
        elif template.kind == "rewrite":
            ready = has_references
            reason = "根据参考文稿改写文案" if ready else REWRITE_NEEDS_REFERENCES
        else:
            ready = bool(outline) or has_references
            reason = ("根据视频大纲生成文案" if outline else "根据参考文稿生成文案") if ready else (
                "任务没有视频大纲，请手动编写文案")
        return {
            "key": "script",
            "stage": "script",
            "shot_id": None,
            "action": "rebuild" if ready else "blocked",
            "reason": reason,
        }

    def run(self, ctx):
        config, diagnostics = ctx.config, ctx.diagnostics
        template = plan_template(config, ctx.p)
        if script_templates.reference(template) != ctx.spec["parameters"].get("template"):
            raise studio.StudioError("文案模板已变化，请刷新后重试")
        if references.reference_ids(ctx.p) != ctx.spec["parameters"].get("references", []):
            raise studio.StudioError("参考文稿已变化，请刷新后重试")
        # 逐份复验摘要：文件缺失或被改动时拒绝执行，不用错误的正文写文案。
        texts = references.load_texts(config, ctx.job_id, ctx.p)
        if template.kind == "rewrite" and not texts:
            raise studio.StudioError(REWRITE_NEEDS_REFERENCES)
        outline = studio.load_job(config, ctx.job_id)["outline"]
        if not outline.strip() and not texts:
            raise studio.StudioError("任务没有视频大纲，请手动编写文案")
        # 素材摘要只是参考资料：生成失败时按空摘要继续（需求 4.13），只记一条中文诊断。
        digest = safe_digest(
            config,
            ctx.job_id,
            on_error=lambda message: diagnostics.record("material-digest-error", {"message": message}),
        )
        writer = ctx.services.get("writer") or adapters.make_writer(config)
        text = writer.write(outline, template=template, materials=digest, references=texts)
        if not text.strip():
            raise studio.StudioError("文案服务返回了空内容")
        if len(text) > config.studio.max_input_chars:
            raise studio.StudioError(f"文案超过 {config.studio.max_input_chars} 字上限，请精简后再试")
        data = {
            "text": text.strip(),
            "origin": "generated",
            "template": {**script_templates.reference(template), "name": template.name},
        }
        if texts:
            data["references"] = copy.deepcopy(references.references(ctx.p))
        if not digest.empty:
            # 只有实际参考了非空摘要才记录全文与统计（需求 4.10、4.11）。
            diagnostics.record("material-digest", {"text": digest.text, **digest.stats()})
            data["materials"] = digest.stats()
        return data, {}


register(ScriptHandler())
