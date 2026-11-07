(
    """`mindex studio` CLI：进程内经 REST 列出任务与方案、预检、构建（等待结束）、查看日志与下载成片；409 """
    """退出码。"""
)

from __future__ import annotations

import hashlib
import json

import pytest
from typer.testing import CliRunner

import cli.studio as cli_studio
from api import create_app
from conftest import FixtureSpeaker, FixtureStoryboarder
from studio import workspace as ws
from test_studio_workspace import prepared, setup_workspace  # noqa: F401 — prepared 是夹具


@pytest.fixture
def cli(prepared, monkeypatch):  # noqa: F811
    config, embedder = prepared
    monkeypatch.setattr(
        cli_studio,
        "make_app",
        lambda cfg: create_app(
            cfg, embedder, speaker=FixtureSpeaker(cfg), storyboarder=FixtureStoryboarder(cfg)
        ),
    )

    def invoke(*args):
        return CliRunner().invoke(cli_studio.app, list(args), obj=config)

    return config, invoke


@pytest.mark.slow
def test_jobs_plans_build_status_download(cli, tmp_path):
    config, invoke = cli
    job_id, _, p = setup_workspace(config)
    listed = invoke("jobs", "--json")
    assert listed.exit_code == 0, listed.output
    assert [item["id"] for item in json.loads(listed.stdout)] == [job_id]

    plans = json.loads(invoke("plans", job_id[:6], "--json").stdout)
    assert plans == [
        {"id": p["id"], "name": p["name"], "active": True, "canvas": "16:9", "shots": 2, "final": None}
    ]

    checked = json.loads(invoke("preflight", job_id, "--json").stdout)
    assert {node["key"] for node in checked["nodes"]} >= {"tts:a", "render:b", "compose"}

    built = invoke("build", job_id, "--json")
    assert built.exit_code == 0, built.output
    result = json.loads(built.stdout)
    assert result["status"] == "complete" and result["plan_id"] == p["id"]

    status = json.loads(invoke("status", job_id, "--json").stdout)
    assert status["status"] == "complete" and status["items"]

    target = tmp_path / "out" / "film.mp4"
    saved = invoke("download", job_id, "-o", str(target), "--json")
    assert saved.exit_code == 0, saved.output
    fresh = ws.load(config, job_id)
    final = ws.version(fresh, ws.plan(fresh, p["id"])["final"])
    assert hashlib.sha256(target.read_bytes()).hexdigest() == final["files"]["mp4"]["sha256"]
    assert json.loads(saved.stdout)["version"] == final["id"]

    # 第二次构建全部复用。
    again = json.loads(invoke("preflight", job_id, "--json").stdout)
    assert {node["action"] for node in again["nodes"]} == {"reuse"}


def test_unknown_job_and_missing_final(cli):
    config, invoke = cli
    job_id, _, _ = setup_workspace(config)
    missing = invoke("plans", "zzzz")
    assert missing.exit_code == 1 and "任务 zzzz 不存在" in missing.output
    nothing = invoke("download", job_id)
    assert nothing.exit_code == 1 and "还没有成片" in nothing.output
    bad = invoke("download", job_id, "--file", "gif")
    assert bad.exit_code == 2


def test_shot_set_transition_and_show(cli):
    config, invoke = cli
    job_id, _, p = setup_workspace(config)
    saved = invoke("shot", "set", job_id, "#1", "--enter", "fade:0.4", "--next", "dissolve", "--json")
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.stdout)["transition"] == {
        "enter": {"kind": "fade", "duration": 0.4},
        "next": {"kind": "dissolve", "duration": 0.5},
    }
    shown = json.loads(invoke("shot", "show", job_id, "a", "--json").stdout)
    assert shown["transition"]["next"]["kind"] == "dissolve"
    assert invoke("shot", "set", job_id, "#1", "--clear-transition").exit_code == 0
    fresh = ws.load(config, job_id)
    plan = ws.plan(fresh, p["id"])
    assert "transition" not in ws.version(fresh, plan["shots"]["a"]["content"])["data"]
    assert invoke("shot", "set", job_id, "#1", "--enter", "spin").exit_code == 2
    assert invoke("shot", "set", job_id, "#9", "--enter", "fade").exit_code == 1


def test_plan_set_subtitle_style_and_highlights(cli):
    config, invoke = cli
    job_id, _, p = setup_workspace(config)
    saved = invoke("plan", "set", job_id, "--burn", "--subtitle-style", "box", "--json")
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.stdout)["options"]["subtitle_style"] == "box"
    assert json.loads(invoke("plan", "show", job_id, "--json").stdout)["options"]["burn"] is True
    assert (
        json.loads(invoke("plan", "set", job_id, "--subtitle-style", "default", "--json").stdout)[
            "options"
        ].get("subtitle_style")
        is None
    )
    assert invoke("plan", "set", job_id, "--subtitle-style", "neon").exit_code == 2
    marked = json.loads(
        invoke("shot", "set", job_id, "#2", "--highlight", "蓝色", "--highlight", "结尾", "--json").stdout
    )
    assert marked["highlights"] == ["蓝色", "结尾"] and marked["transition"] is None
    assert invoke("shot", "set", job_id, "#2", "--clear-highlights").exit_code == 0
    fresh = ws.load(config, job_id)
    assert "highlights" not in ws.version(fresh, ws.plan(fresh, p["id"])["shots"]["b"]["content"])["data"]


