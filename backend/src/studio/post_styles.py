"""成片后期的样式模型：字幕样式与文字层样式。

放在 workspace 包之外，供方案参数（``workspace/plans.py``）、品牌模板（``studio/brands.py``）与 ASS 生成
（``workspace/ass.py``）共用而不产生循环导入。尺寸以 288 线短边为单位（与 ``SubtitleConfig.font_size`` 一致）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mindex.shared.config import StrictModel

HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"


class SubtitleStyle(StrictModel):
    """字幕样式：位置、字号、颜色、描边、阴影、底条与高亮色（尺寸以 288 线短边为单位）。"""

    position: Literal["bottom", "middle", "top"] = "bottom"
    font_size: float | None = Field(default=None, ge=3, le=40)  # None = studio.subtitle.font_size
    bold: bool = False
    primary: str = Field(default="#FFFFFF", pattern=HEX_COLOR)
    outline_color: str = Field(default="#000000", pattern=HEX_COLOR)
    outline: float = Field(default=1.0, ge=0, le=10)
    shadow: float = Field(default=0.5, ge=0, le=10)
    box: bool = False
    box_color: str = Field(default="#000000", pattern=HEX_COLOR)
    box_opacity: float = Field(default=0.6, ge=0, le=1)
    highlight: str = Field(default="#FFD400", pattern=HEX_COLOR)
    margin: float | None = Field(default=None, ge=0, le=100)  # None = 横屏 10、竖屏 36


SUBTITLE_PRESETS: dict[str, dict] = {
    "classic": {},
    "outline": {"bold": True, "outline": 2.5, "shadow": 0},
    "box": {"box": True, "outline": 0, "shadow": 0, "box_opacity": 0.6},
    "highlight": {"bold": True, "outline": 2.0, "shadow": 0.5, "highlight": "#FFD400"},
}
SUBTITLE_PRESET_IDS = tuple(SUBTITLE_PRESETS)
SubtitlePreset = Literal["classic", "outline", "box", "highlight"]


class OverlayStyle(StrictModel):
    """文字层样式：字体（None = 字幕字体）、主色、强调色（数据卡片数值、信息条色条）与底色。"""

    font: str | None = Field(default=None, max_length=100)
    primary: str = Field(default="#FFFFFF", pattern=HEX_COLOR)
    accent: str = Field(default="#FFB000", pattern=HEX_COLOR)
    background: str = Field(default="#000000", pattern=HEX_COLOR)
    background_opacity: float = Field(default=0.55, ge=0, le=1)
