"""视觉输入接入：两种协议适配器的图片块转换，以及运行时按视觉开关构造请求（附件图片、工具图片、
视觉关闭时的说明、日志中只有引用）。"""

from __future__ import annotations

import io
import json
import re

import pytest
from agent_helpers import new_session, result_text, results, wait_idle
from fastapi.testclient import TestClient
from PIL import Image

from mindex.agent.llm import anthropic, openai_compat
from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.llm.types import image_block, resolved_image_block, tool_message, user_message
from mindex.agent.registry import ToolOutput, ToolRegistry, ToolSpec
from mindex.agent.runtime import NO_VISION_PROMPT, NO_VISION_TOOL_NOTE
from mindex.agent.session_store import derive_messages, estimate_tokens
from mindex.agent.vision import IMAGE_TOKENS
from mindex.api import create_app
from mindex.shared.config import StrictModel

BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{1000,}")


def png(size=(32, 20), color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def with_image(text="看图", data="QUJD", ref=None) -> dict:
    message = user_message(text)
    message["content"].append(image_block(ref) if ref else resolved_image_block(data, "image/png"))
    return message


# ------------------------------------------------------------------ 适配器


def test_openai_user_images_as_data_urls_and_refs_skipped():
    converted = openai_compat.convert_messages("sys", [with_image(), with_image(ref="attachment://x")])
    assert converted[1]["content"] == [{"type": "text", "text": "看图"},
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]
    # 只有引用、没有数据的块不发出，内容保持纯文本。
    assert converted[2]["content"] == "看图"
    tool = tool_message("c1", "结果")
    tool["content"].append(resolved_image_block("QUJD"))
    assert openai_compat.convert_messages("", [tool])[0]["content"] == "结果"


def test_anthropic_images_in_user_and_tool_result():
    tool = tool_message("c1", "结果")
    tool["content"].append(resolved_image_block("REVG", "image/jpeg"))
    plain = tool_message("c2", "纯文本")
    converted = anthropic.convert_messages([with_image(), with_image(ref="attachment://x"), tool, plain])
    user = converted[0]["content"]
    assert user[0] == {"type": "text", "text": "看图"}
    assert user[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    assert user[2] == {"type": "text", "text": "看图"}  # 引用块跳过
    results_ = [b for b in user if b["type"] == "tool_result"]
    assert results_[0]["content"] == [
        {"type": "text", "text": "结果"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "REVG"}}]
    assert results_[1]["content"] == "纯文本"


def test_estimate_tokens_counts_images_up_to_limit():
    messages = [with_image(ref="attachment://a"), with_image(ref="attachment://b")]
    base = estimate_tokens(messages)
    assert estimate_tokens(messages, image_tokens=IMAGE_TOKENS) == base + 2 * IMAGE_TOKENS
    assert estimate_tokens(messages, image_tokens=IMAGE_TOKENS, image_limit=1) == base + IMAGE_TOKENS
    tool = tool_message("c", "x", images=["/api/a"])
    events = [{"type": "tool/result", "data": {"message": {**tool, "toolCallId": "c"}}}]
    assert derive_messages(events)[0]["images"] == ["/api/a"]


# ------------------------------------------------------------------ 运行时


class NoArgs(StrictModel):
    pass


def image_registry(urls: list[str]) -> ToolRegistry:
    async def show(_args, _ctx):
        return ToolOutput({"ok": True}, text="已抽帧", images=urls + ["https://evil.example/x.png"])

    return ToolRegistry([ToolSpec("show_frames", "meta", "测试：返回图片", NoArgs, "read", show)])


@pytest.fixture
def app(config, monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.delenv("MINDEX_MCP_TOKEN", raising=False)
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm, config


def upload(client, sid: str, data: bytes) -> str:
    response = client.post("/api/agent/attachments", files={"file": ("p.png", data, "image/png")},
                           data={"session_id": sid})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def log_text(client, sid) -> str:
    return json.dumps(client.get(f"/api/agent/sessions/{sid}/log").json(), ensure_ascii=False)


def test_vision_on_sends_attachment_and_tool_images(app):
    client, llm, config = app
    config.agent.llm.vision = True
    config.agent.max_images = 8
    sid = new_session(client)
    ref = upload(client, sid, png())
    client.app.state.agent_runtime.registry = image_registry([f"/api/agent/attachments/{ref[13:]}/content"])
    llm.extend([call("show_frames", call_id="c1")], [say("看到了")])
    response = client.post(f"/api/agent/sessions/{sid}/messages", json={"content": "看图", "attachments": [ref]})
    assert response.status_code == 202, response.text
    events = wait_idle(client, sid)

    user = next(e["data"] for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "user")
    assert user["content"][1] == {"type": "image", "ref": ref, "mediaType": "image/png"}
    tool = results(events)[0]["message"]
    # 非本站地址被丢弃；视觉开启时文本不加未发送说明。
    assert tool["images"] == [f"/api/agent/attachments/{ref[13:]}/content"]
    assert NO_VISION_TOOL_NOTE not in result_text(results(events)[0])

    first = llm.requests[0].messages
    blocks = [b for m in first for b in m["content"] if b["type"] == "image"]
    assert len(blocks) == 1 and blocks[0]["mediaType"] == "image/jpeg" and blocks[0]["data"]
    second = llm.requests[1].messages
    synthetic = [m for m in second if m["source"].get("origin") == "tool-images"]
    assert len(synthetic) == 1 and any(b["type"] == "image" and b.get("data") for b in synthetic[0]["content"])
    assert all("images" not in m for m in second)
    assert NO_VISION_PROMPT not in llm.requests[1].system
    # 日志与 SSE 来源只含引用。
    text = log_text(client, sid)
    assert "data:image/" not in text and not BASE64_RUN.search(text)


def test_vision_off_strips_images_and_notes(app):
    client, llm, config = app
    config.agent.llm.vision = False
    sid = new_session(client)
    ref = upload(client, sid, png())
    client.app.state.agent_runtime.registry = image_registry(["/api/thumbnails/seg-1"])
    llm.extend([call("show_frames", call_id="c1")], [say("好的")])
    client.post(f"/api/agent/sessions/{sid}/messages", json={"content": "看图", "attachments": [ref]})
    events = wait_idle(client, sid)
    assert result_text(results(events)[0]).endswith(NO_VISION_TOOL_NOTE)
    assert results(events)[0]["message"]["images"] == ["/api/thumbnails/seg-1"]
    for request in llm.requests:
        assert NO_VISION_PROMPT in request.system
        assert not any(b["type"] == "image" for m in request.messages for b in m["content"])
        assert all("images" not in m for m in request.messages)


def test_vision_limit_zero_omits_all(app):
    client, llm, config = app
    config.agent.llm.vision = True
    config.agent.max_images = 0
    sid = new_session(client)
    ref = upload(client, sid, png())
    llm.extend([say("好")])
    client.post(f"/api/agent/sessions/{sid}/messages", json={"content": "看图", "attachments": [ref]})
    wait_idle(client, sid)
    blocks = [b for m in llm.requests[0].messages for b in m["content"]]
    assert not any(b["type"] == "image" for b in blocks)
    assert any(b.get("text") == "（图片已省略）" for b in blocks)
