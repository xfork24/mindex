"""动画生成 LLM 客户端：提示词版本、代码围栏剥离、静态校验与带反馈的重试、两种协议的请求形状。

所有请求都经 monkeypatch 的 ``adapters.request`` 桩返回，绝不调用真实模型。
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mindex.models import animator as module
from mindex.models.adapters import ModelError, make_animator
from mindex.models.animator import (
    ANIMATOR_PROMPT_VERSION,
    SYSTEM_PROMPT,
    AnimationRejected,
    LlmAnimator,
    animation_errors,
    extract_html,
    validate_animation,
)

CLEAN = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;overflow:hidden;background:#102030;font-family:sans-serif}
#bar{width:10px;height:40px;background:url(data:image/png;base64,iVBORw0KGgo=)}
</style></head><body><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>
<div id="bar"></div><img src="data:image/png;base64,iVBORw0KGgo=">
<script>
// 注释里的 // 不是协议相对地址
const bar = document.getElementById("bar");
function frame(t) { bar.style.width = Math.min(300, t / 10) + "px"; requestAnimationFrame(frame); }
requestAnimationFrame(frame);
const url = "http://不在属性里的文本";
</script></body></html>"""

FORBIDDEN_SNIPPETS = {
    '<img src="https://cdn.example.com/a.png">': "属性引用外部地址",
    "<img src='//cdn.example.com/a.png'>": "属性引用外部地址",
    '<a href="http://example.com">x</a>': "属性引用外部地址",
    "<style>div{background:url('https://x.example/b.png')}</style>": "CSS url()",
    "<style>@import 'x.css';</style>": "@import",
    "<script>img.src = 'https://x.example/a.png'</script>": "脚本把 src/href",
    "<script>fetch('/api/x')</script>": "fetch",
    "<script>new XMLHttpRequest()</script>": "XMLHttpRequest",
    "<script>new WebSocket('ws://x')</script>": "WebSocket",
    "<script>new EventSource('/e')</script>": "EventSource",
    "<script>navigator.sendBeacon('/b', 1)</script>": "sendBeacon",
    "<script>import('./m.js')</script>": "动态 import()",
    "<script>new Worker('w.js')</script>": "Worker",
    '<script src="a.js"></script>': "外部脚本",
    '<link rel="stylesheet" href="a.css">': "<link>",
    '<iframe srcdoc="x"></iframe>': "<iframe>",
    '<object data="x.swf"></object>': "<object>",
    '<embed type="x">': "<embed>",
    '<meta http-equiv="refresh" content="0">': "http-equiv=refresh",
    "<script>window.open('/x')</script>": "window.open",
    "<script>localStorage.setItem('a', 1)</script>": "localStorage",
    "<script>indexedDB.open('x')</script>": "indexedDB",
    "<script>document.cookie = 'a=1'</script>": "document.cookie",
}


def test_prompt_version_and_rules():
    assert ANIMATOR_PROMPT_VERSION == 1
    for rule in ("自包含", "requestAnimationFrame", "Math.random", "sans-serif", "fetch", "overflow: hidden"):
        assert rule in SYSTEM_PROMPT


@pytest.mark.parametrize("text", [
    "```html\n" + CLEAN + "\n```",
    "好的，下面是动画：\n```\n" + CLEAN + "\n```\n希望满意。",
    "说明文字\n" + CLEAN + "\n以上。",
    CLEAN,
])
def test_extract_html_strips_fences_and_chatter(text):
    assert extract_html(text) == CLEAN


def test_clean_document_passes():
    assert animation_errors(CLEAN, 200) == []
    assert validate_animation(CLEAN, 200) == CLEAN


@pytest.mark.parametrize("snippet", sorted(FORBIDDEN_SNIPPETS))
def test_static_validation_rejects_each_forbidden_construct(snippet):
    html = CLEAN.replace("</body>", snippet + "</body>")
    errors = animation_errors(html, 200)
    assert len(errors) == 1 and FORBIDDEN_SNIPPETS[snippet] in errors[0], errors
    with pytest.raises(AnimationRejected, match="动画源码未通过安全检查"):
        validate_animation(html, 200)


def test_size_limit_and_document_shape():
    big = CLEAN.replace("</body>", "<p>" + "字" * 2000 + "</p></body>")
    assert any("超过 1 KB 上限" in error for error in animation_errors(big, 1))
    assert animation_errors("<div>只有片段</div>", 200) == ["不是完整的 HTML 文档（缺少 <html> 或 <body>）"]


