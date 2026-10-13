from __future__ import annotations

from .adapters import ModelError, QwenAnalyzer
from .llama_cpp import checked_runtime, manifest


class PinnedQwenAnalyzer(QwenAnalyzer):
    """Bind cached descriptions to the actual GGUF deployment, not an alias alone."""

    def _chat(self, content, instructions, max_tokens=2200):
        original = self.profile
        pinned = manifest(original, "analyzer")
        observable = checked_runtime(original, pinned)
        if not observable["modalities"].get("vision"):
            raise ModelError("Qwen 分析服务没有视觉投影，无法分析视频帧")
        self.profile = original.model_copy(update={"model": observable["model_id"]})
        try:
            result = super()._chat(content, instructions, max_tokens)
        finally:
            self.profile = original
        checked_runtime(original, pinned)
        if manifest(original, "analyzer") != pinned:
            raise ModelError("分析过程中部署清单发生变化")
        return result
