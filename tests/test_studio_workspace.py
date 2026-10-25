from __future__ import annotations

import copy
import json
import time
import wave

import pytest
from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, convert_to_legacy, legacy_job
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.shared.media import command
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio import workspace as ws

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


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


def split_node(data, pid, force=True):
    """The split node's inputs are the plan's current script version."""
    return {
        "revision": data["revision"],
        "inputs": {"script": data["plans"][pid]["script"]},
        "stage": "split",
        "force": force,
    }


def setup_workspace(config):
    job_id = legacy_job(config, "红色开场。蓝色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(
        config,
        job_id,
        w,
        p,
        [
            studio.Shot(id="a", text="红色开场。", query="红色", estimated_seconds=1),
            studio.Shot(id="b", text="蓝色结尾。", query="蓝色", estimated_seconds=1),
        ],
    )
    p["board_script"] = p["script"]  # 分镜由当前文案拆出：不是过期状态
    ws.persist(config, job_id, w)
    return job_id, w, p


def build(config, job_id, w, p, embedder, *, speaker=None, force=False):
    checked = ws.preflight(config, job_id, w, p, force=force)
    bid = str(len(w["builds"]))
    w["builds"].append(
        {
            "id": bid,
            "plan_id": p["id"],
            "status": "running",
            "force": force,
            "nodes": [{**n, "status": "pending"} for n in checked["nodes"]],
        }
    )
    ws.persist(config, job_id, w)
    ws.run_build(config, job_id, bid, speaker=speaker or FixtureSpeaker(config), embedder=embedder)
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


def test_failed_voice_preserves_every_selected_artifact_and_final(prepared):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    w, p = build(config, jid, w, p, embedder)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    prior = copy.deepcopy(p)
    final = ws.version(w, p["final"])
    original = ws.file_path(config, jid, final, "mp4").read_bytes()

    class Broken:
        def speak(self, *_):
            raise RuntimeError("TTS unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        ws.execute(config, jid, w, p, "tts", "a", speaker=Broken())
    assert p == prior
    failed = list(w["versions"].values())[-1]
    assert failed["stage"] == "tts" and failed["status"] == "failed"
    assert failed["inputs"]["content"] == p["shots"]["a"]["content"]
    assert ws.file_path(config, jid, final, "mp4").read_bytes() == original
    with pytest.raises(studio.StudioError, match="成功版本"):
        ws.choose(w, p, "tts", "a", failed["id"])
    for sid in ws.ordered_shots(w, p):
        for stage in ws.STAGES:
            assert ws.valid_files(config, jid, ws.version(w, p["shots"][sid][stage]))


def test_query_text_compatibility_and_plan_structure_isolation(prepared):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    p["options"].update(voice_mode="custom", voice=config.studio.tts.voice)
    w, p = build(config, jid, w, p, embedder)
    config.studio.tts.voice = "另一个全局默认声音"
    config.studio.recall.limit = 7
    config.studio.audio.mode = "ducking"
    config.studio.audio.duck_gain = 0.3
    config.studio.subtitle.burn = True
    assert all(n["action"] == "reuse" for n in ws.preflight(config, jid, w, p)["nodes"])
    other = copy.deepcopy(p)
    other.update(id="other", name="other")
    w["plans"]["other"] = other
    old = copy.deepcopy(p)
    shots = [
        studio.Shot.model_validate(ws.version(w, p["shots"][sid]["content"])["data"])
        for sid in ws.ordered_shots(w, p)
    ]
    shots[0].query = "绿色"
    ws.set_board(config, jid, w, p, shots)
    assert not ws.conflicts(config, jid, w, p, "tts", "a", p["shots"]["a"]["tts"])
    assert ws.conflicts(config, jid, w, p, "recall", "a", p["shots"]["a"]["recall"])
    assert p["shots"]["a"]["render"] == old["shots"]["a"]["render"]
    checked = ws.preflight(config, jid, w, p)
    actions = {n["key"]: n["action"] for n in checked["nodes"]}
    assert actions["tts:a"] == "reuse" and actions["recall:a"] == "rebuild"
    assert actions["render:b"] == "reuse"
    shots[0].text = "改写的旁白。"
    ws.set_board(config, jid, w, p, shots[:1])
    assert ws.conflicts(config, jid, w, p, "tts", "a", p["shots"]["a"]["tts"])
    assert ws.ordered_shots(w, other) == ["a", "b"]
    assert other["board"] == old["board"]
    restored = ws.restore(w, old["final"], "从成片恢复")
    assert restored["shots"] == old["shots"] and restored["board"] == old["board"]
    assert restored["id"] != old["id"]


def test_partial_failure_continues_and_retry_reuses(prepared):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)

    class Partial(FixtureSpeaker):
        def speak(self, text, voice=None):
            if text.startswith("红"):
                raise RuntimeError("first shot fails")
            return super().speak(text, voice)

    w, p = build(config, jid, w, p, embedder, speaker=Partial(config))
    assert w["builds"][-1]["status"] == "partial"
    nodes = {n["key"]: n for n in w["builds"][-1]["nodes"]}
    assert nodes["tts:a"]["status"] == "failed"
    assert ws.version(w, nodes["tts:a"]["version"])["status"] == "failed"
    assert nodes["render:a"]["status"] == "blocked"
    assert nodes["render:b"]["status"] == "complete"
    assert nodes["compose"]["status"] == "blocked"
    assert p["final"] is None
    before = p["shots"]["b"].copy()
    speaker = FixtureSpeaker(config)
    w, p = build(config, jid, w, p, embedder, speaker=speaker)
    assert w["builds"][-1]["status"] == "complete"
    assert speaker.calls == 1
    assert p["shots"]["b"] == before


