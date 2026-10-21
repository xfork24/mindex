from __future__ import annotations

import base64
import json

import pytest
from conftest import FixtureStoryboarder

from mindex.models.adapters import ModelError, QwenStoryboarder, decode_audio
from mindex.shared.types import ShotDraft
from mindex.studio import studio, studio_steps


def chat_payload(content: str, finish_reason: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def test_fixture_storyboarder_is_deterministic(config):
    storyboarder = FixtureStoryboarder(config)
    drafts = storyboarder.split("夜色沉了下去。远处有火车经过，铁轨在黑暗里发亮。她关上灯。")
    assert 2 <= len(drafts) <= 5
    assert storyboarder.calls == 1
    assert all(
        draft.text and draft.query.endswith("画面") and draft.estimated_seconds > 0 for draft in drafts
    )


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


def test_prompt_version_invalidates_split_checkpoint(config, monkeypatch):
    generator = FixtureStoryboarder(config)
    job_id = studio.create_job(config, "山风吹过山谷。远处的寺庙亮着灯。")["id"]
    studio_steps.run_step(config, job_id, "split", storyboarder=generator)
    checkpoints = studio.job_dir(config, job_id) / "checkpoints.json"
    old_output = json.loads(checkpoints.read_text())["storyboard.json"]["path"]
    studio_steps.run_step(config, job_id, "split", storyboarder=generator)
    assert generator.calls == 1

    monkeypatch.setattr(studio, "SPLIT_VERSION", studio.SPLIT_VERSION + 1)
    assert studio.reconcile(config, job_id)["steps"]["split"]["status"] == "pending"
    studio_steps.run_step(config, job_id, "split", storyboarder=generator)
    assert generator.calls == 2
    assert json.loads(checkpoints.read_text())["storyboard.json"]["path"] != old_output
    assert (studio.job_dir(config, job_id) / old_output).is_file()


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
    with pytest.raises(ModelError, match="覆盖原文"):
        studio_steps.run_step(config, job_id, "split", storyboarder=Divergent())
    assert studio.load_job(config, job_id)["steps"]["split"]["status"] == "failed"


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

    monkeypatch.setattr("mindex.models.adapters.request", fake_request)
    drafts = QwenStoryboarder(config).split("山风掠过山谷。")
    assert len(messages) == 2
    feedback = [part["text"] for part in messages[1][-1]["content"] if isinstance(part, dict)]
    assert any("未通过校验" in text for text in feedback)  # 重试时附上了错误反馈
    assert drafts[0].text == "山风掠过山谷。"


def test_qwen_storyboarder_fails_after_exhausted_retries(config, monkeypatch):
    monkeypatch.setattr("mindex.models.adapters.request", lambda *args, **kwargs: chat_payload("坏输出"))
    with pytest.raises(ModelError, match="未通过校验"):
        QwenStoryboarder(config).split("任意文本。")


def test_qwen_storyboarder_rejects_truncated_output(config, monkeypatch):
    monkeypatch.setattr(
        "mindex.models.adapters.request", lambda *args, **kwargs: chat_payload("{", finish_reason="length")
    )
    with pytest.raises(ModelError, match="截断"):
        QwenStoryboarder(config).split("任意文本。")


def test_qwen_storyboarder_rejects_empty_content(config, monkeypatch):
    monkeypatch.setattr("mindex.models.adapters.request", lambda *args, **kwargs: chat_payload("  "))
    with pytest.raises(ModelError, match="空内容"):
        QwenStoryboarder(config).split("任意文本。")


def test_split_prompt_preserves_narration_and_requires_chinese_keywords(config, monkeypatch):
    """旁白保留原语言，检索词统一为基于该分镜的少量中文词汇。"""
    systems = []
    draft = {
        "shots": [
            {
                "text": "The wind crossed the valley.",
                "query": "山风 山谷",
                "estimated_seconds": 5.0,
            }
        ]
    }

    def fake_request(method, url, **kwargs):
        systems.append(kwargs["json"]["messages"][0]["content"])
        return chat_payload(json.dumps(draft))

    monkeypatch.setattr("mindex.models.adapters.request", fake_request)
    drafts = QwenStoryboarder(config).split("The wind crossed the valley.")
    assert drafts[0].text == "The wind crossed the valley."
    system = systems[0]
    assert "与原文相同的语言" in system
    assert "query 必须使用中文，与原文语言无关" in system
    assert "2–5 个中文词汇，以空格分隔" in system
    assert "樟宜机场 旅行 室内" in system
    assert drafts[0].query == "山风 山谷"
    assert "自然中文描述" not in system
    assert "中文解说类" not in system


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


@pytest.mark.parametrize(
    "parts", [["第一句。"], ["第二句。", "第一句。"], ["第一句。", "第一句。", "第二句。"]]
)
def test_coverage_detects_omission_order_and_duplication(parts):
    assert studio_steps.coverage_warning("第一句。第二句。", parts)
    assert studio_steps.coverage_warning("第一句。\n 第二句。", ["第一句。", "第二句。"]) is None


def test_qwen_retries_missing_text(config, monkeypatch):
    outputs = iter(
        [
            {"shots": [{"text": "第一句。", "query": "画面", "estimated_seconds": 1}]},
            {"shots": [{"text": "第一句。第二句。", "query": "画面", "estimated_seconds": 2}]},
        ]
    )
    calls = []

    def response(*args, **kwargs):
        calls.append(kwargs["json"])
        return chat_payload(json.dumps(next(outputs), ensure_ascii=False))

    monkeypatch.setattr("mindex.models.adapters.request", response)
    drafts = QwenStoryboarder(config).split("第一句。第二句。")
    assert len(calls) == 2
    assert drafts[0].text == "第一句。第二句。"
