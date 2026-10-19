"""线上厂商适配器契约：TokenHub 嵌入 / OpenAI-chat ASR / MiniMax TTS / DashScope TTS / llama_template 门控。"""

from __future__ import annotations

import base64
import json

import httpx
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
    assert captured[-1]["max_tokens"] == 12768

    config.analyzer.llama_template = False
    QwenAnalyzer(config)._chat([{"type": "text", "text": "hi"}], "指令")
    assert "chat_template_kwargs" not in captured[-1]

    QwenStoryboarder(config)._chat([{"type": "text", "text": "hi"}], "指令", feedback=None)
    assert "chat_template_kwargs" in captured[-1]
    config.studio.generator.llama_template = False
    QwenStoryboarder(config)._chat([{"type": "text", "text": "hi"}], "指令", feedback=None)
    assert "chat_template_kwargs" not in captured[-1]


def test_analyzer_retries_invalid_json_empty_and_malformed_responses(config, monkeypatch):
    config.analyzer.base_url = "https://api.minimax.cn/v1"
    config.analyzer.model = "MiniMax-M3.1-Flash-Preview"
    config.analyzer.llama_template = False
    replies = iter([
        {"choices": [{"message": {"content": '{"description":'}}]},
        {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "thinking"}}]},
        {"choices": []},
        {"choices": [{"message": {"content": ""}}]},
        {"choices": [{"message": {"content": '```json\n{"description":"机场大厅"}\n```'}}]},
    ])
    calls, delays = [], []

    def reply(method, url, **kwargs):
        calls.append((url, kwargs["json"]))
        return next(replies)

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    monkeypatch.setattr(adapters, "frame_data", lambda *a: [(0, "data:image/jpeg;base64,TEST")])
    result = QwenAnalyzer(config).analyze(None, [])
    assert result.description == "机场大厅"
    assert len(calls) == 5
    assert delays == [1, 2, 4, 8]
    assert all(c[0] == "https://api.minimax.cn/v1/chat/completions" for c in calls)
    assert [c[1]["max_tokens"] for c in calls] == [12768, 25536, 51072, 102144, 131072]
    assert all(c[1]["messages"] == calls[0][1]["messages"] for c in calls)


def test_analysis_prompts_request_only_compact_required_json(config, monkeypatch):
    bodies = []

    def reply(method, url, **kwargs):
        bodies.append(kwargs["json"])
        text = '{"description":"游客在机场花园拍照","actions":["拍照"]}' if len(bodies) == 1 else '{"groups":[[0],[1]]}'
        return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters, "frame_data", lambda *a: [(0, "data:image/jpeg;base64,TEST")])
    analyzer = QwenAnalyzer(config)
    result = analyzer.analyze(None, [])
    assert result.actions == ["拍照"] and result.uncertainty == []
    assert analyzer.group_scenes([{"start": 0, "end": 2}, {"start": 2, "end": 4}]) == [[0], [1]]
    analysis_prompt = bodies[0]["messages"][0]["content"]
    grouping_prompt = bodies[1]["messages"][0]["content"]
    assert "未知或空列表省略" in analysis_prompt and "禁止 Markdown" in analysis_prompt
    assert "仅使用以下字段" in analysis_prompt and "最多3项" in analysis_prompt
    assert "即使只有一项也必须写成数组" in analysis_prompt
    assert 'scene 正确为 ["夜间城市滨水区"]' in analysis_prompt
    assert "每个元素都是字符串" in analysis_prompt
    assert "唯一字段为 scene_starts" in grouping_prompt and "分组说明" in grouping_prompt
    assert "本批共有 2 个片段，index 为 0 到 1" in grouping_prompt
    assert "最后一个场景自动延续到片段 1" in grouping_prompt
    assert "偏好而非最低时长" in grouping_prompt
    assert all("JSON 之外的任何文字" in b["messages"][0]["content"] for b in bodies)


