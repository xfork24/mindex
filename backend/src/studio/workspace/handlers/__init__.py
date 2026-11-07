"""阶段处理器：导入即按阶段顺序登记到 ``stages.REGISTRY``（plan → shot → final）。"""

# 阶段注册与公共导出的装配顺序有语义，保持原顺序。
# isort: off
from studio.workspace.handlers import script, split
from studio.workspace.handlers import (
    recall,
    tts,
    music,
    draw,
    generate,
    animate,
    invoke,
    select,
    overlay,
    render,
)
from studio.workspace.handlers import compose, export, publish
# isort: on

__all__ = [
    "animate",
    "compose",
    "draw",
    "export",
    "generate",
    "invoke",
    "music",
    "overlay",
    "publish",
    "recall",
    "render",
    "script",
    "select",
    "split",
    "tts",
]
