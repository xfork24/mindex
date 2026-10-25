"""背景音乐阶段（逐镜，只在背景音乐提示点分镜上）：调用音乐生成模型生成（或翻唱）一段背景音乐。

- 提示点 generate：按描述生成纯音乐（instrumental=True，缺省）或带歌词的歌曲（歌词为空时由模型按描述写词）；
  cover：按上传的参考音频（audio_id）翻唱，描述为目标风格；
- 背景音乐从提示点分镜起顺延到下一个提示点之前的全部分镜，成片合成时循环/截断到段落长度、淡入淡出并在配音下自动压低；
- 签名：提示点（不含音量）与生成服务（backend/model，ComfyUI 另含工作流摘要，本地后端另含段落预估时长；audiocpp 另含采样步数）；
- 产物：``versions/<vid>/music.<ext>`` 与 ``request.json``。
"""

from __future__ import annotations

from mindex.models.adapters import make_music_generator
from mindex.models.music_generators import MusicRequest
from mindex.shared.media import MediaError, audio_info
from mindex.studio import studio
from mindex.studio.studio_artifacts import sanitized

from .. import sources
from ..music import cue_identity, segment_seconds
from ..stages import StageHandler, music_cue, register, shot_content
from ..versions import file_info

MUSIC_VERSION = 1
NOT_CONFIGURED = "未配置音乐生成服务，请在设置页激活音乐生成档案"


def unavailable(config) -> str | None:
    cfg = config.studio.music_generator
    if not cfg.enabled:
        return NOT_CONFIGURED
    if cfg.backend == "comfyui" and (cfg.comfyui_workflow is None or not cfg.comfyui_mapping):
        return "ComfyUI 音乐生成档案未导入工作流或节点映射，请在设置页补全"
    if cfg.backend != "comfyui" and not cfg.model.strip():
        return "音乐生成档案未填写模型名，请在设置页补全"
    return None


def audio_suffix(payload: bytes) -> str:
    if payload[:4] == b"RIFF":
        return ".wav"
    if payload[:4] == b"fLaC":
        return ".flac"
    if payload[:4] == b"OggS":
        return ".ogg"
    return ".mp3"


class MusicHandler(StageHandler):
    name, label, scope = "music", "背景音乐", "shot"

    def settings(self, config, p, *, store=None, display=False):
        return {"music_generator": config.studio.music_generator.signature()}

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, content = shot_content(w, p, sid)
        cue = music_cue(content) or {}
        payload = {"music": cue_identity(cue), "music_version": MUSIC_VERSION}
        if config.studio.music_generator.backend in ("comfyui", "audiocpp"):  # 本地后端按段落时长生成
            payload["seconds"] = segment_seconds(w, p, sid)
        return payload, {"content": selected["content"]}, params

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        if found:
            return
        _, content = shot_content(w, p, sid)
        cue = music_cue(content) or {}
        reason = unavailable(config)
        if reason is None and cue.get("audio_id") and not sources.present(config, job_id, cue["audio_id"], "audio"):
            reason = "翻唱参考音频缺失或已损坏，请重新上传"
        if reason:
            node.update(action="blocked", reason=reason)

    def run(self, ctx):
        generator = ctx.services.get("music_generator")
        if generator is None:
            if reason := unavailable(ctx.config):
                raise studio.StudioError(reason)
            generator = make_music_generator(ctx.config)
        _, content = shot_content(ctx.w, ctx.p, ctx.sid)
        cue = music_cue(content)
        if cue is None:
            raise studio.StudioError("该分镜没有背景音乐提示点")
        audio = None
        if cue["kind"] == "cover":
            audio = sources.reference_file(ctx.config, ctx.job_id, "audio", cue["audio_id"])
        record_path = ctx.directory / "request.json"
        record = {"music": cue_identity(cue), "generator": ctx.spec["parameters"].get("music_generator"),
                  "submitted_at": studio.now(), "finished_at": None}
        studio.save(record_path, sanitized(record))
        prompt = cue.get("prompt") or "轻柔的纯音乐背景配乐"
        payload, extra = generator.generate(MusicRequest(
            prompt=prompt, lyrics=cue.get("lyrics"), instrumental=bool(cue.get("instrumental", True)),
            cover_audio=audio, seconds=segment_seconds(ctx.w, ctx.p, ctx.sid),
        ))
        if not payload:
            raise studio.StudioError("音乐生成返回空音频")
        target = ctx.directory / f"music{audio_suffix(payload)}"
        studio.save_bytes(target, payload)
        try:
            info = audio_info(target)
        except (MediaError, ValueError, KeyError) as exc:
            record.update(finished_at=studio.now(), error=f"生成的音乐无法解码：{exc}")
            studio.save(record_path, sanitized(record))
            raise studio.StudioError(f"生成的音乐无法解码：{exc}") from exc
        record.update(finished_at=studio.now(), extra=extra)
        studio.save(record_path, sanitized(record))
        data = {"kind": cue["kind"], "prompt": prompt, "lyrics": cue.get("lyrics"),
                "instrumental": bool(cue.get("instrumental", True)), "duration": info["duration"]}
        return data, {"music": file_info(ctx.config, ctx.job_id, target),
                      "request": file_info(ctx.config, ctx.job_id, record_path)}

    def conflict_labels(self, config, w, item, expected, messages):
        found = []
        before = item["parameters"].get("music_generator") or {}
        after = expected["parameters"].get("music_generator") or {}
        for field, label in (("backend", "音乐生成服务"), ("model", "音乐生成模型")):
            if before.get(field) != after.get(field):
                found.append(f"{label}已变化：{before.get(field) or '未记录'} → {after.get(field) or '未设置'}")
        if not found and not messages:
            found.append("背景音乐设置已变化")
        return found


register(MusicHandler())
