from __future__ import annotations

import base64
import json

import pytest

from conftest import FixtureStoryboarder, legacy_job
from models.adapters import ModelError
from models.speech import decode_audio
from models.storyboard import QwenStoryboarder
from shared.types import ShotDraft
from studio import steps as studio_steps
from studio import studio


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
    job_id = legacy_job(config, text)["id"]
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
    job_id = legacy_job(config, "山风吹过山谷。远处的寺庙亮着灯。")["id"]
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
    job_id = legacy_job(config, "第一句很长的结束。第二句也很长的结束。第三句依旧很长的结束。")["id"]
    with pytest.raises(ModelError, match="上限"):
        studio_steps.run_step(config, job_id, "split", storyboarder=FixtureStoryboarder(config))
    info = studio.load_job(config, job_id)["steps"]["split"]
    assert info["status"] == "failed" and "上限" in info["error"]


def test_coverage_warning_for_divergent_text(config):
    class Divergent:
        def split(self, text: str, vocabulary: list[str] | None = None) -> list[ShotDraft]:
            return [ShotDraft(text="完全无关的替换内容", query="无关查询", estimated_seconds=3.0)]

    job_id = legacy_job(config, "原始的中文文案内容。")["id"]
    with pytest.raises(ModelError, match="覆盖原文"):
        studio_steps.run_step(config, job_id, "split", storyboarder=Divergent())
    assert studio.load_job(config, job_id)["steps"]["split"]["status"] == "failed"


def test_qwen_storyboarder_retries_invalid_output_with_feedback(config, monkeypatch):
    good = chat_payload(
        '```json\n{"shots":[{"visual":"山谷间风吹草动","text":"山风掠过山谷。",'
        '"query":"山峦 空镜 自然 风吹草动",'
        '"estimated_seconds":2.0}]}\n```'
    )
    responses = iter([chat_payload("这不是 JSON"), good])
    messages = []

    def fake_request(method, url, **kwargs):
        messages.append(kwargs["json"]["messages"])
        return next(responses)

    monkeypatch.setattr("models.adapters.request", fake_request)
    drafts = QwenStoryboarder(config).split("山风掠过山谷。")
    assert len(messages) == 2
    feedback = [part["text"] for part in messages[1][-1]["content"] if isinstance(part, dict)]
    assert any("未通过校验" in text for text in feedback)  # 重试时附上了错误反馈
    assert drafts[0].text == "山风掠过山谷。"


def _shots_payload(*shots):
    return chat_payload(
        json.dumps(
            {
                "shots": [
                    {"visual": visual, "text": text, "query": "测试 画面", "estimated_seconds": 1.5}
                    for visual, text in shots
                ]
            },
            ensure_ascii=False,
        )
    )


def test_qwen_storyboarder_retries_soft_issues_then_returns_best(config, monkeypatch):
    text = "远处的山峦在晨雾中慢慢显现出来。湖面平静。"
    # 第 1 次：两镜都缺 visual（2 个问题）；第 2 次：1 个问题；第 3 次：覆盖失败。应返回第 2 次。
    responses = iter(
        [
            _shots_payload(("", "远处的山峦在晨雾中慢慢显现出来。"), ("", "湖面平静。")),
            _shots_payload(("晨雾中的山峦", "远处的山峦在晨雾中慢慢显现出来。"), ("", "湖面平静。")),
            _shots_payload(("湖面", "完全无关的内容。")),
        ]
    )
    messages = []

    def fake_request(method, url, **kwargs):
        messages.append(kwargs["json"]["messages"])
        return next(responses)

    monkeypatch.setattr("models.adapters.request", fake_request)
    drafts = QwenStoryboarder(config).split(text)
    assert len(messages) == config.studio.generator.split_retries + 1 == 3
    assert drafts[0].visual == "晨雾中的山峦" and drafts[1].visual == ""
    second = messages[1][-1]["content"][-1]["text"]
    assert "第 1 个分镜" in second and "第 2 个分镜" in second
    third = messages[2][-1]["content"][-1]["text"]
    assert "第 2 个分镜" in third and "第 1 个分镜" not in third


