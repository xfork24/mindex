"""ASS 字幕：字幕样式模板、高亮词与（需求 6 的）文字层，生成一份 ``final.ass`` 供 libass 烧录。

尺寸单位沿用 ``SubtitleConfig.font_size``：以 288 线的短边为基准（libass 把 SRT 转换为 PlayResY=288），
生成时换算到渲染像素（PlayResX/PlayResY = 渲染宽高），所以同一样式在 16:9、9:16、1:1 下相对短边大小一致；
竖屏的底边距默认抬高到 36（与 ``studio_render.PORTRAIT_MARGIN_V`` 一致），避开短视频平台底部按钮区。

只有烧录字幕且选了字幕样式（或有分镜高亮词）时才生成 ASS；否则成片沿用原来的 SRT + force_style 路径，
签名与产物与旧版一致。
"""

from __future__ import annotations

import re

from mindex.studio.post_styles import (  # noqa: F401 — 样式模型在 post_styles（品牌模板与方案参数共用）
    HEX_COLOR,
    SUBTITLE_PRESET_IDS,
    SUBTITLE_PRESETS,
    OverlayStyle,
    SubtitlePreset,
    SubtitleStyle,
)

# 改动 ASS 生成规则（样式换算、转义、高亮或图层布局）时递增：进入使用 ASS 的成片签名。
ASS_VERSION = 1
BASE_LINES = 288


def preset(name: str) -> dict:
    if name not in SUBTITLE_PRESETS:
        raise ValueError(f"未知字幕样式：{name}")
    return SubtitleStyle.model_validate(SUBTITLE_PRESETS[name]).model_dump()



def ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """``#RRGGBB`` + 不透明度 → ASS 的 ``&HAABBGGRR``（AA 为透明度，00 不透明）。"""
    red, green, blue = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    alpha = round((1 - opacity) * 255)
    return f"&H{alpha:02X}{blue}{green}{red}".upper()


def escape(text: str) -> str:
    """ASS 文本转义：花括号与反斜杠换成全角形式（不被当作覆盖标签），换行写成 ``\\N``。"""
    text = text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")
    return re.sub(r"\r?\n", r"\\N", text)


def highlight(text: str, words: list[str], color: str, base: str) -> str:
    """转义后的文本里把高亮词包上颜色标签；词之间按最长优先、不重叠匹配。"""
    escaped = escape(text)
    targets = sorted({escape(word) for word in words if word}, key=len, reverse=True)
    if not targets:
        return escaped
    pattern = re.compile("|".join(re.escape(word) for word in targets))
    return pattern.sub(lambda m: f"{{\\c{color}}}{m.group(0)}{{\\c{base}}}", escaped)


def ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def scale(width: int, height: int) -> float:
    return min(width, height) / BASE_LINES


def style_line(name: str, style: dict, *, font: str, size: float, width: int, height: int,
               alignment: int | None = None, margin_v: float | None = None) -> str:
    unit = scale(width, height)
    margin = style.get("margin")
    if margin_v is None:
        if margin is None:  # 横屏 10；竖屏沿用 force_style 时代的 MarginV=36（以画面高度 288 线计）
            margin_v = 36 * height / BASE_LINES if height > width else 10 * unit
        else:
            margin_v = margin * unit
    if alignment is None:
        alignment = {"bottom": 2, "middle": 5, "top": 8}[style.get("position", "bottom")]
    box = style.get("box")
    back = ass_color(style.get("box_color", "#000000"), style.get("box_opacity", 0.6)) if box else "&H80000000"
    outline_color = back if box else ass_color(style.get("outline_color", "#000000"))
    outline = (max(style.get("outline", 0), 2.0) if box else style.get("outline", 1.0)) * unit
    fields = [
        name, font, f"{size * unit:.1f}", ass_color(style.get("primary", "#FFFFFF")), "&H000000FF", outline_color,
        back, "-1" if style.get("bold") else "0", "0", "0", "0", "100", "100", "0", "0", "3" if box else "1",
        f"{outline:.2f}", f"{style.get('shadow', 0.5) * unit:.2f}", str(alignment), f"{20 * unit:.0f}",
        f"{20 * unit:.0f}", f"{margin_v:.0f}", "1",
    ]
    return "Style: " + ",".join(fields)


FORMAT = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
          "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
          "MarginL, MarginR, MarginV, Encoding")


