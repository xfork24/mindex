from __future__ import annotations

import json
import time

import pytest
from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.shared.storage import Store
from mindex.studio import studio


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
    client.config = config
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


def make_legacy(client: TestClient, job_id: str) -> str:
    """把新建任务改成老任务形态（文本即文案 input_text，没有大纲），覆盖旧分步接口。"""
    path = studio.job_path(client.config, job_id)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["input_text"] = state.pop("outline")
    studio.save(path, state)
    return job_id


def create_legacy_job(client: TestClient, text: str, **body) -> str:
    response = client.post("/api/studio/jobs", json={"text": text, **body})
    assert response.status_code == 201
    return make_legacy(client, response.json()["id"])


def test_studio_page_served(studio_client):
    _, client = studio_client
    for path in ["/studio", "/studio/"]:
        assert client.get(path).status_code == 200


def test_create_job_validation_and_listing(studio_client):
    _, client = studio_client
    assert client.post("/api/studio/jobs", json={"text": "   "}).status_code == 422
    assert (
        client.post("/api/studio/jobs", json={"text": "正常文案。", "options": {"bad": 1}}).status_code == 422
    )
    job_id = create_job(client, "列表里的任务。")
    listing = client.get("/api/studio/jobs").json()
    assert listing["items"][0]["id"] == job_id
    assert listing["items"][0]["current_step"] == "split"
    assert client.get("/api/studio/jobs/missing").status_code == 404


def test_create_job_material_scope(studio_client):
    config, client = studio_client
    (source_id,) = [row["id"] for row in Store(config.data_dir).sources()]
    scoped = client.post(
        "/api/studio/jobs",
        json={"text": "限定素材。", "options": {"source_ids": [source_id]}},
    )
    assert scoped.status_code == 201
    assert scoped.json()["source_ids"] == [source_id]
    assert client.get(f"/api/studio/jobs/{scoped.json()['id']}").json()["job"]["source_ids"] == [
        source_id
    ]
    # 不传即不限，老任务继续读得到 source_ids=None。
    assert client.get(f"/api/studio/jobs/{create_job(client, '不限素材。')}").json()["job"][
        "source_ids"
    ] is None
    assert (
        client.post(
            "/api/studio/jobs", json={"text": "全不选。", "options": {"source_ids": []}}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/studio/jobs", json={"text": "不存在的素材。", "options": {"source_ids": ["缺失"]}}
        ).status_code
        == 422
    )


def test_split_then_recall_flow_with_real_index(studio_client):
    _, client = studio_client
    job_id = create_legacy_job(client, "红色首先出现。随后蓝色的天空调转。绿色在结尾蔓延开来。")
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
    assert top["media_url"].startswith("/api/admin/segments/")
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
    assert all(
        shot["selection"] and shot["selection"]["end"] > shot["selection"]["start"]
        for shot in detail["shots"]
    )
    assert detail["timeline"]["total_seconds"] > 0


def test_recall_failure_without_active_index(config, video_file):
    pipeline = Pipeline(config)
    pipeline.prepare(video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config), storyboarder=FixtureStoryboarder(config)))
    client.config = config
    job_id = create_legacy_job(client, "没有任何索引的时候召回。")
    client.post(f"/api/studio/jobs/{job_id}/steps/split")
    wait_step(client, job_id, "split")
    client.post(f"/api/studio/jobs/{job_id}/steps/recall")
    detail = wait_step(client, job_id, "recall")
    assert detail["job"]["steps"]["recall"]["status"] == "failed"
    assert "尚未发布索引" in detail["job"]["steps"]["recall"]["error"]


