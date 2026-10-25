"""智能助手工具领域名。

shared 只保留领域名元组，供 ``shared/config.py`` 校验 ``agent.mcp.domains``；
领域说明、页面领域映射等助手策略内容在 ``mindex.agent.domains``。
本模块不得导入 ``mindex.agent``，避免循环依赖。
"""

from __future__ import annotations

# 全部工具领域；meta 领域的工具每一步都会下发。
AGENT_DOMAINS: tuple[str, ...] = (
    "meta",
    "studio",
    "studio_detail",
    "studio_post",
    "library",
    "search",
    "index",
    "persons",
    "settings",
    "topics",
)

# 可按需加载的领域（不含 meta）。
LOADABLE_DOMAINS: tuple[str, ...] = AGENT_DOMAINS[1:]