@pytest.mark.parametrize("url,model,template,expected", [
    ("https://open.bigmodel.cn/api/coding/paas/v4", "GLM-5.3-Flash", False,
     {"reasoning_effort": "low", "response_format": {"type": "json_object"}}),
    ("https://open.bigmodel.cn/api/paas/v4", "glm-4.5v", False,
     {"thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}}),
    ("https://api.minimax.cn/v1", "MiniMax-M3.1-Flash-Preview", False,
     {"reasoning_effort": "low", "reasoning_split": True}),
    ("https://api.minimax.io/v1", "MiniMax-M3", False,
     {"thinking": {"type": "disabled"}, "reasoning_split": True}),
    ("http://127.0.0.1:4040/v1", "qwen", True,
     {"chat_template_kwargs": {"enable_thinking": False}, "response_format": {"type": "json_object"}}),
    ("https://custom.example.com/v1", "custom", False, {}),
])
def test_analysis_calls_apply_supported_low_thinking_controls(config, monkeypatch, url, model, template, expected):
    config.analyzer.base_url, config.analyzer.model, config.analyzer.llama_template = url, model, template
    bodies = []

    def reply(*args, **kwargs):
        bodies.append(kwargs["json"])
        return {"choices": [{"message": {"content": '{"groups":[[0]]}'}}]}

    monkeypatch.setattr(adapters, "request", reply)
    assert QwenAnalyzer(config).group_scenes([{"start": 0, "end": 1}]) == [[0]]
    actual = {k: v for k, v in bodies[0].items() if k not in {"model", "temperature", "max_tokens", "messages"}}
    assert actual == expected


def test_glm_visual_analysis_uses_low_without_text_only_json_parameter(config, monkeypatch):
    config.analyzer.base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
    config.analyzer.model = "GLM-5.3-Flash"
    config.analyzer.llama_template = False
    captured = []

    def reply(*args, **kwargs):
        captured.append(kwargs["json"])
        return {"choices": [{"message": {"content": '{"description":"机场"}'}}]}

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters, "frame_data", lambda *a: [(0, "data:image/jpeg;base64,TEST")])
    assert QwenAnalyzer(config).analyze(None, []).description == "机场"
    assert captured[0]["reasoning_effort"] == "low"
    assert "thinking" not in captured[0] and "response_format" not in captured[0]
    config.analyzer.thinking_mode = "provider"
    assert "reasoning_effort" not in adapters.analysis_options(config.analyzer)


def test_scene_grouping_compacts_historical_descriptions(config, monkeypatch):
    captured = []

    def reply(*args, **kwargs):
        captured.append(kwargs["json"])
        return {"choices": [{"message": {"content": '{"groups":[[0]]}'}}]}

    monkeypatch.setattr(adapters, "request", reply)
    units = [{"start": 0.123456, "end": 1.123456, "description": "花园" * 1000,
              "scene": ["机场", "花园"], "unused": "不需要"}]
    assert QwenAnalyzer(config).group_scenes(units) == [[0]]
    sent = json.loads(captured[0]["messages"][1]["content"][0]["text"])[0]
    assert len(sent["description"]) == 160
    assert sent["index"] == 0
    assert sent["start"] == 0.123 and sent["scene"] == ["机场", "花园"]
    assert "unused" not in sent and len(units[0]["description"]) == 2000


@pytest.mark.parametrize("retries", [0, 2, 5])
def test_analyzer_response_retries_exhaust_and_keep_last_reason(config, monkeypatch, retries):
    config.analyzer.response_retries = retries
    calls, delays = [], []

    def reply(*args, **kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "not JSON"}}]}

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    with pytest.raises(ModelError, match=rf"连续 {retries + 1} 次无效.*重试已耗尽.*有效 JSON"):
        QwenAnalyzer(config)._chat([], "指令")
    assert len(calls) == retries + 1
    assert delays == [min(2 ** i, 8) for i in range(retries)]


@pytest.mark.parametrize("payload", [
    {"choices": [{"finish_reason": "stop", "message": {"content": "未完成的 JSON：" + "原文" * 5000}}]},
    {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "原始推理"}}]},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": []},
])
def test_analyzer_logs_full_invalid_response_even_on_final_attempt(config, monkeypatch, caplog, payload):
    config.analyzer.model = "GLM-5.3-Flash"
    config.analyzer.response_retries = 0
    config.analyzer.api_key = "request-secret"
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: payload)
    with caplog.at_level("WARNING", logger="mindex.adapters"), pytest.raises(ModelError, match="自动重试已耗尽"):
        QwenAnalyzer(config)._chat([], "request-instructions")
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "GLM-5.3-Flash" in message and "max_tokens=12768" in message
    assert json.dumps(payload, ensure_ascii=False, indent=2) in message
    assert "—— 模型返回结束 ——" in message
    assert "request-secret" not in message and "request-instructions" not in message