def build_ass(cues: list[dict], *, width: int, height: int, font: str, font_size: float, style: dict | None,
              highlights: dict[str, list[str]] | None = None, layers: list[str] | None = None,
              layer_styles: list[str] | None = None) -> str:
    """生成 ASS 文本。

    ``cues``：``[{shot_id, start, end, text}]``（成片时间，秒）；``style`` 为 None 时不写字幕事件（只有文字层）。
    ``layers``/``layer_styles``：文字层的 Dialogue 与 Style 行（由 ``overlays`` 生成，需求 6）。"""
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        FORMAT,
    ]
    if style is not None:
        size = style.get("font_size") or font_size
        lines.append(style_line("Default", style, font=font, size=size, width=width, height=height))
    lines += layer_styles or []
    lines += ["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    if style is not None:
        base = ass_color(style.get("primary", "#FFFFFF"))
        color = ass_color(style.get("highlight", "#FFD400"))
        for cue in cues:
            words = (highlights or {}).get(cue["shot_id"]) or []
            text = highlight(cue["text"], words, color, base)
            lines.append(f"Dialogue: 0,{ass_time(cue['start'])},{ass_time(cue['end'])},Default,,0,0,0,,{text}")
    lines += layers or []
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 文字层（需求 6）


DEFAULT_OVERLAY_STYLE = OverlayStyle().model_dump()
FADE_MS = 300
SLIDE_MS = 400
# 各类文字层的字号（288 线短边单位）与位置：标题卡居中，信息条左下（在字幕上方），数据卡片右上。
LAYER_SIZES = {"title": 20, "lower_third": 10, "data_card": 9}


def layer_styles(style: dict, *, font: str, width: int, height: int) -> list[str]:
    unit = scale(width, height)
    face = style.get("font") or font
    primary = style.get("primary", "#FFFFFF")
    background = style.get("background", "#000000")
    opacity = style.get("background_opacity", 0.55)
    title = {"primary": primary, "outline_color": "#000000", "outline": 2.0, "shadow": 1.0, "bold": True}
    boxed = {"primary": primary, "box": True, "box_color": background, "box_opacity": opacity, "shadow": 0,
             "outline": 2.0, "bold": True}
    return [
        style_line("Title", title, font=face, size=LAYER_SIZES["title"], width=width, height=height, alignment=5,
                   margin_v=0),
        style_line("LowerThird", boxed, font=face, size=LAYER_SIZES["lower_third"], width=width, height=height,
                   alignment=1, margin_v=48 * unit),
        style_line("Card", {**boxed, "bold": False}, font=face, size=LAYER_SIZES["data_card"], width=width,
                   height=height, alignment=9, margin_v=16 * unit),
    ]


def layer_window(layer: dict, start: float, end: float) -> tuple[float, float] | None:
    """文字层在成片里的起止秒：镜头起点 + 相对时间，截在本镜之内；落在镜外时为 None。"""
    begin = start + (layer.get("start") or 0)
    finish = end if layer.get("end") is None else min(end, start + layer["end"])
    if finish - begin < 0.1:
        return None
    return begin, finish


def layer_text(layer: dict, style: dict, *, width: int, height: int) -> tuple[str, str]:
    """文字层的 (Style 名, 带覆盖标签的文本)。入场 fade 为淡入淡出，slide 为滑入加淡出。"""
    unit = scale(width, height)
    accent = ass_color(style.get("accent", "#FFB000"))
    primary = ass_color(style.get("primary", "#FFFFFF"))
    effect = layer.get("effect", "fade")
    kind = layer["kind"]
    tags = ""
    if effect in ("fade", "slide"):
        tags += f"\\fad({FADE_MS},{FADE_MS})"
    if effect == "slide":
        if kind == "title":
            x, y = width / 2, height / 2
            tags += f"\\move({x:.0f},{y + 30 * unit:.0f},{x:.0f},{y:.0f},0,{SLIDE_MS})"
        elif kind == "lower_third":
            x, y = 20 * unit, height - 48 * unit
            tags += f"\\move({-width * 0.4:.0f},{y:.0f},{x:.0f},{y:.0f},0,{SLIDE_MS})"
        else:
            x, y = width - 20 * unit, 16 * unit
            tags += f"\\move({width * 1.4:.0f},{y:.0f},{x:.0f},{y:.0f},0,{SLIDE_MS})"
    prefix = f"{{{tags}}}" if tags else ""
    small = f"{{\\fs{LAYER_SIZES[kind] * unit * 0.62:.1f}\\b0}}"
    if kind == "title":
        text = escape(layer["title"])
        if layer.get("subtitle"):
            text += f"\\N{small}{escape(layer['subtitle'])}"
        return "Title", prefix + text
    if kind == "lower_third":
        text = f"{{\\c{accent}}}▍{{\\c{primary}}}{escape(layer['title'])}"
        if layer.get("subtitle"):
            text += f"\\N{small}{escape(layer['subtitle'])}"
        return "LowerThird", prefix + text
    rows = []
    if layer.get("title"):
        rows.append(f"{{\\b1}}{escape(layer['title'])}{{\\b0}}")
    for item in layer.get("items") or []:
        rows.append(f"{escape(item['label'])}  {{\\c{accent}\\b1}}{escape(item['value'])}{{\\c{primary}\\b0}}")
    return "Card", prefix + "\\N".join(rows)


def layer_events(shot_layers: list[tuple[str, float, float, list[dict]]], style: dict, *, width: int,
                 height: int) -> list[str]:
    """``[(分镜, 镜头起点, 镜头终点, 文字层列表)]`` → Dialogue 行（Layer 1，压在字幕之上）。"""
    lines = []
    for _sid, start, end, layers in shot_layers:
        for layer in layers:
            window = layer_window(layer, start, end)
            if window is None:
                continue
            name, text = layer_text(layer, style, width=width, height=height)
            lines.append(f"Dialogue: 1,{ass_time(window[0])},{ass_time(window[1])},{name},,0,0,0,,{text}")
    return lines
