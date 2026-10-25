"""背景音乐生成后端：MiniMax 音乐生成（music-3.0）、翻唱（music-cover）与歌词生成，实现 ``adapters.MusicGenerator``。

协议出处（核对日期 2026-10-25）：

- 音乐生成 / 翻唱：https://platform.minimax.cn/docs/api-reference/music-generation （POST /v1/music_generation，
  同步返回；output_format=hex 时 data.audio 为十六进制音频；翻唱用 music-cover + audio_base64 参考音频）
- 歌词生成：https://platform.minimax.cn/docs/api-reference/lyrics-generation （POST /v1/lyrics_generation，
  mode=write_full_song / edit，返回 song_title、style_tags、lyrics）

平台公告：自 2026-08-20 起付费音乐与歌词接口不再向新用户开放，历史付费用户可继续使用；无权限时平台返回的错误
会原样转述。``health()`` 发送缺少必填参数的请求，只验证鉴权与可达性，不生成音乐（不计费）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.models.image_generators import minimax_health
from mindex.models.video_generators import MINIMAX_AUTH, _error_body
from mindex.shared.config import Config

AUDIO_SETTING = {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"}


class MusicContentRejected(ModelError):
    """音乐生成服务的内容审核拒绝了描述、歌词或参考音频。"""


@dataclass(frozen=True)
class MusicRequest:
    prompt: str
    lyrics: str | None = None
    instrumental: bool = True  # 背景音乐缺省为纯音乐
    lyrics_optimizer: bool = False  # 歌词为空且非纯音乐时由模型按描述写歌词
    cover_audio: Path | None = None  # 翻唱参考音频（有值即走翻唱模型）
    seconds: float | None = None  # 期望时长（背景音乐段落时长）：ComfyUI 映射用，MiniMax 不支持指定时长
    seed: int | None = None


@dataclass(frozen=True)
class Lyrics:
    title: str
    style_tags: str
    lyrics: str


def music_message(code: int, detail: str = "") -> str:
    suffix = f"（{detail}）" if detail else ""
    if code in MINIMAX_AUTH:
        return f"MiniMax 鉴权失败，请检查音乐生成档案的 API Key{suffix}"
    if code in (1008, 2053, 2056, 2067):
        return f"MiniMax 账户余额或额度不足，或当前账户没有音乐生成权限{suffix}"
    if code in (1026, 1027):
        return f"MiniMax 内容审核未通过：描述、歌词或参考音频含敏感内容，请修改后重试{suffix}"
    if code in (1002, 1039, 1041, 1045, 2045):
        return f"MiniMax 限流或并发超限，请稍后重试{suffix}"
    if code == 2013:
        return f"MiniMax 音乐生成请求参数无效（描述/歌词长度或参考音频不符合要求）{suffix}"
    return f"MiniMax 音乐生成接口返回错误 {code}{suffix}"


def _error(code: int, detail: str = "") -> ModelError:
    return (MusicContentRejected if code in (1026, 1027) else ModelError)(music_message(code, detail))


class MiniMaxMusicGenerator:
    """MiniMax 音乐：base_url 与语音合成预设一致（``https://api.minimax.cn/v1``）。"""

    def __init__(self, config: Config):
        self.profile = config.studio.music_generator

    def _url(self, path: str) -> str:
        return self.profile.base_url.rstrip("/") + path

    def _call(self, path: str, body: dict, audit: dict | None = None) -> dict:
        try:
            payload = adapters.request("POST", self._url(path), timeout=self.profile.timeout,
                                       headers=self.profile.headers(), json=body, audit=audit)
        except ModelError as exc:
            status, error = _error_body(exc)
            code = (error.get("base_resp") or {}).get("status_code")
            if code:
                raise _error(int(code), str((error.get("base_resp") or {}).get("status_msg") or "")) from exc
            if status in (401, 403):
                raise ModelError(music_message(1004)) from exc
            raise
        base = payload.get("base_resp") or {}
        code = int(base.get("status_code") or 0)
        if code:
            raise _error(code, str(base.get("status_msg") or ""))
        return payload

    def request_body(self, request: MusicRequest) -> dict:
        if request.cover_audio is not None:
            body = {"model": self.profile.cover_model, "prompt": request.prompt,
                    "audio_base64": base64.b64encode(request.cover_audio.read_bytes()).decode("ascii")}
            if request.lyrics:
                body["lyrics"] = request.lyrics
        else:
            body = {"model": self.profile.model, "prompt": request.prompt,
                    "is_instrumental": request.instrumental}
            if request.lyrics and not request.instrumental:
                body["lyrics"] = request.lyrics
            elif not request.instrumental:
                body["lyrics_optimizer"] = True
        body.update(stream=False, output_format="hex", audio_setting=dict(AUDIO_SETTING))
        return body

    def generate(self, request: MusicRequest) -> tuple[bytes, dict]:
        """返回 (MP3 字节, 附加信息：时长毫秒、采样率等)。"""
        body = self.request_body(request)
        audit = {**body, "audio_base64": f"<{len(body['audio_base64'])} 字符>"} if "audio_base64" in body else body
        payload = self._call("/music_generation", body, audit)
        audio = (payload.get("data") or {}).get("audio")
        if not isinstance(audio, str) or not audio:
            raise ModelError("MiniMax 音乐生成没有返回音频")
        try:
            data = bytes.fromhex(audio)
        except ValueError as exc:
            raise ModelError("MiniMax 音乐生成返回的音频无法解码") from exc
        return data, dict(payload.get("extra_info") or {})

    def lyrics(self, prompt: str, *, mode: str = "write_full_song", lyrics: str | None = None,
               title: str | None = None) -> Lyrics:
        body = {"mode": mode, "prompt": prompt}
        if mode == "edit" and lyrics:
            body["lyrics"] = lyrics
        if title:
            body["title"] = title
        payload = self._call("/lyrics_generation", body)
        text = payload.get("lyrics")
        if not isinstance(text, str) or not text.strip():
            raise ModelError("MiniMax 歌词生成没有返回歌词")
        return Lyrics(title=str(payload.get("song_title") or title or ""),
                      style_tags=str(payload.get("style_tags") or ""), lyrics=text)

    def health(self) -> dict:
        return minimax_health(self._url("/music_generation"), self.profile, {"model": self.profile.model},
                              "鉴权通过：音乐生成接口可用（未生成音乐）", "音乐生成")


