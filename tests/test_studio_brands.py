"""品牌模板（spec m3-post-production 需求 7）：内置只读、复制修改、素材上传与复验、方案选用与覆盖、删除保护、
样式进入成片签名。"""

from __future__ import annotations

import io
import shutil

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_studio_sources import make_clip, setup, shot, small  # noqa: F401 — small 是夹具

from mindex.api import create_app
from mindex.shared.storage import Store
from mindex.studio import brands
from mindex.studio import workspace as ws
from mindex.studio.workspace import ass, post


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 20), (255, 255, 255, 180)).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def client(small):  # noqa: F811
    with TestClient(create_app(small)) as client:
        yield small, client


def test_builtins_are_valid_and_read_only(client):
    _, client = client
    items = client.get("/api/studio/brands").json()["items"]
    assert [item["id"] for item in items] == list(brands.BUILTIN_ORDER)
    assert all(item["builtin"] and item["hash"] for item in items)
    assert client.put("/api/studio/brands/news", json={"name": "改"}).status_code == 403
    assert client.delete("/api/studio/brands/news").status_code == 403
    assert client.get("/api/studio/brands/missing").status_code == 404


def test_copy_update_assets_and_hash(client, tmp_path):
    config, client = client
    copied = client.post("/api/studio/brands/news/copy", json={"name": "我的频道"}).json()
    assert copied["id"].startswith("user-") and copied["base_id"] == "news" and not copied["builtin"]
    uploaded = client.post("/api/studio/brand-assets", files={"file": ("logo.png", png(), "image/png")})
    assert uploaded.status_code == 201, uploaded.text
    logo = uploaded.json()
    assert logo["kind"] == "image" and logo["alpha"] is True
    assert (config.data_dir / "brand" / f"{logo['sha256']}.png").is_file()
    assert client.get(logo["url"]).status_code == 200
    clip = make_clip(tmp_path / "intro.mp4", 1.0)
    intro = client.post("/api/studio/brand-assets", files={"file": ("intro.mp4", clip.read_bytes(), "video/mp4")}).json()
    assert intro["kind"] == "video" and intro["duration"] == pytest.approx(1.0, abs=0.1)
    asset = lambda item: {key: item[key] for key in ("sha256", "suffix", "kind")}
    renamed = client.put(f"/api/studio/brands/{copied['id']}", json={"name": "改名"}).json()
    assert renamed["hash"] == copied["hash"] and renamed["version"] == 1  # 名称不影响成片
    updated = client.put(f"/api/studio/brands/{copied['id']}", json={
        "watermark": {"asset": asset(logo), "position": "bottom-right"}, "intro": {"asset": asset(intro)}}).json()
    assert updated["hash"] != copied["hash"] and updated["version"] == 2
    assert updated["watermark"]["position"] == "bottom-right" and updated["intro"]["volume"] == 1.0
    # 水印必须是图片、片头必须是视频；素材缺失时拒绝。
    assert client.put(f"/api/studio/brands/{copied['id']}", json={"watermark": {"asset": asset(intro)}}).status_code == 422
    missing = {"sha256": "0" * 64, "suffix": ".png", "kind": "image"}
    assert client.put(f"/api/studio/brands/{copied['id']}", json={"watermark": {"asset": missing}}).status_code == 422
    removed = client.put(f"/api/studio/brands/{copied['id']}", json={"intro": None}).json()
    assert removed["intro"] is None and removed["watermark"]
    bad = client.post("/api/studio/brand-assets", files={"file": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 422


def test_plan_selects_brand_with_overrides_and_delete_is_protected(client):
    config, client = client
    job_id, _, p = setup(config, [shot("a", "红色开场。")])
    copied = client.post("/api/studio/brands/tech/copy", json={"name": "频道"}).json()
    data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    options = {**data["plans"][data["active_plan"]]["options"], "burn": True, "brand_id": copied["id"],
               "brand_overrides": {"overlay": {"accent": "#123456"}}}
    response = client.patch(f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                            json={"revision": data["revision"], "options": options})
    assert response.status_code == 200, response.text
    saved = response.json()["plans"][p["id"]]["options"]
    assert saved["brand_id"] == copied["id"] and saved["brand_overrides"]["overlay"]["accent"] == "#123456"
    assert client.delete(f"/api/studio/brands/{copied['id']}").status_code == 409
    unknown = {**options, "brand_id": "user-missing"}
    fresh = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
    assert client.patch(f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                        json={"revision": fresh["revision"], "options": unknown}).status_code == 422
    # 换回无品牌后可以删除。
    plain = {**fresh["plans"][p["id"]]["options"], "brand_id": None, "brand_overrides": None}
    assert client.patch(f"/api/studio/jobs/{job_id}/plans/{p['id']}",
                        json={"revision": fresh["revision"], "options": plain}).status_code == 200
    assert client.delete(f"/api/studio/brands/{copied['id']}").status_code == 200


def test_style_precedence_and_compose_payload(small):  # noqa: F811
    job_id, w, p = setup(small, [shot("a", "红色开场。", overlays=[{"kind": "title", "title": "开场"}])])
    store = Store(small.data_dir)
    news = brands.get_brand(store, "news")

    def payload():
        return ws.stages.handler("compose").specification(small, job_id, w, p, None, {}, None)[0]

    plain = payload()
    assert plain["overlay_style"] == ass.DEFAULT_OVERLAY_STYLE and "subtitle_style" not in plain
    p["options"]["brand_id"] = "news"
    branded = payload()
    assert branded["overlay_style"] == news.overlay.model_dump()
    assert "subtitle_style" not in branded  # 软字幕：字幕样式不参与
    p["options"]["burn"] = True
    assert payload()["subtitle_style"] == news.subtitle.model_dump()
    p["options"]["subtitle_style"] = "outline"  # 方案预设优先于品牌样式
    assert payload()["subtitle_style"] == ass.preset("outline")
    override = {**ass.preset("box"), "box_color": "#112233"}
    p["options"]["brand_overrides"] = {"subtitle": override}  # 方案覆盖的完整样式最优先
    assert payload()["subtitle_style"] == override
    assert post.brand_effect(small, {"options": {}}) is None
    effect = brands.resolve(store, {"brand_id": "news", "brand_overrides": {"disable": ["watermark"]}})
    assert effect["watermark"] is None and effect["id"] == "news" and effect["hash"]


# ---------------------------------------------------------------- 片头片尾与水印（需求 8）


def brand_with_assets(config, client, tmp_path, *, intro=True, outro=True, watermark=True, include_bookends=False):
    copied = client.post("/api/studio/brands/plain/copy", json={"name": "频道"}).json()

    def upload(path, kind):
        item = client.post("/api/studio/brand-assets", files={"file": (path.name, path.read_bytes(), kind)}).json()
        return {key: item[key] for key in ("sha256", "suffix", "kind")}

    changes = {}
    if intro:
        changes["intro"] = {"asset": upload(make_clip(tmp_path / "intro.mp4", 1.0, color="red"), "video/mp4")}
    if outro:
        changes["outro"] = {"asset": upload(make_clip(tmp_path / "outro.mp4", 0.6, audio=False, color="blue"),
                                            "video/mp4")}
    if watermark:
        logo = tmp_path / "logo.png"
        Image.new("RGBA", (60, 60), (255, 255, 255, 255)).save(logo)
        changes["watermark"] = {"asset": upload(logo, "image/png"), "position": "bottom-left", "opacity": 1.0,
                                "scale": 0.3, "margin": 0.02, "include_bookends": include_bookends}
    response = client.put(f"/api/studio/brands/{copied['id']}", json=changes)
    assert response.status_code == 200, response.text
    return response.json()


def test_brand_assets_enter_compose_payload_only_when_present(client, tmp_path):
    config, client = client
    job_id, w, p = setup(config, [shot("a", "红色开场。")])
    brand = brand_with_assets(config, client, tmp_path, intro=False, outro=False)

    def payload():
        return ws.stages.handler("compose").specification(config, job_id, w, p, None, {}, None)[0]

    assert "brand_assets" not in payload()
    p["options"]["brand_id"] = brand["id"]
    found = payload()
    assert set(found["brand_assets"]) == {"watermark"} and found["bookend_version"] == post.BOOKEND_VERSION
    p["options"]["brand_overrides"] = {"disable": ["watermark"]}
    assert "brand_assets" not in payload()


@pytest.mark.slow
def test_build_with_intro_outro_and_watermark(client, tmp_path):
    import json
    import subprocess

    from PIL import ImageStat
    from test_studio_sources import build, frame_count, make_image, store_input

    from mindex.shared.media import command

    config, client = client
    job_id, w, p = setup(config, [shot("a", "红色开场。")])
    image = store_input(config, job_id, make_image(tmp_path / "i.png", color=(10, 10, 10, 255)))
    ws.set_board(config, job_id, w, p, [shot("a", "开场画面旁白。", source={"kind": "image", "media_id": image,
                                                                     "motion": "none"})])
    ws.persist(config, job_id, w)
    w, p = build(config, job_id, w, p)
    plain = ws.version(w, p["final"])
    brand = brand_with_assets(config, client, tmp_path)
    p["options"]["brand_id"] = brand["id"]
    ws.persist(config, job_id, w)
    w, p = build(config, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    assert {n["key"]: n["status"] for n in w["builds"][-1]["nodes"]}["render:a"] == "reused"
    final = ws.version(w, p["final"])
    data = final["data"]
    assert data["bookends"]["intro"]["frames"] == 25 and data["bookends"]["outro"]["frames"] == 15
    assert data["total_frames"] == plain["data"]["total_frames"] + 40
    assert data["boundaries"][0]["start"] == pytest.approx(1.0)
    mp4 = ws.file_path(config, job_id, final, "mp4")
    assert frame_count(mp4)[0] == data["total_frames"]
    srt = ws.file_path(config, job_id, final, "srt").read_text(encoding="utf-8")
    assert "00:00:01,000 -->" in srt
    streams = json.loads(command(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of",
                                  "json", str(mp4)]))["streams"]
    audio = next(item for item in streams if item["codec_type"] == "audio")
    assert float(audio["duration"]) == pytest.approx(data["total_seconds"], abs=0.08)

    def frame(at, name):
        target = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(mp4), "-frames:v", "1", str(target)],
                       check=True)
        return Image.open(target).convert("L")

    def corner(img):
        width, height = img.size
        return ImageStat.Stat(img.crop((0, height * 3 // 4, width // 4, height))).mean[0]

    body, intro = frame(1.3, "body.png"), frame(0.3, "intro.png")
    assert corner(body) > 150  # 正文左下角有白色水印
    assert corner(intro) < 120  # 片头（红色）不加水印


# ---------------------------------------------------------------- 内置模板显示文字（中英双语）
def test_builtin_brand_text_bilingual():
    """内置风格模板的名称与说明走文案目录：中文与 YAML 原文一致，英文另有翻译。"""
    import json
    from pathlib import Path

    catalog = json.loads((Path(__file__).resolve().parents[1] / "frontend" / "src" / "shared" / "messages.json")
                         .read_text(encoding="utf-8"))
    for brand_id, template in brands.builtins().items():
        name = catalog[f"ui.brands_name_{brand_id}"]
        desc = catalog[f"ui.brands_desc_{brand_id}"]
        assert name["zh"] == template.name and desc["zh"] == template.description
        assert name["en"] and desc["en"] and name["en"] != template.name


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node 运行前端控制逻辑检查")
def test_studio_brand_text_controls():
    import subprocess
    from pathlib import Path

    result = subprocess.run(["node", "tests/studio_brand_text_unit.cjs"], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
