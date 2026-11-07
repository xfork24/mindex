"""Delivery snapshots: persistence, legacy compatibility, assets and downstream signatures."""

# ruff: noqa: F811
import copy
import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from api import create_app
from shared.storage import Store
from studio import studio
from studio import workspace as ws
from studio.delivery_templates import DeliveryConfig, DeliverySnapshot, apply, copy_cover
from studio.workspace import publish
from studio.workspace.handlers.publish import delivery_issues
from test_studio_sources import small  # noqa: F401


def test_defaults_and_validation():
    cfg = DeliveryConfig()
    assert [x.canvas for x in cfg.exports] == ["16:9", "9:16", "1:1"]
    assert cfg.publish.platforms == ["bilibili", "douyin", "youtube"]
    for canvas in ("16:9", "9:16", "1:1"):
        options = apply({"canvas": canvas}, DeliverySnapshot())
        assert len(options["exports"]) == 2
        assert all(x["canvas"] != canvas for x in options["exports"])
    with pytest.raises(ValidationError):
        DeliveryConfig(exports=[{"canvas": "1:1"}, {"canvas": "1:1"}])
    with pytest.raises(ValidationError):
        DeliveryConfig(package={"filename": "{unknown}"})
    assert "/" not in publish.safe_package_name(
        "../{canvas}/{platform}", job_id="a", plan_id="b", platform="youtube", canvas="16:9"
    )


def test_template_api_snapshots_and_delete(small):
    with TestClient(create_app(small)) as client:
        base = "/api/studio/delivery-templates"
        item = client.get(base).json()["items"][0]
        assert item["builtin"]
        assert client.put(base + "/base", json={"name": "x", "config": item["config"]}).status_code == 403
        assert client.delete(base + "/base").status_code == 403
        response = client.post(base + "/base/copy", json={"name": "My delivery"})
        assert response.status_code == 201, response.text
        custom = response.json()
        config = copy.deepcopy(custom["config"])
        config["exports"][1].update(mode="rerender", fit="pad")
        config["rules"]["youtube"] = {"fixed_tags": ["城市"], "footer": "尾注"}
        assert (
            client.put(base + "/" + custom["id"], json={"name": "Edited", "config": config}).status_code
            == 200
        )
        job = studio.create_job(small, "城市的一天", title="城市")
        w = ws.load(small, job["id"])
        p = ws.plan(w, w["active_plan"])
        assert p["options"]["delivery"]["template_id"] == "base"
        ws.persist(small, job["id"], w)
        detail = client.get(f"/api/studio/jobs/{job['id']}/workspace").json()
        # API revision follows the active plan, as in the task UI.
        payload = {
            "revision": detail["revision"],
            "options": {
                **p["options"],
                "delivery": {"template_id": custom["id"], "template_name": "Edited", "config": config},
            },
        }
        url = f"/api/studio/jobs/{job['id']}/plans/{p['id']}"
        response = client.patch(url, json=payload)
        assert response.status_code == 200, response.text
        saved = ws.load(small, job["id"])["plans"][p["id"]]["options"]
        assert saved["exports"][0]["mode"] == "rerender"
        assert client.delete(base + "/" + custom["id"]).status_code == 200
        assert ws.load(small, job["id"])["plans"][p["id"]]["options"] == saved
        assert client.get(base + "/" + custom["id"]).status_code == 404
        assert client.get("/studio/delivery-templates").status_code == 200


def test_legacy_options_stay_off(small):
    from conftest import legacy_job

    job = legacy_job(small, "旧任务")
    w = ws.load(small, job["id"])
    options = w["plans"][w["active_plan"]]["options"]
    assert not options.get("delivery") and not options.get("exports") and not options.get("publish")


def test_template_cover_is_copied_and_referenced_by_backup(small, tmp_path):
    from maintenance.backup_files import referenced_files
    from studio import brands

    path = tmp_path / "cover.png"
    Image.new("RGB", (100, 100), "red").save(path)
    ref = brands.store_asset(small, path, ".png", "Cover")
    asset = {k: ref[k] for k in ("sha256", "suffix", "kind", "name")}
    snapshot = DeliverySnapshot(config=DeliveryConfig(cover_asset=asset))
    snapshot.config.publish.cover.source = "upload"
    job = studio.create_job(small, "封面任务")
    copy_cover(small, job["id"], snapshot)
    assert snapshot.config.publish.cover.media_id == asset["sha256"]
    assert (
        studio.job_dir(small, job["id"]) / "inputs" / (asset["sha256"] + ".png")
    ).read_bytes() == path.read_bytes()
    with TestClient(create_app(small)) as client:
        item = client.post("/api/studio/delivery-templates/base/copy", json={"name": "图片"}).json()
        response = client.put(
            "/api/studio/delivery-templates/" + item["id"],
            json={"name": "图片", "config": snapshot.config.model_dump()},
        )
        assert response.status_code == 200, response.text
    store = Store(small.data_dir)
    assert "brand/" + asset["sha256"] + ".png" in referenced_files(small.data_dir, store.path)
    brands.asset_path(small, asset).unlink()
    copy_cover(small, job["id"], snapshot)  # Task copy remains usable without the template asset.


def test_footer_and_checks():
    from studio.platforms import preset

    data, _notes = publish.normalize_metadata(
        {"title": "标题", "description": "正文", "tags": ["固定", "固定", "新"]},
        preset("youtube"),
        chapters=[],
        credits=["音乐署名"],
        footer="尾注",
    )
    assert data["tags"] == ["固定", "新"]
    assert "尾注" in data["description"] and "音乐署名" in data["description"]
    p = {"options": {"canvas": "16:9", "publish": {"platforms": ["douyin"], "cover": {"source": "aigc"}}}}
    assert {kind for kind, _ in delivery_issues(None, "a", {}, p, "douyin")} == {"canvas", "cover"}


