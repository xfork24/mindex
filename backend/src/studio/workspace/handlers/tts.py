"""配音阶段（逐镜）：按方案音色合成旁白音频。"""

from __future__ import annotations

from mindex.models.adapters import make_speaker
from mindex.shared.media import audio_info
from mindex.studio import studio
from mindex.studio.studio_artifacts import sanitized

from ..plans import effective_voice
from ..stages import StageHandler, register, shot_content
from ..versions import file_info, version


class TtsHandler(StageHandler):
    name, label, scope = "tts", "配音", "shot"
    checks_parameters = False  # 参数变化已由 conflict_labels 逐项说明

    def settings(self, config, p, *, store=None, display=False):
        voice = effective_voice(config, p)
        return {
            "voice": voice,
            "tts": sanitized({**config.studio.tts.model_dump(), "voice": voice}),
        }

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, content = shot_content(w, p, sid)
        return {"text": content["text"]}, {"content": selected["content"]}, params

    def run(self, ctx):
        _, content = shot_content(ctx.w, ctx.p, ctx.sid)
        target = ctx.directory / "audio.wav"
        speaker = ctx.services.get("speaker") or make_speaker(ctx.config)
        payload = speaker.speak(content["text"], ctx.spec["parameters"]["voice"])
        if not payload:
            raise studio.StudioError("配音返回空音频")
        studio.save_bytes(target, payload)
        return audio_info(target), {"audio": file_info(ctx.config, ctx.job_id, target)}

    def conflict_labels(self, config, w, item, expected, messages):
        found = []
        old_content = w["versions"].get(item["inputs"].get("content"), {}).get("data", {})
        new_content = version(w, expected["inputs"]["content"])["data"]
        if "text" in old_content and old_content["text"] != new_content["text"]:
            found.append("旁白已变化")
        before = item["parameters"].get("tts", {})
        after = expected["parameters"]["tts"]
        for field, label in (("backend", "配音服务"), ("model", "配音模型"), ("voice", "方案音色")):
            if before.get(field) != after.get(field):
                found.append(f"{label}已变化：{before.get(field) or '未记录'} → {after.get(field)}")
        if item["parameters"] != expected["parameters"] and not (messages or found):
            found.append("配音服务参数已变化")
        return found


register(TtsHandler())
