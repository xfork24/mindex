from __future__ import annotations

import json
import logging
import os
import re
import time
from itertools import pairwise
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

from .config import Config, fingerprint
from .media import frame_data, frame_timestamps, probe
from .types import Annotation, Utterance

log = logging.getLogger(__name__)


class ModelError(RuntimeError):
    pass


def request(method: str, url: str, *, timeout: float = 180, attempts: int = 3, **kwargs) -> dict:
    for attempt in range(attempts):
        try:
            response = httpx.request(method, url, timeout=timeout, trust_env=False, **kwargs)
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
                raise ModelError(f"模型服务调用失败 {url}: {exc}") from exc
            # Rewind uploads before retrying a request.
            for value in kwargs.get("files", {}).values():
                handle = value[1] if isinstance(value, tuple) else value
                if hasattr(handle, "seek"):
                    handle.seek(0)
            time.sleep(0.25 * (2**attempt))
        except (ValueError, KeyError) as exc:
            raise ModelError(f"模型服务返回了无效 JSON: {url}") from exc
    raise AssertionError("unreachable")


class MultimodalEmbedder(Protocol):
    def identity(self) -> dict: ...
    def text(self, texts: list[str], role: str = "document") -> list[list[float]]: ...
    def video(self, path: Path) -> list[float]: ...


class VisualAnalyzer(Protocol):
    def analyze(self, path: Path, dialogue: list[dict]) -> Annotation: ...
    def group_scenes(self, units: list[dict]) -> list[list[int]]: ...


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> list[Utterance]: ...


class Aligner(Protocol):
    def align(self, path: Path, text: str) -> list[Utterance]: ...


class ShotDetector(Protocol):
    def detect(self, path: Path) -> list[tuple[float, float]]: ...


