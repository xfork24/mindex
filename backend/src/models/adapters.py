"""模型适配层公共部分：错误类型、带重试的 HTTP 请求、各角色 Protocol 与按配置构造的工厂。

具体后端见 embedders、qwen、speech、storyboard、transcribers、detectors；工厂在调用时才导入它们。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Protocol

import httpx

from mindex.shared.config import Config
from mindex.shared.trace import record
from mindex.shared.types import Annotation, ShotDraft, Utterance


class ModelError(RuntimeError):
    pass


class AnalysisContentRejected(ModelError):
    """分析服务明确拒绝当前内容；不重试或改写输入。"""


def analysis_content_rejection(response) -> str | None:
    """只识别明确的内容安全错误，不把普通 4xx 当成可跳过内容。"""
    if response.status_code not in {400, 403, 422}:
        return None
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    codes = {str(error.get(key, "")).lower() for key in ("code", "type")} if isinstance(error, dict) else set()
    detail = _error_detail(response)
    explicit = re.search(
        r"不安全或敏感内容|内容安全审核未通过|内容审核不通过|违反.{0,12}内容.{0,8}(?:政策|策略)|"
        r"content[_ ](?:filter|policy[_ ]violation)|responsibleaipolicyviolation|"
        r"(?:unsafe|sensitive) content|content.{0,30}(?:blocked|rejected).{0,30}(?:safety|policy)",
        detail, re.IGNORECASE,
    )
    if codes & {"1301", "content_filter", "content_policy_violation", "responsibleaipolicyviolation"} or explicit:
        return detail or "模型内容安全策略拒绝此片段"
    return None


class InvalidAnalysisResponse(ModelError):
    """A model response that can be retried without changing the input."""


class TruncatedAnalysisResponse(InvalidAnalysisResponse):
    """The response exhausted its token budget before completing the JSON."""


class InvalidAnalysisStructure(InvalidAnalysisResponse):
    """Valid JSON whose fields or values do not satisfy the analysis contract."""


AUDIT_BODY_LIMIT = 8192


ERROR_DETAIL_LIMIT = 300


def _error_detail(response) -> str:
    """从 4xx/5xx 响应里取出可读原因。

    httpx 的异常串只有状态码和 URL，而 OpenAI 兼容服务把真实原因放在
    ``error.message`` 里（限流、参数超限、模型不存在等）。丢掉它会让调用方
    只能看到无意义的 "400 Bad Request"。
    """
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        return (response.text or "").strip()[:ERROR_DETAIL_LIMIT]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"].strip()[:ERROR_DETAIL_LIMIT]
        if isinstance(error, str):
            return error.strip()[:ERROR_DETAIL_LIMIT]
        message = payload.get("message") or payload.get("detail")
        if isinstance(message, str):
            return message.strip()[:ERROR_DETAIL_LIMIT]
    return (response.text or "").strip()[:ERROR_DETAIL_LIMIT]


def request(method: str, url: str, *, timeout: float = 180, attempts: int = 3, **kwargs) -> dict:
    for attempt in range(attempts):
        record("model-request", {"method": method, "url": url, "attempt": attempt,
                                 "json": kwargs.get("json")})
        try:
            response = httpx.request(method, url, timeout=timeout, trust_env=False, **kwargs)
            record(
                "model-response",
                {"status": response.status_code, "body": response.text[:AUDIT_BODY_LIMIT]},
            )
            response.raise_for_status()
            return response.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            retry = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {
                429,
                502,
                503,
                504,
            }
            if not retry or attempt == attempts - 1:
                detail = (
                    f"：{_error_detail(exc.response)}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else ""
                )
                raise ModelError(f"模型服务调用失败 {url}: {exc}{detail}") from exc
            # Rewind uploads before retrying a request.
            for value in kwargs.get("files", {}).values():
                handle = value[1] if isinstance(value, tuple) else value
                if hasattr(handle, "seek"):
                    handle.seek(0)
            time.sleep(0.25 * (2**attempt))
        except (ValueError, KeyError) as exc:
            raise ModelError(f"模型服务返回了无效 JSON: {url}") from exc
    raise AssertionError("unreachable")


def request_bytes(method: str, url: str, *, timeout: float = 180, attempts: int = 3, **kwargs) -> bytes:
    """同 request() 的重试语义，但返回原始字节（/audio/speech 等二进制端点）。"""
    for attempt in range(attempts):
        record("model-request", {"method": method, "url": url, "attempt": attempt,
                                 "json": kwargs.get("json")})
        try:
            response = httpx.request(method, url, timeout=timeout, trust_env=False, **kwargs)
            record("tts-response", {"status": response.status_code,
                                    "content_type": response.headers.get("content-type"),
                                    "bytes": len(response.content),
                                    "body": response.text[:AUDIT_BODY_LIMIT] if response.headers.get("content-type", "").startswith("application/json") else None})
            response.raise_for_status()
            return response.content
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            retry = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {
                429,
                502,
                503,
                504,
            }
            if not retry or attempt == attempts - 1:
                detail = (
                    f"：{_error_detail(exc.response)}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else ""
                )
                raise ModelError(f"模型服务调用失败 {url}: {exc}{detail}") from exc
            time.sleep(0.25 * (2**attempt))
    raise AssertionError("unreachable")


class MultimodalEmbedder(Protocol):
    def identity(self) -> dict: ...
    def text(self, texts: list[str], role: str = "document") -> list[list[float]]: ...
    def video(self, path: Path) -> list[float]: ...
    def image(self, path: Path) -> list[float]: ...


class VisualAnalyzer(Protocol):
    def analyze(self, path: Path, dialogue: list[dict]) -> Annotation: ...
    def group_scenes(self, units: list[dict]) -> list[list[int]]: ...


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> list[Utterance]: ...


class Aligner(Protocol):
    def align(self, path: Path, text: str) -> list[Utterance]: ...


class ShotDetector(Protocol):
    def detect(self, path: Path) -> list[tuple[float, float]]: ...


class Speaker(Protocol):
    def speak(self, text: str, voice: str | None = None) -> bytes: ...


class Storyboarder(Protocol):
    def title(self, text: str) -> str: ...

    def split(self, text: str) -> list[ShotDraft]: ...


def parse_json(content: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidAnalysisResponse("分析模型未返回有效 JSON；可重跑 analyze 阶段") from exc


def make_detector(config: Config) -> ShotDetector:
    from mindex.models.detectors import AdaptiveShotDetector, HTTPShotDetector

    return HTTPShotDetector(config) if config.detector.backend == "transnet" else AdaptiveShotDetector(config)


def make_embedder(config: Config) -> MultimodalEmbedder:
    from mindex.models.embedders import HTTPEmbedder, TokenHubEmbedder

    if config.embedding.backend == "llama_cpp":
        from mindex.models.image_embedding import LlamaImageEmbedder

        return LlamaImageEmbedder(config)
    if config.embedding.backend == "tokenhub":
        return TokenHubEmbedder(config)
    return HTTPEmbedder(config)


def make_transcriber(config: Config) -> Transcriber:
    from mindex.models.transcribers import HTTPTranscriber, MiniMaxTranscriber, OpenAIChatTranscriber

    if config.asr.backend == "llama_cpp":
        from mindex.models.llama_cpp import LlamaTranscriber

        return LlamaTranscriber(config)
    if config.asr.backend == "openai":
        return OpenAIChatTranscriber(config)
    if config.asr.backend == "minimax":
        return MiniMaxTranscriber(config)
    return HTTPTranscriber(config)


def make_analyzer(config: Config) -> VisualAnalyzer:
    from mindex.models.qwen import QwenAnalyzer

    if config.analyzer.backend == "llama_cpp":
        from mindex.models.analyzers import PinnedQwenAnalyzer

        return PinnedQwenAnalyzer(config)
    return QwenAnalyzer(config)


def make_speaker(config: Config) -> Speaker:
    from mindex.models.speech import DashScopeSpeaker, MiniMaxSpeaker, OpenAISpeaker

    if config.studio.tts.backend == "minimax":
        return MiniMaxSpeaker(config)
    if config.studio.tts.backend == "dashscope":
        return DashScopeSpeaker(config)
    return OpenAISpeaker(config)


def make_storyboarder(config: Config) -> Storyboarder:
    from mindex.models.storyboard import QwenStoryboarder

    return QwenStoryboarder(config)
