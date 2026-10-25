"""画面意象 visual 的后端集成测试：各保存路径透传、老分镜版本不变、只改 visual 不失效、超长拒绝。"""

from __future__ import annotations

import copy

import pytest
from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, legacy_job
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.studio import studio, studio_steps
from mindex.studio import workspace as ws

# 含首尾空白、emoji 与恰好 120 字的 visual，用于逐字符回读。
VISUAL = "  湖边安静的村落🌅，晨雾里的木屋  "
EXACT = "景" * 119 + "🌄"  # 恰好 120 个 Unicode 字符
TOO_LONG = "雾" * 121


def shot_body(shot: studio.Shot, **changes) -> dict:
    return {**shot.model_dump(), **changes}


def board(w, p) -> list[studio.Shot]:
    return [
        studio.Shot.model_validate(ws.version(w, p["shots"][sid]["content"])["data"])
        for sid in ws.ordered_shots(w, p)
    ]


def content_data(w, p) -> list[dict]:
    return [ws.version(w, p["shots"][sid]["content"])["data"] for sid in ws.ordered_shots(w, p)]


def setup_workspace(config, *, visual=None):
    job_id = legacy_job(config, "红色开场。蓝色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            studio.Shot(id="a", text="红色开场。", query="红色", estimated_seconds=1, visual=visual),
            studio.Shot(id="b", text="蓝色结尾。", query="蓝色", estimated_seconds=1),
        ],
    )
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return job_id, w, p


@pytest.fixture
def client(config):
    return TestClient(create_app(config, storyboarder=FixtureStoryboarder(config)))


def plan_url(job_id, pid):
    return f"/api/studio/jobs/{job_id}/plans/{pid}"


# ---------------------------------------------------------------- 各路径透传（需求 7.6、7.8）


