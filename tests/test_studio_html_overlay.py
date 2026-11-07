"""HTML 透明叠加层（spec m3-post-production 需求 9）：叠加层阶段、模板注入、透明录制合成、人工修改钉住、
预检阻塞，以及 REST / 智能助手 / CLI 入口。录制用确定性的 ffmpeg 替身，不依赖 Chromium。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageStat
from typer.testing import CliRunner

import cli.studio as cli_studio
from api import create_app
from conftest import FixtureSpeaker
from models.animator import animation_errors
from shared.media import command
from studio import animation_capture
from studio import workspace as ws
from studio.workspace.handlers import overlay as overlay_stage
from test_agent_aigc import read as read_tool
from test_agent_studio_recall import run as run_tool
from test_studio_sources import make_image, setup, shot, small, store_input  # noqa: F401 — small 是夹具

slow = pytest.mark.slow


class AlphaCapture:
    """Tests only: 代替透明录制——左上角四分之一为不透明白块、其余全透明的 argb MOV，恰好 frames 帧。"""

    def __init__(self):
        self.calls = []

    def __call__(
        self,
        html,
        *,
        seconds,
        width,
        height,
        fps,
        target,
        max_seconds,
        frame_timeout=5,
        frames=None,
        transparent=False,
    ):
        self.calls.append({"html": html, "frames": frames, "transparent": transparent})
        command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=white:s={width // 4}x{height // 4}:r={fps},format=rgba,"
                f"pad={width}:{height}:0:0:color=black@0",
                "-frames:v",
                str(frames),
                "-c:v",
                "qtrle",
                "-pix_fmt",
                "argb",
                str(target),
            ],
            timeout=60,
        )
        return {"frames": frames}


def layered(sid, image, **layer):
    return shot(
        sid,
        "开场画面旁白。",
        source={"kind": "image", "media_id": image, "motion": "none"},
        overlays=[{"engine": "html", "kind": "lower_third", "title": "张三", "subtitle": "研究员", **layer}],
    )


def test_template_injection_is_valid_and_escaped():
    html = overlay_stage.template_html(
        {
            "kind": "data_card",
            "title": "</script><b>",
            "items": [{"label": "增长", "value": "35%"}],
            "effect": "slide",
        },
        {
            "primary": "#FFFFFF",
            "accent": "#FFB000",
            "background": "#000000",
            "background_opacity": 0.5,
            "font": None,
        },
        seconds=2.5,
        width=320,
        height=180,
    )
    assert animation_errors(html, 200) == []
    assert "</script><b>" not in html and "<\\/script>" in html
    data = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    assert data["seconds"] == 2.5 and data["items"][0]["value"] == "35%"


def test_stage_sequence_and_signatures(small, tmp_path, monkeypatch):  # noqa: F811
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(small, job_id, w, p, [layered("a", image)])
    assert ws.stages.plan_shot_stages(w, p, "a") == ("tts", "overlay", "render")
    # 字幕引擎文字层没有叠加层阶段。
    assert ws.shot_stages({"overlays": [{"engine": "ass", "kind": "title", "title": "x"}]}) == ws.STAGES
    monkeypatch.setattr(animation_capture, "available", lambda: False)
    small.studio.animator.enabled = False
    nodes = {n["key"]: n for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert nodes["overlay:a"]["action"] == "rebuild"
    assert nodes["render:a"]["action"] == "blocked" and "动画录制组件" in nodes["render:a"]["reason"]
    # custom 层需要动画生成服务。
    ws.set_board(
        small,
        job_id,
        w,
        p,
        [
            shot(
                "a",
                "开场。",
                source={"kind": "image", "media_id": image},
                overlays=[{"engine": "html", "kind": "custom", "brief": "动态图表"}],
            )
        ],
    )
    nodes = {n["key"]: n for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert nodes["overlay:a"]["action"] == "blocked" and "动画生成服务" in nodes["overlay:a"]["reason"]


@slow
def test_build_composites_layer_and_manual_edit_is_pinned(small, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(animation_capture, "available", lambda: True)
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png", color=(10, 10, 10, 255)))
    ws.set_board(small, job_id, w, p, [layered("a", image, start=0.2)])
    ws.persist(small, job_id, w)
    capture = AlphaCapture()
    with TestClient(create_app(small, speaker=FixtureSpeaker(small), animation_capture=capture)) as client:
        data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
        token = client.get(f"/api/studio/jobs/{job_id}/plans/{p['id']}/preflight").json()["token"]
        client.post(f"/api/studio/jobs/{job_id}/plans/{p['id']}/build", json={"token": token})
        from test_studio_workspace import wait_build

        data = wait_build(client, job_id)
        assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
        plan = data["plans"][p["id"]]
        made = data["versions"][plan["shots"]["a"]["overlay"]]
        assert made["data"]["layers"][0]["origin"] == "template" and made["data"]["layers"][0]["start"] == 0.2
        assert capture.calls and all(call["transparent"] for call in capture.calls)
        assert capture.calls[0]["frames"] == round((made["data"]["seconds"] - 0.2) * 25)
        render = data["versions"][plan["shots"]["a"]["render"]]
        assert render["inputs"]["overlay"] == made["id"] and render["data"]["overlays"]["layers"] == 1
        w = ws.load(small, job_id)
        video = ws.file_path(small, job_id, ws.version(w, render["id"]), "video")
        frame = tmp_path / "frame.png"
        command(["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(video), "-frames:v", "1", str(frame)])
        picture = Image.open(frame).convert("L")
        width, height = picture.size
        assert ImageStat.Stat(picture.crop((0, 0, width // 5, height // 5))).mean[0] > 200  # 叠加层白块
        assert ImageStat.Stat(picture.crop((width // 2, height // 2, width, height))).mean[0] < 80
        # REST：读取源码，人工保存后选用新的钉住版本，只重建渲染与成片。
        source = client.get(f"/api/studio/jobs/{job_id}/versions/{made['id']}/overlay-0.html")
        assert source.status_code == 200 and "张三" in source.text
        assert "content-security-policy" in {key.lower() for key in source.headers}
        edited = source.text.replace("张三", "李四")
        saved = client.patch(
            f"/api/studio/jobs/{job_id}/plans/{p['id']}/shots/a/overlay",
            json={"revision": data["revision"], "index": 0, "html": edited},
        )
        assert saved.status_code == 200, saved.text
        manual = saved.json()["plans"][p["id"]]["shots"]["a"]["overlay"]
        assert manual != made["id"]
        bad = client.patch(
            f"/api/studio/jobs/{job_id}/plans/{p['id']}/shots/a/overlay",
            json={
                "revision": saved.json()["revision"],
                "index": 0,
                "html": "<html><body><script>fetch('x')</script></body></html>",
            },
        )
        assert bad.status_code == 422
        w = ws.load(small, job_id)
        plan = ws.plan(w, p["id"])
        nodes = {n["key"]: n["action"] for n in ws.preflight(small, job_id, w, plan)["nodes"]}
        assert nodes == {"tts:a": "reuse", "overlay:a": "reuse", "render:a": "rebuild", "compose": "rebuild"}
        # 智能助手 / MCP：读取与修改源码。
        _, read, _ = read_tool(client, small, "get_overlay_source", {"job_id": job_id, "shot": "#1"})
        assert "李四" in read.value["source"] and read.value["origin"] == "manual"
        preview, output, _ = run_tool(
            client,
            small,
            "edit_overlay_source",
            {"job_id": job_id, "shot": "#1", "index": 0, "html": edited.replace("李四", "王五")},
        )
        assert "人工叠加层源码" in preview.summary and output.value["version"]
        with pytest.raises(Exception, match="安全检查"):
            run_tool(
                client,
                small,
                "edit_overlay_source",
                {"job_id": job_id, "shot": "#1", "html": "<html><body><link rel=x></body></html>"},
            )
    # CLI：show 输出源码，edit 用文件替换。
    monkeypatch.setattr(cli_studio, "make_app", lambda cfg: create_app(cfg, speaker=FixtureSpeaker(cfg)))
    runner = CliRunner()
    shown = runner.invoke(cli_studio.app, ["overlay", "show", job_id, "#1"], obj=small)
    assert shown.exit_code == 0, shown.output
    assert "王五" in shown.stdout
    target = tmp_path / "layer.html"
    target.write_text(shown.stdout.replace("王五", "赵六"), encoding="utf-8")
    edited = runner.invoke(
        cli_studio.app, ["overlay", "edit", job_id, "#1", str(target), "--json"], obj=small
    )
    assert edited.exit_code == 0, edited.output
    assert (
        runner.invoke(cli_studio.app, ["overlay", "show", job_id, "#1", "--index", "2"], obj=small).exit_code
        == 1
    )
