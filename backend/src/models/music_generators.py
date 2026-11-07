"""背景音乐生成后端：MiniMax
音乐生成（music-3.0）、翻唱（music-cover）与歌词生成，实现
``adapters.MusicGenerator``。

协议出处（核对日期 2026-10-25）：

- 音乐生成 /
翻唱：https://platform.minimax.cn/docs/api-reference/music-generation
（POST /v1/music_generation，
同步返回；output_format=hex 时 data.audio 为十六进制音频；翻唱用
music-cover + audio_base64 参考音频）
-
歌词生成：https://platform.minimax.cn/docs/api-reference/lyrics-generation
（POST /v1/lyrics_generation，
mode=write_full_song / edit，返回
song_title、style_tags、lyrics）

平台公告：自 2026-08-20
起付费音乐与歌词接口不再向新用户开放，历史付费用户可继续使用；无权限时平台返回的错误
会原样转述。``health()``
发送缺少必填参数的请求，只验证鉴权与可达性，不生成音乐（不计费）。

本地 audio.cpp（``audiocpp`` 后端）：``audiocpp_server``
的 ``POST /v1/tasks/run``（family
``minimax_music3``，
MiniMax-Music3-GGUF），同步返回 base64 WAV；歌词必填，纯音乐用
``[Instrumental]`` 结构标签；不支持翻唱与歌词生成。"""

from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path

from models import adapters
from models.adapters import ModelError
from models.image_generators import minimax_health
from models.video_generators import MINIMAX_AUTH, ComfyUIVideoGenerator, _error_body
from shared.comfyui import MUSIC_MAPPING_FIELDS
from shared.config import Config

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
            payload = adapters.request(
                "POST",
                self._url(path),
                timeout=self.profile.timeout,
                headers=self.profile.headers(),
                json=body,
                audit=audit,
            )
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
            body = {
                "model": self.profile.cover_model,
                "prompt": request.prompt,
                "audio_base64": base64.b64encode(request.cover_audio.read_bytes()).decode("ascii"),
            }
            if request.lyrics:
                body["lyrics"] = request.lyrics
        else:
            body = {
                "model": self.profile.model,
                "prompt": request.prompt,
                "is_instrumental": request.instrumental,
            }
            if request.lyrics and not request.instrumental:
                body["lyrics"] = request.lyrics
            elif not request.instrumental:
                body["lyrics_optimizer"] = True
        body.update(stream=False, output_format="hex", audio_setting=dict(AUDIO_SETTING))
        return body

    def generate(self, request: MusicRequest) -> tuple[bytes, dict]:
        """返回 (MP3 字节, 附加信息：时长毫秒、采样率等)。"""
        body = self.request_body(request)
        audit = (
            {**body, "audio_base64": f"<{len(body['audio_base64'])} 字符>"}
            if "audio_base64" in body
            else body
        )
        payload = self._call("/music_generation", body, audit)
        audio = (payload.get("data") or {}).get("audio")
        if not isinstance(audio, str) or not audio:
            raise ModelError("MiniMax 音乐生成没有返回音频")
        try:
            data = bytes.fromhex(audio)
        except ValueError as exc:
            raise ModelError("MiniMax 音乐生成返回的音频无法解码") from exc
        return data, dict(payload.get("extra_info") or {})

    def lyrics(
        self,
        prompt: str,
        *,
        mode: str = "write_full_song",
        lyrics: str | None = None,
        title: str | None = None,
    ) -> Lyrics:
        body = {"mode": mode, "prompt": prompt}
        if mode == "edit" and lyrics:
            body["lyrics"] = lyrics
        if title:
            body["title"] = title
        payload = self._call("/lyrics_generation", body)
        text = payload.get("lyrics")
        if not isinstance(text, str) or not text.strip():
            raise ModelError("MiniMax 歌词生成没有返回歌词")
        return Lyrics(
            title=str(payload.get("song_title") or title or ""),
            style_tags=str(payload.get("style_tags") or ""),
            lyrics=text,
        )

    def health(self) -> dict:
        return minimax_health(
            self._url("/music_generation"),
            self.profile,
            {"model": self.profile.model},
            "鉴权通过：音乐生成接口可用（未生成音乐）",
            "音乐生成",
        )


class ComfyUIMusicGenerator:
    """本地 ComfyUI 音乐生成（如 ACE-Step 工作流）：把描述、歌词、时长、种子与翻唱参考音频按节点映射写入
    「导出（API）」工作流，完成后取第一个输出音频。歌词生成不受支持。"""

    def __init__(self, config: Config):

        class Runner(ComfyUIVideoGenerator):
            label, noun, role = "ComfyUI 音乐生成", "音频", "音乐生成"
            suffixes = (".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a")
            output_keys = ("audio", "audios")
            fields = MUSIC_MAPPING_FIELDS

        self.profile = config.studio.music_generator
        self.runner = Runner(config, self.profile)

    def generate(self, request: MusicRequest) -> tuple[bytes, dict]:

        mapping = self.profile.comfyui_mapping or {}
        if request.cover_audio is not None and "audio" not in mapping:
            raise ModelError("翻唱需要参考音频，但 ComfyUI 节点映射没有 audio 字段；请映射加载音频节点的输入")
        values = {
            "prompt": request.prompt,
            "lyrics": request.lyrics if not request.instrumental else None,
            "seconds": round(request.seconds) if request.seconds else None,
            "seed": request.seed,
        }
        if request.cover_audio is not None:
            values["audio"] = self.runner._upload(request.cover_audio)
        with tempfile.TemporaryDirectory(prefix="comfyui-music-") as directory:
            target = Path(directory) / "music.bin"
            self.runner.run(values, target)
            return target.read_bytes(), {"backend": "comfyui"}

    def lyrics(
        self,
        prompt: str,
        *,
        mode: str = "write_full_song",
        lyrics: str | None = None,
        title: str | None = None,
    ) -> Lyrics:
        raise ModelError(
            "当前音乐生成档案是本地 ComfyUI，不支持歌词生成；请切换到 MiniMax 音乐生成档案或手动填写歌词"
        )

    def health(self) -> dict:
        return self.runner.health()


