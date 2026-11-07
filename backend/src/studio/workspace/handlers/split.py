"""拆分镜阶段（方案级）：把选用的文案拆成分镜，并直接写入方案的分镜工作台。"""

from __future__ import annotations

from uuid import uuid4

from models.adapters import make_storyboarder
from studio import studio
from studio.artifacts import sanitized
from studio.material_digest import merged_vocabulary, safe_digest, scope_parameters
from studio.workspace.plans import script_stale, script_text, set_board
from studio.workspace.stages import StageHandler, register


class SplitHandler(StageHandler):
    name, label, scope = "split", "拆分镜", "plan"
    reusable = False  # 重新拆分是显式的方案级操作，从不切换指针复用
    uses_store = True
    selects = False  # 执行时已由 set_board 写入分镜工作台
    choosable = False

    def settings(self, config, p, *, store=None, display=False):
        return {
            "generator": sanitized(config.studio.generator.model_dump()),
            "split_version": studio.SPLIT_VERSION,
        }

    def specification(self, config, job_id, w, p, sid, params, resources):
        return (
            {"text": script_text(w, p), "language": w.get("job_language", "zh")},
            {"script": p.get("script")},
            {**params, **scope_parameters(config, job_id, resources.database())},
        )

    def dependencies(self, w, p, sid, *, resplit=False):
        return ["script"] if not p.get("script") else []

    def plan_node(self, config, job_id, w, p):
        if p.get("script") and p["board"] and not script_stale(p):
            return None
        return {
            "key": "split",
            "stage": "split",
            "shot_id": None,
            "action": "rebuild",
            "reason": (
                "文案已变化，重新拆分镜后依次执行召回、配音、选片对齐、分镜渲染和成片合成"
                if script_stale(p)
                else "自动拆分镜后依次执行召回、配音、选片对齐、分镜渲染和成片合成"
            ),
        }

    def column_nodes(self, config, job_id, w, p, inputs_by_shot):
        if inputs_by_shot:
            raise studio.StudioError("整份文案拆分不接受逐镜输入")
        if not p.get("script"):
            raise studio.StudioError("请先生成文案，再拆分镜")
        return [{"key": "split", "stage": "split", "shot_id": None, "action": "rebuild"}]

    def run(self, ctx):
        config, w, p = ctx.config, ctx.w, ctx.p
        text = script_text(w, p)
        if not text:
            raise studio.StudioError("请先生成文案，再拆分镜")
        # 拆分镜同样参照素材摘要的画面词表；摘要出错或为空时不传词表，不影响拆分。
        vocabulary = merged_vocabulary(
            safe_digest(
                config,
                ctx.job_id,
                on_error=lambda message: ctx.diagnostics.record(
                    "material-digest-error", {"message": message}
                ),
            )
        )
        storyboarder = ctx.services.get("storyboarder") or make_storyboarder(config)
        drafts = storyboarder.split(
            text,
            vocabulary=vocabulary,
            **({"language": w.get("job_language", "zh")} if w.get("job_language") == "en" else {}),
        )
        if studio.coverage_warning(text, [s.text for s in drafts]):
            raise studio.StudioError("自动分镜未完整覆盖原文")
        # New identities avoid aliasing an old shot after a structural re-split.
        shots = [
            studio.Shot(
                id=f"shot-{uuid4().hex[:10]}",
                text=d.text,
                query=d.query,
                estimated_seconds=d.estimated_seconds,
                visual=studio.clipped_visual(index, d.visual),
            )
            for index, d in enumerate(drafts, 1)
        ]
        set_board(config, ctx.job_id, w, p, shots, origin=ctx.version_id)
        p["board_script"] = p["script"]
        return {"board": p["board"]}, {}


register(SplitHandler())
