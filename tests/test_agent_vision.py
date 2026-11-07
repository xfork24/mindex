"""视觉输入的性质测试：日志里没有图片数据（Property 6），单次请求的图片上限（Property 7）。

纯函数层直接对 ``materialize`` 生成任意消息序列；端到端层用真实应用（ScriptedLlm + 真实附件上传 +
返回图片的测试工具）跑完整轮次，检查事件日志、SSE 补发与模型实际收到的请求。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from agent.llm.scripted import ScriptedLlm, call, say
from agent.llm.types import image_block, new_id, tool_message, user_message
from agent.registry import ToolOutput, ToolRegistry, ToolSpec
from agent.vision import OMITTED, is_image_ref, materialize
from agent_helpers import new_session, parse_sse, results, wait_idle
from api import create_app
from shared.config import StrictModel

BASE64_RUN = re.compile(r"[A-Za-z0-9+/=]{1000,}")
FAILED_PREFIX = "（图片无法读取："
PROTOCOLS = ("openai", "anthropic")


def png(size: tuple[int, int], color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 生成器
#
# 对话由若干段组成：用户消息（0–4 张图片）或一批工具调用（1–3 个结果，各 0–3 张图片）。
# 每张图片的结局：ok（可解析）、missing（读取失败）、broken（不是图片）。

OUTCOME = st.sampled_from(["ok", "missing", "broken"])
SEGMENT = st.one_of(
    st.tuples(st.just("user"), st.lists(OUTCOME, max_size=4)),
    st.tuples(st.just("tools"), st.lists(st.lists(OUTCOME, max_size=3), min_size=1, max_size=3)),
)
CONVERSATION = st.lists(SEGMENT, max_size=6)


def slot_ref(index: int, outcome: str) -> str:
    # 引用两种形式交替出现；materialize 不区分，读取由测试 loader 决定。
    if index % 2:
        return f"attachment://{outcome}{index:029d}"
    return f"/api/{outcome}/{index}"


def build(conversation) -> tuple[list[dict], list[str], dict[str, bytes]]:
    """返回（消息列表, 每张图片按出现顺序的结局, 可读取的 ref→字节）。

    可解析图片的宽度为 ``8 + 序号``（都小于 1024，不会被缩放），解码后即可反推它来自哪一张。
    """
    messages: list[dict] = []
    outcomes: list[str] = []
    blobs: dict[str, bytes] = {}

    def ref_for(outcome: str) -> str:
        index = len(outcomes)
        outcomes.append(outcome)
        ref = slot_ref(index, outcome)
        if outcome == "ok":
            blobs[ref] = png((8 + index, 6))
        elif outcome == "broken":
            blobs[ref] = b"not an image"
        return ref

    for number, (kind, payload) in enumerate(conversation):
        if kind == "user":
            message = user_message(f"看图{number}")
            message["content"] += [image_block(ref_for(o)) for o in payload]
            messages.append(message)
        else:
            ids = [f"c{number}-{k}" for k in range(len(payload))]
            messages.append(
                {
                    "id": new_id("msg"),
                    "role": "assistant",
                    "source": {"kind": "model"},
                    "content": [
                        {"type": "tool-call", "id": i, "name": "sample_frames", "arguments": "{}"}
                        for i in ids
                    ],
                }
            )
            for call_id, refs in zip(ids, payload, strict=True):
                images = [ref_for(o) for o in refs]
                messages.append(tool_message(call_id, f"帧{call_id}", images=images or None))
    return messages, outcomes, blobs


def recording_loader(blobs: dict[str, bytes]):
    calls: list[str] = []

    async def load(ref: str) -> bytes:
        calls.append(ref)
        if ref not in blobs:
            raise FileNotFoundError("找不到图片")
        return blobs[ref]

    return load, calls


def image_blocks(messages: list[dict]) -> list[dict]:
    return [b for m in messages for b in m.get("content", []) if b.get("type") == "image"]


def texts(messages: list[dict]) -> list[str]:
    return [b["text"] for m in messages for b in m.get("content", []) if b.get("type") == "text"]


def assert_no_image_data(value) -> None:
    """递归检查：没有带 data 的 image 块、没有 data:image/ 地址、没有长 base64 串。"""
    if isinstance(value, dict):
        if value.get("type") == "image":
            assert "data" not in value, value
        for item in value.values():
            assert_no_image_data(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_image_data(item)
    elif isinstance(value, str):
        assert "data:image/" not in value
        assert not BASE64_RUN.search(value)


# ------------------------------------------------------------------ Property 6（纯函数层）


# Feature: agent-capability-expansion, Property 6: 日志无图片数据
@settings(max_examples=100, deadline=None)
@given(
    conversation=CONVERSATION,
    enabled=st.booleans(),
    limit=st.integers(0, 12),
    protocol=st.sampled_from(PROTOCOLS),
)
def test_materialize_never_writes_back_into_log_messages(conversation, enabled, limit, protocol):
    """**Validates: Requirements 5.3**

    ``materialize`` 的输入就是日志派生的消息：无论开关、上限与协议如何，输入都保持原样（只含引用），
    已解析的图片数据只出现在返回的新列表里。
    """
    messages, _outcomes, blobs = build(conversation)
    snapshot = copy.deepcopy(messages)
    load, _calls = recording_loader(blobs)
    out = run(materialize(messages, enabled=enabled, limit=limit, load=load, protocol=protocol))
    assert messages == snapshot
    assert_no_image_data(messages)
    # 输出只给模型：不再带 tool.images，也不残留只有引用的图片块。
    assert all("images" not in m for m in out)
    assert all("data" in b and "ref" not in b for b in image_blocks(out))


# ------------------------------------------------------------------ Property 7（纯函数层）


# Feature: agent-capability-expansion, Property 7: 图片上限
@settings(max_examples=100, deadline=None)
@given(conversation=CONVERSATION, limit=st.integers(0, 12), protocol=st.sampled_from(PROTOCOLS))
def test_materialize_keeps_only_latest_images_up_to_limit(conversation, limit, protocol):
    """**Validates: Requirements 5.5**

    按出现顺序只保留最近 ``limit`` 张：保留区内可解析的成为图片块（顺序不变），不可解析的替换为
    「图片无法读取」；更早的每张替换为一条「图片已省略」，且不会被读取。
    """
    messages, outcomes, blobs = build(conversation)
    load, calls = recording_loader(blobs)
    out = run(materialize(messages, enabled=True, limit=limit, load=load, protocol=protocol))

    n = len(outcomes)
    cutoff = max(0, n - limit)
    kept = range(cutoff, n)
    blocks = image_blocks(out)
    widths = [Image.open(io.BytesIO(base64.b64decode(b["data"]))).width - 8 for b in blocks]
    assert widths == [i for i in kept if outcomes[i] == "ok"]
    assert len(blocks) <= limit
    assert all(b["mediaType"] == "image/jpeg" for b in blocks)

    notes = texts(out)
    assert notes.count(OMITTED) == cutoff
    assert sum(t.startswith(FAILED_PREFIX) for t in notes) == sum(outcomes[i] != "ok" for i in kept)
    # 只读取保留区内的图片。
    assert set(calls) == {slot_ref(i, outcomes[i]) for i in kept}

    synthetic = [m for m in out if m.get("source", {}).get("origin") == "tool-images"]
    if protocol == "openai":
        # OpenAI 兼容协议的 tool 消息只能是文本，图片都在合成的 user 消息里。
        assert not any(b["type"] == "image" for m in out if m["role"] == "tool" for b in m["content"])
        assert all(m["role"] == "user" and m["source"]["kind"] == "context" for m in synthetic)
    else:
        assert synthetic == []
        assert len(out) == len(messages)


# Feature: agent-capability-expansion, Property 7: 图片上限
@settings(max_examples=100, deadline=None)
@given(conversation=CONVERSATION, limit=st.integers(0, 12), protocol=st.sampled_from(PROTOCOLS))
def test_materialize_disabled_sends_no_images(conversation, limit, protocol):
    """**Validates: Requirements 5.6**

    视觉关闭：输出没有任何图片块与 ``tool.images``，也不读取任何图片；非图片内容不变。
    """
    messages, _outcomes, blobs = build(conversation)
    load, calls = recording_loader(blobs)
    out = run(materialize(messages, enabled=False, limit=limit, load=load, protocol=protocol))
    assert image_blocks(out) == [] and calls == []
    assert all("images" not in m for m in out)
    assert texts(out) == texts(messages)
    assert len(out) == len(messages)


# ------------------------------------------------------------------ 端到端（运行时 + HTTP）


class FrameArgs(StrictModel):
    index: int


@pytest.fixture
def app(config, monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.delenv("MINDEX_MCP_TOKEN", raising=False)
    llm = ScriptedLlm()
    with TestClient(create_app(config, agent_llm=llm)) as client:
        yield client, llm, config


def upload(client, sid: str, data: bytes) -> str:
    response = client.post(
        "/api/agent/attachments", files={"file": ("p.png", data, "image/png")}, data={"session_id": sid}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def frame_registry(frames: dict[int, list[str]]) -> ToolRegistry:
    """测试工具：按参数 index 返回本例预设的图片地址（外站地址会被运行时丢弃）。"""

    async def show(args, _ctx):
        return ToolOutput(
            {"ok": True}, text="已抽帧", images=frames.get(args.index, []) + ["https://evil.example/x.png"]
        )

    return ToolRegistry([ToolSpec("show_frames", "meta", "测试：返回图片", FrameArgs, "read", show)])


# 端到端每个样例都要上传文件并跑完整轮次（约数百毫秒），所以只跑 15 例；
# 同一个应用内为每个样例新建会话，覆盖面由上面的纯函数性质（各 100 例）承担。
# Feature: agent-capability-expansion, Property 6: 日志无图片数据
# Feature: agent-capability-expansion, Property 7: 图片上限
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    attachments=st.integers(0, 3),
    tool_calls=st.lists(st.lists(st.sampled_from(["picture", "missing"]), max_size=3), max_size=2),
    vision=st.booleans(),
    limit=st.integers(0, 4),
    protocol=st.sampled_from(PROTOCOLS),
)
def test_runtime_log_and_sse_hold_only_refs_and_requests_respect_limit(
    app, attachments, tool_calls, vision, limit, protocol
):
    """**Validates: Requirements 5.3, 5.5, 5.6**

    事件日志与 SSE 补发里只有图片引用；模型实际收到的每个请求中，已解析图片不超过 ``max_images``，
    视觉关闭时为 0。
    """
    client, llm, config = app
    config.agent.llm.vision = vision
    config.agent.max_images = limit
    llm.provider = "anthropic" if protocol == "anthropic" else "scripted"
    start = len(llm.requests)

    sid = new_session(client)
    refs = [upload(client, sid, png((40 + k, 30), (10 * k, 120, 200))) for k in range(attachments)]
    picture = upload(client, sid, png((64, 48)))
    picture_url = f"/api/agent/attachments/{picture[len('attachment://') :]}/content"
    frames = {
        k: [picture_url if kind == "picture" else f"/api/admin/sources/nope-{k}/frame?t=1" for kind in kinds]
        for k, kinds in enumerate(tool_calls)
    }
    client.app.state.agent_runtime.registry = frame_registry(frames)
    if tool_calls:
        llm.extend([call("show_frames", index=k) for k in range(len(tool_calls))])
    llm.extend([say("看到了")])

    response = client.post(
        f"/api/agent/sessions/{sid}/messages", json={"content": "看图", "attachments": refs}
    )
    assert response.status_code == 202, response.text
    events = wait_idle(client, sid)

    # 日志：只有引用，工具结果只记录本站地址。
    log = client.get(f"/api/agent/sessions/{sid}/log").json()
    assert_no_image_data(log)
    assert "data:image/" not in json.dumps(log, ensure_ascii=False)
    for data in results(events):
        assert all(is_image_ref(ref) for ref in data["message"].get("images") or [])
    # SSE 补发：与日志同源，同样只有引用。
    raw = client.get(f"/api/agent/sessions/{sid}/events", params={"follow": "false"}).text
    assert "data:image/" not in raw and not BASE64_RUN.search(raw)
    frames_ = parse_sse(raw)
    assert any(name == "session.event" for name, _ in frames_)
    assert_no_image_data([data for _, data in frames_])

    requests = llm.requests[start:]
    assert len(requests) == (2 if tool_calls else 1)
    for request in requests:
        blocks = image_blocks(request.messages)
        assert all(b.get("data") for b in blocks)
        assert len(blocks) <= (limit if vision else 0)
        assert all("images" not in m for m in request.messages)
    if vision and limit and attachments:
        # 附件图片都可解析：首个请求里保留的正是最近的 min(附件数, 上限) 张。
        assert len(image_blocks(requests[0].messages)) == min(attachments, limit)
