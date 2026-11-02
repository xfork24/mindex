"""任务内文案创作：大纲建任务、方案级 script 阶段、过期重拆、老任务迁移与构建顺序。"""

from __future__ import annotations

import time

import pytest
from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, FixtureWriter, convert_to_legacy
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.models.adapters import ModelError
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.studio import studio
from mindex.studio import workspace as ws

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行

OUTLINE = "红色开场。蓝色结尾。"


@pytest.fixture
def env(config, video_file):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    Pipeline(config).prepare(video_file)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    writer = FixtureWriter(config, vary=True)
    client = TestClient(
        create_app(
            config, embedder, speaker=FixtureSpeaker(config), storyboarder=FixtureStoryboarder(config),
            writer=writer,
        )
    )
    return config, client, writer


def workspace(client, jid):
    response = client.get(f"/api/studio/jobs/{jid}/workspace")
    assert response.status_code == 200, response.text
    return response.json()


def wait_build(client, jid):
    for _ in range(2000):
        data = workspace(client, jid)
        if data["builds"] and data["builds"][-1]["status"] != "running":
            return data
        time.sleep(0.01)
    raise AssertionError("build timeout")


def new_job(client, outline=OUTLINE):
    response = client.post("/api/studio/jobs", json={"text": outline, "options": {"burn": False}})
    assert response.status_code == 201, response.text
    jid = response.json()["id"]
    return jid, workspace(client, jid)["active_plan"]


def run_node(client, jid, pid, stage, *, force=True):
    data = workspace(client, jid)
    inputs = {"script": data["plans"][pid]["script"]} if stage == "split" else {}
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/nodes",
        json={"revision": data["revision"], "inputs": inputs, "stage": stage, "force": force},
    )
    assert response.status_code == 202, response.text
    return wait_build(client, jid)


def patch_script(client, jid, pid, text):
    data = workspace(client, jid)
    return client.patch(
        f"/api/studio/jobs/{jid}/plans/{pid}", json={"revision": data["revision"], "script": text}
    )


def build_all(client, jid, pid):
    checked = client.get(f"/api/studio/jobs/{jid}/plans/{pid}/preflight").json()
    response = client.post(f"/api/studio/jobs/{jid}/plans/{pid}/build", json={"token": checked["token"]})
    assert response.status_code == 202, response.text
    return wait_build(client, jid)


def test_new_job_stores_outline_only_and_generates_no_script(env):
    config, client, writer = env
    jid, pid = new_job(client)
    state = studio.load_job(config, jid)
    assert state["outline"] == OUTLINE and "input_text" not in state
    data = workspace(client, jid)
    plan = data["plans"][pid]
    assert plan["script"] is None and plan["board"] is None
    assert not data["script_stale"] and data["job"]["outline"] == OUTLINE
    assert writer.calls == 0  # 创建任务只异步生成标题，不写文案
    assert client.post("/api/studio/jobs", json={"text": "  "}).status_code == 422
    too_long = "长" * (config.studio.max_input_chars + 1)
    assert client.post("/api/studio/jobs", json={"text": too_long}).status_code == 422


def test_generate_script_creates_a_new_version_each_time(env):
    _, client, writer = env
    jid, pid = new_job(client)
    data = run_node(client, jid, pid, "script")
    build = data["builds"][-1]
    assert build["status"] == "complete" and [n["stage"] for n in build["nodes"]] == ["script"]
    first = data["plans"][pid]["script"]
    version = data["versions"][first]
    assert version["stage"] == "script" and version["status"] == "complete"
    assert version["data"]["origin"] == "generated" and version["data"]["text"] == OUTLINE + "第1版。"
    assert writer.outlines == [OUTLINE]  # 模型只看到大纲
    # 重复点击重新生成：新版本，旧版本保留可选。
    data = run_node(client, jid, pid, "script")
    second = data["plans"][pid]["script"]
    assert second != first and first in data["versions"]
    assert data["versions"][second]["data"]["text"] == OUTLINE + "第2版。"
    assert writer.calls == 2