def test_execute_split_produces_and_persists_visual(config):
    text = "红色首先出现在画面里。随后蓝色铺满屏幕。"
    job_id = legacy_job(config, text)["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    expected = [d.visual for d in FixtureStoryboarder(config).split(text)]
    ws.execute(config, job_id, w, p, "split", storyboarder=FixtureStoryboarder(config))
    ws.persist(config, job_id, w)
    fresh = ws.load(config, job_id)
    data = content_data(fresh, ws.plan(fresh, p["id"]))
    assert len(data) >= 2
    assert [d["visual"] for d in data] == expected
    assert all(d["visual"].startswith("画面：") for d in data)


def test_execute_split_truncates_overlong_model_visual(config):
    class Verbose(FixtureStoryboarder):
        def split(self, text, vocabulary=None):
            drafts = super().split(text, vocabulary)
            drafts[0].visual = TOO_LONG + "尾"
            return drafts

    job_id = legacy_job(config, "红色开场。")["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.execute(config, job_id, w, p, "split", storyboarder=Verbose(config))
    assert content_data(w, p)[0]["visual"] == "雾" * 120


def test_run_split_carries_visual_to_legacy_storyboard(config):
    text = "红色首先出现在画面里。随后蓝色铺满屏幕。"
    state = legacy_job(config, text)
    expected = [d.visual for d in FixtureStoryboarder(config).split(text)]
    studio_steps.run_split(config, state["id"], state, FixtureStoryboarder(config))
    shots = studio.load_storyboard(config, state["id"]).shots
    assert [s.visual for s in shots] == expected


def test_patch_storyboard_legacy_job_roundtrips_and_rejects_overlong(config, client):
    state = legacy_job(config, "红色首先出现在画面里。随后蓝色铺满屏幕。")
    job_id = state["id"]
    studio_steps.run_step(config, job_id, "split", storyboarder=FixtureStoryboarder(config))
    shots = [s.model_dump() for s in studio.load_storyboard(config, job_id).shots]
    shots[0]["visual"] = VISUAL
    shots[1]["visual"] = EXACT
    response = client.patch(f"/api/studio/jobs/{job_id}/storyboard", json={"shots": shots})
    assert response.status_code == 200, response.text
    saved = studio.load_storyboard(config, job_id).shots
    assert saved[0].visual == VISUAL.strip()
    assert saved[1].visual == EXACT

    path = studio.job_dir(config, job_id) / "storyboard.json"
    before = path.read_bytes()
    shots[0]["visual"] = TOO_LONG
    response = client.patch(f"/api/studio/jobs/{job_id}/storyboard", json={"shots": shots})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "120" in detail and "第 1 个分镜" in detail
    assert path.read_bytes() == before


def test_patch_plan_roundtrips_visual(config, client):
    job_id, w, p = setup_workspace(config)
    shots = board(w, p)
    body = {
        "revision": ws.revision(p),
        "shots": [shot_body(shots[0], visual=VISUAL), shot_body(shots[1], visual=EXACT)],
    }
    response = client.patch(plan_url(job_id, p["id"]), json=body)
    assert response.status_code == 200, response.text
    fresh = ws.load(config, job_id)
    data = content_data(fresh, ws.plan(fresh, p["id"]))
    assert data[0]["visual"] == VISUAL.strip()
    assert data[1]["visual"] == EXACT


def test_patch_plan_rejects_overlong_visual_without_writing(config, client):
    job_id, w, p = setup_workspace(config)
    path = ws.path(config, job_id)
    before = path.read_bytes()
    versions = sorted((ws.root(config, job_id) / "versions").iterdir())
    shots = board(w, p)
    body = {
        "revision": ws.revision(p),
        "shots": [shot_body(shots[0], visual="合法的画面"), shot_body(shots[1], visual=TOO_LONG)],
    }
    response = client.patch(plan_url(job_id, p["id"]), json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "第 2 个分镜" in detail and "120" in detail
    assert path.read_bytes() == before
    assert sorted((ws.root(config, job_id) / "versions").iterdir()) == versions


def test_select_content_version_restores_its_visual(config, client):
    job_id, w, p = setup_workspace(config, visual="甲：晨雾山道")
    original = p["shots"]["a"]["content"]
    shots = board(w, p)
    shots[0].visual = "乙：夜市摊位"
    ws.set_board(config, job_id, w, p, shots)
    ws.persist(config, job_id, w)
    assert p["shots"]["a"]["content"] != original
    response = client.post(
        f"{plan_url(job_id, p['id'])}/select-version",
        json={"revision": ws.revision(p), "stage": "content", "shot_id": "a", "version_id": original},
    )
    assert response.status_code == 200, response.text
    fresh = ws.load(config, job_id)
    fp = ws.plan(fresh, p["id"])
    assert fp["shots"]["a"]["content"] == original
    assert content_data(fresh, fp)[0]["visual"] == "甲：晨雾山道"


# ---------------------------------------------------------------- 老分镜版本号不变（需求 7.7）


@pytest.mark.parametrize("visual", [None, "", "   \n\t"])
def test_resaving_old_shots_without_visual_keeps_content_versions(config, client, visual):
    job_id, w, p = setup_workspace(config)
    before = {sid: p["shots"][sid]["content"] for sid in ws.ordered_shots(w, p)}
    assert all("visual" not in d for d in content_data(w, p))
    shots = board(w, p)
    body = {"revision": ws.revision(p), "shots": [shot_body(s, visual=visual) for s in shots]}
    response = client.patch(plan_url(job_id, p["id"]), json=body)
    assert response.status_code == 200, response.text
    fresh = ws.load(config, job_id)
    fp = ws.plan(fresh, p["id"])
    assert {sid: fp["shots"][sid]["content"] for sid in ws.ordered_shots(fresh, fp)} == before
    assert all("visual" not in d for d in content_data(fresh, fp))


# ---------------------------------------------------------------- 只改 visual 不失效（需求 7.10）


@pytest.fixture
def prepared(config, video_file):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 29.97
    Pipeline(config).prepare(video_file)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    return config, embedder


def build(config, job_id, w, p, embedder):
    checked = ws.preflight(config, job_id, w, p)
    bid = str(len(w["builds"]))
    w["builds"].append(
        {
            "id": bid,
            "plan_id": p["id"],
            "status": "running",
            "force": False,
            "nodes": [{**n, "status": "pending"} for n in checked["nodes"]],
        }
    )
    ws.persist(config, job_id, w)
    ws.run_build(config, job_id, bid, speaker=FixtureSpeaker(config), embedder=embedder)
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


def test_visual_only_edit_reuses_every_downstream_artifact(prepared):
    config, embedder = prepared
    job_id, w, p = setup_workspace(config)
    w, p = build(config, job_id, w, p, embedder)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    old = copy.deepcopy(p)
    shots = board(w, p)
    shots[0].visual = "红色幕布缓缓拉开"
    shots[1].visual = "蓝色天空的远景"
    ws.set_board(config, job_id, w, p, shots)
    # 只改 visual 会生成新的 content 版本，但各阶段已选产物原样保留。
    assert p["shots"]["a"]["content"] != old["shots"]["a"]["content"]
    for sid in ws.ordered_shots(w, p):
        for stage in ws.STAGES:
            vid = p["shots"][sid][stage]
            assert vid == old["shots"][sid][stage]
            assert not ws.conflicts(config, job_id, w, p, stage, sid, vid), (stage, sid)
    checked = ws.preflight(config, job_id, w, p)
    actions = {n["key"]: n["action"] for n in checked["nodes"]}
    for sid in ("a", "b"):
        for stage in ("recall", "tts", "select", "render"):
            assert actions[f"{stage}:{sid}"] == "reuse", (stage, sid, actions)
    assert actions["compose"] == "reuse"
    assert not any(ws.detail(config, job_id, w)["conflicts"].values())


# ---------------------------------------------------------------- SPLIT_VERSION 递增不改方案（需求 7.11）


def test_split_version_bump_keeps_existing_board_and_content(config, client, monkeypatch):
    job_id, w, p = setup_workspace(config, visual="红色幕布")
    before = ws.path(config, job_id).read_bytes()
    board_before, shots_before = p["board"], copy.deepcopy(p["shots"])
    data_before = content_data(w, p)
    monkeypatch.setattr(studio, "SPLIT_VERSION", studio.SPLIT_VERSION + 1)
    assert client.get(f"/api/studio/jobs/{job_id}/workspace").status_code == 200
    fresh = ws.load(config, job_id)
    fp = ws.plan(fresh, p["id"])
    checked = ws.preflight(config, job_id, fresh, fp)
    assert not any(n["stage"] == "split" and n["action"] != "reuse" for n in checked["nodes"])
    assert fp["board"] == board_before and fp["shots"] == shots_before
    assert content_data(fresh, fp) == data_before
    assert ws.path(config, job_id).read_bytes() == before
