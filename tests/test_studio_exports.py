"""多画幅导出（spec m3-post-production 需求 11）：快速派生（clean 画面重新排版）与重新渲染（关联子方案、
构建全部画幅），以及签名与删除保护。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api import create_app
from conftest import FixtureSpeaker
from studio import studio
from studio import workspace as ws
from test_studio_sources import build, frame_count, make_image, setup, shot, small, store_input  # noqa: F401

slow = pytest.mark.slow


@pytest.fixture
def canvases(small):  # noqa: F811
    small.studio.render.canvases = {"9:16": (90, 160), "1:1": (120, 120)}
    return small


def image_plan(config, tmp_path, **options):
    job_id, w, p = setup(config, [shot("a", "红色开场。")])
    image = store_input(config, job_id, make_image(tmp_path / "i.png"))
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [shot("a", "红色开场画面。", source={"kind": "image", "media_id": image}, highlights=["红色"])],
    )
    p["options"].update(options)
    ws.persist(config, job_id, w)
    return job_id, w, p


def test_export_options_validation():
    assert ws.PlanOptions.model_validate({}).exports is None
    assert ws.PlanOptions.model_validate({"exports": []}).exports is None
    with pytest.raises(ValidationError):
        ws.PlanOptions.model_validate({"exports": [{"canvas": "16:9"}]})  # 与方案画幅相同
    with pytest.raises(ValidationError):
        ws.PlanOptions.model_validate({"exports": [{"canvas": "1:1"}, {"canvas": "1:1", "mode": "rerender"}]})


def test_compose_signature_gains_clean_only_with_derive(canvases, tmp_path):
    job_id, w, p = image_plan(canvases, tmp_path)

    def payload():
        return ws.stages.handler("compose").specification(canvases, job_id, w, p, None, {}, None)[0]

    assert "clean" not in payload()
    assert [n["key"] for n in ws.preflight(canvases, job_id, w, p)["nodes"] if n["stage"] == "export"] == []
    p["options"]["exports"] = [{"canvas": "9:16", "fit": "crop", "mode": "rerender"}]
    assert "clean" not in payload()
    p["options"]["exports"] = [{"canvas": "9:16", "fit": "crop", "mode": "derive"}]
    assert payload()["clean"] is True
    nodes = {n["key"]: n for n in ws.preflight(canvases, job_id, w, p)["nodes"]}
    assert nodes["export:9:16"]["shot_id"] == "9:16" and ws.node_dependencies(w, p, "export", "9:16") == [
        "compose"
    ]


@slow
def test_derive_export_build(canvases, tmp_path):
    job_id, w, p = image_plan(
        canvases,
        tmp_path,
        burn=True,
        subtitle_style="box",
        exports=[{"canvas": "9:16", "fit": "crop", "mode": "derive"}],
    )
    w, p = build(canvases, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    final = ws.version(w, p["final"])
    assert "clean" in final["files"]
    exported = ws.version(w, p["finals"]["export:9:16"])
    assert exported["inputs"] == {"compose": final["id"]} and exported["data"]["size"] == [90, 160]
    mp4 = ws.file_path(canvases, job_id, exported, "mp4")
    frames, width, height = frame_count(mp4)
    assert (width, height) == (90, 160) and frames == final["data"]["total_frames"]
    ass = ws.file_path(canvases, job_id, exported, "ass").read_text(encoding="utf-8")
    assert "PlayResX: 90" in ass and "PlayResY: 160" in ass and "红色" in ass
    # 再次构建全部复用；改字幕样式后成片与导出都重建。
    nodes = {n["key"]: n["action"] for n in ws.preflight(canvases, job_id, w, p)["nodes"]}
    assert nodes["compose"] == nodes["export:9:16"] == "reuse"
    p["options"]["subtitle_style"] = "outline"
    nodes = {n["key"]: n["action"] for n in ws.preflight(canvases, job_id, w, p)["nodes"]}
    assert nodes["compose"] == nodes["export:9:16"] == "rebuild" and nodes["render:a"] == "reuse"


@slow
def test_rerender_child_plan_and_build_all(canvases, tmp_path):
    job_id, _, p = image_plan(canvases, tmp_path)
    with TestClient(create_app(canvases, speaker=FixtureSpeaker(canvases))) as client:
        base = f"/api/studio/jobs/{job_id}"
        data = client.get(f"{base}/workspace").json()
        options = {
            **data["plans"][p["id"]]["options"],
            "exports": [{"canvas": "1:1", "fit": "pad", "mode": "rerender"}],
        }
        saved = client.patch(
            f"{base}/plans/{p['id']}", json={"revision": data["revision"], "options": options}
        ).json()
        child = next(item for item in saved["plans"].values() if item.get("variant_of"))
        assert child["variant_of"] == {"plan": p["id"], "canvas": "1:1"}
        assert child["options"]["canvas"] == "1:1" and child["options"]["exports"] is None
        assert child["board"] == saved["plans"][p["id"]]["board"]
        # 单独预检时导出被阻塞；构建全部画幅时先构建子方案。
        single = {n["key"]: n for n in client.get(f"{base}/plans/{p['id']}/preflight").json()["nodes"]}
        assert single["export:1:1"]["action"] == "blocked"
        checked = client.get(f"{base}/plans/{p['id']}/preflight", params={"all_canvases": "true"}).json()
        keys = [n["key"] for n in checked["nodes"]]
        assert f"{child['id']}/render:a" in keys and keys[-1] == "export:1:1"
        built = client.post(
            f"{base}/plans/{p['id']}/build", json={"token": checked["token"], "all_canvases": True}
        )
        assert built.status_code == 202, built.text
        from test_studio_workspace import wait_build

        data = wait_build(client, job_id)
        assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
        nodes = {n["key"]: n["status"] for n in data["builds"][-1]["nodes"]}
        assert nodes[f"{child['id']}/tts:a"] == "complete" and nodes["tts:a"] == "reused"  # 配音共用
        main, sub = data["plans"][p["id"]], data["plans"][child["id"]]
        exported = data["versions"][main["finals"]["export:1:1"]]
        assert exported["data"]["final"] == sub["final"] and exported["data"]["plan"] == child["id"]
        w = ws.load(canvases, job_id)
        assert frame_count(ws.file_path(canvases, job_id, ws.version(w, exported["id"]), "mp4"))[1:] == (
            120,
            120,
        )
        # 子方案被主方案引用时不能删除。
        refused = client.delete(f"{base}/plans/{child['id']}")
        assert refused.status_code == 422 and "子方案" in refused.json()["detail"]
    with pytest.raises(studio.StudioError):
        ws.remove_plan(w, child["id"])


@slow
def test_export_entry_points_cli_and_agent(canvases, tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    import cli.studio as cli_studio
    from test_agent_studio_recall import run as run_tool

    job_id, _, _ = image_plan(canvases, tmp_path)
    monkeypatch.setattr(cli_studio, "make_app", lambda cfg: create_app(cfg, speaker=FixtureSpeaker(cfg)))
    runner = CliRunner()

    def invoke(*args):
        return runner.invoke(cli_studio.app, list(args), obj=canvases)

    saved = json.loads(invoke("export", "set", job_id, "9:16", "--json").stdout)
    assert (
        saved["exports"] == [{"canvas": "9:16", "fit": "crop", "mode": "derive"}] and saved["children"] == []
    )
    assert invoke("export", "set", job_id, "16:9").exit_code == 1  # 与方案画幅相同（422）
    built = invoke("build", job_id, "--json")
    assert built.exit_code == 0, built.output
    target = tmp_path / "out.mp4"
    assert invoke("download", job_id, "--export", "9:16", "-o", str(target)).exit_code == 0
    assert frame_count(target)[1:] == (90, 160)
    assert invoke("download", job_id, "--export", "1:1").exit_code == 1
    with TestClient(create_app(canvases, speaker=FixtureSpeaker(canvases))) as client:
        preview, output, _ = run_tool(
            client,
            canvases,
            "set_exports",
            {"job_id": job_id, "exports": [{"canvas": "1:1", "mode": "rerender"}]},
        )
        assert "重新渲染" in preview.changes[0]["after"] and output.value["children"]
        preview, _, _ = run_tool(
            client, canvases, "run_build", {"job_id": job_id, "all_canvases": True}, execute=False
        )
        assert preview.value["counts"]
    listed = json.loads(invoke("export", "list", job_id, "--json").stdout)
    assert listed["exports"][0]["mode"] == "rerender"
    cleared = json.loads(invoke("export", "clear", job_id, "--json").stdout)
    assert cleared["exports"] is None
