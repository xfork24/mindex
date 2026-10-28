"""ASS 字幕样式与高亮词（spec m3-post-production 需求 5）。

- 生成：颜色换算、转义、高亮、字号按短边换算、竖屏边距；
- 兼容：未选样式且没有高亮词（或未烧录）时 compose 签名不变、不生成 ASS；
- 构建：烧录 ASS 后成片带 ass 文件，字幕区像素非空。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_studio_sources import (
    build,
    frame_count,
    make_image,
    setup,
    shot,
    small,  # noqa: F401 — small 是夹具
    store_input,
)

from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.workspace import ass

slow = pytest.mark.slow


def test_colors_escape_and_highlight():
    assert ass.ass_color("#FFD400") == "&H0000D4FF"
    assert ass.ass_color("#000000", 0.6) == "&H66000000"
    assert ass.escape("a{b}\\c\n下一行") == "a｛b｝＼c\\N下一行"
    text = ass.highlight("人工智能改变了智能手机", ["智能", "智能手机"], "&H0000D4FF", "&H00FFFFFF")
    assert text == "人工{\\c&H0000D4FF}智能{\\c&H00FFFFFF}改变了{\\c&H0000D4FF}智能手机{\\c&H00FFFFFF}"
    assert ass.ass_time(3725.456) == "1:02:05.46"


def test_highlight_normalization_on_shot():
    item = shot("a", "旁白", highlights=[" 智能 ", "智能", "手机", ""])
    assert item.highlights == ["智能", "手机"]
    assert "highlights" not in shot("a", "旁白", highlights=[]).model_dump()
    assert shot("a", "旁白", highlights="甲，乙 丙").highlights == ["甲", "乙", "丙"]
    with pytest.raises(ValidationError):
        shot("a", "旁白", highlights=["一二三四五六七八九十一二三四五六七八九十一"])
    with pytest.raises(ValidationError):
        shot("a", "旁白", highlights=[str(i) for i in range(11)])


def test_build_ass_scales_by_short_side_and_portrait_margin():
    cues = [{"shot_id": "a", "start": 0, "end": 1.5, "text": "智能{时代}"}]
    landscape = ass.build_ass(cues, width=1280, height=720, font="Noto Sans CJK SC", font_size=7,
                              style=ass.preset("box"), highlights={"a": ["智能"]})
    portrait = ass.build_ass(cues, width=720, height=1280, font="Noto Sans CJK SC", font_size=7,
                             style=ass.preset("box"), highlights={"a": ["智能"]})
    style = next(line for line in landscape.splitlines() if line.startswith("Style: Default"))
    fields = style.split(",")
    assert fields[2] == "17.5"  # 7 × 720 / 288
    assert fields[15] == "3"  # 底条 = BorderStyle 3
    assert fields[6] == "&H66000000"
    assert fields[21] == "25"  # 横屏边距 10 × 2.5
    portrait_style = next(line for line in portrait.splitlines() if line.startswith("Style: Default")).split(",")
    assert portrait_style[2] == "17.5" and portrait_style[21] == "160"  # 竖屏 36 × 1280 / 288
    dialogue = next(line for line in landscape.splitlines() if line.startswith("Dialogue:"))
    assert dialogue.endswith("Default,,0,0,0,,{\\c&H0000D4FF}智能{\\c&H00FFFFFF}｛时代｝")
    assert "PlayResX: 1280" in landscape and "PlayResY: 720" in landscape


def test_subtitle_style_only_enters_signature_when_burned(small, tmp_path):  # noqa: F811
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(small, job_id, w, p, [shot("a", "红色开场。", source={"kind": "image", "media_id": image})])

    def payload():
        return ws.stages.handler("compose").specification(small, job_id, w, p, None, {}, None)[0]

    plain = payload()
    p["options"]["subtitle_style"] = "outline"
    assert payload() == plain  # 软字幕：样式不参与
    p["options"]["burn"] = True
    burned = ws.specification(small, job_id, w, p, "compose")["signature"]
    p["options"]["subtitle_style"] = None
    assert "subtitle_style" not in payload()
    assert ws.specification(small, job_id, w, p, "compose")["signature"] != burned
    ws.set_board(small, job_id, w, p, [shot("a", "红色开场。", source={"kind": "image", "media_id": image},
                                            highlights=["红色"])])
    found = payload()
    assert found["highlights"] == [["a", ["红色"]]] and found["subtitle_style"] == ass.preset("classic")
    # 高亮词只影响成片，不重建分镜渲染。
    assert "highlights" not in str(ws.specification(small, job_id, w, p, "render", "a"))
    assert studio.Shot.model_validate(ws.version(w, p["shots"]["a"]["content"])["data"]).highlights == ["红色"]


@slow
def test_burned_ass_build(small, tmp_path):  # noqa: F811
    from mindex.studio.studio_render import has_subtitles_filter

    if not has_subtitles_filter():
        pytest.skip("ffmpeg 没有 libass")
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png", color=(20, 20, 20, 255)))
    ws.set_board(small, job_id, w, p, [shot("a", "红色开场画面。", source={"kind": "image", "media_id": image},
                                            highlights=["红色"])])
    p["options"].update(burn=True, subtitle_style="box")
    ws.persist(small, job_id, w)
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    final = ws.version(w, p["final"])
    assert "ass" in final["files"] and "srt" in final["files"]
    content = ws.file_path(small, job_id, final, "ass").read_text(encoding="utf-8")
    assert "{\\c&H0000D4FF}红色{\\c&H00FFFFFF}" in content
    assert frame_count(ws.file_path(small, job_id, final, "mp4"))[0] == final["data"]["total_frames"]


# ---------------------------------------------------------------- 文字层（需求 6）


def test_overlay_model_validation_and_dump():
    item = shot("a", "旁白", overlays=[{"kind": "title", "title": " 第一章 "}])
    assert item.model_dump()["overlays"] == [{"engine": "ass", "kind": "title", "title": "第一章", "effect": "fade"}]
    assert "overlays" not in shot("a", "旁白", overlays=[]).model_dump()
    for bad in ({"kind": "title"}, {"kind": "data_card", "title": "x"}, {"kind": "custom", "brief": "动态图表"},
                {"kind": "lower_third", "title": "x", "start": 2, "end": 1}):
        with pytest.raises(ValidationError):
            shot("a", "旁白", overlays=[bad])
    assert shot("a", "旁白", overlays=[{"engine": "html", "kind": "custom", "brief": "动态柱状图"}]).overlays
    with pytest.raises(ValidationError):
        shot("a", "旁白", overlays=[{"kind": "title", "title": str(i)} for i in range(5)])


def test_layer_events_window_and_text():
    style = ass.DEFAULT_OVERLAY_STYLE
    layers = [{"kind": "lower_third", "title": "张三", "subtitle": "研究员", "effect": "slide", "start": 0.5},
              {"kind": "data_card", "title": "营收", "items": [{"label": "Q1", "value": "12亿"}], "effect": "none"},
              {"kind": "title", "title": "超出", "start": 9}]
    lines = ass.layer_events([("b", 10.0, 13.0, layers)], style, width=1280, height=720)
    assert len(lines) == 2  # 第三个起点超出镜头，忽略
    assert lines[0].startswith("Dialogue: 1,0:00:10.50,0:00:13.00,LowerThird,")
    assert "\\move(" in lines[0] and "\\fad(300,300)" in lines[0] and "张三" in lines[0] and "\\N" in lines[0]
    assert lines[1].startswith("Dialogue: 1,0:00:10.00,0:00:13.00,Card,") and "{\\c&H0000B0FF\\b1}12亿" in lines[1]
    styles = ass.layer_styles(style, font="Noto Sans CJK SC", width=1280, height=720)
    assert [line.split(",")[0] for line in styles] == ["Style: Title", "Style: LowerThird", "Style: Card"]


def test_overlays_enter_compose_signature_even_with_soft_subtitles(small, tmp_path):  # noqa: F811
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(small, job_id, w, p, [shot("a", "红色开场。", source={"kind": "image", "media_id": image})])

    def payload():
        return ws.stages.handler("compose").specification(small, job_id, w, p, None, {}, None)[0]

    assert "overlays" not in payload() and "ass_version" not in payload()
    ws.set_board(small, job_id, w, p, [shot("a", "红色开场。", source={"kind": "image", "media_id": image},
                                            overlays=[{"kind": "title", "title": "开场"},
                                                      {"engine": "html", "kind": "custom", "brief": "图表"}])])
    found = payload()
    assert found["overlays"] == [["a", [{"engine": "ass", "kind": "title", "title": "开场", "effect": "fade"}]]]
    assert found["overlay_style"] == ass.DEFAULT_OVERLAY_STYLE and found["ass_version"] == ass.ASS_VERSION
    assert "subtitle_style" not in found


@slow
def test_soft_subtitles_with_burned_layer(small, tmp_path):  # noqa: F811
    import json

    from mindex.shared.media import command
    from mindex.studio.studio_render import has_subtitles_filter

    if not has_subtitles_filter():
        pytest.skip("ffmpeg 没有 libass")
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png", color=(10, 10, 10, 255)))
    ws.set_board(small, job_id, w, p, [shot("a", "开场画面旁白。", source={"kind": "image", "media_id": image,
                                                                    "motion": "none"},
                                            overlays=[{"kind": "data_card", "title": "数据",
                                                       "items": [{"label": "增长", "value": "35%"}],
                                                       "effect": "none"}])])
    ws.persist(small, job_id, w)
    w, p = build(small, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    final = ws.version(w, p["final"])
    assert "ass" in final["files"]
    text = ws.file_path(small, job_id, final, "ass").read_text(encoding="utf-8")
    assert "Style: Default" not in text and "Card" in text  # 只有文字层，字幕仍是软字幕
    mp4 = ws.file_path(small, job_id, final, "mp4")
    streams = json.loads(command(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json",
                                  str(mp4)]))["streams"]
    assert [item["codec_type"] for item in streams].count("subtitle") == 1
    # 数据卡片在右上角：成片该区域与分镜渲染画面（未叠加文字层）明显不同，左下角相同。
    from PIL import Image, ImageChops

    def frame(path, name):
        target = tmp_path / name
        command(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(path), "-frames:v", "1", str(target)])
        return Image.open(target).convert("L")

    render = ws.version(w, p["shots"]["a"]["render"])
    burned, plain = frame(mp4, "final.png"), frame(ws.file_path(small, job_id, render, "video"), "plain.png")
    width, height = burned.size

    def change(box):
        from PIL import ImageStat

        return ImageStat.Stat(ImageChops.difference(burned.crop(box), plain.crop(box))).mean[0]

    card = change((width * 2 // 3, 0, width, height // 3))
    control = change((0, height * 2 // 3, width // 3, height))
    # 余量只兜底编码噪声（控制区约 0.09）：不同平台的字体回退会显著改变文字层像素量，
    # 卡片区只要远高于控制区即说明文字层已烧录。
    assert card > 5 * control + 0.5, f"卡片区变化 {card} 未明显高于控制区 {control}"
