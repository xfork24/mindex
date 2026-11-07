"""与运行方式无关的工具注册表。

工具定义沿用 dsh 的 ToolDefinition 语义：发给模型的只有 ``name``、``description``、
``parameters``（JSON Schema）；执行返回规范化的 JSON 值，``text`` 是给模型看的渲染文本，
``meta`` 是界面卡片用的展示数据（落库为 ``tool/result.meta``）。工具抛出 ``ToolError`` 表示失败，
面向用户的原因放在 ``reason``，不混入模型内容。

风险级别：``read`` 直接执行；``write`` 可逆写操作，预览后确认；``danger`` 删除、改模型或密钥、
构建等高风险操作，必须显式确认。
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from shared.agent_domains import AGENT_DOMAINS

Risk = Literal["read", "write", "danger"]
DOMAINS = AGENT_DOMAINS
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_TEXT = 12000


class ToolError(Exception):
    def __init__(self, message: str, code: str = "tool_error", reason: str | None = None, value: Any = None):
        super().__init__(message)
        self.code = code
        self.reason = reason or message
        self.value = value


@dataclass
class ToolOutput:
    value: Any
    text: str | None = None
    meta: Any = None
    # 成功写操作的作用范围：前端据此派发 mindex:agent-mutation 刷新宿主页面。
    mutation: dict | None = None
    # 提交构建后由运行时跟踪：{"job_id", "build_id"}。
    watch_build: dict | None = None
    # 提交后台任务后由运行时跟踪，终态时发 followup：
    # {"kind": "source"|"dialogue"|"person", "task_id", "source_id"?, "work"?}；
    # kind="build" 等价于 watch_build。
    watch_task: dict | None = None
    # 页面定位描述：{page, job_id, plan_id, shots, stage, section, role, profile_id, query, ...}。
    focus: dict | None = None
    # 面板媒体卡片：[{kind: image|audio|video, url, title, poster?, playlist?, collapsed}]，只允许本站地址。
    media: list[dict] | None = None
    # 交给模型看的图片引用：本站 /api/ 地址或 attachment://；运行时写进 tool 消息的 images（只存引用），
    # 视觉开启时由 agent.vision.materialize 在发请求前解析。
    images: list[str] | None = None

    def render(self) -> str:
        if self.text is not None:
            return self.text
        return dumps(self.value)


def dumps(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"…（已截断，原长 {len(text)} 字符）"
    return text


@dataclass
class ToolContext:
    """单次工具调用的环境：回环客户端、会话与页面上下文。"""

    client: Any
    session_id: str | None = None
    page: dict = field(default_factory=dict)
    # 当前激活的智能助手档案 id（自身档案保护用）。
    agent_profile_id: int | None = None
    extra: dict = field(default_factory=dict)

    def job_id(self, value: str | None) -> str:
        job = value or self.page.get("job_id")
        if not job:
            raise ToolError("未指定创作任务：请提供 job_id，或先用 list_jobs 查找", code="missing_job")
        return job


Handler = Callable[[Any, ToolContext], Awaitable[ToolOutput]]


@dataclass
class Preview:
    """确认卡片内容：``summary`` 一句话说明，``changes`` 结构化前后对比，``value`` 供模型参考。"""

    summary: str
    changes: list[dict] = field(default_factory=list)
    value: Any = None
    warnings: list[str] = field(default_factory=list)
    # 确认前先把页面定位到将要修改的位置。
    focus: dict | None = None

    def as_json(self) -> dict:
        return {"summary": self.summary, "changes": self.changes, "warnings": self.warnings}


@dataclass
class ToolSpec:
    name: str
    domain: str
    description: str
    input_model: type[BaseModel]
    risk: Risk
    execute: Handler
    preview: Callable[[Any, ToolContext], Awaitable[Preview]] | None = None
    # 不导出给 MCP（例如依赖界面交互的 ask_user_question）。
    mcp: bool = True

    def __post_init__(self):
        if not NAME_PATTERN.match(self.name):
            raise ValueError(f"工具名不合法：{self.name}")
        if self.domain not in DOMAINS:
            raise ValueError(f"未知工具领域：{self.domain}")

    def parameters(self) -> dict:
        return inline_schema(self.input_model.model_json_schema())

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters()}

    def parse(self, arguments: Any) -> BaseModel:
        if not isinstance(arguments, dict):
            raise ToolError("工具参数必须是 JSON 对象", code="invalid_arguments")
        try:
            return self.input_model.model_validate(arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in item['loc']) or '参数'}: {item['msg']}" for item in exc.errors()
            )
            raise ToolError(f"工具参数无效：{problems}", code="invalid_arguments") from exc


def inline_schema(schema: dict) -> dict:
    """展开 $defs/$ref 并去掉 title：部分 OpenAI 兼容服务不支持引用。"""
    definitions = schema.get("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                target = definitions[node["$ref"].split("/")[-1]]
                merged = {**copy.deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged)
            result = {}
            for k, v in node.items():
                if k in ("$defs", "title"):
                    continue
                if k == "properties" and isinstance(v, dict):
                    # 键是字段名而不是 schema 关键字：名为 title 的字段（如 rename_job.title）必须保留。
                    result[k] = {name: resolve(sub) for name, sub in v.items()}
                else:
                    result[k] = resolve(v)
            return result
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    result = resolve(schema)
    result.setdefault("type", "object")
    result.setdefault("properties", {})
    return result


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None):
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.add(spec)

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具重名：{spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def restrict(self, domains) -> ToolRegistry:
        allowed = set(domains)
        return ToolRegistry([s for s in self._specs.values() if s.domain in allowed])

    def schemas(self, first: tuple[str, ...] = (), domains=None) -> list[dict]:
        """``first`` 中的领域排在前面（当前页面相关的工具优先），其余保持注册顺序。

        ``domains`` 为 None 时不过滤（CLI 与 MCP 使用）；否则只返回这些领域的工具。
        """
        allowed = None if domains is None else set(domains)
        candidates = [s for s in self._specs.values() if allowed is None or s.domain in allowed]
        specs = sorted(candidates, key=lambda s: (s.domain not in first, s.domain != "meta"))
        return [spec.schema() for spec in specs]

    def domain_tools(self) -> dict[str, list[str]]:
        """领域 → 工具名（注册顺序）；没有工具的领域不出现。"""
        result: dict[str, list[str]] = {}
        for spec in self._specs.values():
            result.setdefault(spec.domain, []).append(spec.name)
        return result

    def __len__(self):
        return len(self._specs)
