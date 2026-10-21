from __future__ import annotations

from mindex.models.adapters import ModelError, QwenAnalyzer
from mindex.models.llama_cpp import check_after, checked_runtime, manifest


class PinnedQwenAnalyzer(QwenAnalyzer):
    """Bind cached descriptions to the actual GGUF deployment, not an alias alone."""

    def _chat(self, content, instructions, *, validate=None):
        original = self.profile
        pinned = manifest(original, "analyzer")
        report = checked_runtime(original, pinned)
        if not report["capabilities"].get("vision"):
            raise ModelError("Qwen 分析服务没有视觉投影，无法分析视频帧")
        self.profile = original.model_copy(update={"model": report["model_id"]})
        try:
            result = super()._chat(content, instructions, validate=validate)
        finally:
            self.profile = original
        check_after(original, pinned, report)
        return result
