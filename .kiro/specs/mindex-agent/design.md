# 设计文档：Mindex 智能助手

## 总览

```
浏览器 agent-panel.js ──SSE/REST──▶ /api/agent (agent/api.py)
                                     │
                              AgentRuntime（BuiltinRuntime；未来 DshRuntime）
                               │          │            │
                        LlmClient     ToolRegistry   SessionStore(SQLite)
                     (openai/anthropic)   │
                                      Policy → MindexClient(ASGI 回环) → 既有 REST 路由
                                          │
                                      MCP 服务（/mcp、mindex mcp）复用同一注册表
```

## 模块（backend/src/agent/）

| 模块 | 职责 |
|---|---|
| `llm/types.py` | dsh 对齐词汇：内容块、消息、StreamChunk、结束原因、TokenUsage；`Assembler` 还原内容块 |
| `llm/openai_compat.py` | `/chat/completions` 流式工具调用；`reasoning_content` → reasoning 块 |
| `llm/anthropic.py` | `/v1/messages` 流式；工具结果并入 user 消息，相邻同角色合并 |
| `llm/scripted.py` | 测试专用脚本化模型 |
| `llm/factory.py` | 按 `config.agent.llm.backend` 选择适配器 |
| `registry.py` | `ToolSpec`、`ToolRegistry`（`schemas()`、`restrict(domains)`） |
| `loopback.py` | `MindexClient`：`httpx.AsyncClient + ASGITransport(app)`，内部凭证头，`secret://` 解引用，响应规整为 `{ok, status, data/error}` |
| `ids.py` | 分镜/版本解析：完整 id、8 位前缀（可带 `shot-`）、`#N`；歧义返回候选 |
| `tools/` | 第一期工具：`meta`、`studio`、`settings`、`library` |
| `policy.py` | 执行前决策 allow/ask/deny；自身档案保护；MCP 策略 |
| `session_store.py` | 表 `agent_sessions`、`agent_events`、`agent_secrets`、`agent_gaps`；追加、按 seq 读、`derive_messages`、脱敏 |
| `runtime.py` | `AgentRuntime` 协议与 `BuiltinRuntime`（asyncio 轮次循环、等待确认/提问、取消、重启恢复） |
| `watcher.py` | 轮询构建状态，终态写入 build-finished 并排队 followup 轮次 |
| `prompts.py` | 系统提示词 |
| `api.py` | `/api/agent` 路由 |
| `mcp_server.py` | MCP 导出与策略 |

## 事件（与 dsh 对齐）

- 信封：`{session_id, seq, time, type, data, ignorable?}`
- 核心：`turn/start {turn}`、`turn/end {turn, reason{kind}}`、`step/start|end {turn, step}`、`user/message`、`assistant/message {turn, step, message, usage?}`、`tool/call {turn, step, callId, name, arguments}`、`tool/result {turn, step, message, error?, meta?}`
- 扩展：`plugin:mindex/approval-asked|approval-decided|question-asked|question-answered|build-watch|build-finished|gap|mutation|grant`
- SSE 事件名：`session.event`（持久事件）、`assistant.stream`（临时 StreamChunk）、`session.status`（`idle|running|waiting`）

## 轮次循环

1. 追加 `user/message`（`source.kind` 为 `user`/`context`/`followup`），`turn/start`。
2. 每步：`step/start` → 从日志派生消息 → 流式调用模型（转发 `assistant.stream`）→ 追加 `assistant/message`。
3. 对每个工具调用：追加 `tool/call` → 策略决策；需要确认时生成预览，追加 `approval-asked`，状态变为 `waiting`，等待答复 → 执行 → 追加 `tool/result`。
4. 无工具调用则 `turn/end{completed}`；超过 `max_steps` 则 `turn/end{max-tokens}`；取消为 `aborted`；模型错误为 `error`。

## 确认

- `ask_user_question` 本身是 meta 工具，挂起等待答复，答复中的 secret 值在 API 层换成引用后才写日志。
- write/danger 默认 ask；会话授权（grant）仅对 write 生效，danger 每次都问。

## 安全

- 内部凭证：进程启动时 `secrets.token_urlsafe(32)`，`web_auth` 用常量时间比较放行。
- 脱敏：事件写入前，把会话保险库中已知的明文替换为引用。
- `/api/agent` 与其余接口一样受 `MINDEX_WEB_TOKEN` 保护；`/mcp` 另需 `MINDEX_MCP_TOKEN`。

## 前端

`frontend/src/shared/agent-panel.js` 与 `agent-panel.css` 被五个页面引入；各页面定义 `window.MindexAgentContext`；收到 `plugin:mindex/mutation` 后派发 `mindex:agent-mutation`，studio.js 与 settings.js 监听并刷新。

## 测试

- 后端：`tests/test_agent_llm.py`、`test_agent_tools.py`、`test_agent_runtime.py`、`test_agent_settings.py`、`test_agent_mcp.py`、`test_agent_scenarios.py`
- 前端：`test_page_assets.py` 更新与 `agent_panel_ui.cjs`
