"""多画幅导出阶段（成片，变体 = 画幅）：把方案成片导出为另一个画幅。

- derive（快速派生）：取成片阶段另存的无字幕、无水印画面 ``clean.mp4``，按目标画幅 pad/crop 适配，再按新画幅
  重新排版字幕（ASS 字号与边距按短边换算）、字幕引擎文字层与水印，音轨直接取自成片。只重新编码一次画面。
- rerender（重新渲染）：方案维护一个按该画幅设置的关联子方案（``variant_of``），子方案像普通方案一样逐镜渲染与
  合成（配音、选片等未变产物按签名复用）；导出阶段登记子方案的成片。见 ``workspace/variants.py``。

签名：上游成片版本（或子方案成片版本）、目标画幅与适配方式、导出方式与版本常量。HTML 文字层在逐镜渲染时已叠进
画面，快速派生只能随画面一起缩放裁切；需要按新画幅重新排版时用 rerender。
"""

from __future__ import annotations

import copy

from mindex.shared.work_budget import copy_file
from mindex.studio import studio

from ..plans import canvas_render
from ..stages import StageHandler, register
from ..versions import file_info, file_path, version

EXPORT_VERSION = 1


def export_spec(p, canvas):
    for item in p["options"].get("exports") or []:
        if item["canvas"] == canvas:
            return item
    raise studio.StudioError(f"方案没有 {canvas} 画幅的导出设置")


def target_plan(p, item) -> dict:
    """按导出画幅改写的方案副本（只用于排版计算，不保存）。"""
    copied = copy.deepcopy(p)
    copied["options"] = {**copied["options"], "canvas": item["canvas"], "fit": item["fit"], "exports": None}
    return copied