class ComfyUIMusicGenerator:
    """本地 ComfyUI 音乐生成（如 ACE-Step 工作流）：把描述、歌词、时长、种子与翻唱参考音频按节点映射写入
    「导出（API）」工作流，完成后取第一个输出音频。歌词生成不受支持。"""

    def __init__(self, config: Config):
        from mindex.models.video_generators import ComfyUIVideoGenerator
        from mindex.shared.comfyui import MUSIC_MAPPING_FIELDS

        class Runner(ComfyUIVideoGenerator):
            label, noun, role = "ComfyUI 音乐生成", "音频", "音乐生成"
            suffixes = (".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a")
            output_keys = ("audio", "audios")
            fields = MUSIC_MAPPING_FIELDS

        self.profile = config.studio.music_generator
        self.runner = Runner(config, self.profile)

    def generate(self, request: MusicRequest) -> tuple[bytes, dict]:
        import tempfile

        mapping = self.profile.comfyui_mapping or {}
        if request.cover_audio is not None and "audio" not in mapping:
            raise ModelError("翻唱需要参考音频，但 ComfyUI 节点映射没有 audio 字段；请映射加载音频节点的输入")
        values = {"prompt": request.prompt, "lyrics": request.lyrics if not request.instrumental else None,
                  "seconds": round(request.seconds) if request.seconds else None, "seed": request.seed}
        if request.cover_audio is not None:
            values["audio"] = self.runner._upload(request.cover_audio)
        with tempfile.TemporaryDirectory(prefix="comfyui-music-") as directory:
            target = Path(directory) / "music.bin"
            self.runner.run(values, target)
            return target.read_bytes(), {"backend": "comfyui"}

    def lyrics(self, prompt: str, *, mode: str = "write_full_song", lyrics: str | None = None,
               title: str | None = None) -> Lyrics:
        raise ModelError("当前音乐生成档案是本地 ComfyUI，不支持歌词生成；请切换到 MiniMax 音乐生成档案或手动填写歌词")

    def health(self) -> dict:
        return self.runner.health()
