"""Private, session-bound MCP listener. Never uses the public MCP policy or token."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from contextlib import asynccontextmanager
from uuid import uuid4


@asynccontextmanager
async def bridge(runtime, sid):
    import mcp.types as types
    import uvicorn
    from mcp.server.lowlevel import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route

    from mindex.agent.mcp_server import StreamableEndpoint

    server = Server("mindex", instructions="Use load_tools before calling tools in unloaded domains.")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(name=s.name, description=s.description, inputSchema=s.parameters())
            for s in runtime.registry.all()
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        # SDK notification and MCP HTTP arrive on independent channels. Wait for
        # the original call so approvals and result metadata use its actual id.
        native = None
        for _ in range(100):
            native = next(
                (
                    e["data"]
                    for e in reversed(runtime.store.events(sid))
                    if e["type"] == "tool/call"
                    and e["data"]["name"] == f"mcp__mindex__{name}"
                    and (sid, e["data"]["callId"]) not in runtime.used_calls
                ),
                None,
            )
            if native:
                break
            await asyncio.sleep(0.01)
        call = {
            "id": native["callId"] if native else f"bridge-{uuid4().hex}",
            "name": name,
            "arguments": json.dumps(arguments),
        }
        runtime.used_calls.add((sid, call["id"]))
        turn = runtime.store.last_turn(sid)
        step = native.get("step", 0) if native else 0
        if not native:
            runtime._append(
                sid,
                "tool/call",
                {
                    "turn": turn,
                    "step": step,
                    "callId": call["id"],
                    "name": name,
                    "arguments": call["arguments"],
                },
            )
        await runtime._call_tool(sid, turn, step, call, runtime.store.get(sid)["context"] or {})
        result = runtime.bridge_results[(sid, call["id"])]
        content = [
            types.TextContent(type="text", text=b["text"])
            for b in result["message"]["content"]
            if b["type"] == "text"
        ]
        if runtime.settings().llm.vision and runtime.settings().max_images:
            import base64

            from mindex.agent.vision import encode_jpeg

            for ref in (result["message"].get("images") or [])[-runtime.settings().max_images :]:
                data = encode_jpeg(await runtime._image_loader(sid)(ref))
                content.append(
                    types.ImageContent(
                        type="image", data=base64.b64encode(data).decode(), mimeType="image/jpeg"
                    )
                )
        return types.CallToolResult(content=content, isError="error" in result)

    token = secrets.token_urlsafe(32)
    manager = StreamableHTTPSessionManager(app=server, stateless=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    app = Starlette(
        routes=[Route("/mcp", StreamableEndpoint(manager, token), methods=["GET", "POST", "DELETE"])]
    )
    service = uvicorn.Server(
        uvicorn.Config(
            app, log_level="critical", lifespan="off", access_log=False, timeout_graceful_shutdown=0.2
        )
    )
    task = None
    try:
        async with manager.run():
            task = asyncio.create_task(service.serve(sockets=[sock]))
            while not service.started:
                if task.done():
                    await task
                    raise RuntimeError("内部 MCP 服务启动失败")
                await asyncio.sleep(0.01)
            try:
                yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", token
            finally:
                service.should_exit = True
                await task
    finally:
        sock.close()
