"""线上厂商适配器契约：TokenHub 嵌入 / OpenAI-chat ASR / MiniMax TTS / DashScope TTS / llama_template 门控。"""

from __future__ import annotations

import base64
import json

import pytest

from mindex import adapters
from mindex.adapters import (
    DashScopeSpeaker,
    MiniMaxSpeaker,
    MiniMaxTranscriber,
    ModelError,
    OpenAIChatTranscriber,
    QwenAnalyzer,
    QwenStoryboarder,
    TokenHubEmbedder,
    make_embedder,
    make_speaker,
    make_transcriber,
)


def test_tokenhub_identity_is_pure_config(config, monkeypatch):
    monkeypatch.setattr(
        adapters, "request", lambda *a, **kw: pytest.fail("identity() 不应发起网络请求")
    )
    embedder = TokenHubEmbedder(config)
    identity = embedder.identity()
    assert identity == {
        "model": config.embedding.model,
        "revision": config.embedding.revision,
        "quantization": config.embedding.quantization,
        "dimensions": config.embedding.dimensions,
        "normalized": True,
        "fps": config.video.fps,
        "max_frames": config.video.max_frames,
        "max_pixels": config.video.max_pixels,
        "implementation": "tokenhub-multimodal-1",
    }


def test_tokenhub_text_normalizes_and_validates(config, monkeypatch):
    config.embedding.dimensions = 2
    captured = {}

    def reply(method, url, **kwargs):
        captured.update(url=url, json=kwargs["json"])
        return {"data": [{"embedding": [3.0, 4.0]}]}

    monkeypatch.setattr(adapters, "request", reply)
    vector = TokenHubEmbedder(config).text(["中文查询"], role="query")[0]
    assert vector == pytest.approx([0.6, 0.8])
    assert captured["url"].endswith("/embeddings/multimodal")
    assert captured["json"]["input"] == [{"type": "text", "text": "中文查询"}]
    assert captured["json"]["model"] == config.embedding.model

    monkeypatch.setattr(
        adapters, "request", lambda *a, **kw: {"data": [{"embedding": [1.0, 2.0, 3.0]}]}
    )
    with pytest.raises(ModelError, match="维度"):
        TokenHubEmbedder(config).text(["再次"])


def test_tokenhub_video_samples_frames_by_config(config, monkeypatch, tmp_path):
    config.video.fps, config.video.max_frames = 4, 6
    counts, requests = [], []

    def fake_frames(path, count):
        counts.append(count)
        return [(i * 1.0, f"data:image/jpeg;base64,FRAME{i}") for i in range(min(count, 3))]

    def reply(method, url, **kwargs):
        requests.append(kwargs["json"])
        return {"data": [{"embedding": [1.0] * config.embedding.dimensions}]}

    monkeypatch.setattr(adapters, "frame_data", fake_frames)
    monkeypatch.setattr(adapters, "probe", lambda path: {"duration": 10.0})
    monkeypatch.setattr(adapters, "request", reply)
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fake")
    vector = TokenHubEmbedder(config).video(clip)
    assert len(vector) == config.embedding.dimensions
    # 10 秒 × 4fps = 40 帧，受 max_frames=6 封顶
    assert counts == [6]
    inputs = requests[0]["input"]
    assert all(item["type"] == "image_url" for item in inputs)
    assert len(inputs) == 3  # 抽帧桩只回 3 帧


def test_tokenhub_image_uses_data_uri(config, monkeypatch, tmp_path):
    captured = []

    def reply(method, url, **kwargs):
        captured.append(kwargs["json"])
        return {"data": [{"embedding": [1.0] * config.embedding.dimensions}]}

    monkeypatch.setattr(adapters, "request", reply)
    picture = tmp_path / "frame.png"
    picture.write_bytes(b"\x89PNG fake")
    TokenHubEmbedder(config).image(picture)
    uri = captured[0]["input"][0]["image_url"]["url"]
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == b"\x89PNG fake"


def test_openai_chat_transcriber_contract(config, monkeypatch, tmp_path):
    captured = {}

    def reply(method, url, **kwargs):
        captured.update(url=url, json=kwargs["json"])
        return {"choices": [{"message": {"content": "  雨夜有人在说话。 "}}]}

    monkeypatch.setattr(adapters, "request", reply)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF-fake-wav")
    result = OpenAIChatTranscriber(config).raw(audio)
    assert result["text"] == "雨夜有人在说话。"
    assert result["language"] == ""
    content = captured["json"]["messages"][0]["content"][0]
    assert content["type"] == "input_audio"
    assert content["input_audio"]["format"] == "wav"
    assert content["input_audio"]["data"].startswith("data:audio/wav;base64,")

    with pytest.raises(ModelError, match="时间戳"):
        OpenAIChatTranscriber(config).transcribe(audio)
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"choices": []})
    with pytest.raises(ModelError):
        OpenAIChatTranscriber(config).raw(audio)


