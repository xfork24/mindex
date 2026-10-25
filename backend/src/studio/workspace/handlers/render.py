"""分镜渲染阶段（逐镜）：把选定区间与配音渲染为单镜视频。

素材区间的签名含 ``ShotMedia`` 身份；上传片段的文件区间以文件引用（文件名含 sha256）为身份；静态图片
（image 来源，没有选片阶段）直接由配音与图片运动参数生成画面，签名含图片摘要、motion、strength 与变速；
AI 动画（animation 来源，没有选片阶段）在沙箱里逐帧录制动画源码，签名含动画版本、源码摘要与变速
（画幅与帧率在渲染参数里）。
"""

from __future__ import annotations

from .. import sources
from ..media_nodes import render_node
from ..plans import DEFAULT_CANVAS, canvas_of, canvas_render
from ..stages import StageHandler, plan_shot_stages, register, shot_content
from ..versions import version


def canvas_settings(p, result):
    """非默认画幅（或 crop）才把 canvas/fit 写进参数；16:9 + pad 的参数与旧版逐字节一致。"""
    canvas, fit = canvas_of(p)
    if (canvas, fit) != DEFAULT_CANVAS:
        result.update(canvas=canvas, fit=fit)
    return result


IMAGE_VERSION = 1
ANIMATION_VERSION = 1


class RenderHandler(StageHandler):
    name, label, scope = "render", "分镜渲染", "shot"

    def settings(self, config, p, *, store=None, display=False):
        o = p["options"]
        result = {
            "render": canvas_render(config, p)[0].model_dump(),
            "audio": {
                **config.studio.audio.model_dump(),
                "mode": o["audio_mode"],
                "duck_gain": o["duck_gain"],
            },
            "audio_mode": o["audio_mode"],
            "duck_gain": o["duck_gain"],
        }
        return canvas_settings(p, result)

    def specification(self, config, job_id, w, p, sid, params, resources):
        params = {**params, "media_version": "shot-media-v1"}
        selected, content = shot_content(w, p, sid)
        inputs = {"content": selected["content"]}
        source = sources.content_source(content)
        if source and source["kind"] == "image":
            inputs["tts"] = selected.get("tts")
            payload = {
                "tts": inputs["tts"],
                "source": sources.identity(source),
                "atempo": p["options"]["atempo"] or 1.0,
                "image_version": IMAGE_VERSION,
            }
            return payload, inputs, params
        if source and source["kind"] == "aigc_image":
            # AI 生成图片：图片由 draw 版本决定（其签名含提示词与模型），其余与静态图片相同。
            inputs.update(tts=selected.get("tts"), draw=selected.get("draw"))
            drawn = w["versions"].get(inputs["draw"]) or {}
            payload = {
                "tts": inputs["tts"],
                "draw": inputs["draw"],
                "image": (drawn.get("files", {}).get("image") or {}).get("sha256"),
                "source": {"kind": "aigc_image", "motion": source.get("motion", "zoom-in"),
                           "strength": source.get("strength", 0.3)},
                "atempo": p["options"]["atempo"] or 1.0,
                "image_version": IMAGE_VERSION,
            }
            return payload, inputs, params
        if source and source["kind"] == "animation":
            inputs.update(tts=selected.get("tts"), animate=selected.get("animate"))
            animated = w["versions"].get(inputs["animate"]) or {}
            payload = {
                "tts": inputs["tts"],
                "animate": inputs["animate"],
                "html": (animated.get("files", {}).get("animation") or {}).get("sha256"),
                "atempo": p["options"]["atempo"] or 1.0,
                "animation_version": ANIMATION_VERSION,
            }
            return payload, inputs, params
        inputs.update(select=selected.get("select"), tts=selected.get("tts"))
        event = w["versions"].get(inputs["select"], {}).get("data", {}).get("event")
        source = None
        if event and event["video"].get("kind") == "file":
            source = {"file": event["video"]["ref"]}
        elif event:
            store = resources.database()
            row = store.one("SELECT path FROM sources WHERE id=?", (event["video"]["source_id"],))
            if row:
                source = resources.media(store).identity(
                    event["video"]["source_id"], event["video"]["start"], event["video"]["end"]
                )
        return {"select": inputs["select"], "tts": inputs["tts"], "source": source}, inputs, params

    def dependencies(self, w, p, sid, *, resplit=False):
        present = plan_shot_stages(w, p, sid)
        return [f"{stage}:{sid}" for stage in ("select", "draw", "animate", "tts") if stage in present]

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        source = sources.plan_source(w, p, sid)
        if source and source["kind"] == "image" and not found and not sources.present(
            config, job_id, source["media_id"], source["kind"]
        ):
            node.update(action="blocked", reason=sources.MISSING)

    def column_reason(self, w, p, sid, spec):
        if "select" not in spec["inputs"]:
            return None
        selection = version(w, spec["inputs"]["select"])
        if selection["inputs"].get("tts") != spec["inputs"]["tts"]:
            return "选片绑定的配音与当前选择不同，请先重新对齐"
        return None

    def run(self, ctx):
        return render_node(ctx.config, ctx.job_id, ctx.w, ctx.p, ctx.sid, ctx.directory,
                           capture=ctx.services.get("animation_capture"))


register(RenderHandler())