@settings(max_examples=60, deadline=None)
@given(snippet=st.sampled_from(sorted(FORBIDDEN_SNIPPETS)), position=st.floats(min_value=0, max_value=1),
       case=st.booleans())
def test_forbidden_constructs_rejected_anywhere(snippet, position, case):
    """性质：把任一违禁构造插到干净文档的任意位置（大小写不同），校验都不通过。"""
    cut = int(len(CLEAN) * position)
    if case:
        snippet = snippet.upper().replace("DATA:", "data:")
    assert animation_errors(CLEAN[:cut] + snippet + CLEAN[cut:], 200)


class Stub:
    """adapters.request 的桩：依次返回 responses（末项重复），记录全部请求。"""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def openai_reply(text, finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}]}


def test_retry_with_feedback_then_success(config, monkeypatch):
    bad = CLEAN.replace("</body>", "<script>fetch('/x')</script></body>")
    stub = Stub(openai_reply("```html\n" + bad + "\n```"), openai_reply(CLEAN))
    monkeypatch.setattr(module.adapters, "request", stub)
    config.studio.animator.api_key = "sk-test-key-123456"
    html = make_animator(config).animate("增长曲线", seconds=2.5, width=1280, height=720, fps=25)
    assert html == CLEAN and len(stub.calls) == 2
    first, second = (call["json"]["messages"][1]["content"] for call in stub.calls)
    assert "1280×720" in first and "2.5 秒" in first and "增长曲线" in first
    assert "上一次输出未通过校验" in second and "fetch" in second
    assert stub.calls[0]["url"].endswith("/chat/completions")
    assert stub.calls[0]["headers"] == {"Authorization": "Bearer sk-test-key-123456"}
    assert stub.calls[0]["json"]["max_tokens"] == 16000
    assert stub.calls[0]["json"]["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_retries_exhausted_raise_chinese_error(config, monkeypatch):
    stub = Stub(openai_reply("<div>不是文档</div>"))
    monkeypatch.setattr(module.adapters, "request", stub)
    config.studio.animator.retries = 1
    with pytest.raises(AnimationRejected, match="缺少 <html> 或 <body>"):
        LlmAnimator(config).animate("x", seconds=1, width=10, height=10, fps=10)
    assert len(stub.calls) == 2


def test_truncated_and_empty_outputs(config, monkeypatch):
    monkeypatch.setattr(module.adapters, "request", Stub(openai_reply(CLEAN, finish="length")))
    with pytest.raises(ModelError, match="max_tokens"):
        LlmAnimator(config).animate("x", seconds=1, width=10, height=10, fps=10)
    monkeypatch.setattr(module.adapters, "request", Stub(openai_reply("  ")))
    with pytest.raises(ModelError, match="空内容"):
        LlmAnimator(config).animate("x", seconds=1, width=10, height=10, fps=10)
    with pytest.raises(ModelError, match="画面说明为空"):
        LlmAnimator(config).animate("  ", seconds=1, width=10, height=10, fps=10)


def test_anthropic_backend_request_shape(config, monkeypatch):
    stub = Stub({"content": [{"type": "text", "text": CLEAN}], "stop_reason": "end_turn"})
    monkeypatch.setattr(module.adapters, "request", stub)
    cfg = config.studio.animator
    cfg.backend, cfg.base_url, cfg.model, cfg.api_key = "anthropic", "https://api.anthropic.com", "claude-x", "sk-a"
    assert LlmAnimator(config).animate("示意图", seconds=1, width=320, height=180, fps=30,
                                       previous="<html></html>", feedback="字号更大") == CLEAN
    call = stub.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-a" and call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["json"]["system"] == SYSTEM_PROMPT and call["json"]["model"] == "claude-x"
    content = call["json"]["messages"][0]["content"]
    assert "【当前源码】" in content and "字号更大" in content
    monkeypatch.setattr(module.adapters, "request",
                        Stub({"content": [{"type": "text", "text": "..."}], "stop_reason": "max_tokens"}))
    with pytest.raises(ModelError, match="max_tokens"):
        LlmAnimator(config).animate("x", seconds=1, width=10, height=10, fps=10)


def test_ping_is_a_minimal_chat(config, monkeypatch):
    stub = Stub(openai_reply("ok"))
    monkeypatch.setattr(module.adapters, "request", stub)
    assert LlmAnimator(config).ping()["reply"] == "ok"
    assert stub.calls[0]["json"]["max_tokens"] <= 1024
