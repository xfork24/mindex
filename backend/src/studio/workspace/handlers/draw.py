"""生成图片阶段（逐镜，aigc_image 来源）：调用图片生成模型生成一张图片，之后与静态图片相同地推拉/平移渲染。

- 提示词缺省用画面意象，再缺省用召回查询；给出主体参考图（reference_id）时为图生图；
- 宽高比按方案画幅（MiniMax 取最接近的 aspect_ratio，ComfyUI 直接映射宽高）；
- 签名：提示词、主体参考图摘要、seed、提示词优化开关、画幅尺寸与生成服务（backend/model，ComfyUI
另含工作流摘要）；
  运动方式与强度只影响渲染，不在这里；
- 产物：``versions/<vid>/image.png``（经 ``image_png`` 规范化）与 ``request.json``（脱敏请求）。
"""

from __future__ import annotations

from models.adapters import make_image_generator
from models.image_generators import ImageRequest, nearest_aspect
from shared.images import image_png
from studio import studio
from studio.artifacts import sanitized
from studio.workspace import sources
from studio.workspace.plans import canvas_render
from studio.workspace.stages import StageHandler, register, shot_content
from studio.workspace.versions import file_info

DRAW_VERSION = 1
NOT_CONFIGURED = "未配置图片生成服务，请在设置页激活图片生成档案"
MAX_PIXELS = 16_000_000


def unavailable(config) -> str | None:
    cfg = config.studio.image_generator
    if not cfg.enabled:
        return NOT_CONFIGURED
    if cfg.backend == "comfyui" and (cfg.comfyui_workflow is None or not cfg.comfyui_mapping):
        return "ComfyUI 图片生成档案未导入工作流或节点映射，请在设置页补全"
    if cfg.backend != "comfyui" and not cfg.model.strip():
        return "图片生成档案未填写模型名，请在设置页补全"
    return None


def draw_request(config, w, p, sid) -> dict:
    _, content = shot_content(w, p, sid)
    source = content.get("source") or {}
    render = canvas_render(config, p)[0]
    return {
        "prompt": source.get("prompt") or (content.get("visual") or "").strip() or content["query"],
        "reference": source.get("reference_id"),
        "seed": source.get("seed"),
        "prompt_optimizer": bool(source.get("prompt_optimizer")),
        "width": render.width,
        "height": render.height,
    }


class DrawHandler(StageHandler):
    name, label, scope = "draw", "生成图片", "shot"

    def settings(self, config, p, *, store=None, display=False):
        return {"image_generator": config.studio.image_generator.signature()}

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, _ = shot_content(w, p, sid)
        payload = {**draw_request(config, w, p, sid), "draw_version": DRAW_VERSION}
        return payload, {"content": selected["content"]}, params

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        if found:
            return
        source = sources.plan_source(w, p, sid) or {}
        reason = unavailable(config) or sources.missing_reference(config, job_id, source)
        if reason:
            node.update(action="blocked", reason=reason)

    def run(self, ctx):
        generator = ctx.services.get("image_generator")
        if generator is None:
            if reason := unavailable(ctx.config):
                raise studio.StudioError(reason)
            generator = make_image_generator(ctx.config)
        request = draw_request(ctx.config, ctx.w, ctx.p, ctx.sid)
        source = sources.plan_source(ctx.w, ctx.p, ctx.sid) or {}
        reference = sources.reference_image(ctx.config, ctx.job_id, source, "reference_id")
        record_path = ctx.directory / "request.json"
        record = {
            "request": request,
            "generator": ctx.spec["parameters"].get("image_generator"),
            "submitted_at": studio.now(),
            "finished_at": None,
        }
        studio.save(record_path, sanitized(record))
        payload = generator.generate(
            ImageRequest(
                prompt=request["prompt"],
                aspect_ratio=nearest_aspect(request["width"], request["height"]),
                reference=reference,
                seed=request["seed"],
                prompt_optimizer=request["prompt_optimizer"],
                width=request["width"],
                height=request["height"],
            )
        )
        raw = ctx.directory / "download.bin"
        studio.save_bytes(raw, payload)
        try:
            png = image_png(raw, MAX_PIXELS)
        except ValueError as exc:
            record.update(finished_at=studio.now(), error=f"生成的图片无法解码：{exc}")
            studio.save(record_path, sanitized(record))
            raise studio.StudioError(f"生成的图片无法解码：{exc}") from exc
        raw.unlink(missing_ok=True)
        target = ctx.directory / "image.png"
        studio.save_bytes(target, png)
        record.update(finished_at=studio.now())
        studio.save(record_path, sanitized(record))
        from PIL import Image

        with Image.open(target) as image:
            width, height = image.size
        data = {
            "prompt": request["prompt"],
            "width": width,
            "height": height,
            "reference": request["reference"],
            "seed": request["seed"],
        }
        return data, {
            "image": file_info(ctx.config, ctx.job_id, target),
            "request": file_info(ctx.config, ctx.job_id, record_path),
        }

    def conflict_labels(self, config, w, item, expected, messages):
        found = []
        before = item["parameters"].get("image_generator") or {}
        after = expected["parameters"].get("image_generator") or {}
        for field, label in (("backend", "图片生成服务"), ("model", "图片生成模型")):
            if before.get(field) != after.get(field):
                found.append(
                    f"{label}已变化：{before.get(field) or '未记录'} → {after.get(field) or '未设置'}"
                )
        if before.get("workflow") != after.get("workflow"):
            found.append("ComfyUI 工作流已变化")
        return found


register(DrawHandler())
