"""Studio 第三方工具源工具：列出、测试连接与登记（``/api/studio/tool-sources``）。

- 登记或修改工具源为 danger 级（它决定 Mindex 会连接哪些外部服务、把哪些素材发出去）。
- ``save_stdio_tool_source`` 会让 Mindex 在本机启动登记的命令，**不导出到 MCP**（外部 MCP 客户端不能让 Mindex
  执行任意命令）；http / mcp-http 登记、列表与测试连接经 MCP 导出并受 ``agent.mcp.policy`` 约束。
- 密钥只接受 ask_user_question 的 secret 字段返回的 ``secret://`` 引用（回环客户端发送前解引用），绝不接收明文；
  连接设置里用 ``secret:名称`` 引用同名密钥。
- 工具源是全局数据：改动事件为 ``{domain: "studio", tool_sources: true}``。删除只在第三方工具页或 CLI 中进行。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check
from mindex.shared.config import StrictModel
from mindex.studio.studio_models import HttpConfig, McpHttpConfig, StdioConfig, ToolEntry

SourceId = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$", description="工具源 ID（小写字母、数字与连字符）")
FOCUS = {"page": "studio_post", "section": "tools"}
MUTATION = {"domain": "studio", "tool_sources": True}


def source_line(item: dict) -> str:
    tools = "、".join(f"{tool['name']}({'图片' if tool['output'] == 'image' else '视频'}"
                     f"{'，可上传' if tool['allow_upload'] else ''})" for tool in item["tools"])
    missing = f"；缺少密钥 {'、'.join(item['missing_secrets'])}" if item.get("missing_secrets") else ""
    return f"{item['id']} | {item['name']} | {item['protocol']} | {tools}{missing}"


def connection_text(protocol: str, connection: dict) -> str:
    if protocol == "http":
        return f"HTTP {connection['base_url']}（提交 {connection['submit_path']}，轮询 {connection.get('poll_path') or '无'}）"
    if protocol == "mcp-http":
        return f"MCP streamable-http {connection['url']}"
    return f"MCP stdio {connection['command']} {' '.join(connection.get('args') or [])}"


class ListToolSources(StrictModel):
    pass


async def list_tool_sources(args: ListToolSources, ctx: ToolContext) -> ToolOutput:
    items = check(await ctx.client.get("/api/studio/tool-sources"), "列出第三方工具源")["items"]
    rows = [{key: item[key] for key in ("id", "name", "protocol", "tools", "artifact_hosts", "missing_secrets")}
            for item in items]
    text = "\n".join(source_line(item) for item in items) or "（还没有登记第三方工具源）"
    return ToolOutput({"items": rows}, text=f"共 {len(rows)} 个工具源（ID | 名称 | 协议 | 白名单工具）：\n{text}\n"
                                            "分镜用 edit_shots 的 source={kind: tool, tool_id, tool, arguments} 调用。",
                      focus=FOCUS)


class ProbeToolSource(StrictModel):
    source_id: str = SourceId


async def preview_probe_tool_source(args: ProbeToolSource, ctx: ToolContext) -> Preview:
    item = check(await ctx.client.get(f"/api/studio/tool-sources/{args.source_id}"), "读取工具源")
    warning = ("会在本机启动登记的 MCP 服务器进程" if item["protocol"] == "mcp-stdio"
               else "会连接登记的外部服务（不提交任务、不发送素材）")
    return Preview(f"测试第三方工具源「{item['name']}」的连接",
                   [{"label": "连接", "after": connection_text(item["protocol"], item["connection"])}],
                   warnings=[warning], focus=FOCUS)


async def probe_tool_source(args: ProbeToolSource, ctx: ToolContext) -> ToolOutput:
    result = check(await ctx.client.post(f"/api/studio/tool-sources/{args.source_id}/test", {}), "测试工具源")
    if not result["ok"]:
        return ToolOutput(result, text=f"连接失败：{result['error']}", focus=FOCUS)
    names = "、".join(tool["name"] for tool in result.get("tools") or []) or "（服务端未提供工具列表）"
    missing = f"\n白名单中有、服务端没有：{'、'.join(result['missing'])}" if result.get("missing") else ""
    return ToolOutput(result, text=f"连接成功（{result['elapsed_ms']} ms）。服务端工具：{names}{missing}", focus=FOCUS)


class SaveToolSourceBase(StrictModel):
    source_id: str = SourceId
    name: str = Field(min_length=1, max_length=60, description="显示名称")
    tools: list[ToolEntry] = Field(min_length=1, max_length=50,
                                   description="工具白名单：每项 name、output（video/image）、allow_upload、timeout、max_mb")
    artifact_hosts: list[str] = Field(default_factory=list, max_length=20,
                                      description="产物下载主机（主机名，可带端口）；http 协议自动包含 base_url 的主机")
    secrets: dict[str, str] | None = Field(
        default=None, description="密钥：名称 → ask_user_question 的 secret 字段返回的 secret:// 引用（绝不传明文）；"
                                  "连接设置里用 secret:名称 引用")

    @model_validator(mode="after")
    def references_only(self):
        for name, value in (self.secrets or {}).items():
            if not value.startswith("secret://"):
                raise ValueError(f"密钥 {name} 必须是 secret:// 引用；请用 ask_user_question 的 secret 字段收集")
        return self

    def body(self) -> dict:
        raise NotImplementedError


class SaveToolSource(SaveToolSourceBase):
    protocol: Literal["http", "mcp-http"] = Field(description="http=按 manifest 提交/轮询的 HTTP 服务；mcp-http=streamable-http MCP 服务器")
    http: HttpConfig | None = Field(default=None, description="protocol=http 时必填：base_url、headers、submit_path、"
                                                              "poll_path、result_field 等")
    mcp_http: McpHttpConfig | None = Field(default=None, description="protocol=mcp-http 时必填：url 与 headers")

    def body(self) -> dict:
        connection = self.http if self.protocol == "http" else self.mcp_http
        if connection is None:
            raise ToolError(f"protocol={self.protocol} 需要 {'http' if self.protocol == 'http' else 'mcp_http'} 连接设置",
                            code="invalid_arguments")
        key = "http" if self.protocol == "http" else "mcp_http"
        return {"name": self.name, "protocol": self.protocol, key: connection.model_dump(),
                "tools": [tool.model_dump() for tool in self.tools], "artifact_hosts": self.artifact_hosts,
                **({"secrets": self.secrets} if self.secrets else {})}


class SaveStdioToolSource(SaveToolSourceBase):
    stdio: StdioConfig = Field(description="本机 MCP 服务器：command（绝对路径）、args、env（可用 secret:名称）、cwd")

    def body(self) -> dict:
        return {"name": self.name, "protocol": "mcp-stdio", "stdio": self.stdio.model_dump(),
                "tools": [tool.model_dump() for tool in self.tools], "artifact_hosts": self.artifact_hosts,
                **({"secrets": self.secrets} if self.secrets else {})}


async def preview_save(args: SaveToolSourceBase, ctx: ToolContext) -> Preview:
    body = args.body()
    current = await ctx.client.get(f"/api/studio/tool-sources/{args.source_id}")
    before = current["data"] if current["ok"] else None
    connection = body.get("http") or body.get("mcp_http") or body.get("stdio")
    rows = [{"label": "工具源", "before": before and f"{before['name']}（{before['protocol']}）",
             "after": f"{args.name}（{body['protocol']}）"},
            {"label": "连接", "before": before and connection_text(before["protocol"], before["connection"]),
             "after": connection_text(body["protocol"], connection)},
            {"label": "工具白名单", "before": before and "、".join(tool["name"] for tool in before["tools"]),
             "after": "、".join(f"{tool.name}{'（可上传素材）' if tool.allow_upload else ''}" for tool in args.tools)}]
    if args.secrets:
        rows.append({"label": "密钥", "after": "、".join(sorted(args.secrets)) + "（只写，不回显）"})
    warnings = ["分镜调用这些工具时会把参数（以及允许上传的素材）发送给该服务"]
    if body["protocol"] == "mcp-stdio":
        warnings.insert(0, f"构建与测试连接时会在本机执行：{connection['command']}")
    if before:
        warnings.append("整体替换现有登记；改动连接或工具产物设置会让已调用的产物过期")
    return Preview(("修改" if before else "登记") + f"第三方工具源 {args.source_id}", rows, warnings=warnings, focus=FOCUS)


async def save(args: SaveToolSourceBase, ctx: ToolContext) -> ToolOutput:
    item = check(await ctx.client.put(f"/api/studio/tool-sources/{args.source_id}", args.body()), "登记工具源")
    return ToolOutput(item, text=f"已登记工具源：{source_line(item)}。可用 test_tool_source 测试连接。",
                      mutation=MUTATION, focus=FOCUS)


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_tool_sources", "studio_post",
                 "列出登记的第三方工具源：协议、白名单工具（产物类型、是否允许上传素材）与缺少的密钥。",
                 ListToolSources, "read", list_tool_sources),
        ToolSpec("test_tool_source", "studio_post",
                 "测试第三方工具源的连接并列出服务端工具（不提交任务、不发送素材；mcp-stdio 会启动本机进程）。",
                 ProbeToolSource, "write", probe_tool_source, preview_probe_tool_source),
        ToolSpec("save_tool_source", "studio_post",
                 "登记或整体替换 http / mcp-http 第三方工具源（连接、工具白名单、产物主机与密钥引用）。",
                 SaveToolSource, "danger", save, preview_save),
        ToolSpec("save_stdio_tool_source", "studio_post",
                 "登记或整体替换在本机启动的 MCP 服务器（mcp-stdio）工具源；command 必须是绝对路径。只在 Mindex 内置助手中可用。",
                 SaveStdioToolSource, "danger", save, preview_save, mcp=False),
    ]
