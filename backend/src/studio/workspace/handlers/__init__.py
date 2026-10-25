"""阶段处理器：导入即按阶段顺序登记到 ``stages.REGISTRY``（plan → shot → final）。"""

from . import script, split  # noqa: I001 — 导入顺序即登记顺序
from . import recall, tts, music, draw, generate, animate, select, render
from . import compose

__all__ = ["animate", "compose", "draw", "generate", "music", "recall", "render", "script", "select", "split", "tts"]
