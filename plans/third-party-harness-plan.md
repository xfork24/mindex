# MCP 接入验证与 dsh 运行时支持

## 已确认的现状

- **当前已支持外部 harness 通过 MCP 调用**：提供 stdio 与带 Bearer 鉴权的 Streamable HTTP `/mcp`。
- 已运行 MCP 相关测试，**23 项通过**，覆盖 stdio 实际协议调用、HTTP 鉴权、领域过滤和风险策略；尚未逐个启动外部 harness 实测。
- `DshRuntime` 尚未实现。现有消息格式兼容设计可以复用，但还需补齐进程生命周期、工具交互、附件和会话恢复。

现有接入方式：

```bash
uv sync --extra mcp

# Claude Code
claude mcp add mindex -- uv run --project /Users/hengliu/workspace/mindex mindex mcp
```

Codex 的 `~/.codex/config.toml`：

```toml
[mcp_servers.mindex]
command = "uv"
args = ["run", "--project", "/Users/hengliu/workspace/mindex", "mindex", "mcp"]
```

deepseek-harness 的插件配置：

```yaml
- id: mcp-mindex
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: mindex
    transport: stdio
    command: uv
    args: ['run', '--project', '/Users/hengliu/workspace/mindex', 'mindex', 'mcp']
```

默认只允许读取；需要修改时，在 `mcp` 后添加 `--policy write`；允许 danger 工具需显式使用 `--policy all`。连接已运行的 Mindex 可添加 `--base-url http://127.0.0.1:8000`，凭证读取 `MINDEX_WEB_TOKEN`。文档保留并验证 HTTP 配置示例。

## 运行时与工具实现

- 增加 `DshRuntime` 和按会话分派的运行时管理器，补全公共运行时协议，移除路由及任务跟踪对 `BuiltinRuntime` 的具体类型依赖。
- 使用官方 Python `HarnessClient`，在线程中处理同步 SDK 请求，通过线程安全队列接入 Web asyncio；订阅事件、审批与提问须并行消费，避免等待确认时阻塞通知。
- 使用 Mindex 专属 `DSH_HOME`、受控 profile 与持久会话目录，不读取用户个人 dsh 配置。增加可选 dsh 安装依赖，锁定 SDK 与对应运行时版本；依据[官方 SDK 接口](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)适配。
- 继续使用设置页当前智能助手档案，映射 OpenAI 兼容及 Anthropic 连接配置、模型和视觉能力；凭证只通过受控进程环境传递，不写入生成配置或日志。
- 抽取现有工具执行通道，使两个运行时共用参数校验、领域加载、预览确认、revision、密钥解引用、附件归属、页面定位及任务跟踪。
- 为受管 dsh 提供会话专属的内部 MCP 桥接；使用独立短期凭证绑定会话，不依赖公开 `/mcp` 开关或扩大外部 MCP 权限。桥接包含页面交互工具，工具审批由 Mindex 共用执行通道完成，避免重复确认。
- 受管 profile 只启用助手所需组件与 Mindex 工具，不引入默认 shell、文件编辑等额外能力。原生 dsh 审批和提问请求映射到现有卡片。
- dsh 核心事件保持原始内容，经过统一脱敏后落入现有事件库；保留上游序号用于去重，本地序号统一分配，确保扩展事件与 SSE 续传连续。兼容工具名称映射，避免前端显示 MCP 前缀后失去现有卡片能力。
- 保留图片引用存储规则，图片仅在模型请求边界解析；非图片附件仍由工具通过会话归属校验读取。

## 设置、持久化与失败处理

- 新增 `agent.runtime: builtin | dsh`，默认 `builtin`；设置页增加运行时选择、可用状态及依赖安装提示。
- 增加运行时设置读取、保存及就绪检查接口，沿用现有设置持久化与鉴权方式；保存 dsh 前检查依赖及模型配置，不发送付费模型请求。
- 会话表增加 `runtime` 字段，现有会话迁移为 `builtin`；会话 API 返回所属运行时。
- 按已确认偏好，**设置切换仅影响新会话**；已有会话、运行中的轮次和待确认操作继续使用原运行时。
- 重启后恢复 dsh 会话与后台任务跟踪。子进程退出时结束受影响轮次、清理待确认请求并显示可理解错误；不自动重发可能已经执行的操作。
- dsh 不可用时保留选择并明确提示，不静默切回内置运行时。应用退出、会话删除和取消操作均清理对应工作线程及进程资源。

## 验收与文档

- 保持已有 MCP 测试通过，增加通过真实 HTTP MCP 客户端完成初始化、工具列举和读取调用的测试。
- 两个运行时共用行为验收：多步工具调用、动态加载、审批允许与拒绝、密钥表单、附件隔离、视觉、定位刷新、任务完成后跟进。
- 覆盖运行时选择持久化、旧数据库迁移、新旧会话分派、SSE 重连去重、取消、子进程异常和重启恢复。
- 使用脚本化 SDK 验证异常分支，并以真实 dsh 进程和本地确定性模型服务完成端到端测试，避免仅靠 SDK mock 宣称集成成功。
- 设置页浏览器验收切换、缺失依赖提示，以及已有会话继续使用原运行时。
- 更新 `docs/agent.md`、设置与安装文档，明确外部 MCP 与内置 dsh 模式的权限和交互差异，替换“规划中的 DshRuntime”说明。
