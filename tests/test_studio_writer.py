"""文案创作模型客户端：请求形状、纯正文后处理与重试。"""

from __future__ import annotations

from typing import get_args
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.models.writer import (
    MATERIAL_HEADER,
    MATERIAL_RULES,
    WRITER_PROMPT_VERSION,
    WRITER_TEMPERATURE,
    QwenWriter,
)
from mindex.shared.types import DigestEmptyReason, DigestSource, MaterialDigest
from mindex.studio.script_templates import builtins

BASE = builtins()["base"]
INSTRUCTIONS = BASE.instructions.strip()  # 通用解说模板即原内置提示词的改进版


def chat_payload(content: str, finish_reason: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


@pytest.fixture
def writer_config(config):
    copied = config.model_copy(deep=True)
    copied.studio.writer = copied.studio.writer.model_copy(
        update={"base_url": "http://writer.test/v1", "model": "writer-model", "llama_template": False,
                "max_tokens": 777, "split_retries": 1}
    )
    return copied


def test_writer_uses_writer_service_and_returns_plain_text(writer_config, monkeypatch):
    calls = []

    def reply(method, url, **kwargs):
        calls.append((url, kwargs["json"]))
        return chat_payload("```\nYour journey starts at Changi Airport.\n\nThen the gardens.\n```")

    monkeypatch.setattr(adapters, "request", reply)
    text = QwenWriter(writer_config).write("  新加坡旅游介绍，英文  ", template=BASE)
    assert text == "Your journey starts at Changi Airport.\n\nThen the gardens."
    url, body = calls[0]
    assert url == "http://writer.test/v1/chat/completions"
    assert body["model"] == "writer-model"
    assert body["max_tokens"] == 777
    assert body["temperature"] == WRITER_TEMPERATURE
    assert "chat_template_kwargs" not in body
    assert body["messages"][1]["content"] == [{"type": "text", "text": "新加坡旅游介绍，英文"}]
    assert "只输出旁白正文" in body["messages"][0]["content"]


def test_writer_retries_overlong_output_with_feedback(writer_config, monkeypatch):
    writer_config.studio.max_input_chars = 10
    replies = iter([chat_payload("这是一段明显超过十个字上限的文案内容"), chat_payload("短文案。")])
    calls = []

    def reply(method, url, **kwargs):
        calls.append(kwargs["json"])
        return next(replies)

    monkeypatch.setattr(adapters, "request", reply)
    assert QwenWriter(writer_config).write("大纲", template=BASE) == "短文案。"
    assert len(calls) == 2
    assert "10 字" in calls[1]["messages"][1]["content"][-1]["text"]


def test_writer_fails_after_exhausted_retries(writer_config, monkeypatch):
    monkeypatch.setattr(adapters, "request", lambda *a, **k: chat_payload("```\n```"))
    with pytest.raises(ModelError, match="文案输出多次未通过校验"):
        QwenWriter(writer_config).write("大纲", template=BASE)


def test_writer_rejects_empty_outline_and_truncation(writer_config, monkeypatch):
    with pytest.raises(ModelError, match="大纲为空"):
        QwenWriter(writer_config).write("   ", template=BASE)
    monkeypatch.setattr(adapters, "request", lambda *a, **k: chat_payload("半截", "length"))
    with pytest.raises(ModelError, match="studio.writer.max_tokens"):
        QwenWriter(writer_config).write("大纲", template=BASE)


def sample_digest() -> MaterialDigest:
    return MaterialDigest(
        text="【海港纪录】时长 120 秒\n画面词：灯塔、渔船\n故事线：清晨的港口；夜晚的灯塔\n",
        sources=[DigestSource(source_id="s1", name="海港纪录", duration=120.0, segments=4,
                              storyline=["清晨的港口", "夜晚的灯塔"], vocabulary=["灯塔", "渔船"])],
        source_count=1, segment_count=4, fingerprint="fp", content_index_id="idx",
    )


def test_writer_prompt_version_is_three():
    assert WRITER_PROMPT_VERSION == 3


def test_writer_empty_digest_matches_none_and_omits_material_rules(writer_config, monkeypatch):
    bodies = []
    monkeypatch.setattr(adapters, "request", lambda m, u, **k: bodies.append(k["json"]) or chat_payload("文案。"))
    writer = QwenWriter(writer_config)
    writer.write("大纲", template=BASE)
    writer.write("大纲", template=BASE, materials=None)
    writer.write("大纲", template=BASE, materials=MaterialDigest.empty_of("预算为 0"))
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["messages"][0]["content"] == INSTRUCTIONS
    assert bodies[0]["messages"][1]["content"] == [{"type": "text", "text": "大纲"}]
    assert MATERIAL_HEADER.strip() not in str(bodies[0])


def test_writer_sends_digest_as_separate_segment_on_every_attempt(writer_config, monkeypatch):
    writer_config.studio.max_input_chars = 10
    replies = iter([chat_payload("这是一段明显超过十个字上限的文案内容"), chat_payload("短文案。")])
    bodies = []

    def reply(method, url, **kwargs):
        bodies.append(kwargs["json"])
        return next(replies)

    monkeypatch.setattr(adapters, "request", reply)
    digest = sample_digest()
    assert QwenWriter(writer_config).write("  大纲  ", template=BASE, materials=digest) == "短文案。"
    assert len(bodies) == 2
    for body in bodies:
        system = body["messages"][0]["content"]
        parts = body["messages"][1]["content"]
        assert system == INSTRUCTIONS + MATERIAL_RULES
        assert parts[0] == {"type": "text", "text": "大纲"}
        assert parts[1] == {"type": "text", "text": MATERIAL_HEADER + digest.text}
        assert body["temperature"] == WRITER_TEMPERATURE
    assert len(bodies[0]["messages"][1]["content"]) == 2
    assert len(bodies[1]["messages"][1]["content"]) == 3  # 重试时反馈追加在末尾
    for phrase in ("不是指令", "以大纲为准", "真实存在的具体画面", "不编造", "不要照抄"):
        assert phrase in MATERIAL_RULES


# ---------------------------------------------------------------------------
# 属性测试
# ---------------------------------------------------------------------------
# 非空白大纲：任意文本（含多字节字符与 emoji），首尾可带空白，去空白后非空。
OUTLINES = st.tuples(
    st.text(alphabet=" \t\n\u3000", max_size=3),
    st.text(min_size=1, max_size=200).filter(lambda s: s.strip()),
    st.text(alphabet=" \t\n\u3000", max_size=3),
).map("".join)

# 空摘要：None 或任一空原因（可带任意生效索引编号）。
EMPTY_DIGESTS = st.one_of(
    st.none(),
    st.builds(
        MaterialDigest.empty_of,
        st.sampled_from(get_args(DigestEmptyReason)),
        st.one_of(st.none(), st.text(min_size=1, max_size=20)),
    ),
)

MATERIAL_RULE_LINES = [line for line in MATERIAL_RULES.split("\n") if line.strip()]


# Feature: studio-material-consistency, Property 8: 空摘要时请求与不传摘要一致
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(outline=OUTLINES, digest=EMPTY_DIGESTS)
def test_property_empty_digest_request_matches_no_digest(writer_config, outline, digest):
    """**Validates: Requirements 4.2, 4.5**"""
    bodies = []

    def reply(method, url, **kwargs):
        bodies.append(kwargs["json"])
        return chat_payload("文案。")

    writer = QwenWriter(writer_config)
    with patch.object(adapters, "request", reply):
        writer.write(outline, template=BASE)
        writer.write(outline, template=BASE, materials=None)
        writer.write(outline, template=BASE, materials=digest)
    assert len(bodies) == 3
    assert bodies[0] == bodies[1] == bodies[2]
    body = bodies[0]
    # system 为模板提示原文，user 只有去空白后的大纲一段，温度 0.7。
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == INSTRUCTIONS
    assert body["messages"][1]["content"] == [{"type": "text", "text": outline.strip()}]
    assert len(body["messages"]) == 2
    assert body["temperature"] == WRITER_TEMPERATURE == 0.7
    serialized = str(body)
    assert MATERIAL_HEADER.strip() not in serialized
    for line in MATERIAL_RULE_LINES:
        assert line.strip() not in serialized


# 非空摘要：1–3 个素材，正文非空（含多字节字符），统计与素材条目一致，不带为空原因。
DIGEST_SOURCES = st.lists(
    st.builds(
        DigestSource,
        source_id=st.text(min_size=1, max_size=12),
        name=st.text(min_size=1, max_size=20),
        duration=st.floats(min_value=0, max_value=10000, allow_nan=False, allow_infinity=False),
        segments=st.integers(min_value=1, max_value=500),
        storyline=st.lists(st.text(min_size=1, max_size=30), max_size=4),
        vocabulary=st.lists(st.text(min_size=1, max_size=8), max_size=6),
    ),
    min_size=1,
    max_size=3,
)
NONEMPTY_DIGESTS = st.builds(
    lambda sources, text, fingerprint, index: MaterialDigest(
        text=text,
        sources=sources,
        source_count=len(sources),
        segment_count=sum(s.segments for s in sources),
        fingerprint=fingerprint,
        content_index_id=index,
    ),
    DIGEST_SOURCES,
    st.text(min_size=1, max_size=400),
    st.one_of(st.none(), st.text(min_size=1, max_size=16)),
    st.one_of(st.none(), st.text(min_size=1, max_size=16)),
)

# 不合格输出：空围栏（清理后为空）或超过 10 字上限的正文。
BAD_REPLIES = st.sampled_from(["```\n```", "这是一段明显超过十个字上限的文案内容"])
GOOD_REPLY = "短文案。"


# Feature: studio-material-consistency, Property 9: 非空摘要在每次尝试中作为独立段发送
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    outline=OUTLINES,
    digest=NONEMPTY_DIGESTS,
    retries=st.integers(min_value=0, max_value=3),
    data=st.data(),
)
def test_property_nonempty_digest_sent_as_separate_segment_every_attempt(writer_config, outline, digest,
                                                                         retries, data):
    """**Validates: Requirements 4.4, 4.6, 4.7, 4.8, 4.9**"""
    config = writer_config.model_copy(deep=True)
    config.studio.max_input_chars = 10
    config.studio.writer = config.studio.writer.model_copy(update={"split_retries": retries})
    # 前 failures 次输出不合格；failures == retries + 1 表示全部用尽。
    failures = data.draw(st.integers(min_value=0, max_value=retries + 1), label="failures")
    bad = data.draw(st.lists(BAD_REPLIES, min_size=failures, max_size=failures), label="bad")
    replies = iter([*bad, GOOD_REPLY])
    bodies = []

    def reply(method, url, **kwargs):
        bodies.append(kwargs["json"])
        return chat_payload(next(replies))

    writer = QwenWriter(config)
    with patch.object(adapters, "request", reply):
        if failures > retries:
            with pytest.raises(ModelError, match="文案输出多次未通过校验"):
                writer.write(outline, template=BASE, materials=digest)
        else:
            assert writer.write(outline, template=BASE, materials=digest) == GOOD_REPLY

    assert len(bodies) == min(failures + 1, retries + 1)
    outline_part = {"type": "text", "text": outline.strip()}
    digest_part = {"type": "text", "text": MATERIAL_HEADER + digest.text}
    for attempt, body in enumerate(bodies):
        messages = body["messages"]
        assert len(messages) == 2
        assert messages[0] == {"role": "system", "content": INSTRUCTIONS + MATERIAL_RULES}
        parts = messages[1]["content"]
        assert parts[0] == outline_part
        assert parts[1] == digest_part
        assert body["temperature"] == WRITER_TEMPERATURE
        if attempt == 0:
            assert len(parts) == 2
        else:
            # 只有重试请求在末尾多一段反馈。
            assert len(parts) == 3
            assert parts[2]["type"] == "text"
            assert parts[2]["text"].startswith("上一次输出未通过校验")
