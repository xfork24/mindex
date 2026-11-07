"""元工具：按领域加载工具、向用户提问（含密钥表单）与记录能力缺口。

``ask_user_question`` 的参数是 dsh 同名工具的超集：``questions[]{id, question, header?, options?,
multi_select?}``，
另加 ``fields``（text/number/select/secret）。结果形状同 dsh：``{answers[]{id, selected[], custom?}}``，另加
``values{fieldId: 值 | 'secret://<id>'}``——secret 字段的明文只进入本机保险库，模型只拿到引用。
该工具依赖界面交互，不导出到 MCP；``load_tools`` 依赖会话的已加载领域，同样不导出。
"""

from __future__ import annotations

import inspect
from typing import Literal

from pydantic import Field

from agent.registry import ToolContext, ToolError, ToolOutput, ToolSpec, dumps
from shared.agent_domains import LOADABLE_DOMAINS
from shared.config import StrictModel


class Option(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=400)


class FormField(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    label: str = Field(min_length=1, max_length=100)
    type: Literal["text", "number", "select", "secret"] = Field(
        default="text", description="secret 用于 API Key 等密钥：明文不会返回给你，只返回 secret:// 引用"
    )
    required: bool = False
    options: list[str] | None = Field(default=None, max_length=50, description="type=select 时的可选值")
    placeholder: str | None = Field(default=None, max_length=200)
    default: str | None = Field(default=None, max_length=400, description="预填值（secret 字段不支持）")


class Question(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    question: str = Field(min_length=1, max_length=1000)
    header: str | None = Field(default=None, max_length=60)
    options: list[Option] | None = Field(default=None, max_length=12)
    multi_select: bool = False
    fields: list[FormField] | None = Field(default=None, max_length=12, description="需要用户填写的表单字段")


class AskUser(StrictModel):
    questions: list[Question] = Field(min_length=1, max_length=6)


async def ask_user_question(args: AskUser, ctx: ToolContext) -> ToolOutput:
    runtime = ctx.extra.get("runtime")
    if runtime is None or ctx.session_id is None:
        raise ToolError("当前环境无法向用户提问", code="unavailable")
    questions = [q.model_dump(exclude_none=True) for q in args.questions]
    answer = await runtime.ask_question(ctx.session_id, ctx.extra.get("call_id"), questions)
    if answer.get("cancelled"):
        raise ToolError("用户取消了提问", code="cancelled")
    value = {"answers": answer["answers"]}
    return ToolOutput(value, text=dumps(value))


class Gap(StrictModel):
    request: str = Field(min_length=1, max_length=1000, description="用户想做而现有能力做不到的事")
    reason: str = Field(min_length=1, max_length=1000, description="为什么现有工具或产品能力做不到")
    suggestion: str = Field(default="", max_length=1000, description="已给用户的替代做法")


async def report_capability_gap(args: Gap, ctx: ToolContext) -> ToolOutput:
    record = ctx.extra.get("record_gap")
    if record is None:
        raise ToolError("当前环境无法记录能力缺口", code="unavailable")
    item = record(ctx.session_id, args.request, args.reason, args.suggestion)
    if inspect.isawaitable(item):  # 远程 MCP 模式经 HTTP 记录
        item = await item
    return ToolOutput(
        {"gap_id": item["id"]},
        text=f"已记录能力缺口 #{item['id']}。",
        meta={"kind": "gap", **args.model_dump()},
    )


# Literal 取值来自领域常量：未知领域或 meta 在参数校验阶段即报 invalid_arguments。
LoadableDomain = Literal[LOADABLE_DOMAINS]  # type: ignore[valid-type]


class LoadTools(StrictModel):
    domains: list[LoadableDomain] = Field(
        min_length=1,
        max_length=3,
        description="要加载的工具领域（1–3 个）；最多同时保持 3 个，最久未用的会被卸载",
    )


async def load_tools(args: LoadTools, ctx: ToolContext) -> ToolOutput:
    runtime = ctx.extra.get("runtime")
    if runtime is None or ctx.session_id is None:
        raise ToolError("当前环境不支持按需加载工具", code="unavailable")
    loaded = runtime.load_domains(ctx.session_id, list(args.domains))
    names = runtime.registry.domain_tools()
    tools = {domain: names.get(domain, []) for domain in loaded}
    lines = [f"已加载领域：{'、'.join(loaded)}（下一步起可用）。"]
    lines += [f"- {domain}：{'、'.join(items) or '（暂无工具）'}" for domain, items in tools.items()]
    return ToolOutput({"loaded": loaded, "tools": tools}, text="\n".join(lines))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "load_tools",
            "meta",
            "按领域加载工具：需要的工具属于未加载领域时调用，加载后从下一步起可用。"
            "可选领域：" + "、".join(LOADABLE_DOMAINS) + "；最多同时保持 3 个领域，最久未用的会被卸载。",
            LoadTools,
            "read",
            load_tools,
            mcp=False,
        ),
        ToolSpec(
            "ask_user_question",
            "meta",
            "向用户提问并等待回答：可给出选项，或用 fields 收集表单信息（如模型 endpoint、模型名）。"
            "密钥必须使用 type=secret 字段收集，你只会拿到 secret:// 引用，原样传给需要密钥的工具即可。",
            AskUser,
            "read",
            ask_user_question,
            mcp=False,
        ),
        ToolSpec(
            "report_capability_gap",
            "meta",
            "记录用户提出、但 Mindex "
            "现有能力做不到的需求（供产品后续补齐）。先向用户如实说明并给出替代做法，再调用。",
            Gap,
            "read",
            report_capability_gap,
        ),
    ]
