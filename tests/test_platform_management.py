"""Dynamic platforms, independent delivery rule snapshots, and real custom-platform delivery."""
# ruff: noqa: F811
import copy
import json

import pytest
from fastapi.testclient import TestClient
from test_studio_sources import small  # noqa: F401

from mindex.api import create_app
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.delivery_templates import get as get_template
from mindex.studio.platforms import catalogue, for_plan, preset


def custom_platform():
    item = preset("youtube").model_dump()
    item.update(id="custom-video", name="自定义平台", language="en")
    item["cover"].update(width=320, height=180)
    return item


def test_platform_crud_defaults_and_tombstones(small):
    with TestClient(create_app(small)) as client:
        root = "/api/studio/platforms"
        assert len(client.get(root).json()["items"]) == 3
        item = custom_platform()
        assert client.post(root, json=item).status_code == 201
        assert client.post(root, json=item).status_code == 409
        assert len(client.get(root).json()["items"]) == 4
        assert "custom-video" not in get_template(Store(small.data_dir), "base")["config"]["publish"]["platforms"]
        bad = {**item, "id": "invalid/id"}
        assert client.post(root, json=bad).status_code == 422
        assert client.put(root + "/custom-video", json={**item, "id": "renamed"}).status_code == 422
        for key in ("bilibili", "douyin", "youtube"):
            original = client.get(root + "/" + key).json()
            edited = copy.deepcopy(original)
            edited["title"]["max_chars"] = 12
            assert client.put(root + "/" + key, json=edited).status_code == 200
            base = get_template(Store(small.data_dir), "base")["config"]["publish"]
            assert base["platform_specs"][key]["title"]["max_chars"] == 12
            assert client.delete(root + "/" + key).status_code == 200
            new = studio.create_job(small, "默认平台")
            selected = new["options"].get("publish") or {}
            assert key not in selected.get("platforms", [])
        assert get_template(Store(small.data_dir), "base")["config"]["publish"] is None
        assert client.delete(root + "/custom-video").status_code == 200
        assert client.post(root, json=item).status_code == 409
    assert catalogue(Store(small.data_dir)) == []  # Reopening the database cannot reseed deleted records.


def test_template_and_task_snapshots_survive_catalogue_changes(small):
    with TestClient(create_app(small)) as client:
        root = "/api/studio/platforms"
        custom = client.post(root, json=custom_platform()).json()
        templates = "/api/studio/delivery-templates"
        template = client.post(templates + "/base/copy", json={"name": "四平台"}).json()
        config = template["config"]
        config["publish"]["platforms"].append(custom["id"])
        config["rules"][custom["id"]] = {"fixed_tags": ["固定"]}
        response = client.put(templates + "/" + template["id"], json={"name": "四平台", "config": config})
        assert response.status_code == 200, response.text
        frozen = response.json()["config"]
        assert len(frozen["publish"]["platform_specs"]) == 4
        job = studio.create_job(small, "任务快照")
        base = f"/api/studio/jobs/{job['id']}"
        view = client.get(base + "/workspace").json()
        pid = view["active_plan"]
        options = {**view["plans"][pid]["options"], "delivery": {"template_id": template["id"], "template_name": template["name"], "config": frozen}}
        response = client.patch(base + f"/plans/{pid}", json={"revision": view["revision"], "options": options})
        assert response.status_code == 200, response.text
        custom["title"]["max_chars"] = 5
        custom["canvas"] = "9:16"
        client.put(root + "/custom-video", json=custom)
        assert client.get(templates + "/" + template["id"]).json()["config"] == frozen
        client.delete(root + "/custom-video")
        assert client.get(templates + "/" + template["id"]).json()["config"] == frozen
        plan = ws.load(small, job["id"])["plans"][pid]
        assert for_plan(plan, "custom-video").canvas == "16:9"
        assert for_plan(plan, "custom-video").title.max_chars == 100
        # Explicit refresh updates the template rules while the task keeps its own original copy.
        frozen["publish"]["platform_specs"]["custom-video"] = custom
        response = client.put(templates + "/" + template["id"], json={"name": "四平台", "config": frozen})
        assert response.status_code == 200
        assert for_plan(ws.load(small, job["id"])["plans"][pid], "custom-video").canvas == "16:9"


def test_legacy_template_uses_frozen_builtin_rules(small):
    store = Store(small.data_dir)
    with TestClient(create_app(small)) as client:
        template = client.post("/api/studio/delivery-templates/base/copy", json={"name": "旧模板"}).json()
        legacy = template["config"]
        legacy["publish"].pop("platform_specs")
        store.execute("UPDATE delivery_templates SET body=? WHERE id=?", (json.dumps(legacy), template["id"]))
        platform = client.get("/api/studio/platforms/youtube").json()
        platform["title"]["max_chars"] = 5
        client.put("/api/studio/platforms/youtube", json=platform)
        client.delete("/api/studio/platforms/youtube")
        loaded = client.get("/api/studio/delivery-templates/" + template["id"]).json()
        assert loaded["config"]["publish"]["platform_specs"]["youtube"]["title"]["max_chars"] == 100


