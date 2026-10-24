"""视觉输入 materialize 与图片块辅助函数的基础测试（性质测试见 test_agent_vision.py）。"""

from __future__ import annotations

import asyncio
import base64
import io
import json

from PIL import Image

from mindex.agent.attachments import AttachmentStore
from mindex.agent.llm.types import image_block, text_block, tool_message, user_message
from mindex.agent.registry import ToolOutput
from mindex.agent.vision import (
    OMITTED,
    encode_jpeg,
    image_count,
    is_image_ref,
    make_loader,
    materialize,
    protocol_for,
)


def png(width: int, height: int, mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), (200, 10, 10, 128) if mode == "RGBA" else (200, 10, 10)).save(buffer, "PNG")
    return buffer.getvalue()


def decoded(block: dict) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(block["data"])))


def fake_loader(images: dict[str, bytes]):
    calls: list[str] = []

    async def load(ref: str) -> bytes:
        calls.append(ref)
        if ref not in images:
            raise FileNotFoundError("找不到图片")
        return images[ref]

    return load, calls


def user_with_images(text: str, *refs: str) -> dict:
    message = user_message(text)
    message["content"] += [image_block(ref) for ref in refs]
    return message


def assistant_calls(*ids: str) -> dict:
    return {"id": "m-a", "role": "assistant", "source": {"kind": "model"},
            "content": [{"type": "tool-call", "id": i, "name": "sample_frames", "arguments": "{}"} for i in ids]}


def run(coro):
    return asyncio.run(coro)


def test_blocks_and_tool_message_images():
    assert image_block("attachment://" + "a" * 32) == {
        "type": "image", "ref": "attachment://" + "a" * 32, "mediaType": "image/jpeg"}
    message = tool_message("call-1", "ok", images=["/api/x"])
    assert message["images"] == ["/api/x"]
    assert "images" not in tool_message("call-1", "ok")
    assert ToolOutput({"a": 1}).images is None
    assert protocol_for("anthropic") == "anthropic"
    assert protocol_for("openai-compatible") == "openai"


def test_image_ref_whitelist():
    assert is_image_ref("attachment://" + "0" * 32)
    assert is_image_ref("/api/admin/sources/s1/frame?t=1.5&width=768")
    for bad in ("https://evil.example/a.jpg", "//evil.example/a.jpg", "/api/../secret", "data:image/png;base64,AA", 3):
        assert not is_image_ref(bad)


def test_encode_jpeg_bounds_longest_side_and_flattens_alpha():
    image = Image.open(io.BytesIO(encode_jpeg(png(3000, 1500, "RGBA"))))
    assert image.format == "JPEG" and image.mode == "RGB"
    assert max(image.size) == 1024
    small = Image.open(io.BytesIO(encode_jpeg(png(200, 100))))
    assert small.size == (200, 100)


def test_disabled_strips_all_images_without_loading():
    load, calls = fake_loader({})
    messages = [user_with_images("看图", "/api/a"), assistant_calls("c1"), tool_message("c1", "帧", images=["/api/b"])]
    out = run(materialize(messages, enabled=False, limit=8, load=load))
    assert image_count(out) == 0 and calls == []
    assert out[0]["content"] == [text_block("看图")]
    assert "images" not in out[2]


def test_openai_keeps_latest_and_places_tool_images_after_batch():
    images = {f"/api/{n}": png(1600, 900) for n in "abcd"}
    load, calls = fake_loader(images)
    messages = [
        user_with_images("第一张", "/api/a"),
        assistant_calls("c1", "c2"),
        tool_message("c1", "帧1", images=["/api/b"]),
        tool_message("c2", "帧2", images=["/api/c", "/api/d"]),
        user_message("继续"),
    ]
    snapshot = json.dumps(messages, sort_keys=True)
    out = run(materialize(messages, enabled=True, limit=3, load=load, protocol="openai"))
    # 输入不被修改（日志里只留引用）。
    assert json.dumps(messages, sort_keys=True) == snapshot
    assert sorted(calls) == ["/api/b", "/api/c", "/api/d"]
    assert out[0]["content"][-1] == text_block(OMITTED)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "user", "user"]
    assert all(b["type"] == "text" for m in out if m["role"] == "tool" for b in m["content"])
    synthetic = out[4]
    assert synthetic["source"]["kind"] == "context"
    pictures = [b for b in synthetic["content"] if b["type"] == "image"]
    assert len(pictures) == 3 and image_count(out) == 3
    assert max(decoded(pictures[0]).size) == 1024
    assert out[5]["content"] == [text_block("继续")]


def test_anthropic_puts_tool_images_into_tool_result():
    load, _ = fake_loader({"/api/b": png(64, 64)})
    messages = [assistant_calls("c1"), tool_message("c1", "帧", images=["/api/b"])]
    out = run(materialize(messages, enabled=True, limit=8, load=load, protocol="anthropic"))
    assert len(out) == 2
    assert [b["type"] for b in out[1]["content"]] == ["text", "image"]
    assert out[1]["content"][1]["mediaType"] == "image/jpeg"


def test_unreadable_image_is_replaced_and_limit_zero_omits_all():
    load, _ = fake_loader({"/api/ok": b"not an image"})
    messages = [user_with_images("图", "/api/missing", "/api/ok")]
    out = run(materialize(messages, enabled=True, limit=8, load=load))
    texts = [b["text"] for b in out[0]["content"][1:]]
    assert texts[0].startswith("（图片无法读取：") and "找不到图片" in texts[0]
    assert texts[1].startswith("（图片无法读取：")
    out = run(materialize(messages, enabled=True, limit=0, load=load))
    assert image_count(out) == 0 and out[0]["content"][1:] == [text_block(OMITTED)] * 2


def test_loader_reads_attachment_with_owner_check_and_rejects_foreign_urls(tmp_path):
    store = AttachmentStore(tmp_path / "data")
    upload = tmp_path / "in.png"
    upload.write_bytes(png(32, 32))
    ref = store.put(upload, filename="in.png", content_type="image/png", owner="session",
                    session_id="s1", ttl_hours=1)["id"]
    messages = [user_with_images("图", ref, "https://evil.example/x.png")]
    own = make_loader(attachment=store.resolver(session_id="s1", owner="session"))
    out = run(materialize(messages, enabled=True, limit=8, load=own))
    assert out[0]["content"][1]["type"] == "image"
    assert "本站" in out[0]["content"][2]["text"]
    other = make_loader(attachment=store.resolver(session_id="s2", owner="session"))
    out = run(materialize(messages, enabled=True, limit=8, load=other))
    assert image_count(out) == 0 and "不属于当前会话" in out[0]["content"][1]["text"]
