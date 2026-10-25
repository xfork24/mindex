"""嵌入服务客户端：自研 mindex 协议与腾讯 TokenHub。"""

from __future__ import annotations

import base64
import math
from pathlib import Path

import numpy as np

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.shared.config import Config, fingerprint
from mindex.shared.media import frame_data, probe


class HTTPEmbedder:
    def __init__(self, config: Config):
        self.config = config
        self.profile = config.embedding
        self._identity: dict | None = None

    def identity(self) -> dict:
        payload = adapters.request(
            "GET",
            self.profile.base_url.rstrip("/") + "/health",
            timeout=self.profile.timeout,
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
        payload = adapters.request(
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
            payload = adapters.request(
                "POST",
                self.profile.base_url.rstrip("/") + "/embed/video",
                timeout=self.profile.timeout,
                headers=self.profile.headers(),
                files={"file": (path.name, handle, "video/mp4")},
            )
        return self._vectors(payload, 1)[0]

    def image(self, path: Path) -> list[float]:
        if self._identity is None:
            self.identity()
        with path.open("rb") as handle:
            payload = adapters.request(
                "POST",
                self.profile.base_url.rstrip("/") + "/embed/image",
                timeout=self.profile.timeout,
                headers=self.profile.headers(),
                files={"file": (path.name, handle, "image/png")},
            )
        return self._vectors(payload, 1)[0]


class TokenHubEmbedder:
    """腾讯 TokenHub 多模态向量（WeMM 系）：文本/图片/视频统一空间。

    端点 /v1/embeddings/multimodal 按输入序列融合为单个向量；视频由客户端按
    config.video（fps/max_frames）抽帧后以图片序列发送，保证预处理指纹诚实。
    线上服务无 /health 空间协议，身份纯由配置构造并进入索引指纹。
    """

    def __init__(self, config: Config):
        self.config = config
        self.profile = config.embedding

    def identity(self) -> dict:
        return {
            "model": self.profile.model,
            "revision": self.profile.revision,
            "quantization": self.profile.quantization,
            "dimensions": self.profile.dimensions,
            "normalized": True,
            "fps": self.config.video.fps,
            "max_frames": self.config.video.max_frames,
            "max_pixels": self.config.video.max_pixels,
            "implementation": "tokenhub-multimodal-1",
        }

    def _sequence(self, inputs: list[dict]) -> list[float]:
        payload = adapters.request(
            "POST",
            self.profile.base_url.rstrip("/") + "/embeddings/multimodal",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={"model": self.profile.model, "input": inputs},
        )
        try:
            data = payload["data"]
            if len(data) != 1:
                raise ValueError(f"期望单个融合向量，实际 {len(data)} 个")
            vector = np.asarray(data[0]["embedding"], dtype=np.float32)
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelError("TokenHub 嵌入服务未返回有效的融合向量") from exc
        if vector.shape != (self.profile.dimensions,) or not np.isfinite(vector).all():
            raise ModelError(f"TokenHub 向量维度或数值无效，期望 {self.profile.dimensions} 维")
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm < 1e-8:
            raise ModelError("TokenHub 返回零向量或无效范数")
        return (vector / norm).tolist()

    def text(self, texts: list[str], role: str = "document") -> list[list[float]]:
        # 多模态端点对输入序列只产出一个融合向量，因此逐条请求。
        return [self._sequence([{"type": "text", "text": text}]) for text in texts]

    def video(self, path: Path) -> list[float]:
        duration = probe(path)["duration"]
        count = max(1, min(self.config.video.max_frames, math.ceil(duration * self.config.video.fps)))
        inputs = [
            {"type": "image_url", "image_url": {"url": uri}} for _, uri in frame_data(path, count)
        ]
        return self._sequence(inputs)

    def image(self, path: Path) -> list[float]:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode()
        uri = f"data:{mime};base64,{encoded}"
        return self._sequence([{"type": "image_url", "image_url": {"url": uri}}])
