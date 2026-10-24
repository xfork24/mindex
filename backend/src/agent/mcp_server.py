"""把智能助手工具注册表导出为 MCP 服务（服务名 ``mindex``），供 deepseek-harness、Claude Code、Codex 等使用。

- 传输：streamable-http ``/mcp``（Bearer 令牌，默认关闭，``agent.mcp.http_enabled``）与 stdio（``mindex mcp``）。
- 工具名不加前缀；dsh 会暴露为 ``mcp__mindex__<tool>``（≤64 字符，已在测试中校验）。
- 注解：read → ``readOnlyHint``，danger → ``destructiveHint``。dsh 不读取注解、也不支持 elicitation，
  所以策略由本服务端强制执行：``agent.mcp.policy``（read/write/all）之外的调用返回 ``isError``。
- ``ask_user_question`` 依赖界面交互，不导出；MCP 调用方用自己的提问机制。
- 工具仍经回环调用 REST 接口；MCP 调用方没有密钥保险库，需要密钥的档案请改用 ``api_key_env`` 或在设置页填写。
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os

import mcp.types as types
from mcp.server.lowlevel import Server

from mindex.agent import policy as policies
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError, ToolRegistry

logger = logging.getLogger(__name__)
SERVER_NAME = "mindex"
INSTRUCTIONS = (
    "Mindex 视频素材管理与 AI 视频创作工作台。创作任务的方案流水线：文案 → 拆分镜 → 每镜召回/配音 → 选片 → 渲染 → 成片。"
    "修改前先用 get_workspace_summary 读取最新状态；用户给出的分镜短 ID 先用 resolve_shots 解析。"
    "拆分、合并分镜或改写旁白前，先用 get_shot_details 读取完整旁白（摘要中的旁白是截断的）。"
    "写操作与高风险操作受服务端策略 agent.mcp.policy 限制；提交构建后用 get_build_status 查询进度。"
)


def annotations(spec) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=spec.name,
        readOnlyHint=spec.risk == "read",
        destructiveHint=spec.risk == "danger",
        idempotentHint=spec.risk == "read",
        openWorldHint=False,
    )


def exported(registry: ToolRegistry) -> list:
    return [spec for spec in registry.all() if spec.mcp]


def build_server(registry: ToolRegistry, client_factory, *, policy, agent_profile=lambda: None,
                 record_gap=None, publish=None) -> Server:
    """``policy`` 为字符串或返回字符串的函数（热生效）。

    ``publish(kind, data)`` 把成功调用的改动与定位写入全局通道，让打开的 Mindex 页面刷新并被动高亮；
    可以是同步或异步函数（远程模式经 HTTP 报告）。
    """
    server = Server(SERVER_NAME, instructions=INSTRUCTIONS)
    specs = {spec.name: spec for spec in exported(registry)}

    def current_policy() -> str:
        return policy() if callable(policy) else policy

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=spec.name, description=f"[{spec.risk}] {spec.description}", inputSchema=spec.parameters(),
                       annotations=annotations(spec))
            for spec in specs.values()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        def error(text: str) -> types.CallToolResult:
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)

        spec = specs.get(name)
        if spec is None:
            return error(f"未知工具 {name}")
        decision = policies.mcp_decide(spec, current_policy())
        if decision.kind != "allow":
            return error(decision.reason)
        try:
            args = spec.parse(arguments)
            ctx = ToolContext(client=client_factory(), agent_profile_id=agent_profile(),
                              extra={"record_gap": record_gap} if record_gap else {})
            output = await spec.execute(args, ctx)
        except ToolError as exc:
            return error(str(exc))
        except Exception as exc:  # noqa: BLE001 — 工具错误一律作为 isError 结果返回
            return error(f"工具执行出错：{exc}")
        if publish is not None:
            for kind, data in (("mutation", output.mutation), ("focus", output.focus)):
                if not data:
                    continue
                try:
                    result = publish(kind, data)
                    if hasattr(result, "__await__"):
                        await result
                except Exception:  # noqa: BLE001 — 通知失败不影响工具结果
                    logger.warning("发布页面改动通知失败", exc_info=True)
        text = output.render()
        if output.watch_build:
            text += f"\n（MCP 调用方不会自动收到构建结束通知：请用 get_build_status 查询构建 {output.watch_build['build_id']}）"
        structured = output.value if isinstance(output.value, dict) else {"value": output.value}
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)],
                                    structuredContent=json.loads(json.dumps(structured, default=str)), isError=False)

    return server


def server_for_app(app, runtime_config, store, *, policy: str | None = None) -> Server:
    from mindex.agent.session_store import SessionStore
    from mindex.agent.tools import build_registry
    from mindex.settings.runtime_settings import get_setting

    holder = {}

    def client_factory():
        if "client" not in holder:
            holder["client"] = MindexClient(app)
        return holder["client"]

    def agent_profile():
        value = get_setting(store, "active.profile.agent")
        return int(value) if value else None

    from mindex.agent.changes import ChangeFeed

    gaps = SessionStore(store.root)
    feed = ChangeFeed(store.root)
    return build_server(build_registry(), client_factory,
                        policy=policy or (lambda: runtime_config.current().agent.mcp.policy),
                        agent_profile=agent_profile, record_gap=gaps.add_gap,
                        publish=lambda kind, data: feed.publish(kind, data, source="mcp"))


def remote_publisher(client: MindexClient):
    """远程模式：把改动报告给运行中的 Mindex 服务（POST /api/agent/changes）。"""

    async def publish(kind: str, data: dict):
        result = await client.post("/api/agent/changes", {"kind": kind, "data": data})
        if not result["ok"]:
            logger.warning("报告页面改动失败：%s", result.get("error"))

    return publish


class StreamableEndpoint:
    """挂载在 ``/mcp`` 的 ASGI 端点：先校验 Bearer 令牌，再交给 MCP 会话管理器（无状态模式）。"""

    def __init__(self, manager, token: str):
        self.manager = manager
        self.token = token

    async def __call__(self, scope, receive, send):
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        supplied = headers.get("authorization", "")
        if not (supplied.lower().startswith("bearer ") and hmac.compare_digest(supplied[7:].strip(), self.token)):
            body = json.dumps({"detail": "MCP 需要访问令牌：Authorization: Bearer <MINDEX_MCP_TOKEN>"},
                              ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.manager.handle_request(scope, receive, send)


def install_http(app, runtime_config, store) -> bool:
    """``agent.mcp.http_enabled`` 为真时挂载 ``/mcp``；未设置令牌时拒绝启用，不暴露无认证的写入面。"""
    cfg = runtime_config.base.agent.mcp
    if not cfg.http_enabled:
        return False
    token = os.environ.get(cfg.token_env, "").strip()
    if not token:
        raise ValueError(f"已开启 agent.mcp.http_enabled，但环境变量 {cfg.token_env} 未设置 MCP 访问令牌")
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.routing import Route

    manager = StreamableHTTPSessionManager(app=server_for_app(app, runtime_config, store), stateless=True)
    app.router.routes.append(Route("/mcp", endpoint=StreamableEndpoint(manager, token),
                                   methods=["GET", "POST", "DELETE"], include_in_schema=False))
    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application):
        async with manager.run(), previous(application) as state:
            yield state

    app.router.lifespan_context = lifespan
    return True


async def serve_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
