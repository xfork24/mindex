"""OpenAI 兼容 ``/chat/completions`` 工具调用协议（DeepSeek、Qwen、GLM、自建服务等）。

图片：user 消息中的已解析图片块转为 ``image_url`` 数据 URL；tool 消息只能是文本，工具结果的图片由
``agent.vision.materialize`` 放进紧随其后的合成 user 消息。

流式解析 SSE：``delta.content`` → text 块，``delta.reasoning_content`` → reasoning 块，
``delta.tool_calls[i]`` 按 index 累积参数片段。``finish_reason`` 映射为 dsh 结束原因，
``usage`` 映射为 TokenUsage（inputTokens 不含缓存命中）。
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx

from mindex.agent.llm.types import LlmError, LlmRequest, StreamEmitter, message_text, tool_calls

RETRY_STATUS = {429, 500, 502, 503, 504}


def user_content(message: dict) -> str | list[dict]:
    """user 消息内容：没有已解析图片时为纯文本（兼容不支持数组内容的服务）；
    有图片时按块顺序输出 ``text`` / ``image_url``（数据 URL）数组。

    只有带 ``data`` 的已解析图片块才发出：未解析的引用块（只有 ``ref``）防御性跳过，
    正常情况下 ``agent.vision.materialize`` 已经替换掉它们。
    """
    blocks = message.get("content", [])
    if not any(b.get("type") == "image" and b.get("data") for b in blocks):
        return message_text(message)
    parts: list[dict] = []
    for block in blocks:
        if block.get("type") == "text" and block.get("text"):
            parts.append({"type": "text", "text": block["text"]})
        elif block.get("type") == "image" and block.get("data"):
            media_type = block.get("mediaType") or "image/jpeg"
            parts.append({"type": "image_url",
                          "image_url": {"url": f"data:{media_type};base64,{block['data']}"}})
    return parts


def convert_messages(system: str, messages: list[dict]) -> list[dict]:
    result = [{"role": "system", "content": system}] if system else []
    for message in messages:
        role = message["role"]
        if role == "user":
            result.append({"role": "user", "content": user_content(message)})
        elif role == "assistant":
            calls = tool_calls(message)
            item = {"role": "assistant", "content": message_text(message) or None}
            if calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {"name": call["name"], "arguments": call["arguments"] or "{}"},
                    }
                    for call in calls
                ]
            if item["content"] is None and not calls:
                item["content"] = ""
            result.append(item)
        elif role == "tool":
            result.append(
                {"role": "tool", "tool_call_id": message["toolCallId"], "content": message_text(message)}
            )
    return result


def convert_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
        }
        for t in tools
    ]


def map_usage(raw: dict) -> dict:
    prompt = int(raw.get("prompt_tokens") or 0)
    cached = int(
        (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
        or raw.get("prompt_cache_hit_tokens")
        or 0
    )
    usage = {"inputTokens": max(prompt - cached, 0), "outputTokens": int(raw.get("completion_tokens") or 0)}
    if cached:
        usage["cacheReadTokens"] = cached
    if raw.get("total_tokens") is not None:
        usage["totalTokens"] = int(raw["total_tokens"])
    return usage


FINISH = {"stop": "stop", "tool_calls": "tool-calls", "function_call": "tool-calls", "length": "max-tokens"}


class OpenAICompatClient:
    provider = "openai-compatible"

    def __init__(self, profile, *, transport: httpx.AsyncBaseTransport | None = None, retries: int = 2):
        self.profile = profile
        self.model = profile.model
        self.transport = transport
        self.retries = retries

    def headers(self) -> dict:
        key = getattr(self.profile, "api_key", "") or os.environ.get(self.profile.api_key_env, "")
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def payload(self, request: LlmRequest) -> dict:
        body = {
            "model": self.profile.model,
            "messages": convert_messages(request.system, request.messages),
            "max_tokens": min(request.max_tokens, self.profile.max_tokens),
            "temperature": request.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            body["tools"] = convert_tools(request.tools)
            body["tool_choice"] = "auto"
        return body

    async def stream(self, request: LlmRequest):
        url = self.profile.base_url.rstrip("/") + "/chat/completions"
        body = self.payload(request)
        async with httpx.AsyncClient(timeout=self.profile.timeout, transport=self.transport) as client:
            for attempt in range(self.retries + 1):
                try:
                    async with client.stream("POST", url, json=body, headers=self.headers()) as response:
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
        finish = None
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError as exc:
                raise LlmError("智能助手模型服务返回了无法解析的流式数据", code="bad_stream") from exc
            if event.get("error"):
                raise LlmError(f"智能助手模型服务报错：{event['error']}", code="provider_error")
            if event.get("usage"):
                yield {"type": "usage", "usage": map_usage(event["usage"])}
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    for chunk in emitter.text("reasoning", delta["reasoning_content"], "reasoning"):
                        yield chunk
                if delta.get("content"):
                    for chunk in emitter.text("text", delta["content"]):
                        yield chunk
                for call in delta.get("tool_calls") or []:
                    function = call.get("function") or {}
                    for chunk in emitter.tool(
                        f"tool:{call.get('index', 0)}",
                        call_id=call.get("id"),
                        name=function.get("name"),
                        delta=function.get("arguments") or "",
                    ):
                        yield chunk
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        for chunk in emitter.close_all():
            yield chunk
        yield {"type": "finish", "reason": {"kind": FINISH.get(finish or "stop", "stop")}}