def test_manual_script_edit_is_a_versioned_save(env):
    _, client, _ = env
    jid, pid = new_job(client)
    run_node(client, jid, pid, "script")
    response = patch_script(client, jid, pid, "  手动改写的文案。  ")
    assert response.status_code == 200, response.text
    data = response.json()
    version = data["versions"][data["plans"][pid]["script"]]
    assert version["data"] == {"text": "手动改写的文案。", "origin": "manual"}
    versions = sum(1 for v in data["versions"].values() if v["stage"] == "script")
    # 内容没有变化不产生新版本；空文案被拒绝。
    again = patch_script(client, jid, pid, "手动改写的文案。")
    assert again.status_code == 200
    assert sum(1 for v in again.json()["versions"].values() if v["stage"] == "script") == versions
    assert patch_script(client, jid, pid, "   ").status_code == 422


def test_script_length_limit_follows_configuration(env):
    config, client, _ = env
    jid, pid = new_job(client)
    config.studio.max_input_chars = 5  # create_app 持有同一个配置对象
    assert patch_script(client, jid, pid, "六个字的文案。").status_code == 422
    assert patch_script(client, jid, pid, "五个字文案").status_code == 200


def test_split_requires_a_script(env):
    _, client, _ = env
    jid, pid = new_job(client)
    data = workspace(client, jid)
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/nodes",
        json={"revision": data["revision"], "inputs": {"script": None}, "stage": "split", "force": True},
    )
    assert response.status_code == 422
    assert "请先生成文案" in response.text


def test_build_from_scratch_starts_with_script_and_depends_on_it(env):
    _, client, writer = env
    jid, pid = new_job(client)
    checked = client.get(f"/api/studio/jobs/{jid}/plans/{pid}/preflight").json()
    assert [n["stage"] for n in checked["nodes"][:2]] == ["script", "split"]
    assert all(n["action"] != "blocked" for n in checked["nodes"])
    data = build_all(client, jid, pid)
    build = data["builds"][-1]
    assert build["status"] == "complete", build
    stages = [n["stage"] for n in build["nodes"]]
    assert list(dict.fromkeys(stages)) == ["script", "split", "recall", "tts", "select", "render", "compose",
                                          "export", "metadata", "cover", "package"]
    assert stages.count("script") == stages.count("split") == 1
    plan = data["plans"][pid]
    assert plan["final"] and plan["board_script"] == plan["script"] and not data["script_stale"]
    # 成片的依赖树一直追溯到文案版本。
    tree = client.get(f"/api/studio/jobs/{jid}/versions/{plan['final']}/dependencies").json()["nodes"]
    assert plan["script"] in tree and tree[plan["script"]]["stage"] == "script"
    split_versions = [v for v in tree.values() if v["stage"] == "split"]
    assert split_versions and all(v["inputs"] == {"script": plan["script"]} for v in split_versions)
    # 已有文案时再次构建直接复用：不会悄悄重写。
    again = client.get(f"/api/studio/jobs/{jid}/plans/{pid}/preflight").json()
    assert "script" not in [n["stage"] for n in again["nodes"]]
    assert writer.calls == 1


