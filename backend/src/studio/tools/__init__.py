"""第三方工具：工具源登记（``registry``）、HTTP manifest 与 MCP 客户端适配器、产物下载与校验。

分镜画面来源 ``kind=tool`` 由 invoke 阶段（``workspace/handlers/invoke.py``）调用；登记与测试连接的接口见
``studio/studio_tools_api.py``。
"""

from __future__ import annotations


def adapter(record: dict):
    """按协议构造适配器（连接设置中的 ``secret:名称`` 已解引用）。"""
    from .registry import resolve

    connection = resolve(record["connection"], record["secrets"])
    if record["protocol"] == "http":
        from .http import HttpAdapter

        return HttpAdapter(connection)
    from .mcp import McpAdapter

    return McpAdapter(record["protocol"], connection)
