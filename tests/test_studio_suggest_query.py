"""QwenStoryboarder.suggest_query 与 FixtureStoryboarder.suggest_query 的示例测试（需求 9.5、10.1）。"""

from __future__ import annotations

import json

import pytest
from conftest import FixtureStoryboarder

from mindex.models.adapters import ModelError
from mindex.models.storyboard import QUERY_RULES, VOCABULARY_HEADER, VOCABULARY_RULE, QwenStoryboarder


def chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def _capture(monkeypatch, content: str) -> list[dict]:
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append(kwargs["json"])
        return chat_payload(content)

    monkeypatch.setattr("mindex.models.adapters.request", fake_request)
    return calls


def _texts(message) -> list[str]:
    content = message["content"]
    if isinstance(content, str):
        return [content]
    return [part["text"] for part in content if isinstance(part, dict) and "text" in part]


def test_suggest_query_sends_json_context_and_vocabulary_once(config, monkeypatch):
    calls = _capture(monkeypatch, '```json\n{"query":"  夜市   摊位 人群 "}\n```')
    query = QwenStoryboarder(config).suggest_query(
        "to bustling night markets", "人头攒动的夜市摊位", ("From trails", "and noodles,"), ["夜市", "摊位"]
    )
    assert query == "夜市 摊位 人群"
    assert len(calls) == 1  # 只调用一次，不重试
    messages = calls[0]["messages"]
    system = "\n".join(_texts(messages[0]))
    assert QUERY_RULES in system and VOCABULARY_RULE in system
    user = _texts(messages[-1])
    assert json.loads(user[0]) == {
        "text": "to bustling night markets", "visual": "人头攒动的夜市摊位",
        "previous": "From trails", "next": "and noodles,",
    }
    assert "人头攒动" in user[0]  # ensure_ascii=False
    assert VOCABULARY_HEADER + "夜市、摊位" in user


@pytest.mark.parametrize("vocabulary", [None, []])
def test_suggest_query_without_vocabulary_has_no_vocabulary_segment(config, monkeypatch, vocabulary):
    calls = _capture(monkeypatch, '{"query":"湖畔 村庄"}')
    QwenStoryboarder(config).suggest_query("and quiet lakeside villages", "湖边村落", ("", ""), vocabulary)
    messages = calls[0]["messages"]
    assert VOCABULARY_RULE not in "\n".join(_texts(messages[0]))
    assert QUERY_RULES in "\n".join(_texts(messages[0]))
    assert not any(VOCABULARY_HEADER in text for text in _texts(messages[-1]))


@pytest.mark.parametrize("content", ['{"query":"   "}', '{"other":1}', '["夜市"]'])
def test_suggest_query_empty_result_raises(config, monkeypatch, content):
    _capture(monkeypatch, content)
    with pytest.raises(ModelError, match="空召回词"):
        QwenStoryboarder(config).suggest_query("text", "画面", ("", ""), None)


def test_suggest_query_invalid_json_raises_model_error(config, monkeypatch):
    _capture(monkeypatch, "不是 JSON")
    with pytest.raises(ModelError, match="召回词重写"):
        QwenStoryboarder(config).suggest_query("text", "画面", ("", ""), None)


def test_suggest_query_clips_to_400_chars(config, monkeypatch):
    _capture(monkeypatch, json.dumps({"query": "词" * 500}, ensure_ascii=False))
    assert len(QwenStoryboarder(config).suggest_query("text", "画面", ("", ""), None)) == 400


def test_fixture_suggest_query_is_deterministic_and_recorded(config):
    fixture = FixtureStoryboarder(config)
    args = ("text here", "湖边 安静的 村落", ("前", "后"), ["湖畔"])
    first = fixture.suggest_query(*args)
    assert first == fixture.suggest_query(*args) == "湖边 安静的 画面"
    assert fixture.suggestions[0] == {
        "text": "text here", "visual": "湖边 安静的 村落", "context": ("前", "后"), "vocabulary": ["湖畔"],
    }
    assert fixture.suggest_query("t", "", ("", ""), None)  # 永远非空
