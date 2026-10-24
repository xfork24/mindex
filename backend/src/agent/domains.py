"""会话已加载工具领域的最近使用（LRU）维护。

``loaded`` 按最近使用排序，末尾为最近使用；meta 领域始终下发，不计入其中。
"""

from __future__ import annotations

from collections.abc import Iterable

from mindex.shared.agent_domains import LOADABLE_DOMAINS


def touch(loaded: list[str], domains: Iterable[str], limit: int = 3) -> list[str]:
    """把 domains 依次移到最近端（列表末尾），返回新列表，不修改入参。

    meta 与未知领域忽略（``loaded`` 里残留的也一并剔除）；结果无重复；
    超过 ``limit`` 时从头部淘汰最久未使用的领域。
    """
    known = set(LOADABLE_DOMAINS)
    result: list[str] = []
    for domain in [*loaded, *domains]:
        if domain not in known:
            continue
        if domain in result:
            result.remove(domain)
        result.append(domain)
    return result[-limit:] if limit > 0 else []
