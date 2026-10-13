from __future__ import annotations

import base64
import json

import pytest
from conftest import FixtureStoryboarder

from mindex import studio, studio_steps
from mindex.adapters import ModelError, QwenStoryboarder, decode_audio
from mindex.types import ShotDraft


def chat_payload(content: str, finish_reason: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def test_fixture_storyboarder_is_deterministic(config):
    storyboarder = FixtureStoryboarder(config)
    drafts = storyboarder.split("夜色沉了下去。远处有火车经过，铁轨在黑暗里发亮。她关上灯。")
    assert 2 <= len(drafts) <= 5
    assert storyboarder.calls == 1
    assert all(draft.text and draft.query.endswith("画面") and draft.estimated_seconds > 0 for draft in drafts)


def test_run_split_assigns_ids_and_writes_storyboard(config):
    text = "山风吹过山谷。远处的寺庙亮着灯。有人推门进来，抖落一身雪。"
    job_id = studio.create_job(config, text)["id"]
    result = studio_steps.run_step(config, job_id, "split", storyboarder=FixtureStoryboarder(config))
    assert result["shots"] >= 2
    assert result["coverage_warning"] is None
    storyboard = studio.load_storyboard(config, job_id)
    assert storyboard.shots[0].id == "shot-01"
    assert [shot.id for shot in storyboard.shots] == [
        f"shot-{index:02d}" for index in range(1, len(storyboard.shots) + 1)
    ]
    assert studio.load_job(config, job_id)["steps"]["split"]["status"] == "complete"


def test_run_split_rejects_too_many_shots(config):
    config.studio.max_shots = 2
    job_id = studio.create_job(config, "第一句很长的结束。第二句也很长的结束。第三句依旧很长的结束。")["id"]
    with pytest.raises(ModelError, match="上限"):
        studio_steps.run_step(config, job_id, "split", storyboarder=FixtureStoryboarder(config))
    info = studio.load_job(config, job_id)["steps"]["split"]
    assert info["status"] == "failed" and "上限" in info["error"]


def test_coverage_warning_for_divergent_text(config):
    class Divergent:
        def split(self, text: str) -> list[ShotDraft]:
            return [ShotDraft(text="完全无关的替换内容", query="无关查询", estimated_seconds=3.0)]

    job_id = studio.create_job(config, "原始的中文文案内容。")["id"]
    result = studio_steps.run_step(config, job_id, "split", storyboarder=Divergent())
    assert result["coverage_warning"] is not None
    assert studio.load_storyboard(config, job_id).coverage_warning == result["coverage_warning"]


def test_qwen_storyboarder_retries_invalid_output_with_feedback(config, monkeypatch):
    good = chat_payload(
        '```json\n{"shots":[{"text":"山风掠过山谷。","query":"山峦 空镜 自然 风吹草动",'
        '"estimated_seconds":2.0}]}\n```'
    )
    responses = iter([chat_payload("这不是 JSON"), good])
    messages = []

    def fake_request(method, url, **kwargs):
        messages.append(kwargs["json"]["messages"])
        return next(responses)

    monkeypatch.setattr("mindex.adapters.request", fake_request)
    drafts = QwenStoryboarder(config).split("山风掠过山谷。")
    assert len(messages) == 2
    feedback = [part["text"] for part in messages[1][-1]["content"] if isinstance(part, dict)]
    assert any("未通过校验" in text for text in feedback)  # 重试时附上了错误反馈
    assert drafts[0].text == "山风掠过山谷。"


def test_qwen_storyboarder_fails_after_exhausted_retries(config, monkeypatch):
    monkeypatch.setattr("mindex.adapters.request", lambda *args, **kwargs: chat_payload("坏输出"))
    with pytest.raises(ModelError, match="未通过校验"):
        QwenStoryboarder(config).split("任意文本。")


def test_qwen_storyboarder_rejects_truncated_output(config, monkeypatch):
    monkeypatch.setattr(
        "mindex.adapters.request", lambda *args, **kwargs: chat_payload("{", finish_reason="length")
    )
    with pytest.raises(ModelError, match="截断"):
        QwenStoryboarder(config).split("任意文本。")


def test_qwen_storyboarder_rejects_empty_content(config, monkeypatch):
    monkeypatch.setattr("mindex.adapters.request", lambda *args, **kwargs: chat_payload("  "))
    with pytest.raises(ModelError, match="空内容"):
        QwenStoryboarder(config).split("任意文本。")


def test_decode_audio_handles_binary_and_json_base64():
    assert decode_audio(b"RIFFxxxx") == b"RIFFxxxx"
    assert decode_audio(b"ID3tag") == b"ID3tag"
    assert decode_audio(b"\xff\xfb\x90\x00") == b"\xff\xfb\x90\x00"
    encoded = json.dumps({"data": base64.b64encode(b"RIFFpayload").decode()}).encode()
    assert decode_audio(encoded) == b"RIFFpayload"
    with pytest.raises(ModelError):
        decode_audio(b"plain garbage text")
    with pytest.raises(ModelError):
        decode_audio(json.dumps({"no_field": 1}).encode())
