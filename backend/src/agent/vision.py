"""模型视觉输入：发请求前把消息里的图片引用解析成内存中的图片块。

事件日志、SSE 与 ``derive_messages`` 的输出只含图片引用：用户消息里的
``{"type": "image", "ref": …}`` 块，以及工具结果消息的 ``images: [ref]``。``materialize`` 返回新的
消息列表（不修改输入），供协议适配器转换：

- ``enabled=False``：去掉全部图片块与 ``tool.images``，模型看不到任何图片；
- 否则按出现顺序只保留最近 ``limit`` 张，解析为最长边不超过 1024 像素的 JPEG（base64），
  更早的替换为文字「（图片已省略）」，解析失败的替换为「（图片无法读取：原因）」；
- 工具图片的位置按协议区分：``openai`` 在该批连续工具结果之后合成一条 user 消息
  （``source.kind=context``）承载图片，因为 OpenAI 兼容协议的 tool 消息只能是文本；
  ``anthropic`` 直接把图片块放进工具结果消息的 content（适配器转为 ``tool_result`` 内的 image 块）。
"""

from __future__ import annotations

import asyncio
import base64
import io
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from agent.llm.types import new_id, resolved_image_block, text_block

Protocol = Literal["openai", "anthropic"]
Loader = Callable[[str], Awaitable[bytes]]

MAX_SIDE = 1024
JPEG_QUALITY = 85
# 发给模型前的估算：每张图片按固定 token 计入上下文预算。
IMAGE_TOKENS = 800
MAX_SOURCE_PIXELS = 40_000_000
MAX_REASON = 120
ATTACHMENT_PREFIX = "attachment://"
OMITTED = "（图片已省略）"
TOOL_IMAGES_HEADER = "以下是工具结果中的图片："


class VisionError(Exception):
    """图片引用无法读取或解码；消息为中文，会写进替换文字。"""


def protocol_for(provider: str | None) -> Protocol:
    """按模型客户端的 ``provider`` 选择图片放置方式；未知协议按 OpenAI 兼容处理。"""
    return "anthropic" if provider == "anthropic" else "openai"


def is_image_ref(ref: Any) -> bool:
    """只接受附件引用或本站 ``/api/`` 相对地址（拒绝 ``//host`` 等协议相对地址）。"""
    return isinstance(ref, str) and (
        ref.startswith(ATTACHMENT_PREFIX)
        or (ref.startswith("/api/") and "\\" not in ref and ".." not in ref.split("?", 1)[0])
    )


def image_count(messages: list[dict]) -> int:
    """消息中的图片数（引用块、已解析块与 ``tool.images``），供 token 估算使用。"""
    total = 0
    for message in messages:
        total += sum(1 for block in message.get("content", []) if block.get("type") == "image")
        total += len(message.get("images") or [])
    return total