def test_mix_versions_composition_graph_samples_and_full_decode(prepared):
    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    w, p = build(config, jid, w, p, embedder)
    old = copy.deepcopy(p)
    ws.execute(config, jid, w, p, "tts", "a", speaker=FixtureSpeaker(config))
    assert ws.conflicts(config, jid, w, p, "render", "a", p["shots"]["a"]["render"])
    ws.execute(config, jid, w, p, "select", "a")
    ws.execute(config, jid, w, p, "render", "a")
    ws.execute(config, jid, w, p, "compose")
    assert p["shots"]["b"] == old["shots"]["b"]
    result = ws.version(w, p["final"])
    data = result["data"]
    assert data["graph"][p["shots"]["a"]["render"]]["inputs"]["tts"] == p["shots"]["a"]["tts"]
    assert data["graph"][p["shots"]["b"]["render"]]["inputs"]["tts"] == old["shots"]["b"]["tts"]
    assert data["boundaries"][0]["end_sample"] == data["boundaries"][1]["start_sample"]
    with wave.open(
        str(ws.file_path(config, jid, ws.version(w, p["shots"]["a"]["render"]), "audio")), "rb"
    ) as handle:
        assert handle.getframerate() == 48000
    path = ws.file_path(config, jid, result, "mp4")
    raw = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))
    video = next(s for s in raw["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == data["total_frames"]
    assert abs(float(video["duration"]) - data["total_seconds"]) < 0.002
    srt = ws.file_path(config, jid, result, "srt").read_text()
    assert "红色开场。" in srt and "蓝色结尾。" in srt
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ]
    )
    for node in data["graph"].values():
        assert ws.valid_files(config, jid, node)


def test_manual_contiguous_range_hidden_restrictions_and_realignment(prepared):
    config, _embedder = prepared
    jid, w, p = setup_workspace(config)
    ws.execute(config, jid, w, p, "tts", "a", speaker=FixtureSpeaker(config))
    store = Store(config.data_dir)
    source = store.one("SELECT id FROM sources")["id"]
    shots = ws.source_shots(config, source)
    assert len(shots) >= 3
    good = {
        "source_id": source,
        "start": shots[0]["start"],
        "end": shots[1]["end"],
        "segment_ids": [r["id"] for r in shots[:2]],
    }
    ws.validate_range(config, good)
    bad = {**good, "end": shots[2]["end"], "segment_ids": [shots[0]["id"], shots[2]["id"]]}
    with pytest.raises(studio.StudioError, match="连续"):
        ws.validate_range(config, bad)
    p["ranges"]["a"] = good
    ws.execute(config, jid, w, p, "select", "a")
    data = ws.version(w, p["shots"]["a"]["select"])["data"]
    assert data["range"] == good
    assert data["actual_range"]["end"] < good["end"]
    ws.execute(config, jid, w, p, "tts", "a", speaker=FixtureSpeaker(config))
    ws.execute(config, jid, w, p, "select", "a")
    assert ws.version(w, p["shots"]["a"]["select"])["data"]["range"] == good
    store.execute("INSERT INTO segment_curation(id,hidden) VALUES(?,1)", (shots[1]["id"],))
    with pytest.raises(studio.StudioError, match="隐藏"):
        ws.validate_range(config, good)


