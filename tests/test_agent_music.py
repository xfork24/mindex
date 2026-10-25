"""曲库的智能助手 / MCP 与 CLI 入口（spec m3-post-production 需求 16、22）：studio_post 领域的曲库工具、
edit_shots 的曲库提示点与 ``mindex studio music`` 命令组。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_agent_aigc import read
from test_agent_studio_recall import attachment, run
from test_studio_beats import metronome
from test_studio_sources import setup, shot, small  # noqa: F401 — small 是夹具
from typer.testing import CliRunner

from mindex import cli_studio
from mindex.agent.mcp_server import exported
from mindex.agent.registry import ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app
from mindex.studio import workspace as ws

NAMES = ("list_music_tracks", "get_music_track", "import_music_track", "update_music_track", "analyze_music_track",
         "recommend_music")
LICENSE = {"source": "自制", "license": "CC0", "attribution": "测试作者"}


class Writer:
    def complete(self, instructions, text):
        return '{"moods": ["calm"], "bpm_min": 70, "bpm_max": 110}'


@pytest.fixture
def agent(small):  # noqa: F811
    job_id, _, _ = setup(small, [shot("a", "安静的夜晚。"), shot("b", "城市入睡。")])
    with TestClient(create_app(small, writer=Writer())) as client:
        yield small, client, job_id


def test_music_tools_are_in_studio_post_and_exported():
    registry = build_registry()
    assert {registry.get(name).domain for name in NAMES} == {"studio_post"}
    assert registry.get("recommend_music").risk == "read" and registry.get("import_music_track").risk == "write"
    assert set(NAMES) <= {spec.name for spec in exported(registry)}


def test_import_recommend_and_select_with_edit_shots(agent):
    config, client, job_id = agent
    ref = attachment(client, metronome(90, 6), "calm.wav", "audio/wav")
    preview, output, _ = run(client, config, "import_music_track", {"attachment": ref, **LICENSE, "title": "夜曲"})
    assert "CC0" in preview.changes[1]["after"]
    track = output.value
    assert track["title"] == "夜曲" and track["analysis_status"] == "ready" and "beats" not in track
    listed = read(client, config, "list_music_tracks", {"mood": "calm"})[1]
    assert [item["id"] for item in listed.value["items"]] == [track["id"]]
    with pytest.raises(ToolError, match="至少修改一项"):
        run(client, config, "update_music_track", {"track_id": track["id"]})
    updated = run(client, config, "update_music_track", {"track_id": track["id"], "tags": ["夜景"]})[1].value
    assert updated["tags"] == ["夜景"]
    recommended = read(client, config, "recommend_music", {"job_id": job_id, "shot": "#1"})[1]
    assert recommended.value["moods"] == ["calm"] and recommended.value["items"][0]["id"] == track["id"]
    assert "edit_shots" in recommended.text
    preview, _, _ = run(client, config, "edit_shots", {"job_id": job_id, "operations": [
        {"op": "update", "shot": "#1", "fields": {"music": {"kind": "library", "track_id": track["id"], "volume": 0.3}}}]})
    assert any("曲库音乐" in warning for warning in preview.warnings)
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    cue = ws.version(w, p["shots"]["a"]["content"])["data"]["music"]
    assert cue == {"kind": "library", "prompt": None, "lyrics": None, "instrumental": True, "audio_id": None,
                   "track_id": track["id"], "volume": 0.3}
    analyzed = run(client, config, "analyze_music_track", {"track_id": track["id"], "retag": False})[1].value
    assert analyzed["moods"] == track["moods"]


def test_cli_music_commands(agent, monkeypatch, tmp_path):
    config, _, job_id = agent
    monkeypatch.setattr(cli_studio, "make_app", lambda cfg: create_app(cfg, writer=Writer()))

    def invoke(*args):
        return CliRunner().invoke(cli_studio.app, list(args), obj=config)

    path = tmp_path / "beat.wav"
    path.write_bytes(metronome(120, 6))
    missing = invoke("music", "import", str(path), "--source", "x", "--license", "y")
    assert missing.exit_code != 0
    imported = invoke("music", "import", str(path), "--source", "自制", "--license", "CC0", "--attribution", "作者",
                      "--tag", "电子", "--no-ai", "--json")
    assert imported.exit_code == 0, imported.output
    track = json.loads(imported.stdout)
    assert track["mood_source"] == "features" and track["tags"] == ["电子"]
    prefix = track["id"][:8]
    assert prefix in invoke("music", "list").stdout
    assert "署名：作者" in invoke("music", "show", prefix).stdout
    updated = invoke("music", "update", prefix, "--mood", "tech", "--expires", "2099-01-01", "--json")
    assert json.loads(updated.stdout)["moods"] == ["tech"]
    recommended = invoke("music", "recommend", "--job", job_id, "--shot", "#1", "--json")
    assert recommended.exit_code == 0, recommended.output
    assert json.loads(recommended.stdout)["items"][0]["id"] == track["id"]
    used = invoke("music", "use", job_id, "#2", prefix, "--volume", "0.5")
    assert used.exit_code == 0, used.output
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    assert ws.version(w, p["shots"]["b"]["content"])["data"]["music"]["track_id"] == track["id"]
    refused = invoke("music", "delete", prefix, "--yes")
    assert refused.exit_code == 2, refused.output
    assert invoke("music", "analyze", prefix, "--keep-moods", "--json").exit_code == 0