def test_script_change_marks_storyboard_stale_and_rebuild_resplits(env):
    _, client, _ = env
    jid, pid = new_job(client)
    data = build_all(client, jid, pid)
    plan = data["plans"][pid]
    old_board, old_script = plan["board"], plan["script"]
    assert not data["script_stale"]

    data = patch_script(client, jid, pid, "红色开场。蓝色结尾。绿色收尾。").json()
    assert data["script_stale"] is True
    # 过期的分镜和成片产物都保留，只提示。
    assert data["plans"][pid]["board"] == old_board and data["plans"][pid]["final"]
    checked = client.get(f"/api/studio/jobs/{jid}/plans/{pid}/preflight").json()
    assert checked["nodes"][0]["stage"] == "split" and "文案已变化" in checked["nodes"][0]["reason"]
    assert "script" not in [n["stage"] for n in checked["nodes"]]
    assert not any(n["stage"] in ("recall", "tts") for n in checked["nodes"][:1])

    data = build_all(client, jid, pid)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    plan = data["plans"][pid]
    assert not data["script_stale"] and plan["board"] != old_board and plan["script"] != old_script
    # 新分镜的内容版本记录它们来自哪次拆分，拆分版本记录所用的文案版本。
    contents = [data["versions"][plan["shots"][sid]["content"]] for sid in plan["shots"]]
    split_ids = {c["inputs"].get("split") for c in contents} - {None}
    assert split_ids
    split = data["versions"][next(iter(split_ids))]
    assert split["stage"] == "split" and split["inputs"] == {"script": plan["script"]}

    # 选回旧文案版本：分镜工作台同步还原为依赖该版本的最后一次构建结果，不再过期。
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/select-version",
        json={"revision": data["revision"], "version_id": old_script, "stage": "script"},
    )
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["script_stale"] is False
    assert restored["plans"][pid]["script"] == old_script
    assert restored["plans"][pid]["board"] == old_board
    assert restored["plans"][pid]["board_script"] == old_script


def test_regenerate_switches_version_and_clears_storyboard(env):
    _, client, _ = env
    jid, pid = new_job(client)
    data = build_all(client, jid, pid)
    plan = data["plans"][pid]
    old_script, old_board = plan["script"], plan["board"]
    assert old_board and plan["shots"]

    # 重新生成文案（单独执行 script 节点）：自动切到新版本，分镜工作台立即清空。
    data = run_node(client, jid, pid, "script")
    plan = data["plans"][pid]
    new_script = plan["script"]
    assert new_script != old_script
    assert plan["board"] is None and plan["shots"] == {}
    assert plan["ranges"] == {} and plan["anchors"] == {}
    # 清空后不再提示“过期”，而是干净的空工作台。
    assert data["script_stale"] is False

    # 切回旧文案版本：分镜工作台恢复为它的最后一次构建结果。
    data = workspace(client, jid)
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/select-version",
        json={"revision": data["revision"], "version_id": old_script, "stage": "script"},
    )
    assert response.status_code == 200, response.text
    restored = response.json()["plans"][pid]
    assert restored["script"] == old_script
    assert restored["board"] == old_board and restored["board_script"] == old_script

    # 再切回新文案版本：它从未拆过分镜，工作台再次清空。
    data = workspace(client, jid)
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/select-version",
        json={"revision": data["revision"], "version_id": new_script, "stage": "script"},
    )
    assert response.status_code == 200, response.text
    again = response.json()["plans"][pid]
    assert again["script"] == new_script and again["board"] is None and again["shots"] == {}


def test_switch_to_unbuilt_script_version_clears_storyboard(env):
    _, client, _ = env
    jid, pid = new_job(client)
    # 先完整构建一次，得到一个带分镜的文案版本。
    data = build_all(client, jid, pid)
    built_script = data["plans"][pid]["script"]
    assert data["plans"][pid]["board"]
    # 手动改写出一个从未参与构建的新文案版本。
    data = patch_script(client, jid, pid, "红色开场。蓝色结尾。绿色收尾。").json()
    unbuilt_script = data["plans"][pid]["script"]
    assert unbuilt_script != built_script

    # 切到从未构建的版本：分镜工作台整体清空。
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/select-version",
        json={"revision": data["revision"], "version_id": unbuilt_script, "stage": "script"},
    )
    assert response.status_code == 200, response.text
    plan = response.json()["plans"][pid]
    assert plan["script"] == unbuilt_script
    assert plan["board"] is None and plan["shots"] == {}
    assert plan["ranges"] == {} and plan["anchors"] == {}

    # 切回已构建的版本：分镜工作台恢复为那次构建的结果。
    data = workspace(client, jid)
    response = client.post(
        f"/api/studio/jobs/{jid}/plans/{pid}/select-version",
        json={"revision": data["revision"], "version_id": built_script, "stage": "script"},
    )
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["script_stale"] is False
    assert restored["plans"][pid]["board"] and restored["plans"][pid]["board_script"] == built_script


