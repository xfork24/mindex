"""`mindex agent` 命令组：智能助手工具注册表与 MCP 导出。"""

from __future__ import annotations

import json
from typing import Annotated

import typer

from shared.cli_support import SortedGroup

app = typer.Typer(
    cls=SortedGroup,
    help="智能助手工具层：tools 输出工具清单与参数 JSON Schema。\n\n"
    "工具与运行方式无关，同一注册表供内置助手面板与 MCP 服务使用，详见 docs/agent.md。"
    "示例：mindex agent tools --domain studio。",
    no_args_is_help=True,
)


@app.command("tools")
def agent_tools(
    domain: Annotated[
        list[str] | None,
        typer.Option(
            help="只列出指定领域，可重复：meta/studio/studio_detail/studio_post/library/search/index/persons/settings/topics。"
        ),
    ] = None,
    names_only: Annotated[bool, typer.Option("--names-only", help="只输出工具名、风险级别与领域。")] = False,
):
    """输出工具注册表：名称、领域、风险级别、描述与参数 JSON Schema，只读，不访问素材库。

    示例：

    \b
      mindex agent tools
      mindex agent tools --domain studio --names-only
    """
    from agent.tools import build_registry

    registry = build_registry()
    if domain:
        registry = registry.restrict(domain)
    if names_only:
        for spec in registry.all():
            typer.echo(f"{spec.name}\t{spec.risk}\t{spec.domain}")
        return
    payload = [
        {**spec.schema(), "domain": spec.domain, "risk": spec.risk, "mcp": spec.mcp}
        for spec in registry.all()
    ]
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def mcp_command(
    ctx: typer.Context,
    base_url: Annotated[
        str | None,
        typer.Option(help="连接已运行的 Mindex 服务（如 http://127.0.0.1:8000）；省略时在本进程内装配应用。"),
    ] = None,
    token_env: Annotated[
        str, typer.Option(help="连接远端服务时读取 Bearer 令牌的环境变量名（即 MINDEX_WEB_TOKEN）。")
    ] = "MINDEX_WEB_TOKEN",
    policy: Annotated[
        str | None, typer.Option(help="覆盖 agent.mcp.policy：read / write / all；省略使用配置值。")
    ] = None,
    domain: Annotated[
        list[str] | None,
        typer.Option(
            help="只导出指定领域的工具，可重复：studio/studio_detail/studio_post/library/search/index/persons/settings/topics；"
            "覆盖 agent.mcp.domains，省略使用配置值（未配置则全部导出）。report_capability_gap 始终导出。"
        ),
    ] = None,
):
    """以 stdio 传输运行 MCP 服务（服务名 mindex），供 deepseek-harness、Claude Code、Codex 等调用。

    标准输出只用于 MCP 协议，日志写到标准错误。工具与网页智能助手共用同一注册表；写操作与高风险操作受
    agent.mcp.policy 限制（默认 read）。需要安装 MCP 依赖：uv sync --extra mcp。详见 docs/agent.md。

    示例：

    \b
      mindex mcp
      mindex mcp --policy write
      mindex mcp --domain studio --domain search
      mindex mcp --base-url http://127.0.0.1:8000
    """
    import asyncio
    import os

    from shared.agent_domains import LOADABLE_DOMAINS

    unknown = [name for name in domain or [] if name not in LOADABLE_DOMAINS]
    if unknown:
        raise typer.BadParameter(
            f"未知领域：{', '.join(unknown)}；可选：{', '.join(LOADABLE_DOMAINS)}", param_hint="--domain"
        )
    domains = list(dict.fromkeys(domain)) if domain else None

    try:
        from agent import mcp_server
    except ImportError as exc:
        raise typer.BadParameter("需要安装 MCP 依赖：uv sync --extra mcp") from exc
    from agent.loopback import MindexClient
    from agent.tools import build_registry

    config = ctx.obj
    if not base_url and config.database.backend == "postgres" and config.server.role == "api":
        base_url = config.agent.gateway_url
        if not base_url:
            raise typer.BadParameter(
                "共享数据库模式的 mindex mcp 需要 --base-url 或 agent.gateway_url，不能在本机创建会话库"
            )
    chosen = policy or config.agent.mcp.policy
    if chosen not in {"read", "write", "all"}:
        raise typer.BadParameter("policy 只能是 read、write 或 all")

    async def main():
        if base_url:
            token = os.environ.get(token_env, "").strip() or None
            client = MindexClient(base_url=base_url, token=token)
            server = mcp_server.build_server(
                build_registry(),
                lambda: client,
                policy=chosen,
                agent_profile=mcp_server.RemoteAgentProfile(client),
                record_gap=mcp_server.remote_gap_recorder(client),
                publish=mcp_server.remote_publisher(client),
                domains=domains if domains is not None else config.agent.mcp.domains,
            )
        else:
            from api import create_app

            config.agent.mcp.http_enabled = False  # stdio 进程不再挂载 HTTP 端点
            app = create_app(config)
            server = mcp_server.server_for_app(
                app, app.state.runtime_config, app.state.store, policy=policy, domains=domains
            )
        await mcp_server.serve_stdio(server)

    asyncio.run(main())
