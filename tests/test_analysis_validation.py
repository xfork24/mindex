"""LLM 返回合法 JSON 但字段类型/值不符合约定时，使用同一无效响应重试预算。"""

from __future__ import annotations

import json

import pytest

from mindex.models import adapters, qwen
from mindex.models.adapters import ModelError
from mindex.models.qwen import QwenAnalyzer
from mindex.processing.pipeline import Pipeline


def chat_response(result):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps(result, ensure_ascii=False),
    }}]}


@pytest.mark.parametrize("invalid,field", [
    ({"description": "夜景", "scene": "夜间城市滨水区，水面倒映灯光"}, "scene"),
    ({"description": "夜景", "actions": "行走"}, "actions"),
    ({"description": "夜景", "shot_size": "远景"}, "shot_size"),
    ({"description": "夜景", "people": None}, "people"),
    ({"description": "夜景", "objects": {"name": "灯"}}, "objects"),
    ({"description": "夜景", "emotions": [42]}, "emotions.0"),
    ({"description": ["夜景"]}, "description"),
    ({"description": ""}, "description"),
    ({"scene": ["滨水区"]}, "description"),
    ([{"description": "夜景"}], "根对象"),
    (None, "根对象"),
])
def test_annotation_validation_failure_retries_with_field_feedback(config, monkeypatch, invalid, field):
    config.analyzer.response_retries = 1
    config.analyzer.initial_output_tokens = 256
    config.analyzer.max_output_tokens = 400
    valid = {"description": "水面倒映城市灯光。", "scene": ["夜间城市滨水区"]}
    responses = iter([chat_response(invalid), chat_response(valid)])
    bodies, delays, sampled, records = [], [], [], []

    def reply(*args, **kwargs):
        bodies.append(kwargs["json"])
        return next(responses)

    def frames(path, count):
        sampled.append(count)
        return [(0, "data:image/jpeg;base64,TEST")]

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(qwen, "frame_data", frames)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    monkeypatch.setattr(qwen, "record", lambda kind, data: records.append((kind, data)))
    dialogue = [{"start": 0, "end": 1, "text": "看看这里。"}]
    result = QwenAnalyzer(config).analyze(None, dialogue)
    assert result.description == valid["description"] and result.scene == valid["scene"]
    assert result.actions == []
    assert sampled == [config.video.analysis_frames]  # 重试复用抽帧与对白。
    assert delays == [1] and [body["max_tokens"] for body in bodies] == [256, 400]
    assert bodies[0]["messages"][1] == bodies[1]["messages"][1]
    original_prompt = bodies[0]["messages"][0]["content"]
    retry_prompt = bodies[1]["messages"][0]["content"]
    assert retry_prompt.startswith(original_prompt)
    assert f"{field}:" in retry_prompt and "上次响应未通过结构校验" in retry_prompt
    assert "完整 JSON" in retry_prompt and "https://errors.pydantic.dev" not in retry_prompt
    assert len(records) == 1 and records[0][0] == "analysis-retry"
    assert records[0][1]["attempts"] == 2 and field in records[0][1]["error"]


@pytest.mark.parametrize("retries", [0, 2, 5])
def test_annotation_validation_exhausts_configured_retries(config, monkeypatch, caplog, retries):
    config.analyzer.response_retries = retries
    invalid = chat_response({"description": "夜景", "scene": "滨水区"})
    bodies, delays = [], []

    def reply(*args, **kwargs):
        bodies.append(kwargs["json"])
        return invalid

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(qwen, "frame_data", lambda *args: [])
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    with caplog.at_level("WARNING", logger="mindex.models.adapters"), pytest.raises(
        ModelError, match=rf"连续 {retries + 1} 次无效.*重试已耗尽.*scene: Input should be a valid list",
    ):
        QwenAnalyzer(config).analyze(None, [])
    assert len(bodies) == retries + 1
    assert delays == [min(2 ** i, 8) for i in range(retries)]
    payload_logs = [record.getMessage() for record in caplog.records if "—— 模型返回结束 ——" in record.getMessage()]
    assert len(payload_logs) == retries + 1
    assert all(json.dumps(invalid, ensure_ascii=False, indent=2) in message for message in payload_logs)


