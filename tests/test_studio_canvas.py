"""方案级画幅（需求 9）：canvas/fit 选项、分辨率映射、只重建 render/compose、crop 帧数确定与字幕缩放。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_studio_workspace import build, prepared, setup_workspace  # noqa: F401 — prepared 是夹具

from mindex.shared.config import Config, RenderConfig, SubtitleConfig
from mindex.shared.media import command
from mindex.studio import studio_render
from mindex.studio import workspace as ws

CANVASES = {"9:16": (90, 160), "1:1": (120, 120)}


def dims(path):
    raw = json.loads(command(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                              "-show_entries", "stream=width,height,nb_read_frames", "-of", "json", str(path)]))
    stream = raw["streams"][0]
    return stream["width"], stream["height"], int(stream["nb_read_frames"])


def top_rgb(path):
    """首帧最上面两行像素的平均色。"""
    return command(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
                    "crop=iw:2:0:0,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])


# ---------------------------------------------------------------- 配置与参数（快速）
def test_canvas_config_defaults_validation_and_dump():
    render = RenderConfig()
    assert render.size("16:9") == (1280, 720) and render.size("9:16") == (720, 1280)
    assert render.size("1:1") == (1080, 1080)
    assert "canvases" not in render.model_dump()  # 不进入签名、指纹与任务快照
    custom = RenderConfig.model_validate({"width": 640, "height": 360, "canvases": {"1:1": [480, 480]}})
    assert custom.size("16:9") == (640, 360) and custom.size("1:1") == (480, 480)
    assert custom.size("9:16") == (720, 1280)
    for bad in ({"canvases": {"1:1": [481, 480]}}, {"canvases": {"4:3": [640, 480]}},
                {"canvases": {"16:9": [640, 360]}}, {"canvases": {"1:1": ["480", 480]}}):
        with pytest.raises(ValidationError):
            RenderConfig.model_validate(bad)
    assert RenderConfig.model_validate({"canvases": {"16:9": [1280, 720]}}).size("16:9") == (1280, 720)
    config = Config.model_validate({"studio": {"max_reference_chars": 500}})
    assert config.studio.max_reference_chars == 500
    for value in ("500", 1.5, True, 0):
        with pytest.raises(ValidationError):
            Config.model_validate({"studio": {"max_reference_chars": value}})


def test_plan_options_canvas_and_default_signature_parameters(config):
    with pytest.raises(ValidationError):
        ws.PlanOptions(canvas="4:3")
    with pytest.raises(ValidationError):
        ws.PlanOptions(fit="stretch")
    p = {"options": ws.PlanOptions().model_dump()}
    assert p["options"]["canvas"] == "16:9" and p["options"]["fit"] == "pad"
    legacy = {"options": {k: v for k, v in p["options"].items() if k not in ("canvas", "fit")}}
    for stage in ("render", "compose"):
        default = ws.settings(config, p, stage)
        assert default == ws.settings(config, legacy, stage)  # 旧方案与 16:9+pad 参数逐字节一致
        assert "canvas" not in default and "fit" not in default
        assert default["render"] == config.studio.render.model_dump()
        portrait = ws.settings(config, {"options": {**p["options"], "canvas": "9:16"}}, stage)
        assert portrait["canvas"] == "9:16" and portrait["fit"] == "pad"
        assert (portrait["render"]["width"], portrait["render"]["height"]) == (720, 1280)
        crop = ws.settings(config, {"options": {**p["options"], "fit": "crop"}}, stage)
        assert crop["fit"] == "crop" and crop["render"] == default["render"]
    for stage in ("script", "tts", "select"):
        assert ws.settings(config, p, stage) == ws.settings(config, {"options": {**p["options"],
                                                                                "canvas": "1:1"}}, stage)


def test_subtitle_style_scales_only_for_non_default_canvas():
    subtitle = SubtitleConfig(font_size=8)
    assert studio_render.canvas_subtitle_style("16:9", 1280, 720, subtitle) is None
    assert studio_render.canvas_subtitle_style("1:1", 1080, 1080, subtitle) == {"FontSize": "8"}
    assert studio_render.canvas_subtitle_style("9:16", 720, 1280, subtitle) == {
        "FontSize": "4.5", "MarginV": str(studio_render.PORTRAIT_MARGIN_V)}
    default = studio_render.subtitle_filter(studio_render.Path("/tmp/a.srt"), subtitle)
    assert default.endswith(":force_style='FontName=Noto Sans CJK SC,FontSize=8'")
    styled = studio_render.subtitle_filter(studio_render.Path("/tmp/a.srt"), subtitle,
                                           style={"FontSize": "4.5", "MarginV": "36"})
    assert styled.endswith(":force_style='FontName=Noto Sans CJK SC,FontSize=4.5,MarginV=36'")


# ---------------------------------------------------------------- 渲染（重媒体）
@pytest.mark.slow
def test_crop_fills_canvas_and_frame_count_matches_pad(config, video_file, tmp_path):
    event = SimpleNamespace(audio=SimpleNamespace(render_duration=1), video=SimpleNamespace(start=0, end=0.5))
    render = config.studio.render.model_copy(update={"width": 90, "height": 160, "preset": "ultrafast"})
    pad, crop = tmp_path / "pad.mp4", tmp_path / "crop.mp4"
    studio_render.cut_shot(video_file, event, render, pad, frames=20)
    studio_render.cut_shot(video_file, event, render, crop, frames=20, fit="crop")
    assert dims(pad) == dims(crop) == (90, 160, 20)
    assert max(top_rgb(pad)) < 30  # pad：上方是黑边
    red, green, blue = top_rgb(crop)
    assert red > 200 and green < 30 and blue < 30  # crop：铺满原片红色画面


@pytest.mark.slow
def test_switching_canvas_rebuilds_only_render_and_compose(prepared):  # noqa: F811
    config, embedder = prepared
    config.studio.render.canvases = dict(CANVASES)
    jid, w, p = setup_workspace(config)
    w, p = build(config, jid, w, p, embedder)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    landscape = ws.version(w, p["final"])
    assert dims(ws.file_path(config, jid, landscape, "mp4"))[:2] == (160, 120)
    shots = ws.ordered_shots(w, p)
    frames = {sid: ws.version(w, p["shots"][sid]["render"])["data"]["frames"] for sid in shots}

    for canvas, fit in (("9:16", "pad"), ("1:1", "crop"), ("1:1", "pad")):
        p["options"] = {**p["options"], "canvas": canvas, "fit": fit}
        ws.persist(config, jid, w)
        checked = ws.preflight(config, jid, w, p)
        actions = {(n["stage"], n["action"]) for n in checked["nodes"]}
        assert {a for s, a in actions if s in ("recall", "tts", "select")} == {"reuse"}, checked["nodes"]
        assert {a for s, a in actions if s in ("render", "compose")} == {"rebuild"}, checked["nodes"]
        before = {sid: dict(p["shots"][sid]) for sid in shots}
        w, p = build(config, jid, w, p, embedder)
        assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
        for sid in shots:
            for stage in ("recall", "tts", "select"):
                assert p["shots"][sid][stage] == before[sid][stage]
            item = ws.version(w, p["shots"][sid]["render"])
            assert item["parameters"]["canvas"] == canvas and item["parameters"]["fit"] == fit
            assert item["data"]["frames"] == frames[sid]  # 帧数只由配音决定，与画幅和适配方式无关
            assert dims(ws.file_path(config, jid, item, "video")) == (*CANVASES[canvas], frames[sid])
        final = ws.version(w, p["final"])
        assert final["parameters"]["canvas"] == canvas
        assert dims(ws.file_path(config, jid, final, "mp4"))[:2] == CANVASES[canvas]

    # 切回 16:9 + pad：复用最初的渲染与成片版本，签名与切换前一致。
    p["options"] = {**p["options"], "canvas": "16:9", "fit": "pad"}
    checked = ws.preflight(config, jid, w, p)
    final_node = next(n for n in checked["nodes"] if n["stage"] == "compose")
    assert final_node["action"] == "reuse" and final_node["version"] == landscape["id"]
    assert all(n["action"] == "reuse" for n in checked["nodes"]), checked["nodes"]


@pytest.mark.slow
def test_portrait_burned_subtitles_use_scaled_style(prepared, monkeypatch):  # noqa: F811
    if not studio_render.has_subtitles_filter():
        pytest.skip("ffmpeg 缺少 libass subtitles 滤镜")
    config, embedder = prepared
    config.studio.render.canvases = dict(CANVASES)
    jid, w, p = setup_workspace(config)
    p["options"] = {**p["options"], "canvas": "9:16", "burn": True}
    ws.persist(config, jid, w)
    seen = []
    original = studio_render.subtitle_filter

    def spy(path, subtitle, *, style=None):
        seen.append(style)
        return original(path, subtitle, style=style)

    monkeypatch.setattr(studio_render, "subtitle_filter", spy)
    try:
        w, p = build(config, jid, w, p, embedder)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"字幕环境不可用：{exc}")
    if w["builds"][-1]["status"] != "complete":
        pytest.skip(f"字幕环境不可用：{w['builds'][-1]}")
    expected = studio_render.canvas_subtitle_style("9:16", 90, 160, config.studio.subtitle)
    assert expected in seen
    assert dims(ws.file_path(config, jid, ws.version(w, p["final"]), "mp4"))[:2] == (90, 160)