class ExportHandler(StageHandler):
    name, label, scope = "export", "多画幅导出", "final"
    buildable = False
    column = False
    reason = "成片合成后导出其他画幅"

    def variants(self, config, w, p):
        return [item["canvas"] for item in p["options"].get("exports") or []]

    def settings(self, config, p, *, store=None, display=False):
        return {"export_version": EXPORT_VERSION}

    def specification(self, config, job_id, w, p, sid, params, resources):
        from .. import variants

        item = export_spec(p, sid)
        if item["mode"] == "rerender":
            child = variants.child_plan(w, p, sid)
            inputs = {"variant_final": (child or {}).get("final")}
            payload = {"mode": "rerender", "canvas": sid, "fit": item["fit"], "final": inputs["variant_final"],
                       "plan": (child or {}).get("id")}
            return payload, inputs, params
        target = target_plan(p, item)
        render, _ = canvas_render(config, target)
        inputs = {"compose": p.get("final")}
        payload = {"mode": "derive", "canvas": sid, "fit": item["fit"], "compose": inputs["compose"],
                   "size": [render.width, render.height], "burn": p["options"].get("burn")}
        from ..post import compose_payload

        # 字幕样式、文字层与水印按新画幅重新排版：与成片签名中的同名部分一致。
        payload["post"] = {key: value for key, value in compose_payload(
            config, w, target, resources.database if resources else None).items()
            if key in ("subtitle_style", "highlights", "overlays", "overlay_style", "ass_version", "brand_assets")}
        return payload, inputs, params

    def dependencies(self, w, p, sid, *, resplit=False):
        if resplit:
            return ["split"]
        item = next((x for x in p["options"].get("exports") or [] if x["canvas"] == sid), None)
        return ["compose"] if not item or item["mode"] == "derive" else []

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        from .. import variants

        item = export_spec(p, sid)
        if item["mode"] != "rerender" or found:
            return
        child = variants.child_plan(w, p, sid)
        if child is None:
            node.update(action="blocked", reason="还没有该画幅的重新渲染子方案；保存导出设置后自动创建")
        elif not child.get("final"):
            node.update(action="blocked", reason=f"子方案「{child['name']}」还没有成片，请先构建全部画幅")

    def run(self, ctx):
        item = export_spec(ctx.p, ctx.sid)
        if item["mode"] == "rerender":
            return self.run_rerender(ctx, item)
        return self.run_derive(ctx, item)

    def run_rerender(self, ctx, item):

        from .. import variants

        child = variants.child_plan(ctx.w, ctx.p, ctx.sid)
        if child is None or not child.get("final"):
            raise studio.StudioError("子方案还没有成片，请先构建全部画幅")
        final = version(ctx.w, child["final"])
        if final["status"] != "complete":
            raise studio.StudioError("子方案的成片未成功完成")
        files = {}
        for name in ("mp4", "srt", "vtt"):
            if name in final["files"]:
                target = ctx.directory / f"export.{name}"
                copy_file(file_path(ctx.config, ctx.job_id, final, name), target)
                files[name] = file_info(ctx.config, ctx.job_id, target)
        data = {"mode": "rerender", "canvas": ctx.sid, "fit": item["fit"], "plan": child["id"],
                "final": final["id"], "total_seconds": final["data"].get("total_seconds"),
                "size": final["data"].get("size"), "boundaries": final["data"].get("boundaries"),
                "bookends": final["data"].get("bookends")}
        if final["data"].get("music"):  # 背景音乐段落（含曲库署名）随导出成片传给发布元数据
            data["music"] = final["data"]["music"]
        return data, files

    def run_derive(self, ctx, item):
        from mindex.studio.studio_render import (
            canvas_subtitle_style,
            check_font,
            fit_filters,
            mux,
            validate_final,
        )

        from .. import post

        config, p = ctx.config, ctx.p
        final = version(ctx.w, p.get("final")) if p.get("final") else None
        if not final or final["status"] != "complete":
            raise studio.StudioError("缺少成功的成片，请先合成成片")
        if "clean" not in final["files"]:
            raise studio.StudioError("成片缺少无字幕画面（clean），请重新合成成片")
        clean = file_path(config, ctx.job_id, final, "clean")
        source = file_path(config, ctx.job_id, final, "mp4")
        srt = file_path(config, ctx.job_id, final, "srt")
        target = target_plan(p, item)
        render, fit = canvas_render(config, target)
        boundaries = final["data"]["boundaries"]
        cues = post.subtitle_cues(ctx.w, final["data"].get("plan") or p, boundaries)
        ass_path, ass_subtitles = post.write_ass_cues(config, ctx.w, target, cues, ctx.directory, "export.ass")
        burn = bool(p["options"].get("burn"))
        if burn or ass_path is not None:
            check_font(srt, config.studio.subtitle)
        bookends = final["data"].get("bookends") or {}
        extra = [*fit_filters(render.width, render.height, fit), "setsar=1"]
        # 水印片段以 [in] 开头，必须是滤镜链的第一段：先叠水印（新画幅尺寸）再适配会把水印一起缩放，
        # 所以这里先单独适配画幅，再在 mux 里叠水印与字幕。
        fitted = ctx.directory / "fitted.mp4"
        from mindex.shared.media import atomic_media

        atomic_media(["-i", str(clean), "-map", "0:v:0", "-an", "-vf", ",".join(extra), "-c:v", "libx264",
                      "-preset", render.preset, "-crf", str(render.crf), "-pix_fmt", "yuv420p"], fitted,
                     timeout=max(600, float(final["data"]["total_seconds"]) * 10))
        mark = post.watermark_filter(config, target, None, bookends)
        style = canvas_subtitle_style(item["canvas"], render.width, render.height, config.studio.subtitle)
        output = ctx.directory / "export.mp4"
        mux(fitted, source, srt, output, config, burn=burn, style=style, ass=ass_path, ass_subtitles=ass_subtitles,
            extra_filters=mark)
        fitted.unlink(missing_ok=True)
        seconds = float(final["data"]["total_seconds"])
        validate_final(output, seconds, burn, config.studio.render.fps, size=(render.width, render.height))
        files = {"mp4": file_info(config, ctx.job_id, output)}
        if ass_path is not None:
            files["ass"] = file_info(config, ctx.job_id, ass_path)
        data = {"mode": "derive", "canvas": ctx.sid, "fit": item["fit"], "compose": final["id"],
                "size": [render.width, render.height], "total_seconds": seconds, "boundaries": boundaries,
                "bookends": bookends or None}
        if final["data"].get("music"):
            data["music"] = final["data"]["music"]
        return data, files


register(ExportHandler())