def wait_build(client, jid):
    for _ in range(1000):
        response = client.get(f"/api/studio/jobs/{jid}/workspace")
        assert response.status_code == 200, response.text
        data = response.json()
        if data["builds"] and data["builds"][-1]["status"] != "running":
            return data
        time.sleep(0.01)
    raise AssertionError("build timeout")


def test_workspace_reports_retrieval_combination_dependency(prepared):
    """分镜表上方的检索组合：生效组合与各分镜选用召回版本记录的组合一致 / 不一致。"""
    config, embedder = prepared
    client = TestClient(
        create_app(config, embedder, speaker=FixtureSpeaker(config), storyboarder=FixtureStoryboarder(config))
    )
    jid = convert_to_legacy(
        config,
        client.post("/api/studio/jobs", json={"text": "红色开场。", "options": {"burn": False}}).json()["id"],
    )["id"]
    base = f"/api/studio/jobs/{jid}"
    data = client.get(f"{base}/workspace").json()
    pid = data["active_plan"]
    assert client.post(f"{base}/plans/{pid}/nodes", json=split_node(data, pid)).status_code == 202
    wait_build(client, jid)
    checked = client.get(f"{base}/plans/{pid}/preflight").json()
    assert client.post(f"{base}/plans/{pid}/build", json={"token": checked["token"]}).status_code == 202
    data = wait_build(client, jid)

    shots = data["plans"][pid]["shots"]
    recalled = [s["recall"] for s in shots.values() if s.get("recall")]
    assert recalled
    parameters = data["versions"][recalled[0]]["parameters"]
    retrieval = data["retrieval"]
    assert retrieval["active"]["content_id"] == parameters["index_id"]
    assert retrieval["active"]["release_id"] == parameters["person_release"]
    assert [(g["content_id"], g["release_id"], g["shots"], g["current"]) for g in retrieval["used"]] == [
        (parameters["index_id"], parameters["person_release"], len(recalled), True)
    ]
    # 成片合成版本沿 渲染 → 选片 → 召回 依赖链用到的组合。
    composes = [vid for vid, item in data["versions"].items() if item["stage"] == "compose"]
    assert composes and set(retrieval["compose"]) == set(composes)
    assert [(g["content_id"], g["release_id"], g["shots"], g["current"]) for g in retrieval["compose"][composes[0]]] == [
        (parameters["index_id"], parameters["person_release"], len(recalled), True)
    ]

    # 激活另一个内容索引后，生效组合改变，已有召回仍显示它们生成时依赖的组合。
    store = Store(config.data_dir)
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('other-index','{}','ready')")
    store.activate("other-index")
    time.sleep(1.1)  # 详情读取 active_index 有 1 秒展示缓存
    changed = client.get(f"{base}/workspace").json()["retrieval"]
    assert changed["active"]["content_id"] == "other-index"
    assert changed["active"]["release_id"] != parameters["person_release"]
    assert [(g["content_id"], g["current"]) for g in changed["used"]] == [(parameters["index_id"], False)]
    assert [(g["content_id"], g["current"]) for g in changed["compose"][composes[0]]] == [(parameters["index_id"], False)]
    assert client.get(f"{base}/workspace?compact=true").json()["retrieval"] == changed


def test_plan_options_patch_applies_to_task_recall(prepared):
    """方案参数 PATCH 后直接作用于本任务：旧召回版本标记不一致，重跑按新召回数量执行。"""
    config, embedder = prepared
    client = TestClient(
        create_app(config, embedder, speaker=FixtureSpeaker(config), storyboarder=FixtureStoryboarder(config))
    )
    jid = convert_to_legacy(
        config,
        client.post("/api/studio/jobs", json={"text": "红色开场。", "options": {"burn": False}}).json()["id"],
    )["id"]
    base = f"/api/studio/jobs/{jid}"
    data = client.get(f"{base}/workspace").json()
    pid = data["active_plan"]
    assert (
        client.post(
            f"{base}/plans/{pid}/nodes",
            json=split_node(data, pid),
        ).status_code
        == 202
    )
    data = wait_build(client, jid)
    checked = client.get(f"{base}/plans/{pid}/preflight").json()
    assert client.post(f"{base}/plans/{pid}/build", json={"token": checked["token"]}).status_code == 202
    data = wait_build(client, jid)
    sid = next(iter(data["plans"][pid]["shots"]))
    version = data["plans"][pid]["shots"][sid]["recall"]
    assert data["versions"][version]["parameters"]["limit"] == 20
    patched = client.patch(
        f"{base}/plans/{pid}",
        json={
            "revision": data["revision"],
            "options": {**data["plans"][pid]["options"], "recall_limit": 5},
        },
    )
    assert patched.status_code == 200, patched.text
    detail = patched.json()
    assert detail["plans"][pid]["options"]["recall_limit"] == 5
    assert detail["conflicts"][f"recall:{sid}"], "旧召回版本应标记与当前方案参数不一致"
    checked = client.get(f"{base}/plans/{pid}/preflight").json()
    assert client.post(f"{base}/plans/{pid}/build", json={"token": checked["token"]}).status_code == 202
    data = wait_build(client, jid)
    version = data["plans"][pid]["shots"][sid]["recall"]
    assert data["versions"][version]["parameters"]["limit"] == 5


