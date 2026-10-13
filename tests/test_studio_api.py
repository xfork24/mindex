from __future__ import annotations

import time

import pytest
from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.indexing import IndexBuilder
from mindex.pipeline import Pipeline


@pytest.fixture
def studio_client(config, video_file):
    pipeline = Pipeline(config)
    pipeline.prepare(video_file)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    client = TestClient(
        create_app(
            config,
            embedder,
            storyboarder=FixtureStoryboarder(config),
            speaker=FixtureSpeaker(config),
        )
    )
    return config, client


def wait_step(client: TestClient, job_id: str, step: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/studio/jobs/{job_id}").json()
        status = detail["job"]["steps"][step]["status"]
        if status in {"complete", "failed"}:
            return detail
        time.sleep(0.02)
    raise AssertionError(f"步骤 {step} 在 {timeout}s 内未结束")


def create_job(client: TestClient, text: str) -> str:
    response = client.post("/api/studio/jobs", json={"text": text})
    assert response.status_code == 201
    return response.json()["id"]


def test_studio_page_served(studio_client):
    _, client = studio_client
    for path in ["/studio", "/studio/"]:
        assert client.get(path).status_code == 200


def test_create_job_validation_and_listing(studio_client):
    _, client = studio_client
    assert client.post("/api/studio/jobs", json={"text": "   "}).status_code == 422
    assert client.post("/api/studio/jobs", json={"text": "正常文案。", "options": {"bad": 1}}).status_code == 422
    job_id = create_job(client, "列表里的任务。")
    listing = client.get("/api/studio/jobs").json()
    assert listing["items"][0]["id"] == job_id
    assert listing["items"][0]["current_step"] == "split"
    assert client.get("/api/studio/jobs/missing").status_code == 404


def test_split_then_recall_flow_with_real_index(studio_client):
    _, client = studio_client
    job_id = create_job(client, "红色首先出现。随后蓝色的天空调转。绿色在结尾蔓延开来。")
    assert client.post(f"/api/studio/jobs/{job_id}/steps/select").status_code == 422  # 依赖未完成
    assert client.post(f"/api/studio/jobs/{job_id}/steps/unknown").status_code == 422
    assert client.post(f"/api/studio/jobs/{job_id}/steps/split").status_code == 202
    detail = wait_step(client, job_id, "split")
    assert detail["job"]["steps"]["split"]["status"] == "complete"
    assert len(detail["storyboard"]["shots"]) >= 2
    # 召回可先于配音执行（只依赖分镜）
    assert client.post(f"/api/studio/jobs/{job_id}/steps/recall").status_code == 202
    detail = wait_step(client, job_id, "recall")
    assert detail["job"]["steps"]["recall"]["status"] == "complete"
    shots = detail["shots"]
    assert all(shot["candidates"]["status"] == "ok" and shot["candidates"]["count"] >= 1 for shot in shots)
    top = shots[0]["candidates"]["top"]
    assert top["media_url"].startswith("/api/media/")
    assert client.get(top["media_url"]).status_code == 200  # 候选预览走真实媒体端点
    assert client.get(top["thumbnail_url"]).status_code == 200
    # 配音 → 每镜精确音频时长可试听
    assert client.post(f"/api/studio/jobs/{job_id}/steps/tts").status_code == 202
    detail = wait_step(client, job_id, "tts")
    assert detail["job"]["steps"]["tts"]["status"] == "complete"
    assert all(shot["audio"] and shot["audio"]["duration"] > 0 for shot in detail["shots"])
    response = client.get(detail["shots"][0]["audio"]["url"], headers={"Range": "bytes=0-127"})
    assert response.status_code == 206 and len(response.content) == 128
    # 选片 → 时间线事件与分镜一一对应
    assert client.post(f"/api/studio/jobs/{job_id}/steps/select").status_code == 202
    detail = wait_step(client, job_id, "select")
    assert detail["job"]["steps"]["select"]["status"] == "complete"
    assert detail["timeline"] and detail["timeline"]["mode"] == "auto"
    assert len(detail["timeline"]["events"]) == len(detail["shots"])
    assert all(shot["selection"] and shot["selection"]["end"] > shot["selection"]["start"] for shot in detail["shots"])
    assert detail["timeline"]["total_seconds"] > 0


def test_recall_failure_without_active_index(config, video_file):
    pipeline = Pipeline(config)
    pipeline.prepare(video_file)
    client = TestClient(
        create_app(config, FixtureEmbedder(config), storyboarder=FixtureStoryboarder(config))
    )
    job_id = create_job(client, "没有任何索引的时候召回。")
    client.post(f"/api/studio/jobs/{job_id}/steps/split")
    wait_step(client, job_id, "split")
    client.post(f"/api/studio/jobs/{job_id}/steps/recall")
    detail = wait_step(client, job_id, "recall")
    assert detail["job"]["steps"]["recall"]["status"] == "failed"
    assert "尚未发布索引" in detail["job"]["steps"]["recall"]["error"]


def test_run_all_rejects_when_complete_and_artifacts_download(studio_client):
    _, client = studio_client
    # burn=false 走软字幕路径：烧录依赖 libass，CI/开发机 ffmpeg 未必编译
    created = client.post("/api/studio/jobs", json={"text": "下载产物的任务。", "options": {"burn": False}})
    job_id = created.json()["id"]
    client.post(f"/api/studio/jobs/{job_id}/steps/split")
    wait_step(client, job_id, "split")
    response = client.get(f"/api/studio/jobs/{job_id}/artifacts/storyboard.json?download=true")
    assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
    assert response.json()["shots"]
    # 路径穿越与缺失产物
    assert client.get(f"/api/studio/jobs/{job_id}/artifacts/../job.json").status_code in {404, 400}
    assert client.get(f"/api/studio/jobs/{job_id}/artifacts/render/final.mp4").status_code == 404
    # 全自动连跑：五步全部完成，成片可下载
    assert client.post(f"/api/studio/jobs/{job_id}/run").status_code == 202
    deadline = time.monotonic() + 60
    state = None
    while time.monotonic() < deadline:
        state = client.get(f"/api/studio/jobs/{job_id}").json()["job"]["steps"]
        if state["render"]["status"] in {"complete", "failed"}:
            break
        time.sleep(0.05)
    assert state["split"]["status"] == "complete"
    assert state["recall"]["status"] == "complete"
    assert state["tts"]["status"] == "complete"
    assert state["select"]["status"] == "complete"
    assert state["render"]["status"] == "complete", state["render"]
    final = client.get(f"/api/studio/jobs/{job_id}/artifacts/render/final.mp4")
    assert final.status_code == 200 and final.headers["content-type"] == "video/mp4"
    srt = client.get(f"/api/studio/jobs/{job_id}/artifacts/render/final.srt?download=true")
    assert srt.status_code == 200 and "attachment" in srt.headers["content-disposition"]
    # 全部完成后再连跑 -> 422
    assert client.post(f"/api/studio/jobs/{job_id}/run").status_code == 422


def test_delete_job(studio_client):
    _, client = studio_client
    job_id = create_job(client, "待删除的任务。")
    assert client.delete(f"/api/studio/jobs/{job_id}").status_code == 200
    assert client.get(f"/api/studio/jobs/{job_id}").status_code == 404
    assert client.delete(f"/api/studio/jobs/{job_id}").status_code == 404


def test_patch_storyboard_cascades_and_patch_timeline_reselects(studio_client):
    _, client = studio_client
    job_id = client.post(
        "/api/studio/jobs",
        json={"text": "红色首先出现在画面里，占据了整个视野。随后蓝色调转过来慢慢铺满屏幕。", "options": {"burn": False}},
    ).json()["id"]
    for step in ("split", "recall", "tts", "select"):
        client.post(f"/api/studio/jobs/{job_id}/steps/{step}")
        wait_step(client, job_id, step)
    detail = client.get(f"/api/studio/jobs/{job_id}").json()
    original = detail["timeline"]["events"]
    assert detail["job"]["steps"]["select"]["status"] == "complete"

    # 换选：shot-01 改用候选池里的另一个片段，覆盖全部分镜
    pool = client.get(f"/api/studio/jobs/{job_id}/artifacts/candidates/shot-01.json").json()
    chosen = original[0]["video"]["segment_id"]
    alternative = next(item["segment_id"] for item in pool["results"] if item["segment_id"] != chosen)
    patch = {
        "events": [
            {"shot_id": event["shot_id"], "video": {"segment_id": alternative if event["order"] == 1 else event["video"]["segment_id"]}}
            for event in original
        ]
    }
    response = client.patch(f"/api/studio/jobs/{job_id}/timeline", json=patch)
    assert response.status_code == 200
    updated = response.json()["timeline"]
    assert updated["mode"] == "manual"
    assert updated["events"][0]["video"]["segment_id"] == alternative
    assert response.json()["job"]["steps"]["select"]["status"] == "complete"
    assert response.json()["job"]["steps"]["render"]["status"] == "pending"  # 仅作废渲染
    # 覆盖不完整 -> 422
    partial = {"events": patch["events"][:1]}
    assert client.patch(f"/api/studio/jobs/{job_id}/timeline", json=partial).status_code == 422
    # 候选池外的片段 -> 422
    bogus = {"events": [{"shot_id": event["shot_id"], "video": {"segment_id": "不存在" if event["order"] == 1 else event["video"]["segment_id"]}} for event in updated["events"]]}
    assert client.patch(f"/api/studio/jobs/{job_id}/timeline", json=bogus).status_code == 422

    # 编辑分镜：级联作废 recall 之后的所有步骤
    shots = [
        {"id": shot["id"], "text": shot["text"], "query": shot["query"], "estimated_seconds": shot["estimated_seconds"]}
        for shot in detail["storyboard"]["shots"]
    ]
    shots[0]["text"] = "改写后的红色开场文案。"
    shots[0]["query"] = "红色 画面 开场"
    response = client.patch(f"/api/studio/jobs/{job_id}/storyboard", json={"title": "改标题", "shots": shots})
    assert response.status_code == 200
    state = response.json()["job"]
    assert state["title"] == "改标题"
    assert state["steps"]["split"]["status"] == "complete"
    assert all(state["steps"][step]["status"] == "pending" for step in ("recall", "tts", "select", "render"))
    # 未完成 split 的新任务不可编辑
    other = create_job(client, "还没拆分镜的任务。")
    assert client.patch(f"/api/studio/jobs/{other}/storyboard", json={"shots": shots}).status_code == 422