def test_analyzer_retries_non_json_http_body(config, monkeypatch):
    calls, delays = [], []

    def wire(method, url, **kwargs):
        calls.append(kwargs)
        req = httpx.Request(method, url)
        if len(calls) < 3:
            return httpx.Response(200, text="{truncated", request=req)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"groups":[[0]]}'}}]}, request=req)

    monkeypatch.setattr(adapters.httpx, "request", wire)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    assert QwenAnalyzer(config).group_scenes([{"start": 0, "end": 1}]) == [[0]]
    assert len(calls) == 3 and delays == [1, 2]


def test_analyzer_does_not_repeat_permanent_http_failure(config, monkeypatch):
    calls = []

    def wire(method, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(401, json={"error": {"message": "invalid API key"}},
                              request=httpx.Request(method, url))

    monkeypatch.setattr(adapters.httpx, "request", wire)
    monkeypatch.setattr(adapters.time, "sleep", lambda _: pytest.fail("401 should not retry"))
    with pytest.raises(ModelError, match="invalid API key"):
        QwenAnalyzer(config)._chat([], "指令")
    assert len(calls) == 1


def test_analyzer_retries_http_500_until_json_response(config, monkeypatch):
    calls, delays = [], []

    def wire(method, url, **kwargs):
        calls.append(kwargs)
        req = httpx.Request(method, url)
        if len(calls) < 3:
            return httpx.Response(500, json={"error": {"message": "temporary server error"}}, request=req)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"answer":"ok"}'}}]}, request=req)

    monkeypatch.setattr(adapters.httpx, "request", wire)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    assert QwenAnalyzer(config)._chat([], "指令") == {"answer": "ok"}
    assert len(calls) == 3 and delays == [1, 2]


def test_analyzer_retry_policy_preserves_existing_analysis_key(config):
    original = config.analysis_key
    old_config = config.analyzer.model_dump(
        exclude={"timeout", "api_key_env", "response_retries", "initial_output_tokens", "max_output_tokens", "thinking_mode"}
    )
    from mindex.config import fingerprint

    assert original == fingerprint({
        "prompt": 1, "model": old_config, "deployment": config.analyzer_deployment, "layout": config.layout_key,
    })
    config.analyzer.response_retries = 20
    config.analyzer.initial_output_tokens = 8000
    config.analyzer.max_output_tokens = 32768
    config.analyzer.thinking_mode = "low"
    assert config.analysis_key == original


def test_scene_grouping_increases_budget_after_reasoning_exhausts_tokens(config, monkeypatch):
    budgets, delays = [], []

    def reply(method, url, **kwargs):
        budgets.append(kwargs["json"]["max_tokens"])
        if budgets[-1] < 51072:
            return {"choices": [{"finish_reason": "length", "message": {"reasoning_content": "推理"}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": '{"groups":[[0,1]]}'}}]}

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    assert QwenAnalyzer(config).group_scenes([{"start": 0, "end": 1}, {"start": 1, "end": 2}]) == [[0, 1]]
    assert budgets == [12768, 25536, 51072] and delays == [1, 2]


@pytest.mark.parametrize("cap,expected", [
    (4096, [4096] * 6),
    (30000, [12768, 25536, 30000, 30000, 30000, 30000]),
    (131072, [12768, 25536, 51072, 102144, 131072, 131072]),
])
def test_truncated_json_is_rejected_and_budget_growth_is_capped(config, monkeypatch, cap, expected):
    config.analyzer.max_output_tokens = cap
    budgets, delays = [], []

    def reply(method, url, **kwargs):
        budgets.append(kwargs["json"]["max_tokens"])
        return {"choices": [{"finish_reason": "length", "message": {
            "content": '{"groups":[[0]]}', "reasoning_content": "推理占用预算",
        }}]}

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    with pytest.raises(ModelError, match=rf"重试已耗尽.*finish_reason=length.*max_tokens={cap}.*提高输出 token"):
        QwenAnalyzer(config).group_scenes([{"start": 0, "end": 1}])
    assert budgets == expected


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


def test_pipeline_routes_minimax_asr_to_timestamp_flow(config, monkeypatch):
    """MiniMax 使用服务词级时间戳，不进入本地对齐流程。"""
    from mindex.pipeline import Pipeline

    config.asr.backend = "minimax"
    calls = []
    monkeypatch.setattr(Pipeline, "enrich_dialogue", lambda self, source_id: calls.append(source_id))
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