@pytest.mark.slow
def test_custom_platform_build_after_catalogue_deletion(small, tmp_path):
    from conftest import FixtureSpeaker
    from test_studio_publish import MetadataWriter
    from test_studio_sources import make_image, setup, shot, store_input
    from test_studio_workspace import wait_build

    job_id, w, p = setup(small, [shot("a", "自定义平台交付。")])
    image = store_input(small, job_id, make_image(tmp_path / "source.png"))
    ws.set_board(small, job_id, w, p, [shot("a", "自定义平台交付。", source={"kind": "image", "media_id": image})])
    ws.persist(small, job_id, w)
    writer = MetadataWriter()
    with TestClient(create_app(small, speaker=FixtureSpeaker(small), writer=writer)) as client:
        custom = custom_platform()
        client.post("/api/studio/platforms", json=custom)
        base = f"/api/studio/jobs/{job_id}/plans/{p['id']}"
        view = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
        options = {**view["plans"][p["id"]]["options"], "publish": {"platforms": [custom["id"]], "cover": {"text": False}}}
        response = client.patch(base, json={"revision": view["revision"], "options": options})
        assert response.status_code == 200, response.text
        client.delete("/api/studio/platforms/custom-video")
        token = client.get(base + "/preflight").json()["token"]
        assert client.post(base + "/build", json={"token": token}).status_code == 202
        view = wait_build(client, job_id)
        assert view["builds"][-1]["status"] == "complete", view["builds"][-1]
        final = view["plans"][p["id"]]["finals"]
        package = view["versions"][final["package:custom-video"]]
        assert "生成语言：en" in writer.calls[0]
        assert client.get(f"/api/studio/jobs/{job_id}/artifacts/{package['files']['zip']['path']}?download=true").status_code == 200
        assert view["versions"][final["cover:custom-video"]]["data"]["width"] == 320
        options = copy.deepcopy(view["plans"][p["id"]]["options"])
        options["publish"]["platform_specs"]["custom-video"]["name"] = "只修改名称"
        response = client.patch(base, json={"revision": view["revision"], "options": options})
        assert response.status_code == 200, response.text
        checked = client.get(base + "/preflight").json()
        assert {node["stage"] for node in checked["nodes"] if node["action"] == "rebuild"} == {"package"}



@pytest.mark.slow
def test_custom_platform_cli_and_agent(small):
    from test_agent_studio_recall import run as run_tool
    from typer.testing import CliRunner

    from mindex import cli_studio

    job = studio.create_job(small, "CLI 与助手平台选择")
    with TestClient(create_app(small)) as client:
        assert client.post("/api/studio/platforms", json=custom_platform()).status_code == 201
    runner = CliRunner()
    result = runner.invoke(cli_studio.app, ["publish", "set", job["id"], "--platform", "custom-video", "--json"], obj=small)
    assert result.exit_code == 0, result.output
    saved = json.loads(result.stdout)["publish"]
    assert saved["platform_specs"]["custom-video"]["language"] == "en"
    with TestClient(create_app(small)) as client:
        preview, output, _ = run_tool(client, small, "set_publish", {"job_id": job["id"], "platforms": ["custom-video", "douyin"]})
        assert "custom-video" in output.value["publish"]["platforms"]
        assert output.value["publish"]["platform_specs"]["douyin"]["canvas"] == "9:16"
        assert preview.warnings
        client.delete("/api/studio/platforms/custom-video")
        _, output, _ = run_tool(client, small, "set_publish", {"job_id": job["id"], "platforms": ["custom-video"]})
        assert output.value["publish"]["platform_specs"]["custom-video"]["name"] == "自定义平台"


@pytest.mark.slow
def test_platform_backup_full_and_partial(tmp_path, video_file):
    import yaml
    from test_backup import restore, seed

    from mindex.maintenance.backup import export_backup
    from mindex.maintenance.backup_db import store_at

    config, sources, _ = seed(tmp_path / "origin", video_file)
    config_path = tmp_path / "origin.yaml"
    config_path.write_text(yaml.safe_dump({"data_dir": str(config.data_dir), "embedding": {"dimensions": 8}}))
    with TestClient(create_app(config)) as client:
        custom = custom_platform()
        client.post("/api/studio/platforms", json=custom)
        template = client.post("/api/studio/delivery-templates/base/copy", json={"name": "备份规则"}).json()
        cfg = template["config"]
        cfg["publish"]["platforms"].append(custom["id"])
        assert client.put("/api/studio/delivery-templates/" + template["id"], json={"name": template["name"], "config": cfg}).status_code == 200
        client.delete("/api/studio/platforms/custom-video")
    for name, chosen in (("full", None), ("partial", [sources[0]])):
        bundle = tmp_path / ("bundle-" + name)
        export_backup(config, bundle, all_sources=chosen is None, sources=chosen, config_path=config_path)
        restored = tmp_path / ("restored-" + name)
        restore(bundle, restored)
        store = store_at(restored)
        assert store.one("SELECT deleted FROM delivery_platforms WHERE id='custom-video'")["deleted"] == 1
        template = get_template(store, template["id"])
        assert template["config"]["publish"]["platform_specs"]["custom-video"] == custom


def test_upgraded_delivery_snapshot_accepts_existing_publish_clients(small):
    job = studio.create_job(small, "升级旧交付快照")
    w = ws.load(small, job["id"])
    pid = w["active_plan"]
    options = w["plans"][pid]["options"]
    options["publish"].pop("platform_specs")
    options["delivery"]["config"]["publish"].pop("platform_specs")
    ws.persist(small, job["id"], w)
    with TestClient(create_app(small)) as client:
        client.post("/api/studio/platforms", json=custom_platform())
        view = client.get(f"/api/studio/jobs/{job['id']}/workspace").json()
        options = view["plans"][pid]["options"]
        options["publish"] = {"platforms": ["custom-video"], "cover": {"text": False}}
        response = client.patch(f"/api/studio/jobs/{job['id']}/plans/{pid}", json={"revision": view["revision"], "options": options})
        assert response.status_code == 200, response.text
        saved = response.json()["plans"][pid]["options"]
        assert saved["publish"]["platforms"] == ["custom-video"]
        assert saved["delivery"]["config"]["publish"] == saved["publish"]