def test_minimax_transcriber_contract(config, monkeypatch, tmp_path):
    """MiniMax speech_to_text：multipart 表单上传；静音空文本合法，缺 text 字段才是错误。"""
    captured = {}

    def reply(method, url, **kwargs):
        captured.update(url=url, data=kwargs.get("data"), files=kwargs.get("files"))
        return {"text": "  雨夜有人在说话。 ", "duration": 3.2, "trace_id": "t-1"}

    monkeypatch.setattr(adapters, "request", reply)
    config.asr.backend = "minimax"
    config.asr.base_url = "https://api.minimax.cn/v1"
    config.asr.model = "asr-1.0"
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF-fake-wav")
    result = MiniMaxTranscriber(config).raw(audio)
    assert result["text"] == "雨夜有人在说话。"
    assert result["language"] == ""
    assert captured["url"].endswith("/speech_to_text")
    assert captured["data"] == {"model": "asr-1.0"}
    assert captured["files"]["file"][0] == "audio.wav"

    with pytest.raises(ModelError, match="时间戳"):
        MiniMaxTranscriber(config).transcribe(audio)

    # 官方文档：静音音频可返回空文本，属合法响应而非错误（设置页测试连接依赖此行为）。
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"text": "", "duration": 0.5})
    assert MiniMaxTranscriber(config).raw(audio)["text"] == ""

    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"duration": 1.0})
    with pytest.raises(ModelError, match="MiniMax"):
        MiniMaxTranscriber(config).raw(audio)


def test_minimax_speaker_decodes_hex(config, monkeypatch):
    captured = {}

    def reply(method, url, **kwargs):
        captured.update(url=url, json=kwargs["json"])
        return {"base_resp": {"status_code": 0}, "data": {"audio": b"RIFF-hex-audio".hex()}}

    monkeypatch.setattr(adapters, "request", reply)
    config.studio.tts.voice = "narrator-a"
    audio = MiniMaxSpeaker(config).speak("测试配音")
    assert audio == b"RIFF-hex-audio"
    assert captured["url"].endswith("/t2a_v2")
    assert captured["json"]["voice_setting"] == {"voice_id": "narrator-a"}
    assert captured["json"]["text"] == "测试配音"

    monkeypatch.setattr(
        adapters, "request", lambda *a, **kw: {"base_resp": {"status_code": 1004}, "data": {}}
    )
    with pytest.raises(ModelError, match="MiniMax"):
        MiniMaxSpeaker(config).speak("测试")


def test_dashscope_speaker_downloads_url_or_decodes_base64(config, monkeypatch):
    captured = []

    def reply(method, url, **kwargs):
        captured.append((url, kwargs["json"]))
        return {"output": {"audio": {"url": "https://cdn.example.com/a.wav"}}}

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters, "request_bytes", lambda *a, **kw: b"RIFF-downloaded")
    assert DashScopeSpeaker(config).speak("你好", voice="cosy-clone-1") == b"RIFF-downloaded"
    url, body = captured[0]
    assert url.endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert body["input"]["voice"] == "cosy-clone-1"

    encoded = base64.b64encode(b"RIFF-b64").decode()
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"output": {"audio": {"data": encoded}}})
    assert DashScopeSpeaker(config).speak("你好") == b"RIFF-b64"
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {"output": {}})
    with pytest.raises(ModelError, match="DashScope"):
        DashScopeSpeaker(config).speak("你好")


def test_llama_template_gating(config, monkeypatch):
    captured = []

    def reply(method, url, **kwargs):
        captured.append(kwargs["json"])
        return {"choices": [{"message": {"content": '{"ok": 1}'}}]}

    monkeypatch.setattr(adapters, "request", reply)
    analyzer = QwenAnalyzer(config)
    analyzer._chat([{"type": "text", "text": "hi"}], "指令")
    assert "chat_template_kwargs" in captured[-1]  # 默认 True：本地 llama-server 行为不变

    config.analyzer.llama_template = False
    QwenAnalyzer(config)._chat([{"type": "text", "text": "hi"}], "指令")
    assert "chat_template_kwargs" not in captured[-1]

    QwenStoryboarder(config)._chat([{"type": "text", "text": "hi"}], "指令", feedback=None)
    assert "chat_template_kwargs" in captured[-1]
    config.studio.generator.llama_template = False
    QwenStoryboarder(config)._chat([{"type": "text", "text": "hi"}], "指令", feedback=None)
    assert "chat_template_kwargs" not in captured[-1]