def test_script_failure_blocks_the_rest_of_the_build(config, video_file):
    class Failing:
        def write(self, outline, *, template, materials=None, references=()):
            raise ModelError("文案服务不可用")

    Pipeline(config).prepare(video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config), writer=Failing()))
    jid, pid = new_job(client)
    data = build_all(client, jid, pid)
    build = data["builds"][-1]
    assert build["status"] == "failed"
    statuses = {n["stage"]: n["status"] for n in build["nodes"]}
    assert statuses["script"] == "failed" and statuses["split"] == "blocked"
    assert "文案服务不可用" in next(n for n in build["nodes"] if n["stage"] == "script")["error"]
    assert data["plans"][pid]["script"] is None and data["plans"][pid]["board"] is None


def test_new_and_copied_plans_inherit_the_script(env):
    _, client, _ = env
    jid, pid = new_job(client)
    data = build_all(client, jid, pid)
    script, board = data["plans"][pid]["script"], data["plans"][pid]["board"]
    base = f"/api/studio/jobs/{jid}/plans"
    fresh = client.post(base, json={"name": "新方案"}).json()
    plan = fresh["plans"][fresh["active_plan"]]
    assert plan["script"] == script and plan["board"] is None  # 带文案，不带分镜
    assert not fresh["script_stale"]
    checked = client.get(f"{base}/{plan['id']}/preflight").json()
    assert [n["stage"] for n in checked["nodes"][:1]] == ["split"]
    copied = client.post(base, json={"name": "副本", "copy_from": pid}).json()
    plan = copied["plans"][copied["active_plan"]]
    assert plan["script"] == script and plan["board"] == board


def test_legacy_job_input_text_becomes_a_completed_legacy_script(config):
    from conftest import legacy_job

    jid = legacy_job(config, "历史文案。第二句。")["id"]
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    script = w["versions"][p["script"]]
    assert script["stage"] == "script" and script["status"] == "complete"
    assert script["data"] == {"text": "历史文案。第二句。", "origin": "legacy"}
    assert not ws.script_stale(p)
    # 迁移是确定性的：再次读取得到同一个版本和同一个方案 revision。
    again = ws.load(config, jid)
    assert again["plans"][p["id"]]["script"] == p["script"]
    assert ws.revision(ws.plan(again, p["id"])) == ws.revision(p)
    # 已有文案：预检直接从拆分镜开始，不会生成文案。
    assert ws.preflight(config, jid, w, p)["nodes"][0]["stage"] == "split"


def test_legacy_job_with_storyboard_is_not_stale_after_migration(config):
    from conftest import legacy_job

    jid = legacy_job(config, "红色开场。蓝色结尾。")["id"]
    studio.write_storyboard(
        config,
        jid,
        studio.Storyboard(
            job_id=jid,
            shots=[studio.Shot(id="a", text="红色开场。蓝色结尾。", query="红色", estimated_seconds=1)],
        ),
    )
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    assert p["board"] and p["board_script"] == p["script"] and not ws.script_stale(p)


def test_legacy_step_api_rejects_new_jobs(env):
    config, client, _ = env
    jid = client.post("/api/studio/jobs", json={"text": OUTLINE}).json()["id"]
    for path in ("steps/split", "run"):
        response = client.post(f"/api/studio/jobs/{jid}/{path}")
        assert response.status_code == 422, (path, response.text)
        assert "版本工作台" in response.text
    # 打开过版本工作台的任务，旧接口一律拒绝（既有行为）。
    workspace(client, jid)
    assert client.post(f"/api/studio/jobs/{jid}/steps/split").status_code == 409
    # 而没打开过工作台的老任务仍能使用旧接口。
    legacy = client.post("/api/studio/jobs", json={"text": "老任务文案。"}).json()["id"]
    convert_to_legacy(config, legacy)
    assert client.post(f"/api/studio/jobs/{legacy}/steps/split").status_code == 202
