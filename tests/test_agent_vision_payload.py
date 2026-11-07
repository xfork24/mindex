"""视觉输入的端到端补充测试。

- 两种协议真正发出的 HTTP 请求体：运行时注入真实 ``OpenAICompatClient`` / ``AnthropicClient``，
  用 ``httpx.MockTransport`` 拦截请求 JSON；
- 真实功能工具（``sample_frames``、``search_by_media``、``identify_person``）经运行时调用后，
  下一步 ScriptedLlm 请求里出现对应的已解析图片块（JPEG，最长边 ≤ 1024），日志只含引用；
- 图片引用解析失败（附件被删除 / 过期、站内地址 404、非图片内容、其他会话的附件）时替换为说明文字，
  轮次照常完成；
- 设置往返：经设置接口切换智能助手档案的 vision 开关，同一会话下一轮即生效（无需重启）。
"""

from __future__ import annotations

import base64
import io
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.attachments import AttachmentStore
from agent.llm.anthropic import AnthropicClient
from agent.llm.openai_compat import OpenAICompatClient
from agent.llm.scripted import ScriptedLlm, call, say
from agent.registry import ToolOutput, ToolRegistry, ToolSpec
from agent.runtime import NO_VISION_PROMPT, NO_VISION_TOOL_NOTE
from agent.vision import OMITTED, TOOL_IMAGES_HEADER
from agent_helpers import new_session, result_text, results, wait_idle
from api import create_app
from conftest import FixtureEmbedder
from processing.indexing import IndexBuilder
from processing.pipeline import Pipeline
from shared.config import AgentLlmConfig, StrictModel
from test_channel_recall import add_analysis, add_dialogue
from test_person_identify import StubPersonClient, face, photo, seed_library

BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{1000,}")
FAILED = "（图片无法读取："
DATA_URL = re.compile(r"^data:image/jpeg;base64,(.+)$")


@pytest.fixture(autouse=True)
def no_tokens(monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.delenv("MINDEX_MCP_TOKEN", raising=False)


def png(size=(32, 20), color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def upload(client, sid: str, data: bytes, name="p.png", ctype="image/png") -> str:
    response = client.post(
        "/api/agent/attachments", files={"file": (name, data, ctype)}, data={"session_id": sid}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def content_url(ref: str) -> str:
    return f"/api/agent/attachments/{ref[len('attachment://') :]}/content"


def decode_jpeg(data_b64: str) -> Image.Image:
    raw = base64.b64decode(data_b64)
    assert raw[:2] == b"\xff\xd8"
    image = Image.open(io.BytesIO(raw))
    assert image.format == "JPEG" and max(image.size) <= 1024
    return image


def send(client, sid, text, *, attachments=(), context=None) -> list[dict]:
    response = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"content": text, "attachments": list(attachments), "context": context},
    )
    assert response.status_code == 202, response.text
    return wait_idle(client, sid)


def turn_ends(events: list[dict]) -> list[dict]:
    return [e["data"]["reason"] for e in events if e["type"] == "turn/end"]


def assert_log_has_only_refs(client, sid) -> None:
    text = json.dumps(client.get(f"/api/agent/sessions/{sid}/log").json(), ensure_ascii=False)
    assert "data:image/" not in text and not BASE64_RUN.search(text)


def image_blocks(messages: list[dict]) -> list[dict]:
    return [b for m in messages for b in m.get("content", []) if b.get("type") == "image"]


def texts(messages: list[dict]) -> list[str]:
    return [b["text"] for m in messages for b in m.get("content", []) if b.get("type") == "text"]


class NoArgs(StrictModel):
    pass


def image_tool(images: list[str]) -> ToolRegistry:
    """meta 领域测试工具：返回给定图片引用（模拟抽帧类工具）。"""

    async def show(_args, _ctx):
        return ToolOutput({"ok": True}, text="已抽帧", images=list(images))

    return ToolRegistry([ToolSpec("show_frames", "meta", "测试：返回图片", NoArgs, "read", show)])


# ====================================================================== 两种协议的 HTTP 请求体


def sse(events: list[dict], done: bool) -> bytes:
    body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


OPENAI_REPLIES = [
    sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "show_frames", "arguments": "{}"},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ],
        done=True,
    ),
    sse([{"choices": [{"delta": {"content": "看到了"}, "finish_reason": "stop"}]}], done=True),
]
ANTHROPIC_REPLIES = [
    sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "show_frames", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ],
        done=False,
    ),
    sse(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "看到了"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ],
        done=False,
    ),
]


