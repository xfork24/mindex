"""TTS 语音合成客户端：OpenAI 风格、MiniMax、阿里 DashScope。"""

from __future__ import annotations

import base64
import json

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.shared.config import Config


def decode_audio(payload: bytes) -> bytes:
    """llama-server 各版本 /audio/speech 返回格式不一：按魔数识别二进制，JSON 则尝试 base64。"""
    for magic in (b"RIFF", b"ID3", b"OggS", b"fLaC"):
        if payload.startswith(magic):
            return payload
    if payload[:1] == b"\xff":
        return payload
    try:
        document = json.loads(payload)
    except (ValueError, TypeError):
        raise ModelError("TTS 服务未返回可识别的音频数据") from None
    if isinstance(document, dict):
        for key in ("audio", "data", "content", "b64_json"):
            value = document.get(key)
            if isinstance(value, str):
                try:
                    return base64.b64decode(value, validate=True)
                except (ValueError, TypeError):
                    continue
    raise ModelError("TTS 服务返回的 JSON 中没有可解码的音频字段")


class OpenAISpeaker:
    def __init__(self, config: Config):
        self.profile = config.studio.tts

    def speak(self, text: str, voice: str | None = None) -> bytes:
        payload = adapters.request_bytes(
            "POST",
            self.profile.base_url.rstrip("/") + self.profile.speech_path,
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": self.profile.model,
                "input": text,
                "voice": voice or self.profile.voice,
                "response_format": self.profile.response_format,
            },
        )
        return decode_audio(payload)


class MiniMaxSpeaker:
    """MiniMax t2a_v2：JSON 响应 data.audio 为 hex 编码音频；克隆音色直接用自定义 voice_id。"""

    def __init__(self, config: Config):
        self.profile = config.studio.tts

    def speak(self, text: str, voice: str | None = None) -> bytes:
        payload = adapters.request(
            "POST",
            self.profile.base_url.rstrip("/") + "/t2a_v2",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": self.profile.model,
                "text": text,
                "voice_setting": {"voice_id": voice or self.profile.voice},
                "audio_setting": {"format": self.profile.response_format},
            },
        )
        status = (payload.get("base_resp") or {}).get("status_code", 0)
        if status != 0:
            raise ModelError(f"MiniMax TTS 合成失败：{payload.get('base_resp')}")
        audio = (payload.get("data") or {}).get("audio")
        if not isinstance(audio, str) or not audio:
            raise ModelError("MiniMax TTS 未返回音频数据")
        try:
            return bytes.fromhex(audio)
        except ValueError as exc:
            raise ModelError("MiniMax TTS 音频 hex 解码失败") from exc


class DashScopeSpeaker:
    """阿里百炼原生协议 TTS（qwen-tts 系列，multimodal-generation 端点）。"""

    def __init__(self, config: Config):
        self.profile = config.studio.tts

    def speak(self, text: str, voice: str | None = None) -> bytes:
        payload = adapters.request(
            "POST",
            self.profile.base_url.rstrip("/") + "/api/v1/services/aigc/multimodal-generation/generation",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": self.profile.model,
                "input": {
                    "text": text,
                    "voice": voice or self.profile.voice,
                    "format": self.profile.response_format,
                },
            },
        )
        audio = (payload.get("output") or {}).get("audio") or {}
        url = audio.get("url")
        if isinstance(url, str) and url:
            return adapters.request_bytes("GET", url, timeout=self.profile.timeout)
        data = audio.get("data")
        if isinstance(data, str) and data:
            try:
                return base64.b64decode(data, validate=True)
            except (ValueError, TypeError) as exc:
                raise ModelError("DashScope TTS 音频 base64 解码失败") from exc
        raise ModelError("DashScope TTS 未返回可用的音频 URL 或数据")