def test_factories_dispatch_cloud_backends(config):
    config.embedding.backend = "tokenhub"
    assert isinstance(make_embedder(config), TokenHubEmbedder)
    config.asr.backend = "openai"
    assert isinstance(make_transcriber(config), OpenAIChatTranscriber)
    config.asr.backend = "minimax"
    assert isinstance(make_transcriber(config), MiniMaxTranscriber)
    config.studio.tts.backend = "minimax"
    assert isinstance(make_speaker(config), MiniMaxSpeaker)
    config.studio.tts.backend = "dashscope"
    assert isinstance(make_speaker(config), DashScopeSpeaker)
    config.studio.tts.backend = "openai"
    from mindex.adapters import OpenAISpeaker

    assert isinstance(make_speaker(config), OpenAISpeaker)


def test_pipeline_routes_openai_asr_to_raw_flow(config, monkeypatch):
    from mindex.pipeline import Pipeline

    config.asr.backend = "openai"
    calls = []
    monkeypatch.setattr(Pipeline, "transcribe_raw", lambda self, source_id: calls.append(source_id))
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = config
    pipeline.transcribe("src-1")
    assert calls == ["src-1"]


def test_pipeline_routes_minimax_asr_to_raw_flow(config, monkeypatch):
    """minimax 与 openai 一样只出纯文本，必须走 transcribe_raw → align 流程。"""
    from mindex.pipeline import Pipeline

    config.asr.backend = "minimax"
    calls = []
    monkeypatch.setattr(Pipeline, "transcribe_raw", lambda self, source_id: calls.append(source_id))
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = config
    pipeline.transcribe("src-1")
    assert calls == ["src-1"]


def test_request_bytes_audit_truncates_json_body(monkeypatch):
    from typing import ClassVar

    import httpx

    recorded = []
    monkeypatch.setattr(adapters, "record", lambda kind, payload: recorded.append((kind, payload)))

    class FakeResponse:
        status_code = 200
        content = b"x"
        headers: ClassVar[dict] = {"content-type": "application/json"}
        text = json.dumps({"data": {"audio": "ab" * 5000}})

        def raise_for_status(self):
            return None

    monkeypatch.setattr(httpx, "request", lambda *a, **kw: FakeResponse())
    adapters.request_bytes("POST", "https://tts.example/t2a", timeout=1, json={"a": 1})
    body = recorded[-1][1]["body"]
    assert body is not None and len(body) <= 8192


def test_request_error_surfaces_upstream_reason(monkeypatch):
    """上游 4xx 的错误正文才是可读原因；只抛 httpx 异常串会让页面显示无意义的 '400 Bad Request'。"""
    from typing import ClassVar

    import httpx

    body = {
        "type": "error",
        "error": {
            "type": "bad_request_error",
            "message": "invalid params, model[M1] does not support max tokens > 524288 (2013)",
        },
    }

    class FakeResponse:
        status_code = 400
        text = json.dumps(body)
        headers: ClassVar[dict] = {"content-type": "application/json"}
        content = json.dumps(body).encode()

        def json(self):
            return body

        def raise_for_status(self):
            error = httpx.HTTPStatusError(
                "Client error '400 Bad Request' for url 'https://x/v1/chat/completions'",
                request=httpx.Request("POST", "https://x/v1/chat/completions"),
                response=httpx.Response(400),
            )
            error.response = self  # 真实场景里 exc.response 就是带正文的那个响应
            raise error

    monkeypatch.setattr(httpx, "request", lambda *a, **kw: FakeResponse())
    monkeypatch.setattr(adapters, "record", lambda kind, payload: None)

    with pytest.raises(adapters.ModelError) as excinfo:
        adapters.request("POST", "https://x/v1/chat/completions", json={}, attempts=1)
    message = str(excinfo.value)
    assert "does not support max tokens" in message
    assert "does not support max tokens > 524288" in message


def test_request_error_falls_back_to_raw_body(monkeypatch):
    """非 JSON 错误正文也要带出来，不能只剩状态码。"""
    from typing import ClassVar

    import httpx

    class FakeResponse:
        status_code = 400
        text = "<html>gateway rejected</html>"
        headers: ClassVar[dict] = {"content-type": "text/html"}
        content = b"<html>gateway rejected</html>"

        def json(self):
            raise ValueError("not json")

        def raise_for_status(self):
            error = httpx.HTTPStatusError(
                "Client error '400 Bad Request'",
                request=httpx.Request("POST", "https://x/v1/chat/completions"),
                response=httpx.Response(400),
            )
            error.response = self
            raise error

    monkeypatch.setattr(httpx, "request", lambda *a, **kw: FakeResponse())
    monkeypatch.setattr(adapters, "record", lambda kind, payload: None)

    with pytest.raises(adapters.ModelError) as excinfo:
        adapters.request("POST", "https://x/v1/chat/completions", json={}, attempts=1)
    assert "gateway rejected" in str(excinfo.value)
