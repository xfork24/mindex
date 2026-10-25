"""叠加层阶段（逐镜，分镜含 engine=html 的文字层时才有）：为每个 HTML 文字层产出一份透明背景的 HTML 动画源码。

- 标题卡、信息条、数据卡片用内置模板 ``studio/overlays/layer.html`` 注入数据生成（确定性，不调用模型）；
  custom 按 ``brief`` 由动画生成模型编写（透明背景提示词）。
- 签名：各 HTML 文字层的内容与时间窗、文字层样式（品牌模板）、镜头时长、画幅与帧率；有 custom 层时另含
  动画生成服务签名与提示词版本。
- 产物：``versions/<vid>/overlay-<序号>.html``（序号为该层在分镜 overlays 中的位置）；``data.layers`` 记录每层来源。
- 人工修改：``save_manual`` 替换某一层源码，写入新版本并选用；含人工修改的版本被「钉住」，构建时直接复用。
- 录制与合成在分镜渲染阶段（``media_nodes.composite_overlays``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from mindex.models.adapters import make_animator
from mindex.models.animator import AnimationRejected, validate_animation
from mindex.shared.config import fingerprint
from mindex.studio import studio
from mindex.studio.studio_artifacts import digest
from mindex.studio.studio_logs import active_log

from ..plans import canvas_render, choose
from ..stages import StageHandler, html_overlays, register, shot_content
from ..versions import add_version, file_info, root
from .animate import unavailable

# 改动内置模板、数据注入或透明叠加提示词时递增：进入叠加层阶段签名。
OVERLAY_VERSION = 1
TEMPLATE = Path(__file__).resolve().parents[2] / "overlays" / "layer.html"
PINNED_REASON = "保留人工修改的叠加层源码；需要重新生成时请单独执行生成叠加层"


def layer_window(layer: dict, seconds: float) -> tuple[float, float]:
    """文字层在镜头内的起止秒（截在镜头之内，至少 0.1 秒）。"""
    start = min(float(layer.get("start") or 0), max(0.0, seconds - 0.1))
    end = seconds if layer.get("end") is None else min(seconds, float(layer["end"]))
    return round(start, 3), round(max(end, start + 0.1), 3)


def overlay_request(config, w, p, sid) -> dict:
    """签名载荷与生成参数的唯一来源；配音尚未完成时 seconds 为 None。"""
    from ..post import overlay_style

    selected, content = shot_content(w, p, sid)
    speech = w["versions"].get(selected.get("tts"))
    seconds = None
    if speech and speech["status"] == "complete" and speech["data"].get("duration"):
        seconds = round(speech["data"]["duration"] / (p["options"]["atempo"] or 1.0), 3)
    render = canvas_render(config, p)[0]
    layers = [{"index": index, **layer} for index, layer in html_overlays(content)]
    return {"layers": layers, "style": overlay_style(config, w, p), "seconds": seconds, "width": render.width,
            "height": render.height, "fps": render.fps}


def template_html(layer: dict, style: dict, *, seconds: float, width: int, height: int) -> str:
    data = {key: layer.get(key) for key in ("kind", "title", "subtitle", "items", "effect")}
    data.update(seconds=seconds, width=width, height=height, style=style)
    # </ 写成 <\/：数据里的文字不会提前结束 <script>。
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*MINDEX_DATA*/null", payload)


def check(config, html: str) -> str:
    try:
        return validate_animation(html, config.studio.animator.max_html_kb)
    except AnimationRejected as exc:
        raise studio.StudioError(str(exc)) from exc


def _write(config, job_id, vid, index: int, html: str) -> Path:
    directory = root(config, job_id) / "versions" / vid
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"overlay-{index}.html"
    studio.save_bytes(target, html.encode("utf-8"))
    return target


def save_manual(config, job_id, w, p, sid, index: int, html: str) -> str:
    """人工保存某一层叠加层源码：复制当前选用版本的其余层，替换该层，写入新版本并选用（钉住）。"""
    from ..signatures import specification
    from ..versions import file_path, version

    _, content = shot_content(w, p, sid)
    present = dict(html_overlays(content))
    if index not in present:
        raise studio.StudioError("该分镜没有这个 HTML 文字层")
    check(config, html)
    current = w["versions"].get(p["shots"][sid].get("overlay"))
    spec = specification(config, job_id, w, p, "overlay", sid)
    vid = uuid4().hex
    files, layers = {}, []
    for item in (current or {}).get("data", {}).get("layers", []):
        if item["index"] == index or item["index"] not in present:
            continue
        source = file_path(config, job_id, version(w, current["id"]), f"overlay-{item['index']}")
        files[f"overlay-{item['index']}"] = file_info(config, job_id, _write(
            config, job_id, vid, item["index"], source.read_text(encoding="utf-8")))
        layers.append(item)
    target = _write(config, job_id, vid, index, html)
    files[f"overlay-{index}"] = file_info(config, job_id, target)
    layers.append({"index": index, "kind": present[index]["kind"], "origin": "manual", "chars": len(html)})
    layers.sort(key=lambda item: item["index"])
    add_version(
        config, job_id, w, "overlay", sid, data={"layers": layers, "origin": "manual",
                                                 "based_on": (current or {}).get("id")},
        files=files, inputs=spec["inputs"], parameters={**spec["parameters"], "origin": "manual"},
        signature=fingerprint({"stage": "overlay", "manual": digest(target), "specification": spec["signature"]}),
        version_id=vid,
    )
    choose(w, p, "overlay", sid, vid)
    return vid


class OverlayHandler(StageHandler):
    name, label, scope = "overlay", "生成叠加层", "shot"

    def settings(self, config, p, *, store=None, display=False):
        return {"overlay_version": OVERLAY_VERSION}

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, _ = shot_content(w, p, sid)
        inputs = {"content": selected["content"], "tts": selected.get("tts")}
        payload = overlay_request(config, w, p, sid)
        if any(layer["kind"] == "custom" for layer in payload["layers"]):
            from mindex.models.animator import ANIMATOR_PROMPT_VERSION

            payload["animator"] = config.studio.animator.signature()
            payload["prompt_version"] = ANIMATOR_PROMPT_VERSION
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
        _, content = shot_content(w, p, sid)
        if any(layer["kind"] == "custom" for _, layer in html_overlays(content)) and (reason := unavailable(config)):
            node.update(action="blocked", reason=f"自定义 HTML 文字层需要动画生成服务：{reason}")

    def run(self, ctx):
        request = overlay_request(ctx.config, ctx.w, ctx.p, ctx.sid)
        if request["seconds"] is None:
            raise studio.StudioError("缺少成功配音，无法确定叠加层时长")
        animator = None
        files, layers = {}, []
        log = active_log.get()
        for layer in request["layers"]:
            start, end = layer_window(layer, request["seconds"])
            seconds = round(end - start, 3)
            if layer["kind"] == "custom":
                if animator is None:
                    animator = ctx.services.get("animator")
                    if animator is None:
                        if reason := unavailable(ctx.config):
                            raise studio.StudioError(reason)
                        animator = make_animator(ctx.config)
                if log:
                    log.emit(f"正在生成第 {layer['index'] + 1} 个文字层的叠加动画（{seconds:g} 秒）")
                html = animator.animate(layer["brief"], seconds=seconds, width=request["width"],
                                        height=request["height"], fps=request["fps"], transparent=True)
                origin = "generated"
            else:
                html = template_html(layer, request["style"], seconds=seconds, width=request["width"],
                                     height=request["height"])
                origin = "template"
            check(ctx.config, html)
            target = _write(ctx.config, ctx.job_id, ctx.version_id, layer["index"], html)
            files[f"overlay-{layer['index']}"] = file_info(ctx.config, ctx.job_id, target)
            layers.append({"index": layer["index"], "kind": layer["kind"], "origin": origin, "chars": len(html),
                           "start": start, "end": end})
        return {"layers": layers, "origin": "generated", "seconds": request["seconds"]}, files


register(OverlayHandler())
