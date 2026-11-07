"""成片合成阶段（终态）：按分镜顺序拼接全部分镜渲染结果；有背景音乐段落时把各段铺在配音下面。"""

from __future__ import annotations

from studio.workspace import music, post
from studio.workspace.handlers.render import canvas_settings
from studio.workspace.plans import canvas_render
from studio.workspace.post import MUSIC_MIX_VERSION, compose
from studio.workspace.stages import StageHandler, register
from studio.workspace.versions import ordered_shots


def music_payload(w, p) -> list:
    """背景音乐段落的签名：每段的起始分镜、覆盖的分镜、音乐版本与音量；没有背景音乐时为空（不进签名）。"""
    return [
        {
            "cue": segment["cue"],
            "shots": segment["shots"],
            "music": p["shots"][segment["cue"]].get("music"),
            "volume": segment["music"].get("volume", 0.25),
        }
        for segment in music.segments(w, p)
    ]


class ComposeHandler(StageHandler):
    name, label, scope = "compose", "成片合成", "final"
    buildable = False
    column = False
    reason = "全部分镜就绪后合成"

    def settings(self, config, p, *, store=None, display=False):
        o = p["options"]
        return canvas_settings(
            p,
            {
                "burn": o["burn"],
                "subtitle": {**config.studio.subtitle.model_dump(), "burn": o["burn"]},
                "render": canvas_render(config, p)[0].model_dump(),
            },
        )

    def specification(self, config, job_id, w, p, sid, params, resources):
        shots = ordered_shots(w, p)
        inputs = {"board": p["board"], **{shot: p["shots"][shot].get("render") for shot in shots}}
        # Query/estimate-only board edits do not change an already rendered film.
        # Provenance still records the exact board used when this version was produced.
        payload = {"renders": [[shot, inputs[shot]] for shot in shots]}
        segments = music_payload(w, p)
        if segments:  # 只在有背景音乐时加键：没有背景音乐的成片签名与旧版逐字节一致
            payload["music"] = segments
            payload["music_mix"] = MUSIC_MIX_VERSION
            inputs.update({f"music:{item['cue']}": item["music"] for item in segments})
        # 后期能力：都未参与时为空；数据库只在方案选了品牌模板时才打开。
        payload.update(post.compose_payload(config, w, p, resources.database if resources else None))
        return payload, inputs, params

    def dependencies(self, w, p, sid, *, resplit=False):
        if resplit:
            return ["split"]
        return [f"render:{s}" for s in ordered_shots(w, p)] + [
            f"music:{segment['cue']}" for segment in music.segments(w, p)
        ]

    def run(self, ctx):
        return compose(ctx.config, ctx.job_id, ctx.w, ctx.p, ctx.directory)


register(ComposeHandler())
