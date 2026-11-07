"""生成动画阶段（逐镜，animation 来源）：由动画生成 LLM 按画面说明写自包含 HTML/JS 动画源码并留档。

- 画面说明：来源的 ``brief``，缺省用画面意象，再缺省用旁白；
- 签名：画面说明、动画时长（配音时长 ÷ 方案变速）、画幅尺寸与帧率，以及动画生成服务的 backend/model/
  源码大小上限与提示词版本；不含密钥与超时；
- 产物：``versions/<vid>/animation.html``（``data.origin = "generated"``）；
- 人工修改：``save_manual`` 经同一套静态校验后写入 ``origin = "manual"`` 的新版本并选用。人工版本被「钉住」：
  构建时直接复用（不因签名变化被重新生成覆盖），只有强制重建或单独执行生成动画才会替换；
  render 签名含动画版本与源码摘要，因此手改只重建该分镜的渲染与成片。
"""

from __future__ import annotations

from uuid import uuid4

from models.adapters import make_animator
from models.animator import ANIMATOR_PROMPT_VERSION, AnimationRejected, validate_animation
from shared.config import fingerprint
from studio import studio
from studio.artifacts import digest
from studio.logs import active_log
from studio.workspace import signatures, sources
from studio.workspace.plans import canvas_render, choose
from studio.workspace.stages import StageHandler, register, shot_content
from studio.workspace.versions import add_version, file_info, root

ANIMATE_VERSION = 1
NOT_CONFIGURED = "未配置动画生成服务，请在设置页激活动画生成档案"
NOT_ANIMATION = "此分镜的画面来源不是 AI 动画"
PINNED_REASON = "保留人工修改的动画源码；需要重新生成时请单独执行生成动画"


def unavailable(config) -> str | None:
    """动画生成服务不可用的原因；可用返回 None。"""
    cfg = config.studio.animator
    if not cfg.enabled:
        return NOT_CONFIGURED
    if not cfg.model.strip() or not cfg.base_url.strip():
        return "动画生成档案未填写地址或模型名，请在设置页补全"
    return None


def animation_request(config, w, p, sid) -> dict:
    """签名载荷与生成参数的唯一来源；配音尚未完成时 seconds 为 None。"""
    selected, content = shot_content(w, p, sid)
    source = content.get("source") or {}
    brief = source.get("brief") or (content.get("visual") or "").strip() or content["text"]
    speech = w["versions"].get(selected.get("tts"))
    seconds = None
    if speech and speech["status"] == "complete" and speech["data"].get("duration"):
        seconds = round(speech["data"]["duration"] / (p["options"]["atempo"] or 1.0), 3)
    render = canvas_render(config, p)[0]
    return {
        "brief": brief,
        "seconds": seconds,
        "width": render.width,
        "height": render.height,
        "fps": render.fps,
    }


def check(config, html: str) -> str:
    """静态校验；未通过时转为中文 StudioError（PATCH 返回 422）。"""
    try:
        return validate_animation(html, config.studio.animator.max_html_kb)
    except AnimationRejected as exc:
        raise studio.StudioError(str(exc)) from exc


def _write(config, job_id, vid, html: str):
    directory = root(config, job_id) / "versions" / vid
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "animation.html"
    studio.save_bytes(target, html.encode("utf-8"))
    return target


def save_manual(config, job_id, w, p, sid, html: str) -> str:
    """人工保存动画源码：校验 → 新的 origin=manual 版本 → 选用（下游 render/compose 随之过期）。

    手改版本的签名由源码摘要与当前规格派生，不会被按签名的复用误选；它只经「钉住」规则在选用期间复用。"""

    source = sources.plan_source(w, p, sid)
    if not source or source["kind"] != "animation":
        raise studio.StudioError(NOT_ANIMATION)
    check(config, html)
    spec = signatures.specification(config, job_id, w, p, "animate", sid)
    request = animation_request(config, w, p, sid)
    vid = uuid4().hex
    target = _write(config, job_id, vid, html)
    sha = digest(target)
    data = {
        "origin": "manual",
        "chars": len(html),
        "brief": request["brief"],
        "seconds": request["seconds"],
        "width": request["width"],
        "height": request["height"],
        "fps": request["fps"],
        "based_on": p["shots"][sid].get("animate"),
    }
    add_version(
        config,
        job_id,
        w,
        "animate",
        sid,
        data=data,
        files={"animation": file_info(config, job_id, target)},
        inputs=spec["inputs"],
        parameters={**spec["parameters"], "origin": "manual"},
        signature=fingerprint({"stage": "animate", "manual": sha, "specification": spec["signature"]}),
        version_id=vid,
    )
    choose(w, p, "animate", sid, vid)
    return vid


class AnimateHandler(StageHandler):
    name, label, scope = "animate", "生成动画", "shot"

    def settings(self, config, p, *, store=None, display=False):
        return {"animator": config.studio.animator.signature(), "prompt_version": ANIMATOR_PROMPT_VERSION}

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, _ = shot_content(w, p, sid)
        inputs = {"content": selected["content"], "tts": selected.get("tts")}
        payload = {**animation_request(config, w, p, sid), "animate_version": ANIMATE_VERSION}
        return payload, inputs, params

    def dependencies(self, w, p, sid, *, resplit=False):
        return [f"tts:{sid}"]

    def pinned(self, item):
        return item.get("data", {}).get("origin") == "manual"

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        if found:
            if self.pinned(w["versions"][found]):
                node["reason"] = PINNED_REASON
            return
        if reason := unavailable(config):
            node.update(action="blocked", reason=reason)

    def run(self, ctx):
        animator = ctx.services.get("animator")
        if animator is None:
            if reason := unavailable(ctx.config):
                raise studio.StudioError(reason)
            animator = make_animator(ctx.config)
        request = animation_request(ctx.config, ctx.w, ctx.p, ctx.sid)
        if request["seconds"] is None:
            raise studio.StudioError("缺少成功配音，无法确定动画时长")
        log = active_log.get()
        if log:
            log.emit(f"正在生成动画源码（{request['seconds']:g} 秒，{request['width']}×{request['height']}）")
        html = animator.animate(
            request["brief"],
            seconds=request["seconds"],
            width=request["width"],
            height=request["height"],
            fps=request["fps"],
        )
        check(ctx.config, html)
        target = _write(ctx.config, ctx.job_id, ctx.version_id, html)
        ctx.diagnostics.record("animation", {"chars": len(html), "brief": request["brief"]})
        data = {"origin": "generated", "chars": len(html), **request}
        return data, {"animation": file_info(ctx.config, ctx.job_id, target)}

    def conflict_labels(self, config, w, item, expected, messages):
        found = []
        before = item["parameters"].get("animator") or {}
        after = expected["parameters"].get("animator") or {}
        for field, label in (("backend", "动画生成服务"), ("model", "动画生成模型")):
            if before.get(field) != after.get(field):
                found.append(
                    f"{label}已变化：{before.get(field) or '未记录'} → {after.get(field) or '未设置'}"
                )
        if item["parameters"].get("prompt_version") != expected["parameters"].get("prompt_version"):
            found.append("动画生成提示词已更新")
        return found


register(AnimateHandler())
