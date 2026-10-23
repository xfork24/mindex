"""文案创作模型客户端：请求形状、纯正文后处理与重试。"""

from __future__ import annotations

import pytest

from mindex.models import adapters
from mindex.models.adapters import ModelError
from mindex.models.writer import WRITER_TEMPERATURE, QwenWriter


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
    text = QwenWriter(writer_config).write("  新加坡旅游介绍，英文  ")
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
    assert QwenWriter(writer_config).write("大纲") == "短文案。"
    assert len(calls) == 2
    assert "10 字" in calls[1]["messages"][1]["content"][-1]["text"]


def test_writer_fails_after_exhausted_retries(writer_config, monkeypatch):
    monkeypatch.setattr(adapters, "request", lambda *a, **k: chat_payload("```\n```"))
    with pytest.raises(ModelError, match="文案输出多次未通过校验"):
        QwenWriter(writer_config).write("大纲")


def test_writer_rejects_empty_outline_and_truncation(writer_config, monkeypatch):
    with pytest.raises(ModelError, match="大纲为空"):
        QwenWriter(writer_config).write("   ")
    monkeypatch.setattr(adapters, "request", lambda *a, **k: chat_payload("半截", "length"))
    with pytest.raises(ModelError, match="studio.writer.max_tokens"):
        QwenWriter(writer_config).write("大纲")
