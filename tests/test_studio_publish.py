"""平台交付（spec m3-post-production 需求 12–15）：平台预设、发布元数据（校验修正、章节、人工修改钉住）、封面
（挑帧排版、尺寸与大小上限、AIGC 预检阻塞）、发布包（清单与哈希、上游变化重建），以及 REST 入口。"""

from __future__ import annotations

import json
import zipfile

import pytest
from conftest import FixtureSpeaker
from fastapi.testclient import TestClient
from PIL import Image
from test_studio_sources import make_image, setup, shot, small, store_input  # noqa: F401 — small 是夹具
from test_studio_workspace import wait_build

from mindex.api import create_app
from mindex.studio import platforms
from mindex.studio import workspace as ws
from mindex.studio.workspace import publish

slow = pytest.mark.slow


class MetadataWriter:
    """Tests only: 返回固定 JSON 的文案模型（超长标题与重复标签用来验证修正）。"""

    def __init__(self):
        self.calls = []

    def complete(self, instructions, text):
        self.calls.append(text)
        return json.dumps({"title": "城市夜景里的光与影" * 20, "description": "一段关于城市夜景的解说。",
                           "tags": ["夜景", "#夜景", "城市", "摄影", "延时", "光影", "街道", "霓虹", "建筑", "人文",
                                    "旅行", "纪录"]}, ensure_ascii=False)

    def write(self, outline, **kwargs):
        return outline


def test_presets_load_and_validate():
    items = platforms.presets()
    assert list(items) == ["bilibili", "douyin", "youtube"]
    youtube = items["youtube"]
    assert (youtube.cover.width, youtube.cover.height, youtube.cover.max_kb) == (1280, 720, 2048)
    assert youtube.chapters.min_count == 3 and youtube.chapters.min_seconds == 10 and youtube.title.max_chars == 100
    assert items["douyin"].canvas == "9:16" and not items["douyin"].chapters.enabled
    with pytest.raises(ValueError):
        publish.PublishOptions.model_validate({"platforms": ["youtube", "youtube"]})
    with pytest.raises(ValueError):
        publish.PublishOptions.model_validate({"platforms": ["tiktok"]})


def test_normalize_metadata_rules():
    spec = platforms.preset("youtube")
    data, notes = publish.normalize_metadata(
        {"title": "x" * 150, "description": "正文", "tags": ["a", "a", "#b", "c" * 40]}, spec,
        chapters=[{"start": 0.0, "title": "开场"}, {"start": 12.0, "title": "中段"}, {"start": 30.0, "title": "结尾"}],
        credits=["曲目 by 某人（CC BY 4.0）"])
    assert len(data["title"]) == 100 and data["title"].endswith("…")
    assert data["tags"] == ["a", "b", "c" * 30]
    assert "00:00 开场\n00:12 中段\n00:30 结尾" in data["description"]
    assert "音乐：曲目 by 某人（CC BY 4.0）" in data["description"] and data["body"] == "正文"
    assert notes
    douyin, _ = publish.normalize_metadata({"title": "t", "description": "d", "tags": ["夜景", "城市"]},
                                           platforms.preset("douyin"), chapters=[], credits=[])
    assert douyin["description"].endswith("#夜景 #城市")


def test_publish_nodes_follow_platform_film(small, tmp_path):  # noqa: F811
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(small, job_id, w, p, [shot("a", "红色开场。", source={"kind": "image", "media_id": image})])
    assert [n for n in ws.preflight(small, job_id, w, p)["nodes"] if n["stage"] in ("metadata", "cover")] == []
    p["options"]["publish"] = {"platforms": ["youtube", "douyin"], "cover": {"source": "aigc"}}
    p["options"]["exports"] = [{"canvas": "9:16", "fit": "crop", "mode": "derive"}]
    nodes = {n["key"]: n for n in ws.preflight(small, job_id, w, p)["nodes"]}
    assert ws.node_dependencies(w, p, "metadata", "douyin") == ["export:9:16"]
    assert ws.node_dependencies(w, p, "metadata", "youtube") == ["compose"]
    assert ws.node_dependencies(w, p, "package", "douyin") == ["export:9:16", "cover:douyin", "metadata:douyin"]
    assert nodes["cover:youtube"]["action"] == "blocked" and "提示词" in nodes["cover:youtube"]["reason"]
    assert list(nodes)[-6:] == ["metadata:youtube", "metadata:douyin", "cover:youtube", "cover:douyin",
                                "package:youtube", "package:douyin"]