def http_llm(protocol: str, replies: list[bytes]):
    """真实协议客户端 + MockTransport：按顺序返回脚本化 SSE，记录每次请求的 JSON 体。"""
    bodies: list[dict] = []
    queue = list(replies)

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=queue.pop(0), headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    if protocol == "openai":
        profile = AgentLlmConfig(base_url="https://llm.test/v1", model="vl", api_key="sk-test-123456789")
        return OpenAICompatClient(profile, transport=transport, retries=0), bodies
    profile = AgentLlmConfig(
        backend="anthropic", base_url="https://api.anthropic.test", model="claude", api_key="sk-ant-123456789"
    )
    return AnthropicClient(profile, transport=transport, retries=0), bodies


def http_app(config, protocol: str, *, vision: bool, replies=None):
    config.agent.llm.vision = vision
    config.agent.max_images = 8
    llm, bodies = http_llm(
        protocol, replies or (OPENAI_REPLIES if protocol == "openai" else ANTHROPIC_REPLIES)
    )
    return TestClient(create_app(config, agent_llm=llm)), bodies


def openai_user_parts(body: dict) -> list[list[dict]]:
    return [m["content"] for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list)]


def test_openai_payload_user_and_tool_images(config):
    """OpenAI 兼容：用户图片为 text + image_url 数据 URL；工具图片在 tool 消息之后的合成 user 消息里。"""
    client, bodies = http_app(config, "openai", vision=True)
    with client:
        sid = new_session(client)
        user_ref = upload(client, sid, png((1600, 400)))
        tool_ref = upload(client, sid, png((2000, 1500), (20, 160, 40)), name="frame.png")
        client.app.state.agent_runtime.registry = image_tool([content_url(tool_ref)])
        events = send(client, sid, "看图", attachments=[user_ref])
        assert turn_ends(events) == [{"kind": "completed"}]
        assert_log_has_only_refs(client, sid)

    assert len(bodies) == 2
    first, second = bodies
    # 第一步：用户消息为数组内容，文字在前（含附件说明行），图片为 JPEG 数据 URL，已缩到最长边 1024。
    [parts] = openai_user_parts(first)
    assert parts[0]["type"] == "text" and parts[0]["text"].startswith("看图")
    assert f"附件：{user_ref}" in parts[0]["text"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    match = DATA_URL.match(parts[1]["image_url"]["url"])
    assert match and decode_jpeg(match.group(1)).size == (1024, 256)

    # 第二步：tool 消息仍是纯文本；紧随其后是承载工具图片的合成 user 消息。
    roles = [m["role"] for m in second["messages"]]
    tool_at = roles.index("tool")
    tool = second["messages"][tool_at]
    assert tool["tool_call_id"] == "call_1" and tool["content"] == "已抽帧"
    synthetic = second["messages"][tool_at + 1]
    assert synthetic["role"] == "user" and isinstance(synthetic["content"], list)
    assert synthetic["content"][0] == {"type": "text", "text": TOOL_IMAGES_HEADER}
    assert synthetic["content"][1] == {"type": "text", "text": "工具调用 call_1 的图片："}
    match = DATA_URL.match(synthetic["content"][2]["image_url"]["url"])
    assert match and max(decode_jpeg(match.group(1)).size) == 1024
    # 用户附件图片在第二步仍保留（未超过上限）；请求体里没有任何引用形式的图片或 images 字段。
    assert len(openai_user_parts(second)) == 2
    assert "attachment://" not in json.dumps(
        [p for ps in openai_user_parts(second) for p in ps if p["type"] == "image_url"]
    )
    assert all("images" not in m for m in second["messages"])


def test_anthropic_payload_user_image_and_tool_result_image(config):
    """Anthropic：用户图片为 image{source: base64}；工具图片位于 tool_result.content 内，不生成合成消息。"""
    client, bodies = http_app(config, "anthropic", vision=True)
    with client:
        sid = new_session(client)
        user_ref = upload(client, sid, png((300, 1800)))
        tool_ref = upload(client, sid, png((64, 48), (20, 160, 40)), name="frame.png")
        client.app.state.agent_runtime.registry = image_tool([content_url(tool_ref)])
        events = send(client, sid, "看图", attachments=[user_ref])
        assert turn_ends(events) == [{"kind": "completed"}]

    first, second = bodies
    user = first["messages"][-1]
    assert user["role"] == "user"
    images = [b for b in user["content"] if b["type"] == "image"]
    assert len(images) == 1
    source = images[0]["source"]
    assert source["type"] == "base64" and source["media_type"] == "image/jpeg"
    assert decode_jpeg(source["data"]).size == (171, 1024)
    assert any(b["type"] == "text" and b["text"].startswith("看图") for b in user["content"])

    # 第二步：严格交替 user/assistant/user；工具图片在 tool_result 的块数组里。
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    tail = second["messages"][2]["content"]
    [result] = [b for b in tail if b["type"] == "tool_result"]
    assert result["tool_use_id"] == "toolu_1" and "is_error" not in result
    assert result["content"][0] == {"type": "text", "text": "已抽帧"}
    assert result["content"][1]["type"] == "image"
    assert decode_jpeg(result["content"][1]["source"]["data"]).size == (64, 48)
    # 合成 user 消息只用于 OpenAI 兼容协议。
    assert TOOL_IMAGES_HEADER not in json.dumps(second, ensure_ascii=False)
    # 用户附件图片仍在首条 user 消息里：整个请求共 2 张图片。
    dumped = json.dumps(second)
    assert dumped.count('"type": "image"') == 2


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_payload_without_vision_has_no_image_data(config, protocol):
    """视觉关闭：请求体中没有任何图片数据，用户消息保持纯文本结构，系统提示与工具结果注明。"""
    client, bodies = http_app(config, protocol, vision=False)
    with client:
        sid = new_session(client)
        user_ref = upload(client, sid, png((64, 48)))
        client.app.state.agent_runtime.registry = image_tool([content_url(user_ref)])
        events = send(client, sid, "看图", attachments=[user_ref])
        assert turn_ends(events) == [{"kind": "completed"}]
        assert result_text(results(events)[0]).endswith(NO_VISION_TOOL_NOTE)

    assert len(bodies) == 2
    for body in bodies:
        raw = json.dumps(body, ensure_ascii=False)
        assert "data:image/" not in raw and not BASE64_RUN.search(raw)
        assert '"image"' not in raw and "image_url" not in raw
        if protocol == "openai":
            assert (
                body["messages"][0]["role"] == "system" and NO_VISION_PROMPT in body["messages"][0]["content"]
            )
            assert all(isinstance(m["content"], str) for m in body["messages"] if m["role"] == "user")
        else:
            assert NO_VISION_PROMPT in body["system"]
            assert all(
                b["type"] in ("text", "tool_use", "tool_result")
                for m in body["messages"]
                for b in m["content"]
            )
    if protocol == "openai":
        tool = next(m for m in bodies[1]["messages"] if m["role"] == "tool")
        assert tool["content"].endswith(NO_VISION_TOOL_NOTE)


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_payload_without_images_keeps_plain_structure(config, protocol):
    """视觉开启但对话里没有图片：请求结构与未引入视觉前一致（OpenAI 字符串内容，Anthropic 纯文本块）。"""
    replies = OPENAI_REPLIES[1:] if protocol == "openai" else ANTHROPIC_REPLIES[1:]
    client, bodies = http_app(config, protocol, vision=True, replies=replies)
    with client:
        sid = new_session(client)
        assert turn_ends(send(client, sid, "你好")) == [{"kind": "completed"}]
    [body] = bodies
    if protocol == "openai":
        users = [m for m in body["messages"] if m["role"] == "user"]
        assert users and all(isinstance(m["content"], str) for m in users)
        assert users[-1]["content"] == "你好"
        assert NO_VISION_PROMPT not in body["messages"][0]["content"]
    else:
        assert body["messages"][-1]["content"][-1] == {"type": "text", "text": "你好"}
        assert NO_VISION_PROMPT not in body["system"]


# ====================================================================== 真实工具链路（ScriptedLlm）


def synthetic_for(messages: list[dict], call_id: str) -> list[dict]:
    """OpenAI 兼容放置：取紧随 ``call_id`` 工具结果所在批次之后的合成消息中，属于该调用的图片块。"""
    blocks: list[dict] = []
    for message in messages:
        if message.get("source", {}).get("origin") != "tool-images":
            continue
        current = None
        for block in message["content"]:
            if block["type"] == "text" and block["text"].startswith("工具调用 "):
                current = block["text"]
            elif block["type"] == "image" and current == f"工具调用 {call_id} 的图片：":
                blocks.append(block)
    return blocks


@pytest.fixture
def indexed(config, video_file):
    """一个已发布素材（画面 + 对白 + 分析渠道），以及注入 ScriptedLlm 的应用。"""
    config.embedding.dimensions = 8
    pipeline = Pipeline(config)
    source_id = pipeline.prepare(video_file)
    add_dialogue(config, source_id)
    add_analysis(pipeline.store, pipeline.store.segments(source_id, "moment")[0])
    IndexBuilder(config, FixtureEmbedder(config)).build(activate=True)
    config.agent.llm.vision = True
    config.agent.max_images = 32
    llm = ScriptedLlm()
    with TestClient(create_app(config, FixtureEmbedder(config), agent_llm=llm)) as client:
        yield client, llm, source_id


def test_sample_frames_and_search_by_media_images_reach_model(indexed):
    client, llm, source_id = indexed
    sid = new_session(client)
    query = upload(client, sid, png((1400, 900), (120, 120, 200)), name="query.png")
    llm.extend(
        [call("sample_frames", call_id="c1", source_id=source_id, count=2, width=1024)],
        [call("search_by_media", call_id="c2", attachment=query, kind="image", limit=3)],
        [say("找到了")],
    )
    events = send(client, sid, "看看这段画面再按图找", attachments=[query], context={"page": "search"})
    assert turn_ends(events) == [{"kind": "completed"}]
    frames, search = results(events)
    assert "error" not in frames and "error" not in search, (frames, search)
    frame_urls = frames["message"]["images"]
    assert len(frame_urls) == 2 and all(
        u.startswith(f"/api/admin/sources/{source_id}/frame?") for u in frame_urls
    )
    search_images = search["message"]["images"]
    assert search_images[0] == query and len(search_images) >= 2  # 查询图 + 结果缩略图

    assert len(llm.requests) == 3
    # 抽帧之后的那一步：两帧都作为已解析 JPEG 交给模型（宽 1024 的真实画面）。
    blocks = synthetic_for(llm.requests[1].messages, "c1")
    assert len(blocks) == 2
    for block in blocks:
        assert block["mediaType"] == "image/jpeg" and "ref" not in block
        assert decode_jpeg(block["data"]).width == 1024
    # 以图搜索之后：查询图与结果缩略图都已解析；抽帧图片仍保留（未超过上限）。
    third = llm.requests[2].messages
    searched = synthetic_for(third, "c2")
    assert len(searched) == len(search_images)
    assert decode_jpeg(searched[0]["data"]).size == (1024, 658)
    assert len(synthetic_for(third, "c1")) == 2
    assert not any(t.startswith(FAILED) for t in texts(third))
    assert_log_has_only_refs(client, sid)


def test_identify_person_images_reach_model(config, monkeypatch):
    seed_library(config, monkeypatch)
    StubPersonClient.observations = [face([1, 0, 0])]
    config.agent.llm.vision = True
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        sid = new_session(client)
        ref = upload(client, sid, photo("red", (2048, 1536)), name="who.png")
        llm.extend([call("identify_person", call_id="c1", attachment=ref, top_k=2)], [say("是演员甲")])
        events = send(client, sid, "这是谁", attachments=[ref], context={"page": "persons"})
        assert turn_ends(events) == [{"kind": "completed"}]
        [result] = results(events)
        assert "error" not in result, result
        images = result["message"]["images"]
        assert images[0] == ref and len(images) >= 2  # 上传图 + 获胜参考图
        assert all(i.startswith("/api/persons/") for i in images[1:])

        blocks = synthetic_for(llm.requests[1].messages, "c1")
        assert len(blocks) == len(images)
        assert decode_jpeg(blocks[0]["data"]).size == (1024, 768)
        for block in blocks[1:]:  # 参考图（160×120）不放大
            assert decode_jpeg(block["data"]).size == (160, 120)
        assert_log_has_only_refs(client, sid)


# ====================================================================== 解析失败的替换


@pytest.fixture
def scripted(config):
    config.agent.llm.vision = True
    config.agent.max_images = 8
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm, config


def failures(messages: list[dict]) -> list[str]:
    return [t for t in texts(messages) if t.startswith(FAILED)]


def test_unreadable_tool_images_are_replaced_and_turn_completes(scripted):
    """站内地址 404、非图片内容、其他会话的附件：各自替换为说明文字，可读的图片照常发送。"""
    client, llm, _config = scripted
    other = new_session(client)
    foreign = upload(client, other, png((40, 30)))
    sid = new_session(client)
    good = upload(client, sid, png((48, 36)))
    text_file = upload(client, sid, b"just some text", name="note.txt", ctype="text/plain")
    client.app.state.agent_runtime.registry = image_tool(
        [
            "/api/admin/sources/nope/frame?t=1",
            content_url(text_file),
            foreign,
            content_url(good),
        ]
    )
    llm.extend([call("show_frames", call_id="c1")], [say("部分图片无法读取")])
    events = send(client, sid, "看图")
    assert turn_ends(events) == [{"kind": "completed"}]

    second = llm.requests[1].messages
    tool = next(m for m in second if m["role"] == "tool")
    notes = failures([tool])
    assert len(notes) == 3
    assert notes[0] == f"{FAILED}找不到素材）"
    assert "无法解码" in notes[1]
    assert "不属于当前会话" in notes[2]
    # 唯一可读的那张进入合成消息。
    blocks = synthetic_for(second, "c1")
    assert len(blocks) == 1 and decode_jpeg(blocks[0]["data"]).size == (48, 36)
    assert_log_has_only_refs(client, sid)


def test_user_attachment_deleted_or_expired_later_is_replaced(scripted):
    """首轮附件可读；之后附件被删除或过期，后续轮次把历史中的这张图替换为说明文字，轮次不失败。"""
    client, llm, config = scripted
    sid = new_session(client)
    deleted = upload(client, sid, png((40, 30)))
    expired = upload(client, sid, png((50, 30)))
    llm.extend([say("收到")], [say("还在")])
    assert turn_ends(send(client, sid, "看图", attachments=[deleted, expired])) == [{"kind": "completed"}]
    assert len(image_blocks(llm.requests[0].messages)) == 2

    assert client.delete(content_url(deleted).removesuffix("/content")).status_code in (200, 204)
    store = AttachmentStore(config.data_dir)
    store.store.execute(
        "UPDATE agent_attachments SET expires_at=0 WHERE id=?", (expired[len("attachment://") :],)
    )

    events = send(client, sid, "继续")
    assert turn_ends(events)[-1] == {"kind": "completed"}
    messages = llm.requests[1].messages
    assert image_blocks(messages) == []
    notes = failures(messages)
    assert len(notes) == 2
    assert "找不到附件" in notes[0] and "过期" in notes[1]


# ====================================================================== 设置往返（热生效）


def test_vision_profile_toggle_takes_effect_next_turn(scripted):
    """经设置接口创建并激活 vision 档案：同一会话下一轮即发送图片；改回关闭后下一轮系统提示注明未发送。"""
    client, llm, config = scripted
    config.agent.llm.vision = False

    def agent_role():
        roles = client.get("/api/settings?probe_embedding=false").json()["roles"]
        return next(r for r in roles if r["role"] == "agent")

    sid = new_session(client)
    ref = upload(client, sid, png((40, 30)))
    llm.extend([say("一")], [say("二")], [say("三")], [say("四")])

    send(client, sid, "第一轮", attachments=[ref])
    assert NO_VISION_PROMPT in llm.requests[0].system and image_blocks(llm.requests[0].messages) == []

    body = {
        "role": "agent",
        "name": "视觉助手",
        "provider": "custom",
        "config": {"backend": "openai", "base_url": "https://llm.test/v1", "model": "vl", "vision": True},
    }
    created = client.post("/api/settings/profiles", json=body)
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    assert client.post(f"/api/settings/profiles/{pid}/activate").status_code == 200
    assert agent_role()["effective"]["vision"] is True

    send(client, sid, "第二轮")
    assert NO_VISION_PROMPT not in llm.requests[1].system
    [block] = image_blocks(llm.requests[1].messages)
    assert block["mediaType"] == "image/jpeg" and decode_jpeg(block["data"]).size == (40, 30)

    # max_images 不在设置页，但每轮从生效配置读取：基础配置调为 0 后，下一轮同一张图只留下省略说明。
    assert client.app.state.runtime_config.base is config
    config.agent.max_images = 0
    send(client, sid, "第三轮")
    assert image_blocks(llm.requests[2].messages) == []
    assert OMITTED in texts(llm.requests[2].messages)
    config.agent.max_images = 8

    off = client.put(
        f"/api/settings/profiles/{pid}", json={**body, "config": {**body["config"], "vision": False}}
    )
    assert off.status_code == 200, off.text
    assert agent_role()["effective"]["vision"] is False
    send(client, sid, "第四轮")
    assert NO_VISION_PROMPT in llm.requests[3].system
    assert image_blocks(llm.requests[3].messages) == []
    assert_log_has_only_refs(client, sid)