def test_run_all_rejects_when_complete_and_artifacts_download(studio_client):
    _, client = studio_client
    # burn=false 走软字幕路径：烧录依赖 libass，CI/开发机 ffmpeg 未必编译
    job_id = create_legacy_job(client, "下载产物的任务。", options={"burn": False})
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
    config, client = studio_client
    job_id = create_job(client, "待删除的任务。")
    from mindex.studio import studio

    directory = studio.job_dir(config, job_id)
    for relative in ["versions/example/render.mp4", "previews/quick/example.wav", "inputs/reference.png"]:
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test artifact")
    other_id = create_job(client, "保留的任务。")
    assert client.delete(f"/api/studio/jobs/{job_id}").status_code == 200
    assert not directory.exists()
    assert studio.job_dir(config, other_id).is_dir()
    assert client.get(f"/api/studio/jobs/{job_id}").status_code == 404
    assert client.delete(f"/api/studio/jobs/{job_id}").status_code == 404


def test_patch_storyboard_cascades_and_patch_timeline_reselects(studio_client):
    _, client = studio_client
    job_id = create_legacy_job(
        client,
        "红色首先出现在画面里，占据了整个视野。随后蓝色调转过来慢慢铺满屏幕。",
        options={"burn": False},
    )
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
            {
                "shot_id": event["shot_id"],
                "video": {"segment_id": alternative if event["order"] == 1 else event["video"]["segment_id"]},
            }
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
    bogus = {
        "events": [
            {
                "shot_id": event["shot_id"],
                "video": {"segment_id": "不存在" if event["order"] == 1 else event["video"]["segment_id"]},
            }
            for event in updated["events"]
        ]
    }
    assert client.patch(f"/api/studio/jobs/{job_id}/timeline", json=bogus).status_code == 422

    # 编辑分镜：级联作废 recall 之后的所有步骤
    shots = [
        {
            "id": shot["id"],
            "text": shot["text"],
            "query": shot["query"],
            "estimated_seconds": shot["estimated_seconds"],
        }
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
    # 未完成 split 的任务不可编辑
    other = create_legacy_job(client, "还没拆分镜的任务。")
    assert client.patch(f"/api/studio/jobs/{other}/storyboard", json={"shots": shots}).status_code == 422


def test_accepted_state_lock_conflicts_and_release(config):
    from threading import Event

    from filelock import FileLock

    from mindex.studio import studio

    entered, release = Event(), Event()

    class Slow(FixtureStoryboarder):
        def split(self, text, vocabulary=None):
            entered.set()
            release.wait(10)
            return super().split(text, vocabulary)

    client = TestClient(create_app(config, storyboarder=Slow(config)))
    client.config = config
    job_id = create_legacy_job(client, "这是用于慢速拆分的文案。")
    try:
        response = client.post(f"/api/studio/jobs/{job_id}/steps/split")
        assert response.status_code == 202
        assert response.json()["steps"]["split"] == "running"
        assert response.json()["execution"]["status"] == "running"
        assert entered.wait(3)
        assert client.post(f"/api/studio/jobs/{job_id}/steps/split").status_code == 409
        assert client.delete(f"/api/studio/jobs/{job_id}").status_code == 409
        assert client.get(f"/api/studio/jobs/{job_id}/archive").status_code == 409
        assert client.get(f"/api/studio/jobs/{job_id}").json()["job"]["execution"]["status"] == "running"
    finally:
        release.set()
    wait_step(client, job_id, "split")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = client.get(f"/api/studio/jobs/{job_id}").json()["job"]
        if state["execution"]["status"] == "complete":
            break
        time.sleep(0.01)
    with FileLock(str(studio.lock_path(config)), timeout=0):
        pass


def test_force_validation_artifact_archive_and_paths(studio_client):
    import io
    import zipfile

    _, client = studio_client
    job_id = create_legacy_job(client, "这是第一句配音文本。这是第二句配音文本。")
    client.post(f"/api/studio/jobs/{job_id}/steps/split")
    detail = wait_step(client, job_id, "split")
    shot = detail["storyboard"]["shots"][0]
    for body in [
        {"mode": "force", "shot_ids": []},
        {"mode": "resume", "shot_ids": [shot["id"]]},
        {"mode": "force", "shot_ids": [shot["id"], shot["id"]]},
        {"mode": "force", "shot_ids": ["missing"]},
    ]:
        assert client.post(f"/api/studio/jobs/{job_id}/steps/tts", json=body).status_code == 422
    assert (
        client.post(
            f"/api/studio/jobs/{job_id}/steps/split", json={"mode": "force", "shot_ids": [shot["id"]]}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/studio/jobs/{job_id}/storyboard", json={"shots": [{**shot, "id": "../../escaped"}]}
        ).status_code
        == 422
    )
    assert (
        client.get(f"/api/studio/jobs/{job_id}/artifacts/%2e%2e/%2e%2e/metadata.sqlite3").status_code == 404
    )
    listing = client.get(f"/api/studio/jobs/{job_id}/artifacts").json()["items"]
    assert any(item["attempt"] and item["path"].endswith("manifest.json") for item in listing)
    response = client.get(f"/api/studio/jobs/{job_id}/archive")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        assert "job.json" in bundle.namelist()
        assert "storyboard.json" in bundle.namelist()
        assert not any(path.startswith("exports/") for path in bundle.namelist())


def test_thread_start_failure_is_terminal_and_unlocks(config, monkeypatch):
    import threading

    from filelock import FileLock

    from mindex.studio import studio

    client = TestClient(create_app(config, storyboarder=FixtureStoryboarder(config)))
    client.config = config
    job_id = create_legacy_job(client, "线程启动失败测试。")
    original = threading.Thread.start

    def start(thread):
        if thread.name.startswith("studio-"):
            raise RuntimeError("无法创建工作线程")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    with pytest.raises(RuntimeError, match="工作线程"):
        client.post(f"/api/studio/jobs/{job_id}/steps/split")
    state = studio.load_job(config, job_id)
    assert state["steps"]["split"]["status"] == "failed"
    assert state["execution"]["status"] == "failed"
    with FileLock(str(studio.lock_path(config)), timeout=0):
        pass


def test_preview_manual_interval_and_hidden_context(studio_client):
    from mindex.shared.storage import Store
    from mindex.studio import studio_steps

    config, client = studio_client
    job_id = create_legacy_job(client, "这是需要寻找红色画面的第一句文案。")
    for step in ("split", "recall", "tts", "select"):
        assert client.post(f"/api/studio/jobs/{job_id}/steps/{step}").status_code == 202
        assert wait_step(client, job_id, step)["job"]["steps"][step]["status"] == "complete"
    detail = client.get(f"/api/studio/jobs/{job_id}").json()
    preview = client.get(detail["shots"][0]["selection"]["preview_url"])
    assert preview.status_code == 200 and preview.headers["content-type"] == "video/mp4"
    store = Store(config.data_dir)
    source_id = detail["timeline"]["events"][0]["video"]["source_id"]
    shots = store.rows(
        "SELECT id,start,end FROM segments WHERE source_id=? AND kind='shot' ORDER BY start", (source_id,)
    )
    assert len(shots) >= 3
    assert client.patch(f"/api/admin/segments/{shots[1]['id']}", json={"hidden": True}).status_code == 200
    _, extend_next, _, _ = studio_steps._selection_callbacks(store)
    assert extend_next(source_id, shots[0]["end"]) is None  # 不跨过隐藏邻镜继续找后续镜头
    shot_id = detail["timeline"]["events"][0]["shot_id"]
    base = {"source_id": source_id, "start": shots[0]["start"], "end": shots[0]["end"]}
    response = client.patch(
        f"/api/studio/jobs/{job_id}/timeline", json={"events": [{"shot_id": shot_id, "video": base}]}
    )
    assert response.status_code == 200
    for video in [
        {**base, "start": 10, "end": 11},
        {**base, "end": 0.1, "start": 0.5},
        {**base, "end": shots[1]["end"]},
    ]:
        assert (
            client.patch(
                f"/api/studio/jobs/{job_id}/timeline", json={"events": [{"shot_id": shot_id, "video": video}]}
            ).status_code
            == 422
        )
