"""第三方工具源 API：``/api/studio/tool-sources``（列出、读取、登记或整体替换、删除、测试连接）。

密钥只写不读（视图只给名称与是否已填写）；删除被分镜使用的工具源返回 409。测试连接会实际连接服务：
HTTP 请求 tools_path（或 base_url），MCP 启动 / 连接服务器并 ``list_tools``。挂在创作路由上，删除与方案修改共用
studio 修改锁。Web、CLI、智能助手与 MCP 共用。
"""

from __future__ import annotations

import time

from fastapi import HTTPException

from mindex.studio import studio
from mindex.studio.tools import adapter, registry
from mindex.studio.tools.registry import ToolInUse, ToolSourceSpec


def tool_routes(router, base, store, mutation_lock):
    @router.get("/tool-sources")
    def list_tool_sources():
        return {"items": registry.list_sources(store)}

    @router.get("/tool-sources/{source_id}")
    def get_tool_source(source_id: str):
        return registry.view(registry.get_source(store, source_id))

    @router.put("/tool-sources/{source_id}")
    def save_tool_source(source_id: str, body: ToolSourceSpec):
        return registry.save_source(store, source_id, body, now=studio.now())

    @router.delete("/tool-sources/{source_id}")
    def delete_tool_source(source_id: str):
        with mutation_lock():
            try:
                registry.delete_source(base, store, source_id)
            except ToolInUse as exc:
                raise HTTPException(409, str(exc)) from exc
        return {"deleted": source_id}

    @router.post("/tool-sources/{source_id}/test")
    def test_tool_source(source_id: str):
        """连接工具源并列出服务端工具；与白名单比对，返回缺失（白名单中有、服务端没有）的工具名。"""
        record = registry.get_source(store, source_id)
        started = time.monotonic()
        try:
            found = adapter(record).list_tools()
        except studio.StudioError as exc:
            return {"ok": False, "error": str(exc), "elapsed_ms": round((time.monotonic() - started) * 1000)}
        names = {item["name"] for item in found or []}
        missing = [tool["name"] for tool in record["tools"] if found is not None and tool["name"] not in names]
        return {"ok": True, "tools": found, "missing": missing,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