def encode_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    """解码任意 Pillow 支持的静态图片，按 EXIF 摆正、透明底铺白，缩到最长边不超过 ``max_side``，
    编码为 JPEG。
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.width * original.height > MAX_SOURCE_PIXELS:
                    raise VisionError("图片超过 4000 万像素")
                original.load()
                image = ImageOps.exif_transpose(original)
                image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                if image.mode != "RGB":
                    rgba = image.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    image = Image.alpha_composite(background, rgba).convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=JPEG_QUALITY)
                return output.getvalue()
    except VisionError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise VisionError("图片无法解码或文件损坏") from exc


def make_loader(*, attachment: Callable[[str], Any] | None = None, client: Any = None) -> Loader:
    """组装 ``materialize`` 的读取函数。

    - ``attachment://``：经 ``AttachmentStore.resolver(...)`` 一类解析器得到 ``(文件名, Path|bytes, 类型)``，
      解析器负责归属与有效期校验；
    - 本站 ``/api/`` 地址：经回环 ``MindexClient.fetch_bytes`` 读取；
    - 其他地址一律拒绝。
    """

    async def load(ref: str) -> bytes:
        if not is_image_ref(ref):
            raise VisionError("只支持附件引用或本站 /api/ 地址")
        if ref.startswith(ATTACHMENT_PREFIX):
            if attachment is None:
                raise VisionError("当前环境不能读取附件")
            _name, content, _ctype = attachment(ref)
            if isinstance(content, (bytes, bytearray)):
                return bytes(content)
            return await asyncio.to_thread(Path(content).read_bytes)
        if client is None:
            raise VisionError("当前环境不能读取站内图片")
        content, _ctype = await client.fetch_bytes(ref)
        return content

    return load


def _reason(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:MAX_REASON]


async def _resolve_all(refs: list[str], load: Loader) -> dict[str, dict | str]:
    """同一引用只读一次：成功得到已解析图片块，失败得到替换文字。"""

    async def one(ref: str) -> dict | str:
        try:
            data = await load(ref)
            jpeg = await asyncio.to_thread(encode_jpeg, data)
        except Exception as exc:  # noqa: BLE001 — 任何读取失败都替换为说明文字，本步继续
            return f"（图片无法读取：{_reason(exc)}）"
        return resolved_image_block(base64.b64encode(jpeg).decode("ascii"), "image/jpeg")

    unique = list(dict.fromkeys(refs))
    values = await asyncio.gather(*(one(ref) for ref in unique))
    return dict(zip(unique, values, strict=True))


def _strip(messages: list[dict]) -> list[dict]:
    result = []
    for message in messages:
        copy = {k: v for k, v in message.items() if k != "images"}
        if "content" in message:
            copy["content"] = [b for b in message["content"] if b.get("type") != "image"]
        result.append(copy)
    return result


async def materialize(
    messages: list[dict], *, enabled: bool, limit: int, load: Loader, protocol: Protocol = "openai"
) -> list[dict]:
    """返回发给模型的新消息列表；输入（来自事件日志）保持不变。"""
    if not enabled:
        return _strip(messages)
    # 按出现顺序登记每张图片：(消息序号, 块序号或 None 表示 tool.images, 引用)。
    slots: list[tuple[int, int | None, str]] = []
    for mi, message in enumerate(messages):
        for bi, block in enumerate(message.get("content", [])):
            if block.get("type") == "image" and "ref" in block:
                slots.append((mi, bi, block["ref"]))
        if message.get("role") == "tool":
            for ref in message.get("images") or []:
                slots.append((mi, None, ref))
    cutoff = max(0, len(slots) - max(0, limit))  # 序号 ≥ cutoff 的是最近 limit 张
    resolved = await _resolve_all([ref for _, _, ref in slots[cutoff:]], load)

    def replacement(index: int, ref: str) -> dict:
        if index < cutoff:
            return text_block(OMITTED)
        value = resolved[ref]
        return value if isinstance(value, dict) else text_block(value)

    # 每张图片在 slots 中的序号，便于按消息回填。
    by_message: dict[int, list[tuple[int | None, int, str]]] = {}
    for index, (mi, bi, ref) in enumerate(slots):
        by_message.setdefault(mi, []).append((bi, index, ref))

    result: list[dict] = []
    pending: list[tuple[str, list[dict]]] = []  # openai：本批工具结果的 (callId, 图片块)

    def flush() -> None:
        if not pending:
            return
        content = [text_block(TOOL_IMAGES_HEADER)]
        for call_id, blocks in pending:
            content.append(text_block(f"工具调用 {call_id} 的图片："))
            content.extend(blocks)
        result.append(
            {
                "id": new_id("msg"),
                "role": "user",
                "content": content,
                "source": {"kind": "context", "origin": "tool-images"},
            }
        )
        pending.clear()

    for mi, message in enumerate(messages):
        role = message.get("role")
        if role != "tool":
            flush()
        entries = by_message.get(mi, [])
        content = []
        block_images = {bi: (index, ref) for bi, index, ref in entries if bi is not None}
        for bi, block in enumerate(message.get("content", [])):
            if block.get("type") != "image":
                content.append(block)
            elif bi in block_images:
                content.append(replacement(*block_images[bi]))
            # 已解析或无引用的图片块不应出现在日志派生的消息里，直接丢弃。
        copy = {k: v for k, v in message.items() if k != "images"}
        copy["content"] = content
        tool_images = [(index, ref) for bi, index, ref in entries if bi is None]
        if tool_images:
            images, notes = [], []
            for index, ref in tool_images:
                block = replacement(index, ref)
                (images if block["type"] == "image" else notes).append(block)
            # 省略与失败的说明留在工具结果里；保留的图片按协议放置。
            copy["content"] = content + notes
            if protocol == "anthropic":
                copy["content"] += images
            elif images:
                pending.append((message.get("toolCallId", ""), images))
        result.append(copy)
    flush()
    return result
