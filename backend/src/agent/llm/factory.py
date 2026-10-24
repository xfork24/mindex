"""按生效配置构造智能助手模型客户端。"""

from __future__ import annotations

from mindex.agent.llm.anthropic import AnthropicClient
from mindex.agent.llm.openai_compat import OpenAICompatClient


def make_agent_llm(config, *, transport=None):
    profile = config.agent.llm
    if profile.backend == "anthropic":
        return AnthropicClient(profile, transport=transport)
    return OpenAICompatClient(profile, transport=transport)
