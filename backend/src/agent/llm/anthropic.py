"""Anthropic 原生 Messages API（``/v1/messages``）工具调用协议。

请求：``system`` 独立字段，工具为 ``{name, description, input_schema}``；工具结果并入 user
消息的 ``tool_result`` 块，相邻同角色消息合并以满足严格的 user/assistant 交替。
流式：``content_block_start/delta/stop`` 映射为 dsh StreamChunk；``stop_reason`` 映射为结束原因。
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from mindex.agent.llm.types import LlmError, LlmRequest, StreamEmitter, message_text, parse_arguments

RETRY_STATUS = {429, 500, 502, 503, 504, 529}
API_VERSION = "2023-06-01"
STOP = {"end_turn": "stop", "tool_use": "tool-calls", "max_tokens": "max-tokens", "stop_sequence": "stop"}


def convert_messages(messages: list[dict]) -> list[dict]:
    result: list[dict] = []

    def push(role, blocks):
        if not blocks:
            return
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message["role"]
        if role == "user":
            push("user", [{"type": "text", "text": message_text(message) or "（空）"}])
        elif role == "assistant":
            blocks = []
            for block in message.get("content", []):
                if block["type"] == "text" and block["text"]:
                    blocks.append({"type": "text", "text": block["text"]})
                elif block["type"] == "tool-call":
                    try:
                        arguments = parse_arguments(block["arguments"])
                    except json.JSONDecodeError:
                        arguments = {}
                    blocks.append({"type": "tool_use", "id": block["id"], "name": block["name"],
                                   "input": arguments if isinstance(arguments, dict) else {}})
            push("assistant", blocks or [{"type": "text", "text": "（空）"}])
        elif role == "tool":
            push("user", [{
                "type": "tool_result",
                "tool_use_id": message["toolCallId"],
                "content": message_text(message) or "（无内容）",
                **({"is_error": True} if message.get("isError") else {}),
            }])
    if result and result[0]["role"] != "user":
        result.insert(0, {"role": "user", "content": [{"type": "text", "text": "（继续）"}]})
    return result


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, profile, *, transport: httpx.AsyncBaseTransport | None = None, retries: int = 2):
        self.profile = profile
        self.model = profile.model
        self.transport = transport
        self.retries = retries

    def headers(self) -> dict:
        key = getattr(self.profile, "api_key", "") or os.environ.get(self.profile.api_key_env, "")
        headers = {"content-type": "application/json", "anthropic-version": API_VERSION}
        if key:
            headers["x-api-key"] = key
        return headers

    def url(self) -> str:
        base = self.profile.base_url.rstrip("/")
        return base + ("/messages" if base.endswith("/v1") else "/v1/messages")

    def payload(self, request: LlmRequest) -> dict:
        body = {
            "model": self.profile.model,
            "max_tokens": min(request.max_tokens, self.profile.max_tokens),
            "temperature": request.temperature,
            "system": request.system,
            "messages": convert_messages(request.messages),
            "stream": True,
        }
        if request.tools:
            body["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                for t in request.tools
            ]
        return body

    async def stream(self, request: LlmRequest):
        body = self.payload(request)
        async with httpx.AsyncClient(timeout=self.profile.timeout, transport=self.transport) as client:
            for attempt in range(self.retries + 1):
                try:
                    async with client.stream("POST", self.url(), json=body, headers=self.headers()) as response:
                        if response.status_code >= 400:
                            detail = (await response.aread()).decode("utf-8", "replace")[:500]
                            if response.status_code in RETRY_STATUS and attempt < self.retries:
                                await asyncio.sleep(0.5 * 2**attempt)
                                continue
                            raise LlmError(
                                f"智能助手模型服务返回 HTTP {response.status_code}：{detail}",
                                code=f"http_{response.status_code}",
                                status=response.status_code,
                            )
                        async for chunk in self._parse(response):
                            yield chunk
                        return
                except httpx.TransportError as exc:
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * 2**attempt)
                        continue
                    raise LlmError(f"无法连接智能助手模型服务：{exc}", code="network") from exc

    async def _parse(self, response: httpx.Response):
        emitter = StreamEmitter()
        keys: dict[int, str] = {}
        stop = None
        usage = {"inputTokens": 0, "outputTokens": 0}
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError as exc:
                raise LlmError("智能助手模型服务返回了无法解析的流式数据", code="bad_stream") from exc
            kind = event.get("type")
            if kind == "error":
                error = event.get("error") or {}
                raise LlmError(f"智能助手模型服务报错：{error.get('message') or error}",
                               code=error.get("type") or "provider_error")
            if kind == "message_start":
                raw = (event.get("message") or {}).get("usage") or {}
                usage["inputTokens"] = int(raw.get("input_tokens") or 0)
                usage["outputTokens"] = int(raw.get("output_tokens") or 0)
                if raw.get("cache_read_input_tokens"):
                    usage["cacheReadTokens"] = int(raw["cache_read_input_tokens"])
                if raw.get("cache_creation_input_tokens"):
                    usage["cacheWriteTokens"] = int(raw["cache_creation_input_tokens"])
            elif kind == "content_block_start":
                index, block = event["index"], event.get("content_block") or {}
                key = keys[index] = f"block:{index}"
                if block.get("type") == "tool_use":
                    for chunk in emitter.tool(key, call_id=block.get("id"), name=block.get("name"), delta=""):
                        yield chunk
                elif block.get("type") == "text" and block.get("text"):
                    for chunk in emitter.text(key, block["text"]):
                        yield chunk
                elif block.get("type") == "thinking" and block.get("thinking"):
                    for chunk in emitter.text(key, block["thinking"], "reasoning"):
                        yield chunk
            elif kind == "content_block_delta":
                key = keys.setdefault(event["index"], f"block:{event['index']}")
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    for chunk in emitter.text(key, delta.get("text", "")):
                        yield chunk
                elif delta.get("type") == "thinking_delta":
                    for chunk in emitter.text(key, delta.get("thinking", ""), "reasoning"):
                        yield chunk
                elif delta.get("type") == "input_json_delta":
                    for chunk in emitter.tool(key, call_id=None, name=None, delta=delta.get("partial_json", "")):
                        yield chunk
            elif kind == "content_block_stop":
                for chunk in emitter.close(keys.get(event["index"], f"block:{event['index']}")):
                    yield chunk
            elif kind == "message_delta":
                stop = (event.get("delta") or {}).get("stop_reason") or stop
                raw = event.get("usage") or {}
                if raw.get("output_tokens") is not None:
                    usage["outputTokens"] = int(raw["output_tokens"])
            elif kind == "message_stop":
                break
        for chunk in emitter.close_all():
            yield chunk
        yield {"type": "usage", "usage": usage}
        yield {"type": "finish", "reason": {"kind": STOP.get(stop or "end_turn", "stop")}}
