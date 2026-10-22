"""ASR 转写客户端：自研 mindex 协议、OpenAI 兼容 chat、MiniMax。"""

from __future__ import annotations

import base64
import math
from pathlib import Path

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.shared.config import Config
from mindex.shared.types import Utterance


def parse_minimax_words(payload: dict, duration: float | None = None) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise ModelError("MiniMax ASR 缺少 text 字段")
    if payload.get("base_resp", {}).get("status_code", 0):
        raise ModelError(f"MiniMax ASR 失败：{payload['base_resp']}")
    values = payload.get("words")
    if values is None:
        values = payload.get("segments", [])
    if not isinstance(values, list):
        raise ModelError("MiniMax ASR 时间戳格式无效")
    words = []
    previous = -1.0
    for value in values:
        if not isinstance(value, dict):
            raise ModelError("MiniMax ASR 时间戳格式无效")
        text = value.get("word", value.get("text", ""))
        if not isinstance(text, str):
            raise ModelError("MiniMax ASR 词文本无效")
        if not text.strip():
            continue
        try:
            start, end = float(value["start"]), float(value["end"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ModelError("非空对白缺少有效时间戳") from exc
        if (
            not all(math.isfinite(v) for v in (start, end))
            or start < 0
            or end <= start
            or start < previous
            or (duration is not None and end > duration + 0.2)
        ):
            raise ModelError("MiniMax ASR 时间戳无效或超出音频区间")
        previous = start
        word = {"start": start, "end": min(end, duration) if duration is not None else end, "text": text}
        for field in ("speaker", "speaker_id"):
            if field in value:
                word["speaker_id"] = value[field]
        words.append(word)
    if payload["text"].strip() and not words:
        raise ModelError("非空对白缺少有效词级时间戳，拒绝伪造定位")
    return words


class HTTPTranscriber:
    def __init__(self, config: Config):
        self.profile = config.asr

    def transcribe(self, path: Path) -> list[Utterance]:
        with path.open("rb") as handle:
            payload = adapters.request(
                "POST",
                self.profile.base_url.rstrip("/") + "/transcribe",
                timeout=self.profile.timeout,
                headers=self.profile.headers(),
                files={"file": (path.name, handle, "audio/wav")},
            )
        if payload.get("model") != self.profile.model:
            raise ModelError("ASR 服务模型不匹配")
        if self.profile.revision != "main" and payload.get("revision") != self.profile.revision:
            raise ModelError("ASR 服务权重版本不匹配")
        try:
            return [Utterance.model_validate(item) for item in payload["utterances"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError("ASR 服务未返回有效的带时间戳文本") from exc


class OpenAIChatTranscriber:
    """OpenAI 兼容 chat + input_audio 的 ASR（如百炼 qwen3-asr-flash）。

    只返回纯文本，与 llama_cpp 后端同样走「transcribe 保存原始文本 → align 补时间戳」流程。
    """

    def __init__(self, config: Config):
        self.profile = config.asr

    def raw(self, path: Path) -> dict:
        audio = base64.b64encode(path.read_bytes()).decode()
        payload = adapters.request(
            "POST",
            self.profile.base_url.rstrip("/") + "/chat/completions",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": self.profile.model,
                "temperature": 0,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": f"data:audio/wav;base64,{audio}", "format": "wav"},
                            }
                        ],
                    }
                ],
            },
        )
        try:
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("空文本")
            return {"text": content.strip(), "language": "", "raw_response": payload}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError("ASR 服务未返回有效文本；无法伪造成带时间戳对白") from exc

    def transcribe(self, path: Path) -> list[Utterance]:
        raise ModelError("openai 协议 ASR 只返回纯文本；请先 transcribe 保存原文，再执行 align 补时间戳")


class MiniMaxTranscriber:
    """MiniMax word timestamps; raw() remains available for legacy manual alignment."""

    def __init__(self, config: Config):
        self.profile = config.asr

    def raw(self, path: Path, *, timestamps: bool = False) -> dict:
        data = {"model": self.profile.model}
        if timestamps:
            data.update(response_format="verbose_json", timestamp_level="word")
        with path.open("rb") as handle:
            payload = adapters.request(
                "POST",
                self.profile.base_url.rstrip("/") + "/speech_to_text",
                timeout=self.profile.timeout,
                headers=self.profile.headers(),
                data=data,
                files={"file": (path.name, handle, "audio/wav")},
            )
        if timestamps:
            # Persist the complete upstream response before parsing/validating it.
            return {"raw_response": payload}
        if payload.get("base_resp", {}).get("status_code", 0) != 0:
            raise ModelError(f"MiniMax ASR 失败：{payload['base_resp']}")
        text = payload.get("text")
        if not isinstance(text, str):
            raise ModelError("MiniMax ASR 服务未返回有效文本；无法伪造成带时间戳对白")
        return {"text": text.strip(), "language": payload.get("language", ""), "raw_response": payload}

    def transcribe(self, path: Path) -> list[Utterance]:
        response = self.raw(path, timestamps=True)
        return [Utterance.model_validate(word) for word in parse_minimax_words(response["raw_response"])]