def test_workspace_api_title_plans_preview_confirmation_and_stale_binding(prepared):
    config, embedder = prepared
    client = TestClient(
        create_app(config, embedder, speaker=FixtureSpeaker(config), storyboarder=FixtureStoryboarder(config))
    )
    jid = convert_to_legacy(
        config,
        client.post("/api/studio/jobs", json={"text": "红色开场。", "options": {"burn": False}}).json()["id"],
    )["id"]
    base = f"/api/studio/jobs/{jid}"
    data = client.get(f"{base}/workspace").json()
    pid = data["active_plan"]
    assert (
        client.post(
            f"{base}/plans/{pid}/nodes",
            json=split_node(data, pid),
        ).status_code
        == 202
    )
    data = wait_build(client, jid)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    before = data["revision"]
    assert client.patch(f"{base}/title", json={"title": "新标题"}).status_code == 200
    data = client.get(f"{base}/workspace").json()
    assert data["revision"] == before
    checked = client.get(f"{base}/plans/{pid}/preflight").json()
    assert client.post(f"{base}/plans/{pid}/build", json={"token": checked["token"]}).status_code == 202
    data = wait_build(client, jid)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    assert client.post(f"{base}/plans/{pid}/build", json={"token": checked["token"]}).status_code == 422
    p = data["plans"][pid]
    sid = next(iter(p["shots"]))
    assert (
        client.post(
            f"{base}/plans/{pid}/nodes",
            json={
                "revision": data["revision"],
                "stage": "render",
                "shot_id": sid,
                "inputs": {},
                "force": True,
            },
        ).status_code
        == 422
    )
    source = Store(config.data_dir).one("SELECT id FROM sources")["id"]
    shots = client.get(f"/api/studio/sources/{source}/shots").json()["items"]
    body = {
        "revision": data["revision"],
        "shot_id": sid,
        "source_id": source,
        "start": shots[0]["start"],
        "end": shots[0]["end"],
        "segment_ids": [shots[0]["id"]],
    }
    assert client.post(f"{base}/plans/{pid}/range-confirm", json=body).status_code == 422
    preview = client.post(f"{base}/plans/{pid}/range-preview", json=body)
    assert preview.status_code == 200, preview.text
    assert client.get(preview.json()["preview_url"]).status_code == 200
    body["preview_token"] = preview.json()["preview_token"]
    response = client.post(f"{base}/plans/{pid}/range-confirm", json=body)
    assert response.status_code == 200, response.text
    copied = client.post(f"{base}/plans", json={"name": "副本", "copy_from": pid}).json()
    assert copied["plans"][copied["active_plan"]]["shots"] == response.json()["plans"][pid]["shots"]
    final = p["final"]
    deps = client.get(f"{base}/versions/{final}/dependencies").json()
    assert deps["root"] == final and deps["nodes"][final]["inputs"]["board"] == p["board"]
    artifact = data["versions"][final]["files"]["mp4"]["url"]
    assert client.get(artifact, headers={"Range": "bytes=0-127"}).status_code == 206
    download = client.get(f"{artifact}?download=true")
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    import hashlib

    assert hashlib.sha256(download.content).hexdigest() == data["versions"][final]["files"]["mp4"]["sha256"]


def test_migration_snapshots_and_unknown_dependencies_are_honest(config):
    jid = legacy_job(config, "历史文案。")["id"]
    studio.write_storyboard(
        config,
        jid,
        studio.Storyboard(
            job_id=jid, shots=[studio.Shot(id="a", text="历史文案。", query="历史", estimated_seconds=1)]
        ),
    )
    target = studio.audio_dir(config, jid) / "a.wav"
    studio.save_bytes(target, FixtureSpeaker(config).speak("历史文案。"))
    original = studio.job_path(config, jid).read_bytes()
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    assert (ws.root(config, jid) / "migration-v2/job.json").read_bytes() == original
    audio = ws.version(w, p["shots"]["a"]["tts"])
    assert audio["incomplete"] and audio["inputs"] == {} and audio["signature"] is None
    assert ws.file_path(config, jid, audio, "audio").read_bytes() == target.read_bytes()
    assert ws.load(config, jid) == w


