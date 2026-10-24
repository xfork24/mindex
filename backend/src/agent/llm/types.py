"""与 deepseek-harness（dsh）对齐的模型词汇。

全部用普通 JSON 字典表示，字段名逐字沿用 dsh（camelCase），便于事件日志原样落库、
SSE 原样转发，未来切换到 dsh 运行时也无需转换：

- 内容块：``text`` / ``reasoning`` / ``tool-call{id, name, arguments: 原始 JSON 字符串}``
- 消息：``{id, role, content[], source}``；工具结果消息另有 ``toolCallId`` 与 ``isError``
- 流式块（StreamChunk）：``block-start`` / ``text-delta`` / ``reasoning-delta`` /
  ``tool-call-delta{id, name?, argumentsDelta}`` / ``block-end{block}`` / ``usage`` / ``finish``
- 结束原因：``stop`` / ``tool-calls`` / ``max-tokens`` / ``aborted`` / ``error``（后两者带 failure）
- 用量（TokenUsage）：``inputTokens``（不含缓存命中）/ ``outputTokens`` / ``cacheReadTokens?``
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:16]}"


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def tool_call_block(call_id: str, name: str, arguments: str) -> dict:
    return {"type": "tool-call", "id": call_id, "name": name, "arguments": arguments}


def user_message(text: str, source_kind: str = "user", **source) -> dict:
    return {
        "id": new_id("msg"),
        "role": "user",
        "content": [text_block(text)],
        "source": {"kind": source_kind, **source},
    }


def assistant_message(content: list[dict], provider: str, model: str) -> dict:
    return {
        "id": new_id("msg"),
        "role": "assistant",
        "content": content,
        "source": {"kind": "model", "provider": provider, "model": model},
    }


def tool_message(call_id: str, text: str, *, is_error: bool = False) -> dict:
    message = {
        "id": new_id("msg"),
        "role": "tool",
        "toolCallId": call_id,
        "content": [text_block(text)],
        "source": {"kind": "tool", "callId": call_id},
    }
    if is_error:
        message["isError"] = True
    return message


def message_text(message: dict, kinds: tuple[str, ...] = ("text",)) -> str:
    return "".join(block.get("text", "") for block in message.get("content", []) if block.get("type") in kinds)


def tool_calls(message: dict) -> list[dict]:
    return [block for block in message.get("content", []) if block.get("type") == "tool-call"]


def parse_arguments(arguments: str) -> Any:
    """工具参数是原始 JSON 字符串；空串视为空对象。"""
    return json.loads(arguments) if arguments.strip() else {}


class LlmError(Exception):
    """模型调用失败；``code`` 稳定可判，``status`` 为上游 HTTP 状态。"""

    def __init__(self, message: str, code: str = "llm_error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status

    def failure(self) -> dict:
        value = {"message": str(self), "code": self.code}
        if self.status is not None:
            value["status"] = self.status
        return value


@dataclass
class LlmRequest:
    system: str
    messages: list[dict]
    tools: list[dict] = field(default_factory=list)  # ToolSchema: {name, description, parameters}
    max_tokens: int = 4096
    temperature: float = 0.2


class LlmClient(Protocol):
    provider: str
    model: str

    def stream(self, request: LlmRequest) -> AsyncIterator[dict]:
        """逐个产出 StreamChunk；失败时抛出 LlmError。"""
        ...


@dataclass
class Completion:
    content: list[dict]
    finish: dict
    usage: dict | None = None


class Assembler:
    """把 StreamChunk 序列还原为完整内容块；运行时边转发边累积。"""

    def __init__(self):
        self.blocks: dict[int, dict] = {}
        self.finish: dict | None = None
        self.usage: dict | None = None

    def feed(self, chunk: dict) -> None:
        kind = chunk["type"]
        if kind == "block-end":
            self.blocks[chunk["index"]] = chunk["block"]
        elif kind == "usage":
            self.usage = chunk["usage"]
        elif kind == "finish":
            self.finish = chunk["reason"]

    def result(self) -> Completion:
        content = [self.blocks[index] for index in sorted(self.blocks)]
        # 空文本块对模型与界面都没有意义，去掉以免历史里残留空消息。
        content = [b for b in content if not (b["type"] in ("text", "reasoning") and not b["text"])]
        finish = self.finish or {"kind": "stop"}
        if finish["kind"] == "stop" and any(b["type"] == "tool-call" for b in content):
            finish = {"kind": "tool-calls"}
        return Completion(content=content, finish=finish, usage=self.usage)


class StreamEmitter:
    """协议适配器共用：按块分配索引并产出 block-start / delta / block-end。"""

    def __init__(self):
        self.next_index = 0
        self.open: dict[str, dict] = {}  # key -> {"index", "block"}

    def _start(self, key: str, block: dict) -> list[dict]:
        index = self.next_index
        self.next_index += 1
        self.open[key] = {"index": index, "block": block}
        return [{"type": "block-start", "index": index, "blockType": block["type"]}]

    def text(self, key: str, text: str, kind: str = "text") -> list[dict]:
        chunks = []
        if key not in self.open:
            chunks += self._start(key, {"type": kind, "text": ""})
        entry = self.open[key]
        entry["block"]["text"] += text
        delta = "text-delta" if kind == "text" else "reasoning-delta"
        chunks.append({"type": delta, "index": entry["index"], "text": text})
        return chunks

    def tool(self, key: str, *, call_id: str | None, name: str | None, delta: str) -> list[dict]:
        chunks = []
        if key not in self.open:
            chunks += self._start(key, tool_call_block(call_id or new_id("call"), name or "", ""))
        entry = self.open[key]
        block = entry["block"]
        if name and not block["name"]:
            block["name"] = name
        block["arguments"] += delta
        chunk = {"type": "tool-call-delta", "index": entry["index"], "id": block["id"], "argumentsDelta": delta}
        if name:
            chunk["name"] = name
        chunks.append(chunk)
        return chunks

    def close(self, key: str) -> list[dict]:
        entry = self.open.pop(key, None)
        if entry is None:
            return []
        return [{"type": "block-end", "index": entry["index"], "block": entry["block"]}]

    def close_all(self) -> list[dict]:
        chunks = []
        for key in sorted(self.open, key=lambda k: self.open[k]["index"]):
            chunks += self.close(key)
        return chunks


async def collect(client: LlmClient, request: LlmRequest) -> Completion:
    assembler = Assembler()
    async for chunk in client.stream(request):
        assembler.feed(chunk)
    return assembler.result()