@slow
def test_publish_build_cover_metadata_package(small, tmp_path):  # noqa: F811
    try:
        publish.cover_font(small)
    except Exception:  # noqa: BLE001
        pytest.skip("本机没有可用的中文字体")
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(small, job_id, w, p, [shot(sid, f"第{index}段旁白画面。", source={"kind": "image", "media_id": image},
                                            visual=f"画面{index}") for index, sid in enumerate("abc", 1)])
    p["options"]["publish"] = {"platforms": ["youtube"], "cover": {"subtitle": "副标题"}}
    ws.persist(small, job_id, w)
    writer = MetadataWriter()
    with TestClient(create_app(small, speaker=FixtureSpeaker(small), writer=writer)) as client:
        base = f"/api/studio/jobs/{job_id}"
        assert [item["id"] for item in client.get("/api/studio/platforms").json()["items"]] == list(platforms.BUILTIN_ORDER)
        token = client.get(f"{base}/plans/{p['id']}/preflight").json()["token"]
        client.post(f"{base}/plans/{p['id']}/build", json={"token": token})
        data = wait_build(client, job_id)
        assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
        finals = data["plans"][p["id"]]["finals"]
        meta = data["versions"][finals["metadata:youtube"]]["data"]
        assert len(meta["title"]) <= 100 and meta["tags"][:2] == ["夜景", "城市"] and len(meta["tags"]) == 11
        assert meta["chapters"] == []  # 三个约 1 秒的镜头凑不够 YouTube 每章 10 秒
        cover = data["versions"][finals["cover:youtube"]]
        assert (cover["data"]["width"], cover["data"]["height"]) == (1280, 720) and cover["data"]["lines"]
        w = ws.load(small, job_id)
        with Image.open(ws.file_path(small, job_id, ws.version(w, cover["id"]), "cover")) as picture:
            assert picture.size == (1280, 720) and picture.format == "JPEG"
        package = ws.version(w, finals["package:youtube"])
        archive = ws.file_path(small, job_id, package, "zip")
        with zipfile.ZipFile(archive) as bundle:
            names = sorted(bundle.namelist())
            manifest = json.loads(bundle.read("youtube/manifest.json"))
        assert names == ["youtube/cover.jpg", "youtube/manifest.json", "youtube/metadata.json", "youtube/metadata.txt",
                         "youtube/subtitles.srt", "youtube/subtitles.vtt", "youtube/video.mp4"]
        assert manifest["film"]["stage"] == "compose" and all(item["sha256"] for item in manifest["files"])
        download = client.get(f"/api/studio/jobs/{job_id}/artifacts/{package['files']['zip']['path']}",
                              params={"download": "true"})
        assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
        candidates = client.get(f"{base}/plans/{p['id']}/cover-candidates", params={"platform": "youtube"}).json()
        assert len(candidates["items"]) == 3 and client.get(candidates["items"][0]["url"]).status_code == 200
        # 人工修改元数据：钉住，发布包重建；封面也随标题重建。
        edited = client.patch(f"{base}/plans/{p['id']}/publish-metadata/youtube",
                              json={"revision": data["revision"], "title": "人工标题", "tags": ["夜景"]})
        assert edited.status_code == 200, edited.text
        w = ws.load(small, job_id)
        plan = ws.plan(w, p["id"])
        manual = ws.version(w, plan["finals"]["metadata:youtube"])
        assert manual["data"]["origin"] == "manual" and manual["data"]["title"] == "人工标题"
        nodes = {n["key"]: n for n in ws.preflight(small, job_id, w, plan)["nodes"]}
        assert nodes["metadata:youtube"]["action"] == "reuse"
        assert nodes["cover:youtube"]["action"] == nodes["package:youtube"]["action"] == "rebuild"
        assert len(writer.calls) == 1


@slow
def test_publish_entry_points_cli_and_agent(small, tmp_path, monkeypatch):  # noqa: F811
    from test_agent_aigc import read as read_tool
    from test_agent_studio_recall import run as run_tool
    from typer.testing import CliRunner

    from mindex import cli_studio

    try:
        publish.cover_font(small)
    except Exception:  # noqa: BLE001
        pytest.skip("本机没有可用的中文字体")
    job_id, w, p = setup(small, [shot("a", "红色开场。")])
    image = store_input(small, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(small, job_id, w, p, [shot("a", "红色开场画面。", source={"kind": "image", "media_id": image})])
    ws.persist(small, job_id, w)
    writer = MetadataWriter()
    monkeypatch.setattr(cli_studio, "make_app", lambda cfg: create_app(cfg, speaker=FixtureSpeaker(cfg), writer=writer))
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(cli_studio.app, list(args), obj=small)

    assert [item["id"] for item in json.loads(invoke("platforms", "--json").stdout)] == list(platforms.BUILTIN_ORDER)
    saved = invoke("publish", "set", job_id, "--platform", "bilibili", "--cover-subtitle", "第 1 期", "--json")
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.stdout)["publish"]["cover"]["subtitle"] == "第 1 期"
    built = invoke("publish", "build", job_id, "--json")
    assert built.exit_code == 0, built.output
    shown = json.loads(invoke("publish", "show", job_id, "--json").stdout)
    assert shown["platforms"][0]["package"]
    target = tmp_path / "bili.zip"
    assert invoke("publish", "download", job_id, "bilibili", "-o", str(target)).exit_code == 0
    assert zipfile.is_zipfile(target)
    assert json.loads(invoke("publish", "cover", job_id, "bilibili", "--json").stdout)["items"]
    edited = json.loads(invoke("publish", "metadata", job_id, "bilibili", "--title", "CLI 标题", "--json").stdout)
    assert edited["title"] == "CLI 标题" and edited["origin"] == "manual"
    with TestClient(create_app(small, speaker=FixtureSpeaker(small), writer=writer)) as client:
        _, listed, _ = read_tool(client, small, "list_platforms", {})
        assert len(listed.value["items"]) == 3
        _, meta, _ = read_tool(client, small, "get_publish_metadata", {"job_id": job_id, "platform": "bilibili"})
        assert meta.value["title"] == "CLI 标题"
        preview, output, _ = run_tool(client, small, "edit_publish_metadata", {
            "job_id": job_id, "platform": "bilibili", "tags": ["夜景", "城市"]})
        assert output.value["tags"] == ["夜景", "城市"]
        _, candidates, _ = read_tool(client, small, "cover_candidates", {"job_id": job_id, "platform": "bilibili"})
        assert candidates.images
        preview, output, _ = run_tool(client, small, "set_publish", {"job_id": job_id, "platforms": ["bilibili", "douyin"]})
        assert any("9:16" in item for item in preview.warnings)
        preview, _, _ = run_tool(client, small, "build_publish_package", {"job_id": job_id}, execute=False)
        assert "package:douyin" in preview.changes[2]["after"]
