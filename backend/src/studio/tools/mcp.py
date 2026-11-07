"""MCP 客户端适配器：stdio（本机进程，绝对路径与登记的环境变量）与 streamable-http；``list_tools`` 发现、
``call_tool`` 调用并从结果中解析产物。

``mcp`` 是可选组件（``uv sync --extra mcp``；桌面版已内置），只在调用时惰性导入，缺失时预检给出安装提示。
上传素材放进参数 ``uploads``（与 HTTP 适配器相同的形状）。产物解析顺序：图片内容（base64）、内嵌二进制资源、
资源链接（http(s) 地址）、``structuredContent`` 中的 ``url``/``urls``、文本里的 http(s) 地址。
"""

from __future__ import annotations

import base64
import contextlib
import re
from datetime import timedelta

from studio import studio

from .artifacts import Artifact

INSTALL_HINT = "第三方 MCP 工具需要 mcp 组件：在源码环境运行 uv sync --extra mcp（桌面版已内置）"
URL = re.compile(r"https?://[^\s\"'<>]+")


def unavailable() -> str | None:
    try:
        import mcp  # noqa: F401
    except ImportError:
        return INSTALL_HINT
    return None


class McpAdapter:
    def __init__(self, protocol: str, connection: dict):
        self.protocol, self.cfg = protocol, connection

    def _transport(self):
        if reason := unavailable():
            raise studio.StudioError(reason)
        if self.protocol == "mcp-stdio":
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=self.cfg["command"],
                args=list(self.cfg.get("args") or []),
                env=dict(self.cfg.get("env") or {}) or None,
                cwd=self.cfg.get("cwd"),
            )
            return stdio_client(params)
        return self._http()

    @contextlib.asynccontextmanager
    async def _http(self):
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        async with create_mcp_http_client(headers=self.cfg.get("headers") or None) as client:
            async with streamable_http_client(self.cfg["url"], http_client=client) as streams:
                yield streams

    async def _session(self, work, limit: float):
        import anyio
        from mcp import ClientSession

        with anyio.fail_after(limit):
            async with self._transport() as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await work(session)

    def _run(self, work, timeout: float):
        import anyio

        try:
            return anyio.run(self._session, work, timeout)
        except TimeoutError as exc:
            raise studio.StudioError(f"MCP 工具调用超时：{timeout:g} 秒内未完成") from exc
        except studio.StudioError:
            raise
        except Exception as exc:
            raise studio.StudioError(f"MCP 服务器调用失败：{_message(exc)}") from exc

    def list_tools(self, *, timeout: float = 30) -> list[dict]:
        async def work(session):
            result = await session.list_tools()
            return [{"name": tool.name, "description": tool.description} for tool in result.tools]

        return self._run(work, timeout)

    def call(
        self,
        tool: str,
        arguments: dict,
        uploads: list[dict],
        *,
        timeout: float,
        cancelled=lambda: False,
        notify=lambda status: None,
    ) -> tuple[list[Artifact], dict]:
        payload = {**arguments, **({"uploads": uploads} if uploads else {})}

        async def work(session):
            return await session.call_tool(tool, payload, read_timeout_seconds=timedelta(seconds=timeout))

        result = self._run(work, timeout)
        texts = [item.text for item in result.content if getattr(item, "type", None) == "text"]
        if result.isError:
            raise studio.StudioError(f"MCP 工具返回错误：{' '.join(texts)[:500] or '未说明原因'}")
        found = artifacts(result)
        if not found:
            raise studio.StudioError("MCP 工具结果中没有图片、视频或产物地址")
        return found, {
            "content": [getattr(item, "type", "?") for item in result.content],
            "text": " ".join(texts)[:500] or None,
        }


def _message(exc: BaseException) -> str:
    """anyio 任务组把异常包进 ExceptionGroup：取最内层的第一条。"""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return str(exc) or type(exc).__name__


def artifacts(result) -> list[Artifact]:
    found = []
    for item in result.content:
        kind = getattr(item, "type", None)
        if kind == "image":
            found.append(Artifact(data=base64.b64decode(item.data), mime=item.mimeType))
        elif kind == "resource":
            resource = item.resource
            if getattr(resource, "blob", None):
                found.append(Artifact(data=base64.b64decode(resource.blob), mime=resource.mimeType))
            elif str(resource.uri).startswith(("http://", "https://")):
                found.append(Artifact(url=str(resource.uri), mime=resource.mimeType))
        elif kind == "resource_link" and str(item.uri).startswith(("http://", "https://")):
            found.append(Artifact(url=str(item.uri), mime=item.mimeType))
    structured = getattr(result, "structuredContent", None) or {}
    if not found and isinstance(structured, dict):
        urls = structured.get("urls") or ([structured["url"]] if structured.get("url") else [])
        found.extend(Artifact(url=url) for url in urls if isinstance(url, str) and URL.fullmatch(url))
    if not found:
        for item in result.content:
            if getattr(item, "type", None) == "text":
                found.extend(Artifact(url=url) for url in URL.findall(item.text))
    return found
