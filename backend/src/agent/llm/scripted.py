"""测试专用：按脚本回放的确定性模型，绝不代表真实模型质量。

脚本是一个列表，每次 ``stream`` 消费一项。每项可以是：

- 内容块列表：``[{"type": "text", "text": "..."}, {"type": "tool-call", ...}]``
- 字典：``{"blocks": [...], "finish": "stop", "delay": 0.1, "error": "..."}``
- 可调用对象：``fn(request) -> 上述两种之一``，用于依据真实工具结果动态构造下一步

快捷构造：``say(text)`` 与 ``call(name, **arguments)``。
"""

from __future__ import annotations

import asyncio
import copy
import json

from mindex.agent.llm.types import LlmError, LlmRequest, StreamEmitter, new_id, text_block, tool_call_block


def say(text: str) -> dict:
    return text_block(text)


def call(tool: str, /, call_id: str | None = None, **arguments) -> dict:
    """``tool`` 只能按位置传入，工具参数里的 ``name`` 等字段因此不会冲突。"""
    return tool_call_block(call_id or new_id("call"), tool, json.dumps(arguments, ensure_ascii=False))


class ScriptedLlm:
    provider = "scripted"
    model = "scripted-test"

    def __init__(self, script=None):
        self.script = list(script or [])
        self.requests: list[LlmRequest] = []

    def extend(self, *items):
        self.script.extend(items)

    async def stream(self, request: LlmRequest):
        self.requests.append(copy.deepcopy(request))
        item = self.script.pop(0) if self.script else [say("（脚本已结束）")]
        if callable(item):
            item = item(request)
        spec = item if isinstance(item, dict) and "blocks" in item else {"blocks": item}
        if spec.get("delay"):
            await asyncio.sleep(spec["delay"])
        if spec.get("error"):
            raise LlmError(spec["error"], code=spec.get("code", "scripted_error"))
        emitter = StreamEmitter()
        for position, block in enumerate(spec["blocks"]):
            key = f"b{position}"
            if block["type"] in ("text", "reasoning"):
                text = block["text"]
                pieces = [text[i:i + 8] for i in range(0, len(text), 8)] or [""]
                for piece in pieces:
                    for chunk in emitter.text(key, piece, block["type"]):
                        yield chunk
                    await asyncio.sleep(spec.get("chunk_delay", 0))
            else:
                args = block["arguments"]
                half = len(args) // 2
                for index, piece in enumerate((args[:half], args[half:])):
                    for chunk in emitter.tool(key, call_id=block["id"], name=block["name"] if index == 0 else None,
                                              delta=piece):
                        yield chunk
            for chunk in emitter.close(key):
                yield chunk
        yield {"type": "usage", "usage": {"inputTokens": 10, "outputTokens": 5}}
        finish = spec.get("finish") or (
            "tool-calls" if any(b["type"] == "tool-call" for b in spec["blocks"]) else "stop"
        )
        yield {"type": "finish", "reason": {"kind": finish}}
