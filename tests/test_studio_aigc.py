"""AIGC 视频来源：generate 阶段（提交、轮询、取消、留档、签名、时长取整）、select 冻结帧兜底与预检阻塞。

视频生成服务一律用确定性的 FakeVideoGenerator（本地 ffmpeg 生成纯色片段），绝不调用真实付费接口。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from conftest import FixtureSpeaker
from fastapi.testclient import TestClient
from test_studio_sources import frame_count, make_clip, make_image, setup, store_input

from mindex.api import create_app
from mindex.models.video_generators import VideoStatus, allowed_seconds
from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.workspace.handlers import generate

slow = pytest.mark.slow
SECRET = "sk-video-secret-1234567890"


class FakeVideoGenerator:
    """Tests only: 按请求时长生成纯色 mp4；states 依次作为每次轮询的状态（末项重复）。"""

    def __init__(self, *, states=("queued", "running", "succeeded"), seconds=None, error=None, on_poll=None):
        self.states, self.seconds, self.error, self.on_poll = states, seconds, error, on_poll
        self.requests, self.polls, self.cancelled, self.current = [], 0, [], 0

    def submit(self, request):
        self.requests.append(request)
        self.current = 0
        return f"task-{len(self.requests)}"

    def poll(self, task_id):
        state = self.states[min(self.current, len(self.states) - 1)]
        self.current += 1
        self.polls += 1
        if self.on_poll:
            self.on_poll(self)
        return VideoStatus(
            state=state,
            url="https://cdn.example.invalid/out.mp4?Signature=abc" if state == "succeeded" else None,
            error=self.error if state == "failed" else None,
            raw={"task_id": task_id, "status": state},
        )

    def fetch(self, status, target: Path):
        seconds = self.seconds or self.requests[-1].seconds
        clip = make_clip(target.with_name("fake-source.mp4"), seconds, audio=False, color="blue")
        clip.replace(target)

    def cancel(self, task_id):
        self.cancelled.append(task_id)

    def health(self):
        return {"ready": True}

    def allowed_seconds(self, seconds):
        return allowed_seconds([1.0, 2.0, 3.0], seconds)


@pytest.fixture
def aigc(config):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 25
    generator = config.studio.video_generator
    generator.enabled = True
    generator.backend = "minimax"
    generator.model = "fake-hailuo"
    generator.poll_interval = 0.01
    generator.max_wait = 30
    generator.durations = [1.0, 2.0, 3.0]
    generator.api_key = SECRET
    return config


def shot(sid, text, **extra):
    return studio.Shot(id=sid, text=text, query="夜景城市", estimated_seconds=1, **extra)


def aigc_shot(sid="a", text="红色开场。", **source):
    return shot(sid, text, visual="霓虹灯下的街道", source={"kind": "aigc", **source})


def run(config, job_id, w, p, generator, *, force=False, cancel_event=None):
    checked = ws.preflight(config, job_id, w, p, force=force)
    bid = str(len(w["builds"]))
    w["builds"].append({"id": bid, "plan_id": p["id"], "status": "running", "force": force,
                        "nodes": [{**n, "status": "pending"} for n in checked["nodes"]]})
    ws.persist(config, job_id, w)
    ws.run_build(config, job_id, bid, speaker=FixtureSpeaker(config), video_generator=generator,
                 cancel_event=cancel_event)
    fresh = ws.load(config, job_id)
    return fresh, ws.plan(fresh, p["id"])


def nodes_of(w):
    return {n["key"]: n for n in w["builds"][-1]["nodes"]}


def job_text(config, job_id) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in studio.job_dir(config, job_id).rglob("*") if path.is_file() and path.suffix in (".json", ".jsonl")
    )


# ---------------------------------------------------------------- 模型与预检（快速）


def test_aigc_source_model_and_stage_sequence():
    value = aigc_shot(prompt="  ", negative="  模糊 ", seed=7).model_dump()["source"]
    assert value == {"kind": "aigc", "prompt": None, "negative": "模糊", "image_id": None, "seed": 7}
    assert ws.SHOT_SOURCES["aigc"] == ("tts", "generate", "select", "render")
    assert allowed_seconds([5.0, 10.0], 0.4) == 5.0
    assert allowed_seconds([5.0, 10.0], 6.2) == 10.0
    assert allowed_seconds([5.0, 10.0], 12.0) == 10.0


def test_preflight_blocks_when_generator_not_configured_or_reference_missing(aigc, tmp_path):
    job_id, w, p = setup(aigc, [aigc_shot()])
    nodes = {n["key"]: n for n in ws.preflight(aigc, job_id, w, p)["nodes"]}
    assert set(nodes) == {"tts:a", "generate:a", "select:a", "render:a", "compose"}
    assert nodes["generate:a"]["action"] == "rebuild"
    assert ws.node_dependencies(w, p, "generate", "a") == ["tts:a"]
    assert ws.node_dependencies(w, p, "select", "a") == ["tts:a", "generate:a"]

    aigc.studio.video_generator.enabled = False
    nodes = {n["key"]: n for n in ws.preflight(aigc, job_id, w, p)["nodes"]}
    assert nodes["generate:a"]["action"] == "blocked"
    assert nodes["generate:a"]["reason"] == "未配置视频生成服务，请在设置页激活视频生成档案"
    assert nodes["select:a"]["action"] == nodes["render:a"]["action"] == "blocked"
    assert nodes["tts:a"]["action"] == "rebuild"

    aigc.studio.video_generator.enabled = True
    aigc.studio.video_generator.backend = "comfyui"
    nodes = {n["key"]: n for n in ws.preflight(aigc, job_id, w, p)["nodes"]}
    assert "未导入工作流" in nodes["generate:a"]["reason"]

    aigc.studio.video_generator.backend = "minimax"
    image_id = store_input(aigc, job_id, make_image(tmp_path / "ref.png"))
    ws.set_board(aigc, job_id, w, p, [aigc_shot(image_id=image_id)])
    assert {n["key"]: n for n in ws.preflight(aigc, job_id, w, p)["nodes"]}["generate:a"]["action"] == "rebuild"
    for path in (studio.job_dir(aigc, job_id) / "inputs").glob(image_id + ".*"):
        path.unlink()
    nodes = {n["key"]: n for n in ws.preflight(aigc, job_id, w, p)["nodes"]}
    assert nodes["generate:a"]["reason"] == ws.sources.REFERENCE_MISSING


def test_patch_rejects_missing_reference_image(aigc):
    from mindex.studio.studio_models import validate_source_references

    job_id, _, _ = setup(aigc, [aigc_shot()])
    validate_source_references(aigc, job_id, [aigc_shot()])
    with pytest.raises(studio.StudioError, match="参考图片缺失"):
        validate_source_references(aigc, job_id, [aigc_shot(image_id="c" * 64)])


def test_signature_covers_prompt_model_seed_and_reference(aigc, tmp_path):
    job_id, w, p = setup(aigc, [aigc_shot()])
    base = ws.specification(aigc, job_id, w, p, "generate", "a")
    assert base["inputs"] == {"content": p["shots"]["a"]["content"], "tts": None}
    assert base["parameters"] == {"generator": {"backend": "minimax", "model": "fake-hailuo",
                                                "durations": [1.0, 2.0, 3.0]}}
    assert SECRET not in json.dumps(base)
    image_id = store_input(aigc, job_id, make_image(tmp_path / "ref.png"))
    signatures = {base["signature"]}
    for source in ({"prompt": "雨夜"}, {"seed": 3}, {"negative": "模糊"}, {"image_id": image_id}):
        ws.set_board(aigc, job_id, w, p, [aigc_shot(**source)])
        signatures.add(ws.specification(aigc, job_id, w, p, "generate", "a")["signature"])
    ws.set_board(aigc, job_id, w, p, [aigc_shot()])
    aigc.studio.video_generator.model = "other-model"
    signatures.add(ws.specification(aigc, job_id, w, p, "generate", "a")["signature"])
    assert len(signatures) == 6
    # 超时、轮询参数与密钥不进签名。
    aigc.studio.video_generator.model = "fake-hailuo"
    aigc.studio.video_generator.max_wait = 5
    aigc.studio.video_generator.api_key = "sk-other-key-000000"
    assert ws.specification(aigc, job_id, w, p, "generate", "a")["signature"] == base["signature"]


def test_studio_key_only_changes_when_video_generator_enabled(config):
    before = config.studio_key
    config.studio.video_generator.model = "x"
    assert config.studio_key == before
    config.studio.video_generator.enabled = True
    assert config.studio_key != before


def test_wait_for_backs_off_and_times_out():
    clock = {"now": 0.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    class Cfg:
        poll_interval, max_wait = 1.0, 6.0

    fake = FakeVideoGenerator(states=("running",))
    with pytest.raises(studio.StudioError, match="视频生成超时"):
        generate.wait_for(fake, "t", Cfg, lambda: False, lambda *_: None, clock=lambda: clock["now"], sleep=sleep)
    assert fake.cancelled == ["t"]
    assert clock["now"] == pytest.approx(6.0)
    assert fake.polls == 5  # 0, 1, 2.5, 4.75, 6（间隔 1 → 1.5 → 2.25 → 余下 1.25）


# ---------------------------------------------------------------- 构建（ffmpeg）


@slow
def test_aigc_build_without_index_reuses_generation_and_keeps_no_secret(aigc):
    job_id, w, p = setup(aigc, [aigc_shot()])
    fake = FakeVideoGenerator()
    w, p = run(aigc, job_id, w, p, fake)
    build_record = w["builds"][-1]
    assert build_record["status"] == "complete", build_record
    assert [n["key"] for n in build_record["nodes"]] == ["tts:a", "generate:a", "select:a", "render:a", "compose"]
    [request] = fake.requests
    assert request.prompt == "霓虹灯下的街道"  # 缺省用画面意象
    assert request.seconds == 1.0  # 配音 0.9 秒 → 允许时长 1 秒
    assert (request.width, request.height) == (160, 120)
    generated = ws.version(w, p["shots"]["a"]["generate"])
    assert generated["data"]["task_id"] == "task-1" and generated["data"]["polls"] == 3
    assert set(generated["files"]) == {"generated", "request"}
    record = json.loads(ws.file_path(aigc, job_id, generated, "request").read_text(encoding="utf-8"))
    assert record["task_id"] == "task-1" and record["status"]["state"] == "succeeded"
    assert record["request"]["prompt"] == "霓虹灯下的街道"
    selection = ws.version(w, p["shots"]["a"]["select"])
    assert selection["inputs"]["generate"] == generated["id"]
    assert selection["data"]["event"]["video"]["ref"] == {"version": generated["id"], "file": "generated"}
    final = ws.version(w, p["final"])
    assert frame_count(ws.file_path(aigc, job_id, final, "mp4"))[0] == final["data"]["total_frames"]
    text = job_text(aigc, job_id)
    assert SECRET not in text and "Signature=abc" not in text
    events = (studio.job_dir(aigc, job_id) / "builds" / w["builds"][-1]["id"] / "events.jsonl").read_text(
        encoding="utf-8")
    assert "视频生成：排队中" in events and "视频生成：生成中" in events
    detail = ws.detail(aigc, job_id, w)
    assert detail["shot_stages"] == {"a": ["tts", "generate", "select", "render"]}
    assert detail["conflicts"]["generate:a"] == []

    # 再次构建：签名一致，直接复用生成版本，不再调用服务。
    w, p = run(aigc, job_id, w, p, fake)
    assert nodes_of(w)["generate:a"]["status"] == "reused" and len(fake.requests) == 1
    # 改提示词：只重新生成该镜。
    ws.set_board(aigc, job_id, w, p, [aigc_shot(prompt="雨夜霓虹")])
    ws.persist(aigc, job_id, w)
    w, p = run(aigc, job_id, w, p, fake)
    assert nodes_of(w)["generate:a"]["status"] == "complete" and fake.requests[-1].prompt == "雨夜霓虹"
    assert nodes_of(w)["tts:a"]["status"] == "reused"


@slow
def test_long_narration_rounds_to_max_duration_and_freeze_pads(aigc):
    text = "这是一句比较长的旁白，用来测试生成片段不足时的冻结帧。"
    job_id, w, p = setup(aigc, [aigc_shot(text=text)])
    fake = FakeVideoGenerator()
    w, p = run(aigc, job_id, w, p, fake)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    assert fake.requests[0].seconds == 3.0  # 配音 3.5 秒 > 最大允许 3 秒
    selection = ws.version(w, p["shots"]["a"]["select"])["data"]
    assert selection["event"]["video"]["alignment"] == "padded" and selection["freeze_seconds"] > 0.3
    assert "生成画面" in selection["event"]["warning"] and "冻结末帧" in selection["event"]["warning"]
    render = ws.version(w, p["shots"]["a"]["render"])
    assert frame_count(ws.file_path(aigc, job_id, render, "video"))[0] == render["data"]["frames"]


@slow
def test_cancel_during_polling_marks_node_cancelled_and_blocks_rest(aigc):
    job_id, w, p = setup(aigc, [aigc_shot(), shot("b", "蓝色结尾。", source={"kind": "aigc"})])
    event = threading.Event()

    def cancel_after_two(fake):
        if fake.polls >= 2:
            event.set()

    fake = FakeVideoGenerator(states=("running",), on_poll=cancel_after_two)
    started = time.monotonic()
    w, p = run(aigc, job_id, w, p, fake, cancel_event=event)
    assert time.monotonic() - started < 20
    nodes = nodes_of(w)
    assert w["builds"][-1]["status"] == "cancelled"
    assert nodes["generate:a"]["status"] == "cancelled" and nodes["generate:a"]["error"] == "已取消"
    assert fake.cancelled == ["task-1"] and len(fake.requests) == 1
    for key in ("generate:b", "select:a", "render:a", "compose"):
        assert nodes[key]["status"] == "blocked" and nodes[key]["error"] == "已取消", key
    failed = ws.version(w, nodes["generate:a"]["version"])
    assert failed["status"] == "failed" and "request.json" in failed["files"]  # 失败版本按相对路径留档
    assert p["shots"]["a"].get("generate") is None


@slow
def test_generation_failure_keeps_diagnostics_and_never_selects(aigc):
    job_id, w, p = setup(aigc, [aigc_shot()])
    fake = FakeVideoGenerator(states=("queued", "failed"), error="MiniMax 内容审核未通过：提示词含敏感内容")
    w, p = run(aigc, job_id, w, p, fake)
    nodes = nodes_of(w)
    assert nodes["generate:a"]["status"] == "failed"
    assert "内容审核未通过" in nodes["generate:a"]["error"]
    assert nodes["select:a"]["status"] == "blocked" and nodes["compose"]["status"] == "blocked"
    failed = ws.version(w, nodes["generate:a"]["version"])
    record = json.loads((ws.root(aigc, job_id) / failed["files"]["request.json"]["path"]).read_text(encoding="utf-8"))
    assert record["status"]["state"] == "failed" and "内容审核" in record["error"]
    assert any(name.startswith("debug/") for name in failed["files"])
    assert p["shots"]["a"].get("generate") is None
    assert SECRET not in job_text(aigc, job_id)


@slow
def test_generation_timeout_fails_with_chinese_reason(aigc):
    aigc.studio.video_generator.max_wait = 0.1
    job_id, w, p = setup(aigc, [aigc_shot()])
    fake = FakeVideoGenerator(states=("running",))
    w, p = run(aigc, job_id, w, p, fake)
    node = nodes_of(w)["generate:a"]
    assert node["status"] == "failed" and node["error"].startswith("视频生成超时")
    assert fake.cancelled == ["task-1"]


@slow
def test_mixed_aigc_clip_recall_compose(aigc, tmp_path, video_file):
    from conftest import FixtureEmbedder

    from mindex.processing.indexing import IndexBuilder
    from mindex.processing.pipeline import Pipeline

    Pipeline(aigc).prepare(video_file)
    embedder = FixtureEmbedder(aigc)
    IndexBuilder(aigc, embedder).build(mode="visual", kinds=("shot",), activate=True)
    job_id, w, p = setup(aigc, [shot("r", "红色开场。")])
    clip_id = store_input(aigc, job_id, make_clip(tmp_path / "c.mp4", 2.0))
    ws.set_board(aigc, job_id, w, p, [
        studio.Shot(id="r", text="红色开场。", query="红色", estimated_seconds=1),
        aigc_shot("g", "生成的中段。"),
        shot("c", "片段结尾。", source={"kind": "clip", "media_id": clip_id}),
    ])
    checked = ws.preflight(aigc, job_id, w, p)
    bid = "mixed"
    w["builds"].append({"id": bid, "plan_id": p["id"], "status": "running", "force": False,
                        "nodes": [{**n, "status": "pending"} for n in checked["nodes"]]})
    ws.persist(aigc, job_id, w)
    ws.run_build(aigc, job_id, bid, speaker=FixtureSpeaker(aigc), embedder=embedder,
                 video_generator=FakeVideoGenerator())
    w = ws.load(aigc, job_id)
    p = ws.plan(w, p["id"])
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    final = ws.version(w, p["final"])
    frames = [ws.version(w, p["shots"][sid]["render"])["data"]["frames"] for sid in ("r", "g", "c")]
    assert final["data"]["total_frames"] == sum(frames)
    assert frame_count(ws.file_path(aigc, job_id, final, "mp4"))[0] == sum(frames)
    kinds = [ws.version(w, p["shots"][sid]["select"])["data"]["event"]["video"].get("kind", "library")
             for sid in ("r", "g", "c")]
    assert kinds == ["library", "file", "file"]
    assert ws.detail(aigc, job_id, w)["shot_stages"]["g"] == ["tts", "generate", "select", "render"]


@slow
def test_api_injects_video_generator_and_previews_generated_version(aigc):
    fake = FakeVideoGenerator()
    client = TestClient(create_app(aigc, speaker=FixtureSpeaker(aigc), video_generator=fake))
    job_id, _, p = setup(aigc, [aigc_shot()])
    base = f"/api/studio/jobs/{job_id}/plans/{p['id']}"
    checked = client.get(f"{base}/preflight").json()
    assert {n["key"] for n in checked["nodes"]} >= {"generate:a"}
    response = client.post(f"{base}/build", json={"token": checked["token"]})
    assert response.status_code == 202, response.text
    for _ in range(3000):
        data = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
        if data["builds"][-1]["status"] != "running":
            break
        time.sleep(0.01)
    assert data["builds"][-1]["status"] == "complete", data["builds"][-1]
    assert len(fake.requests) == 1
    vid = data["plans"][p["id"]]["shots"]["a"]["generate"]
    path = data["versions"][vid]["files"]["generated"]["path"]
    assert client.get(f"/api/studio/jobs/{job_id}/artifacts/{path}").status_code == 200
    assert data["shot_stages"]["a"] == ["tts", "generate", "select", "render"]
