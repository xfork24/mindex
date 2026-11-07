"""视频生成后端契约：MiniMax / 通义万相 / ComfyUI 的本地 HTTP 桩（httpx.MockTransport），从不访问真实服务。

覆盖成功路径、排队→生成→完成的轮询、失败、总超时、审核拒绝、鉴权与额度错误、ComfyUI 映射校验、
健康探测不提交生成，以及「视频生成」设置角色的档案往返、掩码与测试连接。
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import create_app
from models import adapters
from models.adapters import ModelError, make_video_generator
from models.video_generators import (
    ComfyUIVideoGenerator,
    DashScopeVideoGenerator,
    MiniMaxVideoGenerator,
    VideoContentRejected,
    VideoRequest,
)
from shared import trace
from shared.comfyui import validate_workflow
from shared.config import VideoGeneratorConfig
from studio import studio
from studio.workspace.handlers import generate

KEY = "sk-video-test-key-0123456789"
VIDEO = b"\x00\x00\x00\x18ftypmp42fake-video-bytes"


class Recorder:
    def __init__(self):
        self.items = []

    def record(self, kind, value):
        self.items.append((kind, value))


@pytest.fixture
def stub(monkeypatch):
    """把 adapters.request 用到的 httpx.request 接到 MockTransport；返回 (install, calls)。"""
    calls: list[httpx.Request] = []
    monkeypatch.setattr(adapters.time, "sleep", lambda _: None)

    def install(handler):
        def recorded(request):
            calls.append(request)
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(recorded))

        def fake(method, url, **kwargs):
            kwargs.pop("trust_env", None)
            return client.request(method, url, **kwargs)

        monkeypatch.setattr(httpx, "request", fake)
        return calls

    return install


@pytest.fixture
def audit():
    recorder = Recorder()
    token = trace.current.set(recorder)
    yield recorder
    trace.current.reset(token)


def configure(config, **values):
    profile = config.studio.video_generator
    for key, value in {"enabled": True, "api_key": KEY, "timeout": 5, **values}.items():
        setattr(profile, key, value)
    return config


def image_file(tmp_path):
    path = tmp_path / "ref.png"
    Image.new("RGB", (64, 48), (200, 0, 0)).save(path)
    return path


def body(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")


def assert_audit_clean(recorder):
    text = json.dumps(recorder.items, ensure_ascii=False, default=str)
    assert KEY not in text and "base64," not in text and "Authorization" not in text


# ---------------------------------------------------------------- MiniMax


def minimax_handler(states, *, submit=None, query_code=0):
    sequence = iter(states)

    def handler(request):
        path = request.url.path
        if path.endswith("/v1/video_generation"):
            return httpx.Response(200, json=submit or {"task_id": "mm-1", "base_resp": {"status_code": 0}})
        if path.endswith("/query/video_generation"):
            status = next(sequence)
            payload = {"task_id": "mm-1", "status": status, "base_resp": {"status_code": query_code}}
            if status == "Success":
                payload["file_id"] = "f-9"
            return httpx.Response(200, json=payload)
        if path.endswith("/files/retrieve"):
            return httpx.Response(
                200,
                json={
                    "file": {"download_url": "https://cdn.example/v.mp4?Expires=1&sig=s"},
                    "base_resp": {"status_code": 0},
                },
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=VIDEO)
        return httpx.Response(404)

    return handler


def test_minimax_success_polls_queue_then_downloads(config, stub, audit, tmp_path):
    configure(
        config, backend="minimax", base_url="https://api.minimax.example/v1", model="MiniMax-Hailuo-2.3"
    )
    calls = stub(minimax_handler(["Preparing", "Queueing", "Processing", "Success"]))
    generator = make_video_generator(config)
    assert isinstance(generator, MiniMaxVideoGenerator)
    task = generator.submit(
        VideoRequest(
            prompt="雨夜街道", negative_prompt="模糊", image=image_file(tmp_path), seconds=6.0, seed=3
        )
    )
    assert task == "mm-1"
    sent = body(calls[0])
    assert (
        calls[0].url.path == "/v1/video_generation" and calls[0].headers["authorization"] == f"Bearer {KEY}"
    )
    assert sent["model"] == "MiniMax-Hailuo-2.3" and sent["prompt"] == "雨夜街道" and sent["duration"] == 6
    assert sent["first_frame_image"].startswith("data:image/png;base64,")
    assert "negative_prompt" not in sent and "seed" not in sent  # 接口不支持，忽略
    states = [generator.poll(task) for _ in range(4)]
    assert [s.state for s in states] == ["queued", "queued", "running", "succeeded"]
    target = tmp_path / "out.bin"
    generator.fetch(states[-1], target)
    assert target.read_bytes() == VIDEO
    download = next(c for c in calls if c.url.host == "cdn.example")
    assert "authorization" not in download.headers
    assert parse_qs(urlsplit(str(calls[-2].url)).query) == {"file_id": ["f-9"]}
    assert_audit_clean(audit)


def test_minimax_maps_moderation_auth_quota_and_failure(config, stub):
    configure(config, backend="minimax", base_url="https://api.minimax.example/v1", model="m")
    generator = make_video_generator(config)
    for code, cls, words in (
        (1026, VideoContentRejected, "内容审核未通过"),
        (1004, ModelError, "鉴权失败"),
        (1008, ModelError, "余额或额度不足"),
        (2013, ModelError, "参数无效"),
    ):
        stub(minimax_handler([], submit={"base_resp": {"status_code": code, "status_msg": "x"}}))
        with pytest.raises(cls, match=words):
            generator.submit(VideoRequest(prompt="p"))
    stub(minimax_handler(["Fail"], query_code=1027))
    status = generator.poll("mm-1")
    assert status.state == "failed" and "生成结果含敏感内容" in status.error
    stub(minimax_handler(["Unknown"]))
    with pytest.raises(ModelError, match="未知状态"):
        generator.poll("mm-1")


def test_minimax_health_queries_only(config, stub):
    configure(config, backend="minimax", base_url="https://api.minimax.example/v1", model="m")
    calls = stub(minimax_handler(["Fail"], query_code=2013))
    result = make_video_generator(config).health()
    assert result["ready"] is True and "未发起生成" in result["reply"]
    assert [(c.method, c.url.path) for c in calls] == [("GET", "/v1/query/video_generation")]
    stub(minimax_handler(["Fail"], query_code=1004))
    with pytest.raises(ModelError, match="鉴权失败"):
        make_video_generator(config).health()


# ---------------------------------------------------------------- 通义万相


def dashscope_handler(states, *, submit=None, task_error=None):
    sequence = iter(states)

    def handler(request):
        path = request.url.path
        if path.endswith("/video-synthesis"):
            if submit is not None:
                return submit
            return httpx.Response(
                200, json={"output": {"task_id": "ds-1", "task_status": "PENDING"}, "request_id": "r"}
            )
        if path.endswith("/cancel"):
            return httpx.Response(400, json={"code": "UnsupportedOperation", "message": "running"})
        if path.startswith("/api/v1/tasks/"):
            if task_error is not None:
                return task_error
            status = next(sequence)
            output = {"task_id": "ds-1", "task_status": status}
            if status == "SUCCEEDED":
                output["video_url"] = "https://oss.example/v.mp4?Expires=1&Signature=s"
            if status == "FAILED":
                output.update(
                    code="DataInspectionFailed", message="Output data may contain inappropriate content."
                )
            return httpx.Response(200, json={"output": output, "request_id": "r"})
        if request.url.host == "oss.example":
            return httpx.Response(200, content=VIDEO)
        return httpx.Response(404)

    return handler


def test_dashscope_text_and_image_requests_follow_async_protocol(config, stub, audit, tmp_path):
    configure(config, backend="dashscope", base_url="https://dashscope.example", model="wan2.6-t2v")
    calls = stub(dashscope_handler(["PENDING", "RUNNING", "SUCCEEDED"]))
    generator = make_video_generator(config)
    assert isinstance(generator, DashScopeVideoGenerator)
    assert (
        generator.submit(
            VideoRequest(prompt="海边", negative_prompt="人群", seconds=5, width=1280, height=720, seed=42)
        )
        == "ds-1"
    )
    first = calls[0]
    assert first.url.path == "/api/v1/services/aigc/video-generation/video-synthesis"
    assert (
        first.headers["x-dashscope-async"] == "enable" and first.headers["authorization"] == f"Bearer {KEY}"
    )
    assert body(first) == {
        "model": "wan2.6-t2v",
        "input": {"prompt": "海边", "negative_prompt": "人群"},
        "parameters": {"duration": 5, "size": "1280*720", "seed": 42},
    }
    generator.submit(
        VideoRequest(prompt="海边", image=image_file(tmp_path), seconds=5, width=720, height=1280)
    )
    second = body(calls[1])
    assert second["input"]["img_url"].startswith("data:image/png;base64,")
    assert second["parameters"] == {"duration": 5, "resolution": "720P"}
    states = [generator.poll("ds-1") for _ in range(3)]
    assert [s.state for s in states] == ["queued", "running", "succeeded"]
    assert calls[2].url.path == "/api/v1/tasks/ds-1"
    target = tmp_path / "out.bin"
    generator.fetch(states[-1], target)
    assert target.read_bytes() == VIDEO and "authorization" not in calls[-1].headers
    assert_audit_clean(audit)


def test_dashscope_errors_failure_and_cancel(config, stub):
    configure(config, backend="dashscope", base_url="https://dashscope.example", model="wan")
    generator = make_video_generator(config)
    for status, code, cls, words in (
        (400, "DataInspectionFailed", VideoContentRejected, "内容审核未通过"),
        (401, "InvalidApiKey", ModelError, "鉴权失败"),
        (400, "Arrearage", ModelError, "欠费"),
    ):
        stub(dashscope_handler([], submit=httpx.Response(status, json={"code": code, "message": "m"})))
        with pytest.raises(cls, match=words):
            generator.submit(VideoRequest(prompt="p"))
    stub(dashscope_handler(["FAILED"]))
    failed = generator.poll("ds-1")
    assert failed.state == "failed" and "内容审核未通过" in failed.error
    stub(dashscope_handler(["UNKNOWN"]))
    assert "不存在或已过期" in generator.poll("ds-1").error
    calls = stub(dashscope_handler([]))
    generator.cancel("ds-1")  # 运行中的任务不可取消：忽略错误
    assert calls[-1].method == "POST" and calls[-1].url.path == "/api/v1/tasks/ds-1/cancel"


def test_dashscope_health_never_submits(config, stub):
    configure(config, backend="dashscope", base_url="https://dashscope.example", model="wan")
    calls = stub(
        dashscope_handler([], task_error=httpx.Response(404, json={"code": "NotFound", "message": "x"}))
    )
    assert make_video_generator(config).health()["ready"] is True
    assert [c.method for c in calls] == ["GET"]
    stub(
        dashscope_handler(
            [], task_error=httpx.Response(401, json={"code": "InvalidApiKey", "message": "bad"})
        )
    )
    with pytest.raises(ModelError, match="鉴权失败"):
        make_video_generator(config).health()


def test_polling_times_out_against_stub(config, stub):
    configure(
        config,
        backend="dashscope",
        base_url="https://dashscope.example",
        model="wan",
        poll_interval=0.01,
        max_wait=0.05,
    )
    calls = stub(dashscope_handler(["RUNNING"] * 1000))
    generator = make_video_generator(config)
    with pytest.raises(studio.StudioError, match="视频生成超时"):
        generate.wait_for(generator, "ds-1", config.studio.video_generator, lambda: False, lambda *_: None)
    assert calls[-1].url.path.endswith("/cancel")


# ---------------------------------------------------------------- ComfyUI


WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 1, "steps": 20, "model": ["4", 0]}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
    "9": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {"width": 832, "height": 480, "length": 33}},
    "12": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
    "20": {"class_type": "VHS_VideoCombine", "inputs": {"frame_rate": 16}},
}
MAPPING = {
    "prompt": "6.inputs.text",
    "negative": "7.inputs.text",
    "seed": "3.inputs.seed",
    "frames": "9.inputs.length",
    "width": "9.inputs.width",
    "height": "9.inputs.height",
    "image": "12.inputs.image",
}


@pytest.mark.parametrize(
    ("workflow", "mapping", "words"),
    [
        ({"nodes": [], "links": []}, MAPPING, "界面格式"),
        ({"1": {"inputs": {}}}, {"prompt": "1.inputs.text"}, "不是 API 格式"),
        (WORKFLOW, {}, "不能为空"),
        (WORKFLOW, {"negative": "7.inputs.text"}, "缺少 prompt"),
        (WORKFLOW, {**MAPPING, "steps": "3.inputs.steps"}, "未知字段 steps"),
        (WORKFLOW, {"prompt": "6.text"}, "映射 prompt（正向提示词）：格式应为"),
        (WORKFLOW, {"prompt": "99.inputs.text"}, "工作流中没有节点 99"),
        (WORKFLOW, {"prompt": "6.inputs.txt"}, "节点 6（CLIPTextEncode）没有输入 txt"),
    ],
)
def test_comfyui_mapping_validation_errors_are_chinese(workflow, mapping, words):
    with pytest.raises(ValueError, match=words):
        validate_workflow(workflow, mapping)
    with pytest.raises(ValueError, match=words):
        VideoGeneratorConfig(comfyui_workflow=workflow, comfyui_mapping=mapping)


def test_comfyui_config_requires_workflow_for_mapping_and_sorts_durations():
    with pytest.raises(ValueError, match="没有导入工作流"):
        VideoGeneratorConfig(comfyui_mapping={"prompt": "6.inputs.text"})
    assert VideoGeneratorConfig(durations=[10, 5, 5]).durations == [5.0, 10.0]
    with pytest.raises(ValueError):
        VideoGeneratorConfig(unknown=1)


def comfy_handler(history_sequence, *, queue=None):
    histories = iter(history_sequence)

    def handler(request):
        path = request.url.path
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "mindex-ref.png", "subfolder": "", "type": "input"})
        if path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "c-1", "number": 1, "node_errors": {}})
        if path == "/history/c-1":
            return httpx.Response(200, json=next(histories))
        if path == "/queue":
            if request.method == "POST":
                return httpx.Response(200, json={})
            return httpx.Response(200, json=queue() if callable(queue) else queue or {})
        if path == "/view":
            return httpx.Response(200, content=VIDEO)
        if path == "/system_stats":
            return httpx.Response(
                200, json={"system": {"comfyui_version": "0.3.60"}, "devices": [{"name": "cuda"}]}
            )
        return httpx.Response(404)

    return handler


def test_comfyui_maps_inputs_polls_queue_and_downloads(config, stub, audit, tmp_path):
    configure(
        config,
        backend="comfyui",
        base_url="http://comfy.local:8188",
        fps=16,
        comfyui_workflow=WORKFLOW,
        comfyui_mapping=MAPPING,
    )
    queues = iter(
        [
            {"queue_pending": [[1, "c-1", {}]], "queue_running": []},
            {"queue_pending": [], "queue_running": [[1, "c-1", {}]]},
        ]
    )
    done = {
        "c-1": {
            "status": {"status_str": "success", "completed": True},
            "outputs": {
                "20": {"gifs": [{"filename": "mindex_00001.mp4", "subfolder": "v", "type": "output"}]},
                "21": {"images": [{"filename": "preview.png", "type": "temp"}]},
            },
        }
    }
    calls = stub(comfy_handler([{}, {}, done], queue=lambda: next(queues)))
    generator = make_video_generator(config)
    assert isinstance(generator, ComfyUIVideoGenerator)
    task = generator.submit(
        VideoRequest(
            prompt="竹林",
            negative_prompt="文字",
            image=image_file(tmp_path),
            seconds=2.0,
            width=640,
            height=360,
            seed=11,
        )
    )
    assert task == "c-1"
    assert calls[0].url.path == "/upload/image"
    prompt = body(calls[1])["prompt"]
    assert prompt["6"]["inputs"]["text"] == "竹林" and prompt["7"]["inputs"]["text"] == "文字"
    assert prompt["3"]["inputs"] == {"seed": 11, "steps": 20, "model": ["4", 0]}
    assert prompt["9"]["inputs"] == {"width": 640, "height": 360, "length": 32}
    assert prompt["12"]["inputs"]["image"] == "mindex-ref.png"
    assert WORKFLOW["6"]["inputs"]["text"] == ""  # 原工作流不被修改
    states = [generator.poll(task) for _ in range(3)]
    assert [s.state for s in states] == ["queued", "running", "succeeded"]
    query = parse_qs(urlsplit(states[-1].url).query)
    assert query == {"filename": ["mindex_00001.mp4"], "subfolder": ["v"], "type": ["output"]}
    target = tmp_path / "out.bin"
    generator.fetch(states[-1], target)
    assert target.read_bytes() == VIDEO
    assert_audit_clean(audit)
    recorded = next(
        value for kind, value in audit.items if kind == "model-request" and value["url"].endswith("/prompt")
    )
    assert recorded["json"]["inputs"]["prompt"] == "竹林" and "workflow" in recorded["json"]


def test_comfyui_failure_missing_task_and_seed_default(config, stub):
    configure(
        config,
        backend="comfyui",
        base_url="http://comfy.local:8188",
        comfyui_workflow=WORKFLOW,
        comfyui_mapping={"prompt": "6.inputs.text", "seed": "3.inputs.seed"},
    )
    error = {
        "c-1": {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    ["execution_start", {}],
                    ["execution_error", {"exception_message": "CUDA out of memory"}],
                ],
            },
            "outputs": {},
        }
    }
    calls = stub(comfy_handler([error]))
    generator = make_video_generator(config)
    generator.submit(VideoRequest(prompt="p"))
    assert body(calls[-1])["prompt"]["3"]["inputs"]["seed"] == 1  # 未给 seed 时保留工作流原值
    failed = generator.poll("c-1")
    assert failed.state == "failed" and "CUDA out of memory" in failed.error
    stub(comfy_handler([{}, {}, {}], queue={"queue_pending": [], "queue_running": []}))
    assert [generator.poll("c-1").state for _ in range(3)] == ["running", "running", "failed"]
    with pytest.raises(ModelError, match="没有 image 字段"):
        generator.submit(VideoRequest(prompt="p", image=Path("x.png")))


def test_comfyui_health_reports_workflow_state_without_prompt(config, stub):
    configure(config, backend="comfyui", base_url="http://comfy.local:8188")
    calls = stub(comfy_handler([]))
    result = make_video_generator(config).health()
    assert result["ready"] is False and "尚未导入工作流" in result["reply"] and result["version"] == "0.3.60"
    configure(config, comfyui_workflow=WORKFLOW, comfyui_mapping=MAPPING)
    assert make_video_generator(config).health()["ready"] is True
    assert {c.url.path for c in calls} == {"/system_stats"}


# ---------------------------------------------------------------- 设置角色


def test_settings_role_profile_roundtrip_masks_key_and_activation_enables(config):
    client = TestClient(create_app(config))
    data = client.get("/api/settings").json()
    role = next(r for r in data["roles"] if r["role"] == "video_generator")
    assert role["label"] == "视频生成" and role["effective"]["enabled"] is False
    assert role["effective"]["backend"] == "comfyui" and role["effective"]["comfyui_workflow"] is None
    assert data["presets"]["minimax"]["video_generator"]["config"]["backend"] == "minimax_h3"
    assert data["presets"]["aliyun"]["video_generator"]["config"]["backend"] == "dashscope"
    created = client.post(
        "/api/settings/profiles",
        json={
            "role": "video_generator",
            "name": "海螺",
            "provider": "minimax",
            "api_key": KEY,
            "config": {
                "backend": "minimax",
                "base_url": "https://api.minimax.example/v1",
                "model": "MiniMax-Hailuo-2.3",
                "durations": [6, 10],
            },
        },
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    assert profile["api_key"] == {"has_key": True, "hint": "…" + KEY[-4:]}
    assert KEY not in created.text
    assert client.post(f"/api/settings/profiles/{profile['id']}/activate").status_code == 200
    effective = next(
        r for r in client.get("/api/settings").json()["roles"] if r["role"] == "video_generator"
    )["effective"]
    assert effective["enabled"] is True and effective["durations"] == [6.0, 10.0]
    assert effective["api_key"]["has_key"] is True
    assert KEY not in client.get("/api/settings").text
    current = client.app.state.runtime_config.current()
    assert current.studio.video_generator.api_key == KEY and current.studio.video_generator.enabled
    assert KEY not in json.dumps(current.studio.video_generator.model_dump())
    assert KEY not in current.studio_key


def test_settings_rejects_invalid_comfyui_mapping_in_chinese(config):
    client = TestClient(create_app(config))
    response = client.post(
        "/api/settings/profiles",
        json={
            "role": "video_generator",
            "name": "本地",
            "provider": "self",
            "config": {
                "backend": "comfyui",
                "base_url": "http://127.0.0.1:8188",
                "comfyui_workflow": WORKFLOW,
                "comfyui_mapping": {"prompt": "99.inputs.text"},
            },
        },
    )
    assert response.status_code == 422
    assert "工作流中没有节点 99" in response.json()["detail"]
    response = client.post(
        "/api/settings/profiles",
        json={
            "role": "video_generator",
            "name": "本地",
            "provider": "self",
            "config": {"backend": "comfyui", "enabled": True},
        },
    )
    assert response.status_code == 422 and "enabled" in response.json()["detail"]


def test_settings_test_connection_uses_health_only(config, monkeypatch):
    calls = []

    class Probe:
        def health(self):
            calls.append("health")
            return {"backend": "minimax", "model": "m", "ready": True, "reply": "鉴权通过"}

        def submit(self, request):  # pragma: no cover - 测试连接绝不能走到这里
            raise AssertionError("测试连接不得提交生成")

    import settings.api as settings_api

    monkeypatch.setattr(settings_api, "make_video_generator", lambda cfg: Probe())
    client = TestClient(create_app(config))
    profile = client.post(
        "/api/settings/profiles",
        json={
            "role": "video_generator",
            "name": "海螺",
            "provider": "minimax",
            "api_key": KEY,
            "config": {"backend": "minimax", "base_url": "https://api.minimax.example/v1", "model": "m"},
        },
    ).json()
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is True and result["reply"] == "鉴权通过" and calls == ["health"]

    def failing(cfg):
        raise ModelError(f"MiniMax 鉴权失败 Bearer {KEY}")

    monkeypatch.setattr(settings_api, "make_video_generator", failing)
    result = client.post(f"/api/settings/profiles/{profile['id']}/test").json()
    assert result["ok"] is False and KEY not in result["detail"]