@pytest.mark.slow
def test_delivery_browser(small):

    from conftest import FixtureSpeaker
    from test_agent_panel_ui import playwright_available, serve
    from test_studio_publish import MetadataWriter

    if not playwright_available():
        pytest.skip("Playwright is not available")
    job = studio.create_job(small, "城市的一天", title="浏览器交付任务")
    server, thread, url = serve(create_app(small, speaker=FixtureSpeaker(small), writer=MetadataWriter()))
    try:
        result = subprocess.run(
            ["node", "tests/delivery_templates_ui.cjs"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "MINDEX_DELIVERY_URL": url, "MINDEX_DELIVERY_JOB": job["id"]},
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.mark.slow
def test_template_build_package_and_rebuild_scope(small, tmp_path):

    from conftest import FixtureSpeaker
    from test_studio_publish import MetadataWriter
    from test_studio_sources import make_image, setup, shot, store_input
    from test_studio_workspace import wait_build

    job_id, w, p = setup(small, [shot("a", "城市街景。")])
    image = store_input(small, job_id, make_image(tmp_path / "image.png"))
    ws.set_board(small, job_id, w, p, [shot("a", "城市街景。", source={"kind": "image", "media_id": image})])
    cfg = DeliveryConfig()
    cfg.publish.platforms = ["youtube"]
    cfg.rules["youtube"] = {"title_instructions": "简洁", "fixed_tags": ["固定标签"], "footer": "这是尾注"}
    cfg.package.vtt = False
    cfg.package.metadata_json = False
    cfg.package.filename = "{plan_id}-{platform}-{canvas}"
    snap = DeliverySnapshot.model_validate({"config": cfg.model_dump()})
    p["options"] = apply(p["options"], snap)
    ws.persist(small, job_id, w)
    writer = MetadataWriter()
    with TestClient(create_app(small, speaker=FixtureSpeaker(small), writer=writer)) as client:
        base = f"/api/studio/jobs/{job_id}/plans/{p['id']}"
        preflight = client.get(base + "/preflight").json()
        assert client.post(base + "/build", json={"token": preflight["token"]}).status_code == 202
        data = wait_build(client, job_id)
        assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
        final = data["plans"][p["id"]]["final"]
        finals = data["plans"][p["id"]]["finals"]
        assert "export:9:16" in finals and "export:1:1" in finals
        metadata = data["versions"][finals["metadata:youtube"]]["data"]
        assert metadata["tags"][0] == "固定标签"
        assert "这是尾注" in metadata["description"]
        assert "简洁" in writer.calls[0]
        package = data["versions"][finals["package:youtube"]]
        download = client.get(
            f"/api/studio/jobs/{job_id}/artifacts/{package['files']['zip']['path']}?download=true"
        )
        assert download.status_code == 200

        with zipfile.ZipFile(io.BytesIO(download.content)) as bundle:
            names = bundle.namelist()
        assert "youtube/video.mp4" in names and "youtube/manifest.json" in names
        assert "youtube/subtitles.vtt" not in names and "youtube/metadata.json" not in names
        assert "youtube/cover.jpg" in names and "youtube/metadata.txt" in names
        assert package["files"]["zip"]["path"].endswith(f"{p['id']}-youtube-16_9.zip")
        options = copy.deepcopy(data["plans"][p["id"]]["options"])
        options["delivery"]["config"]["package"]["metadata_txt"] = False
        response = client.patch(base, json={"revision": data["revision"], "options": options})
        assert response.status_code == 200, response.text
        after = client.get(base + "/preflight").json()
        assert {node["stage"] for node in after["nodes"] if node["action"] == "rebuild"} == {"package"}
        assert response.json()["plans"][p["id"]]["final"] == final


def test_snapshot_canvas_and_external_options_compatibility(small):
    job = studio.create_job(small, "切换画幅")
    with TestClient(create_app(small)) as client:
        view = client.get(f"/api/studio/jobs/{job['id']}/workspace").json()
        pid = view["active_plan"]
        base = f"/api/studio/jobs/{job['id']}/plans/{pid}"
        options = view["plans"][pid]["options"]
        options["canvas"] = "9:16"
        response = client.patch(base, json={"revision": view["revision"], "options": options})
        assert response.status_code == 200, response.text
        view = response.json()
        options = view["plans"][pid]["options"]
        assert {x["canvas"] for x in options["exports"]} == {"16:9", "1:1"}
        options["exports"] = []
        options["publish"] = None
        response = client.patch(base, json={"revision": view["revision"], "options": options})
        assert response.status_code == 200, response.text
        options = response.json()["plans"][pid]["options"]
        assert not options.get("exports") and not options.get("publish")
        assert options["delivery"]["config"]["publish"] is None
        # Enabled platform with missing target canvas can explicitly block packaging.
        snap = DeliverySnapshot()
        snap.config.exports = []
        snap.config.checks.canvas = "block"
        from studio.workspace.handlers.publish import PackageHandler

        p = {"options": apply({"canvas": "16:9"}, snap)}
        node = {"action": "rebuild"}
        PackageHandler().preflight(small, job["id"], {}, p, "douyin", {}, None, node)
        assert node["action"] == "blocked"
