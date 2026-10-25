"""阶段处理器：导入即按阶段顺序登记到 ``stages.REGISTRY``（plan → shot → final）。"""

from . import script, split  # noqa: I001 — 导入顺序即登记顺序
from . import recall, tts, music, draw, generate, animate, invoke, select, overlay, render
from . import compose, export, publish

__all__ = ["animate", "compose", "draw", "export", "generate", "invoke", "music", "overlay", "publish", "recall", "render", "script", "select",
           "split", "tts"]