def test_legacy_render_versions_are_imported_without_inventing_edges(prepared):
    from mindex.studio import studio_steps

    config, embedder = prepared
    jid = legacy_job(config, "历史红色文案。", options={"burn": False})["id"]
    for step in studio.STEPS:
        studio_steps.run_step(
            config,
            jid,
            step,
            storyboarder=FixtureStoryboarder(config),
            speaker=FixtureSpeaker(config),
            embedder=embedder,
        )
    original = studio.job_path(config, jid).read_bytes()
    w = ws.load(config, jid)
    p = ws.plan(w, w["active_plan"])
    assert (ws.root(config, jid) / "migration-v2/job.json").read_bytes() == original
    sid = ws.ordered_shots(w, p)[0]
    for stage in ws.STAGES:
        item = ws.version(w, p["shots"][sid][stage])
        assert item["incomplete"] and item["inputs"] == {}
    final = ws.version(w, p["final"])
    assert ws.valid_files(config, jid, final)
    assert final["incomplete"]
    with pytest.raises(studio.StudioError, match="记录不完整"):
        ws.restore(w, p["final"], "未知历史")


def test_api_execution_lock_and_recovery(config):
    import threading
    from threading import Event

    from filelock import FileLock

    entered, release = Event(), Event()

    class Slow(FixtureStoryboarder):
        def split(self, text, vocabulary=None):
            entered.set()
            release.wait(10)
            return super().split(text, vocabulary)

    client = TestClient(create_app(config, storyboarder=Slow(config)))
    jid = convert_to_legacy(
        config, client.post("/api/studio/jobs", json={"text": "服务中断测试文案。"}).json()["id"]
    )["id"]
    base = f"/api/studio/jobs/{jid}"
    data = client.get(f"{base}/workspace").json()
    pid = data["active_plan"]
    body = split_node(data, pid)
    try:
        assert client.post(f"{base}/plans/{pid}/nodes", json=body).status_code == 202
        assert entered.wait(3)
        assert client.post(f"{base}/plans/{pid}/nodes", json=body).status_code == 409
        assert client.post(f"{base}/plans", json={"name": "busy"}).status_code == 409
        assert client.get(f"{base}/workspace").json()["builds"][-1]["status"] == "running"
    finally:
        release.set()
    assert wait_build(client, jid)["builds"][-1]["status"] == "complete"
    w = ws.load(config, jid)
    w["builds"][-1]["status"] = "running"
    w["builds"][-1]["nodes"][0]["status"] = "running"
    orphan_id = "e" * 32
    orphan = ws.root(config, jid) / "versions" / orphan_id
    p = ws.plan(w, pid)
    ws.VersionDiagnostics(orphan).record(
        "inputs", {"stage": "split", "shot_id": None, **ws.specification(config, jid, w, p, "split")}
    )
    ws.persist(config, jid, w)
    recovered = client.get(f"{base}/workspace").json()
    assert recovered["builds"][-1]["status"] == "partial"
    assert recovered["builds"][-1]["nodes"][0]["status"] == "blocked"
    assert recovered["versions"][orphan_id]["status"] == "failed"
    assert recovered["plans"][pid] == p
    with FileLock(str(studio.lock_path(config)), timeout=0):
        pass
    assert client.post(f"{base}/steps/split").status_code == 409
    # Verify failure to start a worker also leaves a terminal build and releases the lock.
    original = threading.Thread.start

    def start(thread):
        if thread.name.startswith("studio-build-"):
            raise RuntimeError("worker startup failed")
        return original(thread)

    from unittest.mock import patch

    fresh = client.get(f"{base}/workspace").json()
    body["revision"] = fresh["revision"]
    with patch.object(threading.Thread, "start", start), pytest.raises(RuntimeError, match="startup"):
        client.post(f"{base}/plans/{pid}/nodes", json=body)
    assert client.get(f"{base}/workspace").json()["builds"][-1]["status"] == "failed"
    with FileLock(str(studio.lock_path(config)), timeout=0):
        pass


