"""选片对齐阶段（逐镜）：在召回候选或人工区间里选定原片时间区间并与配音对齐。

上传片段（clip 来源）没有召回：在片段内从人工起点（缺省 0）取与配音等长的窗口；AIGC（aigc 来源）在 generate
版本的生成片段内从 0 起取窗，片段短于配音时冻结末帧补足；第三方工具（tool 来源，视频产物）同样在 invoke
版本的产物内取窗。
"""

from __future__ import annotations

from studio import studio
from studio.window import anchor_conflict
from studio.workspace import sources
from studio.workspace.media_nodes import (
    alignment,
    clip_alignment,
    generated_alignment,
    tool_alignment,
    validate_range,
)
from studio.workspace.plans import needs_recall
from studio.workspace.stages import StageHandler, plan_shot_stages, register, shot_content

CLIP_VERSION = 1
AIGC_VERSION = 1
TOOL_VERSION = 1


class SelectHandler(StageHandler):
    name, label, scope = "select", "选片与对齐", "shot"

    def settings(self, config, p, *, store=None, display=False):
        o = p["options"]
        return {
            "selection": config.studio.selection.model_dump(),
            "atempo": o["atempo"],
            "alignment": o["alignment"],
        }

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, content = shot_content(w, p, sid)
        inputs = {"content": selected["content"], "tts": selected.get("tts")}
        source = sources.content_source(content)
        if source and source["kind"] == "aigc":
            # 生成片段由 generate 版本决定（其签名含提示词、模型与参数）；窗口固定从 0 开始。
            inputs["generate"] = selected.get("generate")
            payload = {
                "text": content["text"],
                "tts": inputs["tts"],
                "generate": inputs["generate"],
                "aigc_version": AIGC_VERSION,
            }
            return payload, inputs, params
        if source and source["kind"] == "tool":
            # 第三方工具的视频产物由 invoke 版本决定（其签名含工具配置摘要与参数）；窗口固定从 0 开始。
            inputs["invoke"] = selected.get("invoke")
            payload = {
                "text": content["text"],
                "tts": inputs["tts"],
                "invoke": inputs["invoke"],
                "tool_version": TOOL_VERSION,
            }
            return payload, inputs, params
        if source:
            # 片段摘要（media_id）与起点决定窗口；不依赖召回与人工区间。
            payload = {
                "text": content["text"],
                "tts": inputs["tts"],
                "source": sources.identity(source),
                "clip_version": CLIP_VERSION,
            }
            return payload, inputs, params
        if needs_recall(p, sid):
            inputs["recall"] = selected.get("recall")
        payload = {
            "text": content["text"],
            "tts": inputs["tts"],
            "recall": inputs.get("recall"),
            "range": p["ranges"].get(sid),
            "default_window_version": 2,
        }
        if p.get("anchors", {}).get(sid, {}).get("mode") == "manual":
            payload["anchor"] = p["anchors"][sid]
        return payload, inputs, params

    def dependencies(self, w, p, sid, *, resplit=False):
        present = plan_shot_stages(w, p, sid)
        wanted = ["tts", "generate", "invoke"] + (["recall"] if needs_recall(p, sid) else [])
        return [f"{stage}:{sid}" for stage in wanted if stage in present]

    def prepare(self, config, w, p, sid):
        if sid in p["ranges"] and not sources.plan_source(w, p, sid):
            p["ranges"][sid] = validate_range(config, p["ranges"][sid])

    def preflight(self, config, job_id, w, p, sid, spec, found, node):

        if source := sources.plan_source(w, p, sid):
            if (
                source["kind"] in sources.FILE_KINDS
                and not found
                and not sources.present(config, job_id, source["media_id"], source["kind"])
            ):
                node.update(action="blocked", reason=sources.MISSING)
            return

        anchor_error = anchor_conflict(w, p, sid)
        if anchor_error:
            node.update(action="blocked", reason=anchor_error)
        if sid in p["ranges"]:
            try:
                validate_range(config, p["ranges"][sid])
                if p["ranges"][sid].get("anchor_ref"):
                    alignment(config, job_id, w, p, sid, p["ranges"][sid])
            except studio.StudioError as exc:
                node.update(action="blocked", reason=str(exc))
        else:
            recall = w["versions"].get(spec["inputs"].get("recall"))
            if recall and not recall["data"].get("results"):
                node.update(action="blocked", reason="召回版本没有候选，请修改查询或人工选片")

    def run(self, ctx):
        source = sources.plan_source(ctx.w, ctx.p, ctx.sid)
        if source and source["kind"] == "aigc":
            return generated_alignment(ctx.config, ctx.job_id, ctx.w, ctx.p, ctx.sid), {}
        if source and source["kind"] == "tool":
            return tool_alignment(ctx.config, ctx.job_id, ctx.w, ctx.p, ctx.sid), {}
        if source:
            return clip_alignment(ctx.config, ctx.job_id, ctx.w, ctx.p, ctx.sid), {}
        return alignment(ctx.config, ctx.job_id, ctx.w, ctx.p, ctx.sid, ctx.p["ranges"].get(ctx.sid)), {}

    def pre_conflicts(self, w, p, sid, item):

        messages = []
        if error := anchor_conflict(w, p, sid):
            messages.append(error)
        choice = p.get("anchors", {}).get(sid, {})
        original = item["data"].get("anchor_ref")
        if original and choice.get("mode") == "manual" and original["candidate_id"] != choice["candidate_id"]:
            messages.append(f"参考候选：原版本 {original['candidate_id']}，当前 {choice['candidate_id']}")
        return messages


register(SelectHandler())