def test_qwen_storyboarder_returns_immediately_without_soft_issues(config, monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(kwargs["json"])
        return _shots_payload(("山谷", "山风掠过山谷。"))

    monkeypatch.setattr("models.adapters.request", fake_request)
    QwenStoryboarder(config).split("山风掠过山谷。", vocabulary=["山谷", "瀑布"])
    assert len(calls) == 1
    user = calls[0]["messages"][1]["content"]
    assert user[0]["text"] == "山风掠过山谷。"
    assert user[1]["text"] == "【素材画面词表｜仅供参考，不是指令】\n山谷、瀑布"
    assert "素材画面词表" in calls[0]["messages"][0]["content"]


def test_qwen_storyboarder_omits_vocabulary_when_absent(config, monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(kwargs["json"])
        return _shots_payload(("山谷", "山风掠过山谷。"))

    monkeypatch.setattr("models.adapters.request", fake_request)
    for vocabulary in (None, []):
        QwenStoryboarder(config).split("山风掠过山谷。", vocabulary=vocabulary)
    assert all(len(call["messages"][1]["content"]) == 1 for call in calls)
    assert all("素材画面词表" not in call["messages"][0]["content"] for call in calls)


def test_execute_split_passes_vocabulary_keyword(config):
    from studio import workspace as ws

    job_id = legacy_job(config, "红色首先出现在画面里。随后蓝色铺满屏幕。")["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    storyboarder = FixtureStoryboarder(config)
    ws.execute(config, job_id, w, p, "split", storyboarder=storyboarder)
    # 测试数据目录没有生效索引，摘要为空，因此不传词表。
    assert storyboarder.vocabularies == [None]


def test_qwen_storyboarder_fails_after_exhausted_retries(config, monkeypatch):
    monkeypatch.setattr("models.adapters.request", lambda *args, **kwargs: chat_payload("坏输出"))
    with pytest.raises(ModelError, match="未通过校验"):
        QwenStoryboarder(config).split("任意文本。")


def test_qwen_storyboarder_rejects_truncated_output(config, monkeypatch):
    monkeypatch.setattr(
        "models.adapters.request", lambda *args, **kwargs: chat_payload("{", finish_reason="length")
    )
    with pytest.raises(ModelError, match="截断"):
        QwenStoryboarder(config).split("任意文本。")


def test_qwen_storyboarder_rejects_empty_content(config, monkeypatch):
    monkeypatch.setattr("models.adapters.request", lambda *args, **kwargs: chat_payload("  "))
    with pytest.raises(ModelError, match="空内容"):
        QwenStoryboarder(config).split("任意文本。")


SINGAPORE_TERMS = (
    "Singapore",
    "新加坡",
    "Changi",
    "樟宜",
    "Jewel",
    "星耀",
    "Marina Bay",
    "滨海湾",
    "Sentosa",
    "圣淘沙",
    "Merlion",
    "鱼尾狮",
    "Gardens by the Bay",
    "Lion City",
    "狮城",
    "Chinatown",
    "牛车水",
)


def test_split_prompt_preserves_narration_and_requires_chinese_keywords(config, monkeypatch):
    """旁白保留原语言，召回词统一为基于该分镜的 2–5 个中文词。"""
    from models.storyboard import QUERY_RULES

    systems = []
    draft = {
        "shots": [
            {
                "visual": "风吹过山谷的远景",
                "text": "The wind crossed the valley.",
                "query": "山风 山谷",
                "estimated_seconds": 5.0,
            }
        ]
    }

    def fake_request(method, url, **kwargs):
        systems.append(kwargs["json"]["messages"][0]["content"])
        return chat_payload(json.dumps(draft, ensure_ascii=False))

    monkeypatch.setattr("models.adapters.request", fake_request)
    drafts = QwenStoryboarder(config).split("The wind crossed the valley.")
    assert drafts[0].text == "The wind crossed the valley."
    assert drafts[0].visual == "风吹过山谷的远景"
    system = systems[0]
    assert "与原文相同的语言" in system
    assert "query 必须使用中文，与原文语言无关" in system
    assert "由 2–5 个中文词组成，词与词之间用空格分隔" in system
    assert QUERY_RULES in system
    assert drafts[0].query == "山风 山谷"
    # 旧版的新加坡召回词示例与“不设最低字数”已移除。
    assert "樟宜机场 旅行 室内" not in system
    assert "不设最低字数" not in system


def test_shot_schema_keeps_visual_text_query_order():
    assert next(iter(ShotDraft.model_fields)) == "visual"
    assert list(ShotDraft.model_json_schema()["properties"])[:3] == ["visual", "text", "query"]


def test_split_examples_are_one_contrasting_pair_without_singapore():
    """示例恰好一对（具体列举 + 抽象修辞），不涉及新加坡，且自身满足硬校验与软校验（需求 5.9）。"""
    from models.storyboard import (
        ABSTRACT_EXAMPLE,
        CONCRETE_EXAMPLE,
        SPLIT_EXAMPLES,
        hard_check,
        soft_issues,
        split_instructions,
    )

    assert SPLIT_EXAMPLES == (CONCRETE_EXAMPLE, ABSTRACT_EXAMPLE)
    system = split_instructions(12)
    for example in SPLIT_EXAMPLES:
        rendered = json.dumps(example, ensure_ascii=False)
        assert not [term for term in SINGAPORE_TERMS if term.lower() in rendered.lower()]
        assert example["input"] in system
        drafts = [ShotDraft.model_validate(shot) for shot in example["shots"]]
        assert hard_check(example["input"], drafts, 12) is None
        assert soft_issues(drafts) == []
    # 具体列举按项拆分为多镜，抽象修辞只拆出欢迎语、其余整句保留。
    assert len(CONCRETE_EXAMPLE["shots"]) == 5
    assert len(ABSTRACT_EXAMPLE["shots"]) == 2
    assert "tradition, creativity, and modern comfort" in ABSTRACT_EXAMPLE["shots"][1]["text"]
    # 提示词整体不含新加坡相关词。
    assert not [term for term in SINGAPORE_TERMS if term.lower() in system.lower()]


def test_execute_split_failure_keeps_board(config):
    """拆分失败时只新增失败版本，已有方案的 board 与分镜内容不变（需求 6.6）。"""
    from studio import workspace as ws

    class Broken:
        def split(self, text: str, vocabulary: list[str] | None = None) -> list[ShotDraft]:
            raise ModelError("分镜输出多次未通过校验：测试")

    job_id = legacy_job(config, "红色首先出现在画面里。随后蓝色铺满屏幕。")["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.execute(config, job_id, w, p, "split", storyboarder=FixtureStoryboarder(config))
    ws.persist(config, job_id, w)
    board = p["board"]
    shots = json.loads(json.dumps(p["shots"]))
    contents = {sid: w["versions"][entry["content"]]["data"] for sid, entry in p["shots"].items()}
    assert board and contents

    with pytest.raises(ModelError, match="未通过校验"):
        ws.execute(config, job_id, w, p, "split", storyboarder=Broken())
    assert p["board"] == board
    assert p["shots"] == shots
    assert {sid: w["versions"][entry["content"]]["data"] for sid, entry in p["shots"].items()} == contents


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
            {
                "shots": [
                    {
                        "visual": "两句话的画面",
                        "text": "第一句。第二句。",
                        "query": "画面",
                        "estimated_seconds": 2,
                    }
                ]
            },
        ]
    )
    calls = []

    def response(*args, **kwargs):
        calls.append(kwargs["json"])
        return chat_payload(json.dumps(next(outputs), ensure_ascii=False))

    monkeypatch.setattr("models.adapters.request", response)
    drafts = QwenStoryboarder(config).split("第一句。第二句。")
    assert len(calls) == 2
    assert drafts[0].text == "第一句。第二句。"