class HTTPEmbedder:
    def __init__(self, config: Config):
        self.config = config
        self.profile = config.embedding
        self._identity: dict | None = None

    def identity(self) -> dict:
        payload = request(
            "GET",
            self.profile.base_url.rstrip("/") + "/health",
            timeout=3,
            attempts=1,
            headers=self.profile.headers(),
        )
        spec = payload.get("space")
        if not payload.get("ready") or not isinstance(spec, dict):
            raise ModelError("嵌入服务未就绪或缺少 space 身份信息")
        expected = {
            "model": self.profile.model,
            "dimensions": self.profile.dimensions,
            "quantization": self.profile.quantization,
            "normalized": True,
            "fps": self.config.video.fps,
            "max_frames": self.config.video.max_frames,
            "max_pixels": self.config.video.max_pixels,
        }
        for field, value in expected.items():
            if spec.get(field) != value:
                raise ModelError(f"嵌入服务配置不匹配：{field}，期望 {value!r}，实际 {spec.get(field)!r}")
        if self.profile.revision != "main" and spec.get("revision") != self.profile.revision:
            raise ModelError("模型 revision 不匹配")
        if not spec.get("revision") or not spec.get("implementation"):
            raise ModelError("服务必须提供权重 revision 和 implementation 指纹")
        self._identity = spec
        return spec

    def _vectors(self, payload: dict, count: int) -> list[list[float]]:
        if self._identity is None:
            self.identity()
        if payload.get("space_id") != fingerprint(self._identity):
            raise ModelError("推理过程中模型空间发生变化，已拒绝向量")
        try:
            array = np.asarray(payload.get("vectors"), dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise ModelError("嵌入服务未返回数值向量数组") from exc
        if array.shape != (count, self.profile.dimensions) or not np.isfinite(array).all():
            raise ModelError("向量维度、数量或数值无效")
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or np.any(norms < 1e-8):
            raise ModelError("嵌入服务返回零向量或无效范数")
        return (array / norms).tolist()

    def text(self, texts: list[str], role: str = "document") -> list[list[float]]:
        if not texts:
            return []
        if self._identity is None:
            self.identity()
        payload = request(
            "POST",
            self.profile.base_url.rstrip("/") + "/embed/text",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={"texts": texts, "role": role},
        )
        return self._vectors(payload, len(texts))

    def video(self, path: Path) -> list[float]:
        if self._identity is None:
            self.identity()
        with path.open("rb") as handle:
            payload = request(
                "POST",
                self.profile.base_url.rstrip("/") + "/embed/video",
                timeout=self.profile.timeout,
                headers=self.profile.headers(),
                files={"file": (path.name, handle, "video/mp4")},
            )
        return self._vectors(payload, 1)[0]


def parse_json(content: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelError("分析模型未返回有效 JSON；可重跑 analyze 阶段") from exc


class QwenAnalyzer:
    def __init__(self, config: Config):
        self.config = config
        self.profile = config.analyzer

    def _chat(self, content: list[dict], instructions: str, max_tokens=2200):
        payload = request(
            "POST",
            self.profile.base_url.rstrip("/") + "/chat/completions",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": self.profile.model,
                "temperature": 0,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
            },
        )
        try:
            return parse_json(payload["choices"][0]["message"]["content"])
        except (KeyError, TypeError, IndexError) as exc:
            raise ModelError("分析服务返回格式不符合聊天接口约定") from exc

    def analyze(self, path: Path, dialogue: list[dict]) -> Annotation:
        content = [
            {
                "type": "text",
                "text": "以下是按时间排列的视频帧及对白。对白仅供上下文参考：\n"
                + json.dumps(dialogue, ensure_ascii=False),
            }
        ]
        for timestamp, uri in frame_data(path, self.config.video.analysis_frames):
            content += [
                {"type": "text", "text": f"片段内 {timestamp:.3f} 秒"},
                {"type": "image_url", "image_url": {"url": uri}},
            ]
        instructions = (
            "你为中文影视素材建立可检索的客观描述。只返回符合下列 JSON Schema 的 JSON 对象。"
            "描述人物外观、动作顺序、互动对象、场景、人物情绪、氛围、景别、角度、镜头运动。"
            "情绪用可观察的表情和行为支撑。visual_evidence 记录画面依据，dialogue_inference 记录对白推断。"
            "不确定的信息放 uncertainty；未知列表留空。禁止虚构声音、姓名、身份或画面外事件。"
            "description 写成自然中文，突出有区别的细节；人物只在片段内描述，不识别真实身份。\n"
            + json.dumps(Annotation.model_json_schema(), ensure_ascii=False)
        )
        return Annotation.model_validate(self._chat(content, instructions))

    def group_scenes(self, units: list[dict]) -> list[list[int]]:
        content = [{"type": "text", "text": json.dumps(units, ensure_ascii=False)}]
        result = self._chat(
            content,
            (
                "将连续影视片段按场景、对白和事件组织成连续场景，目标10到30秒，最长45秒。"
                '返回JSON对象 {"groups": [[0,1],[2,3]]}。必须按顺序恰好包含每个索引一次，'
                "不能跳过、重排或重复索引。极短转场或最后不足10秒可以独立成组。"
                "输入时间戳是原视频秒数；一组的时长为最后end减去最先start。"
            ),
            max_tokens=1500,
        )
        groups = result.get("groups") if isinstance(result, dict) else None
        if not isinstance(groups, list) or any(not isinstance(g, list) or not g for g in groups):
            raise ModelError("无效场景分组")
        flat = [i for group in groups for i in group]
        if any(type(i) is not int for i in flat) or flat != list(range(len(units))):
            raise ModelError("场景分组必须连续、完整且无重复")
        if any(units[g[-1]]["end"] - units[g[0]]["start"] > 45.001 for g in groups):
            raise ModelError("场景分组超过45秒")
        return groups


class HTTPTranscriber:
    def __init__(self, config: Config):
        self.profile = config.asr

    def transcribe(self, path: Path) -> list[Utterance]:
        with path.open("rb") as handle:
            payload = request(
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


class AdaptiveShotDetector:
    def __init__(self, config: Config):
        self.config = config

    def detect(self, path: Path) -> list[tuple[float, float]]:
        from scenedetect import SceneManager, open_video
        from scenedetect.detectors import AdaptiveDetector

        log.warning("使用本地 AdaptiveDetector；尚未使用 TransNetV2。分镜质量需人工抽检。")
        video = open_video(str(path))
        manager = SceneManager()
        manager.add_detector(
            AdaptiveDetector(
                adaptive_threshold=self.config.detector.adaptive_threshold,
                min_scene_len=1,
                min_content_val=self.config.detector.min_content_val,
            )
        )
        manager.detect_scenes(video, show_progress=False, frame_skip=0)
        scenes = manager.get_scene_list(start_in_scene=True)
        timestamps = frame_timestamps(path)
        duration = probe(path)["duration"]
        bounds = [0.0]
        for _, end in scenes[:-1]:
            frame = end.get_frames()
            if frame < len(timestamps):
                bounds.append(timestamps[frame])
        bounds.append(duration)
        bounds = sorted(set(bounds))
        return list(pairwise(bounds))


class HTTPShotDetector:
    def __init__(self, config: Config):
        self.config = config

    def detect(self, path: Path) -> list[tuple[float, float]]:
        key = os.environ.get(self.config.detector.api_key_env, "")
        with path.open("rb") as handle:
            payload = request(
                "POST",
                self.config.detector.base_url.rstrip("/") + "/detect/video",
                timeout=1800,
                headers={"Authorization": f"Bearer {key}"} if key else {},
                files={"file": (path.name, handle, "video/mp4")},
                data={"threshold": str(self.config.detector.threshold)},
            )
        timestamps = frame_timestamps(path)
        if payload.get("model") != "TransNetV2" or payload.get("frame_count") != len(timestamps):
            raise ModelError("分镜服务的模型或解码帧数不匹配原视频")
        cuts = payload.get("cut_frames", [])
        if any(type(v) is not int or v < 1 or v >= len(timestamps) for v in cuts):
            raise ModelError("分镜服务返回无效帧索引")
        # Detection service returns one midpoint per transition interval.
        merged = []
        for value in sorted(set(cuts)):
            if not merged or value - merged[-1] > 2:
                merged.append(value)
        bounds = [0.0, *[timestamps[v] for v in merged], probe(path)["duration"]]
        return list(pairwise(bounds))


def make_detector(config: Config) -> ShotDetector:
    return HTTPShotDetector(config) if config.detector.backend == "transnet" else AdaptiveShotDetector(config)


def make_embedder(config: Config) -> MultimodalEmbedder:
    if config.embedding.backend == "llama_cpp":
        from .llama_cpp import LlamaEmbedder

        return LlamaEmbedder(config)
    return HTTPEmbedder(config)


def make_transcriber(config: Config) -> Transcriber:
    if config.asr.backend == "llama_cpp":
        from .llama_cpp import LlamaTranscriber

        return LlamaTranscriber(config)
    return HTTPTranscriber(config)


def make_analyzer(config: Config) -> VisualAnalyzer:
    if config.analyzer.backend == "llama_cpp":
        from .analyzers import PinnedQwenAnalyzer

        return PinnedQwenAnalyzer(config)
    return QwenAnalyzer(config)
