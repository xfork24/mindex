"""分级确认策略（执行前钩子）。

- read：直接执行。
- write：可逆写操作，先给出预览，用户确认后执行；用户可授予「本会话内不再询问此工具」。
- danger：删除、改模型或密钥、构建等，每次都必须显式确认，不接受会话授权。

确认结果只有 dsh 的四种：``allowed-once | rejected | cancelled | unavailable``，只有 ``allowed-once``
放行。没有可用的确认通道时失败关闭（``unavailable`` → 拒绝）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OUTCOMES = ("allowed-once", "rejected", "cancelled", "unavailable")
RISK_ORDER = {"read": 0, "write": 1, "danger": 2}


@dataclass
class Decision:
    kind: Literal["allow", "ask", "deny"]
    reason: str = ""


def decide(spec, grants: list[str]) -> Decision:
    if spec.risk == "read":
        return Decision("allow")
    if spec.risk == "write" and spec.name in grants:
        return Decision("allow", "本会话已授权")
    return Decision("ask", "高风险操作，需要显式确认" if spec.risk == "danger" else "修改操作，需要确认")


def mcp_decide(spec, policy: str) -> Decision:
    """MCP 调用方没有确认交互（dsh 不读取注解、不支持 elicitation），按配置的最高风险级别兜底。"""
    limit = {"read": 0, "write": 1, "all": 2}[policy]
    if RISK_ORDER[spec.risk] <= limit:
        return Decision("allow")
    return Decision(
        "deny",
        f"工具 {spec.name} 属于 {spec.risk} 级操作，需要确认；当前 MCP 策略 agent.mcp.policy={policy} 不允许执行。"
        "请在 Mindex 页面的智能助手中操作，或由管理员调整策略。",
    )