# duration_sec 是自回归帧预算，不是成片时长硬上限；实际生成时长上限由档案的 max_seconds 决定
# （背景音乐成片合成时会循环/截断到段落长度，不必生成整段）。
AUDIOCPP_DEFAULT_SECONDS = 20
AUDIOCPP_INSTRUMENTAL = "[Instrumental]"


class AudioCppMusicGenerator:
    """本地 audio.cpp 服务（``audiocpp_server``）上的 MiniMax-Music3-GGUF：不计费、无需密钥（可选 Bearer）。

    生成是同步长请求（实测 Apple Silicon 的 Metal 后端 30 秒音频约 15 分钟，实时率约 30），耗时与生成时长、
    ``num_inference_steps`` 近似成正比，因此只发一次、不自动重试，生成时长受 ``max_seconds`` 限制，
    超时由档案的 ``timeout`` 控制（须大于预计耗时，否则客户端放弃而服务端仍在白白计算）。"""

    def __init__(self, config: Config):
        self.profile = config.studio.music_generator

    def _url(self, path: str) -> str:
        return self.profile.base_url.rstrip("/") + path

    def request_body(self, request: MusicRequest) -> dict:
        lyrics = AUDIOCPP_INSTRUMENTAL if request.instrumental else (request.lyrics or "").strip()
        if not lyrics:
            raise ModelError("本地音乐生成模型需要歌词：请填写歌词，或把该提示点设为纯音乐")
        seconds = min(self.profile.max_seconds, max(1, round(request.seconds or AUDIOCPP_DEFAULT_SECONDS)))
        options = {
            "lyrics": lyrics,
            "duration_sec": str(seconds),
            "num_inference_steps": str(self.profile.num_inference_steps),
        }
        if request.seed is not None:
            options["seed"] = str(request.seed)
        return {"model": self.profile.model, "request": {"text": request.prompt, "options": options}}

    def generate(self, request: MusicRequest) -> tuple[bytes, dict]:
        if request.cover_audio is not None:
            raise ModelError("本地 audio.cpp 音乐生成不支持翻唱；请切换到 MiniMax 音乐生成档案")
        body = self.request_body(request)
        payload = adapters.request(
            "POST",
            self._url("/v1/tasks/run"),
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json=body,
            attempts=1,
        )
        audio = payload.get("audio") if isinstance(payload, dict) else None
        if not isinstance(audio, str) or not audio:
            raise ModelError("本地音乐生成服务没有返回音频")
        try:
            data = base64.b64decode(audio, validate=True)
        except ValueError as exc:
            raise ModelError("本地音乐生成服务返回的音频无法解码") from exc
        # request：实际发给服务的内容（描述 text 与 options），记入 request.json 便于核对。
        return data, {
            "backend": "audiocpp",
            "request": body["request"],
            "sample_rate": payload.get("sample_rate"),
            "channels": payload.get("channels"),
            "timing": payload.get("timing"),
        }

    def lyrics(
        self,
        prompt: str,
        *,
        mode: str = "write_full_song",
        lyrics: str | None = None,
        title: str | None = None,
    ) -> Lyrics:
        raise ModelError(
            "当前音乐生成档案是本地 audio.cpp，不支持歌词生成；请切换到 MiniMax 音乐生成档案或手动填写歌词"
        )

    def health(self) -> dict:
        """只读探测：GET /health 与 /v1/models，确认服务可达且模型 id 已登记；绝不触发生成或加载模型。"""
        headers = self.profile.headers()
        try:
            adapters.request(
                "GET",
                self._url("/health"),
                timeout=min(self.profile.timeout, 15),
                headers=headers,
                attempts=1,
            )
            models = adapters.request(
                "GET",
                self._url("/v1/models"),
                timeout=min(self.profile.timeout, 15),
                headers=headers,
                attempts=1,
            )
        except ModelError as exc:
            raise ModelError(f"无法连接本地音乐生成服务 {self.profile.base_url}：{exc}") from exc
        items = {item.get("id"): item for item in models.get("data") or [] if isinstance(item, dict)}
        item = items.get(self.profile.model)
        if item is None:
            raise ModelError(
                f"服务可达，但没有登记模型 {self.profile.model!r}；"
                f"已登记：{', '.join(map(str, items)) or '无'}"
            )
        if item.get("family") != "minimax_music3":
            raise ModelError(
                f"模型 {self.profile.model!r} 的 family 是 {item.get('family')!r}，需要 minimax_music3"
            )
        return {"ready": True, "reply": "服务可达，模型已登记（未生成音乐；首次生成时才会加载权重）"}