@pytest.mark.parametrize("burn", [False, True])
def test_mixed_render_picture_audio_and_subtitle_match_bound_versions(prepared, burn):
    import io
    import math
    import struct

    config, embedder = prepared
    jid, w, p = setup_workspace(config)
    p["options"]["burn"] = burn
    w, p = build(config, jid, w, p, embedder)
    old_b = p["shots"]["b"].copy()

    class DifferentVoice:
        def speak(self, text, voice=None):
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(16000)
                handle.writeframes(
                    b"".join(
                        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 880 * i / 16000)))
                        for i in range(8000)
                    )
                )
            return buffer.getvalue()

    ws.execute(config, jid, w, p, "tts", "a", speaker=DifferentVoice())
    store = Store(config.data_dir)
    source = store.one("SELECT id FROM sources")["id"]
    shots = ws.source_shots(config, source)
    # The new first shot uses blue; the second shot and its original 440Hz voice are retained.
    p["ranges"]["a"] = {
        "source_id": source,
        "start": shots[1]["start"],
        "end": shots[1]["end"],
        "segment_ids": [shots[1]["id"]],
    }
    ws.execute(config, jid, w, p, "select", "a")
    ws.execute(config, jid, w, p, "render", "a")
    ws.execute(config, jid, w, p, "compose")
    assert p["shots"]["b"] == old_b
    final = ws.version(w, p["final"])
    video = ws.file_path(config, jid, final, "mp4")
    rgb = command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            "0.2",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    assert rgb[2] > rgb[0] + 100 and rgb[2] > rgb[1] + 100
    import numpy as np

    for index, frequency in enumerate((880, 440)):
        boundary = final["data"]["boundaries"][index]
        raw = command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(boundary["start"] + 0.1),
                "-i",
                str(video),
                "-t",
                "0.2",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "s16le",
                "-",
            ]
        )
        samples = np.frombuffer(raw, dtype="<i2").astype(float)
        frequencies = np.fft.rfftfreq(len(samples), 1 / 48000)
        peak = frequencies[np.abs(np.fft.rfft(samples)).argmax()]
        assert abs(peak - frequency) < 15
    srt = ws.file_path(config, jid, final, "srt").read_text()
    assert srt.index("红色开场。") < srt.index("蓝色结尾。")
    assert all(ws.valid_files(config, jid, node) for node in final["data"]["graph"].values())
    # A query-only edit does not change manual ranges, voice or rendered subtitles.
    board = [
        studio.Shot.model_validate(ws.version(w, p["shots"][sid]["content"])["data"])
        for sid in ws.ordered_shots(w, p)
    ]
    board[0].query = "只改查询，保留人工范围"
    ws.set_board(config, jid, w, p, board)
    checked = ws.preflight(config, jid, w, p)
    assert next(n for n in checked["nodes"] if n["key"] == "compose")["action"] == "reuse"


@pytest.mark.parametrize("legacy", [False, True])
def test_build_without_storyboard_runs_full_pipeline(prepared, legacy):
    """新任务从生成文案开始；老任务已有文案（input_text 迁移而来），直接从拆分镜开始。"""
    config, embedder = prepared
    client = TestClient(
        create_app(config, embedder, speaker=FixtureSpeaker(config), storyboarder=FixtureStoryboarder(config))
    )
    jid = client.post("/api/studio/jobs", json={"text": "红色开场。蓝色结尾。", "options": {"burn": False}}).json()["id"]
    if legacy:
        convert_to_legacy(config, jid)
    base = f"/api/studio/jobs/{jid}"
    data = client.get(f"{base}/workspace").json()
    pid = data["active_plan"]
    assert not data["plans"][pid]["board"]
    assert bool(data["plans"][pid]["script"]) is legacy
    checked = client.get(f"{base}/plans/{pid}/preflight").json()
    first = "split" if legacy else "script"
    assert checked["nodes"][0]["stage"] == first
    assert all(n["action"] != "blocked" for n in checked["nodes"])
    response = client.post(f"{base}/plans/{pid}/build", json={"token": checked["token"]})
    assert response.status_code == 202, response.text
    data = wait_build(client, jid)
    build = data["builds"][-1]
    assert build["status"] == "complete", build
    stages = [n["stage"] for n in build["nodes"]]
    expected = ["split", "recall", "tts", "select", "render", "compose"]
    assert list(dict.fromkeys(stages)) == (expected if legacy else ["script", *expected])
    assert all(n["status"] in ("complete", "reused") for n in build["nodes"])
    assert data["plans"][pid]["final"]