def test_shot_overlays(cli):
    config, invoke = cli
    job_id, _, p = setup_workspace(config)
    saved = invoke(
        "shot", "set", job_id, "#1", "--title", "第一章|起源", "--card", "营收|Q1=12亿,Q2=15亿", "--json"
    )
    assert saved.exit_code == 0, saved.output
    layers = json.loads(saved.stdout)["overlays"]
    assert [layer["kind"] for layer in layers] == ["title", "data_card"]
    assert layers[0]["subtitle"] == "起源" and layers[1]["items"][1] == {"label": "Q2", "value": "15亿"}
    added = json.loads(
        invoke(
            "shot", "set", job_id, "#1", "--lower-third", "张三|研究员", "--engine", "html", "--json"
        ).stdout
    )["overlays"]
    assert len(added) == 3 and added[2]["engine"] == "html"
    assert invoke("shot", "set", job_id, "#1", "--card", "坏数据").exit_code == 2
    assert invoke("shot", "set", job_id, "#1", "--overlay", '{"kind": "custom", "brief": "x"}').exit_code == 2
    assert invoke("shot", "set", job_id, "#1", "--clear-overlays").exit_code == 0
    fresh = ws.load(config, job_id)
    assert "overlays" not in ws.version(fresh, ws.plan(fresh, p["id"])["shots"]["a"]["content"])["data"]


def test_brand_commands_and_plan_brand(cli, tmp_path):
    from test_studio_brands import png

    config, invoke = cli
    job_id, _, _ = setup_workspace(config)
    assert [item["id"] for item in json.loads(invoke("brand", "list", "--json").stdout)][:3] == [
        "plain",
        "news",
        "tech",
    ]
    copied = json.loads(invoke("brand", "copy", "news", "频道", "--json").stdout)
    logo = tmp_path / "logo.png"
    logo.write_bytes(png())
    updated = invoke(
        "brand",
        "update",
        copied["id"],
        "--subtitle",
        "outline",
        "--watermark",
        str(logo),
        "--watermark-position",
        "bottom-left",
        "--json",
    )
    assert updated.exit_code == 0, updated.output
    data = json.loads(updated.stdout)
    assert data["watermark"]["position"] == "bottom-left" and data["subtitle"]["bold"] is True
    asset = f"{data['watermark']['asset']['sha256']}.png"
    assert invoke("brand", "upload", str(logo)).stdout.startswith(asset)
    assert "水印" in invoke("brand", "show", copied["id"]).stdout
    assert invoke("brand", "update", "news", "--name", "x").exit_code == 1  # 内置只读（403）
    saved = json.loads(
        invoke("plan", "set", job_id, "--brand", copied["id"], "--disable", "watermark", "--json").stdout
    )
    assert saved["options"]["brand_id"] == copied["id"]
    assert saved["options"]["brand_overrides"] == {
        "subtitle": None,
        "overlay": None,
        "intro": None,
        "outro": None,
        "watermark": None,
        "disable": ["watermark"],
    }
    assert invoke("brand", "delete", copied["id"], "--yes").exit_code == 2  # 被方案引用
    cleared = json.loads(
        invoke("plan", "set", job_id, "--brand", "none", "--clear-overrides", "--json").stdout
    )
    assert cleared["options"].get("brand_id") is None and cleared["options"].get("brand_overrides") is None
    assert invoke("brand", "delete", copied["id"], "--yes").exit_code == 0


def test_conflict_exits_with_code_two(cli, monkeypatch):
    config, invoke = cli
    job_id, _, _ = setup_workspace(config)

    async def busy(self, method, path, *, json=None, params=None):
        if path.endswith("/build"):
            return {"ok": False, "status": 409, "error": "已有构建正在执行"}
        return await original(self, method, path, json=json, params=params)

    from agent.loopback import MindexClient

    original = MindexClient.request
    monkeypatch.setattr(MindexClient, "request", busy)
    result = invoke("build", job_id)
    assert result.exit_code == 2 and "冲突：已有构建正在执行" in result.output


def test_plan_outro_override(cli, tmp_path):
    from test_studio_sources import make_clip

    config, invoke = cli
    job_id, _, _ = setup_workspace(config)
    clip = make_clip(tmp_path / "outro.mp4", 0.5)
    saved = invoke("plan", "set", job_id, "--brand", "plain", "--outro", str(clip), "--json")
    assert saved.exit_code == 0, saved.output
    overrides = json.loads(saved.stdout)["options"]["brand_overrides"]
    assert overrides["outro"]["asset"]["kind"] == "video" and overrides["intro"] is None
    reset = json.loads(invoke("plan", "set", job_id, "--outro", "none", "--json").stdout)
    assert reset["options"].get("brand_overrides") is None