def test_syntax_and_field_errors_share_one_retry_budget(config, monkeypatch):
    config.analyzer.response_retries = 2
    responses = iter([
        {"choices": [{"message": {"content": "{broken"}}]},
        chat_response({"description": "夜景", "scene": "滨水区"}),
        chat_response({"description": "夜景", "scene": None}),
        chat_response({"description": "不应请求第四次"}),
    ])
    calls, delays = [], []

    def reply(*args, **kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(qwen, "frame_data", lambda *args: [])
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    with pytest.raises(ModelError, match="连续 3 次无效.*scene"):
        QwenAnalyzer(config).analyze(None, [])
    assert len(calls) == 3 and delays == [1, 2]


@pytest.mark.parametrize("invalid,reason", [
    ({}, "groups"),
    ({"groups": "0,1"}, "groups"),
    ({"groups": [0, 1]}, "groups"),
    ({"groups": [[]]}, "groups"),
    ({"groups": []}, "连续、完整且无重复"),
    ({"groups": [["0", "1"]]}, "索引必须为整数"),
    ({"groups": [[False, True]]}, "索引必须为整数"),
    ({"groups": [[0, 0], [1]]}, "连续、完整且无重复"),
    ({"groups": [[0]]}, "连续、完整且无重复"),
    ({"groups": [[1], [0]]}, "连续、完整且无重复"),
    ({"groups": [[0], [1], [2]]}, "连续、完整且无重复"),
    ({"groups": [[0, 1]]}, "超过45秒"),
])
def test_invalid_scene_grouping_uses_response_retries(config, monkeypatch, invalid, reason):
    config.analyzer.response_retries = 1
    responses = iter([chat_response(invalid), chat_response({"groups": [[0], [1]]})])
    bodies, delays = [], []

    def reply(*args, **kwargs):
        bodies.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    units = [{"start": 0, "end": 30}, {"start": 30, "end": 60}]
    assert QwenAnalyzer(config).group_scenes(units) == [[0], [1]]
    assert len(bodies) == 2 and delays == [1]
    assert reason in bodies[1]["messages"][0]["content"]
    assert bodies[0]["messages"][1] == bodies[1]["messages"][1]


def test_pipeline_stores_only_valid_annotations_and_reuses_checkpoints(config, video_file, monkeypatch):
    config.analyzer.response_retries = 1
    bodies, delays = [], []
    annotation_attempts = 0

    def reply(*args, **kwargs):
        nonlocal annotation_attempts
        bodies.append(kwargs["json"])
        if "唯一字段为 scene_starts" in kwargs["json"]["messages"][0]["content"]:
            units = json.loads(kwargs["json"]["messages"][1]["content"][0]["text"])
            assert [unit["index"] for unit in units] == list(range(len(units)))
            return chat_response({"scene_starts": [0]})
        annotation_attempts += 1
        scene = "夜景" if annotation_attempts % 2 else ["夜景"]
        return chat_response({"description": "城市灯光倒映水面。", "scene": scene})

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(qwen, "frame_data", lambda *args: [(0, "data:image/jpeg;base64,TEST")])
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    pipeline = Pipeline(config, analyzer=QwenAnalyzer(config))
    source_id = pipeline.prepare(video_file)
    pipeline.analyze(source_id)
    profile = pipeline.annotation_profile(source_id)
    segments = pipeline.store.segments(source_id)
    assert any(segment["kind"] == "scene" for segment in segments)
    for segment in segments:
        saved = pipeline.store.json_value("annotations", "segment_id", segment["id"], profile)
        assert saved["annotation"]["scene"] == ["夜景"]
    assert annotation_attempts > 0 and delays == [1] * (annotation_attempts // 2)
    completed_requests = len(bodies)
    pipeline.analyze(source_id)
    assert len(bodies) == completed_requests


def test_scene_boundaries_cover_chinatown_dense_batch_through_final_clip(config, monkeypatch):
    # Chinatown has 32 units in its first batch. The earlier model response
    # stopped at index 27; boundaries must still include the final four units.
    starts = [0, 2, 7, 10, 15, 18, 20, 22, 24, 26]
    bodies = []

    def reply(*args, **kwargs):
        bodies.append(kwargs["json"])
        return chat_response({"scene_starts": starts})

    monkeypatch.setattr(adapters, "request", reply)
    units = [{"start": i * 0.85, "end": (i + 1) * 0.85, "description": "市场摊位"} for i in range(32)]
    groups = QwenAnalyzer(config).group_scenes(units)
    assert [index for group in groups for index in group] == list(range(32))
    assert [group[0] for group in groups] == starts
    assert groups[-1] == [26, 27, 28, 29, 30, 31]
    sent = json.loads(bodies[0]["messages"][1]["content"][0]["text"])
    assert [unit["index"] for unit in sent] == list(range(32))
    assert "本批共有 32 个片段，index 为 0 到 31" in bodies[0]["messages"][0]["content"]
    assert len(bodies) == 1


@pytest.mark.parametrize("invalid,reason", [
    ({"scene_starts": None}, "非空整数数组"),
    ({"scene_starts": []}, "非空整数数组"),
    ({"scene_starts": ["0", 1]}, "索引必须为整数"),
    ({"scene_starts": [False, 1]}, "索引必须为整数"),
    ({"scene_starts": [0, 1.0]}, "索引必须为整数"),
    ({"scene_starts": [1]}, "首项必须为0"),
    ({"scene_starts": [0, -1]}, "0到2之间"),
    ({"scene_starts": [0, 3]}, "0到2之间"),
    ({"scene_starts": [0, 1, 1]}, "严格递增且无重复"),
    ({"scene_starts": [0, 2, 1]}, "严格递增且无重复"),
    ({"scene_starts": [0], "groups": [[0, 1, 2]]}, "只能包含 scene_starts"),
    ({"scene_starts": [0]}, "超过45秒"),
])
def test_invalid_scene_boundaries_retry_with_specific_feedback(config, monkeypatch, invalid, reason):
    config.analyzer.response_retries = 1
    responses = iter([chat_response(invalid), chat_response({"scene_starts": [0, 1, 2]})])
    bodies, delays = [], []

    def reply(*args, **kwargs):
        bodies.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters.time, "sleep", delays.append)
    units = [{"start": i * 20, "end": (i + 1) * 20} for i in range(3)]
    assert QwenAnalyzer(config).group_scenes(units) == [[0], [1], [2]]
    assert delays == [1] and len(bodies) == 2
    assert reason in bodies[1]["messages"][0]["content"]
    assert bodies[0]["messages"][1] == bodies[1]["messages"][1]


def test_legacy_scene_grouping_reports_missing_chinatown_tail(config, monkeypatch):
    config.analyzer.response_retries = 1
    responses = iter([
        chat_response({"groups": [[0, 1], list(range(2, 28))]}),
        chat_response({"scene_starts": [0, 2, 28]}),
    ])
    bodies = []

    def reply(*args, **kwargs):
        bodies.append(kwargs["json"])
        return next(responses)

    monkeypatch.setattr(adapters, "request", reply)
    monkeypatch.setattr(adapters.time, "sleep", lambda _: None)
    units = [{"start": i, "end": i + 1} for i in range(32)]
    groups = QwenAnalyzer(config).group_scenes(units)
    assert groups[-1] == [28, 29, 30, 31]
    prompt = bodies[1]["messages"][0]["content"]
    assert "缺失索引=[28, 29, 30, 31]" in prompt
    assert "重复索引=[]，越界索引=[]" in prompt
    assert "首个不匹配位置=28" in prompt


def test_legacy_reordered_scene_groups_report_first_mismatch():
    units = [{"start": i, "end": i + 1} for i in range(3)]
    with pytest.raises(ModelError, match="首个不匹配位置=1"):
        QwenAnalyzer._validate_scene_groups({"groups": [[0, 2], [1]]}, units)


def test_empty_scene_grouping_does_not_call_model(config, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("空输入不应调用模型")

    monkeypatch.setattr(adapters, "request", unexpected)
    assert QwenAnalyzer(config).group_scenes([]) == []
