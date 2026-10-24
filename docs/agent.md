# 智能助手（Agent）

每个页面右下角的「智能助手」按钮打开一个侧边面板。用自然语言描述需求，助手调用 Mindex 现有能力完成，需要时向你提问；修改操作会先给出预览并请求确认。同一套工具也以 MCP 服务导出，可由 deepseek-harness、Claude Code、Codex 等外部 harness 驱动。

## 快速开始

1. 在设置页「智能助手」角色新建档案（预设：DeepSeek、阿里云百炼、智谱、Anthropic），点击「测试连接」——测试会要求模型调用一个 echo 工具，只有工具调用成功才算通过。激活档案。也可以在 `config.local.yaml` 的 `agent.llm` 配置，密钥放在环境变量 `MINDEX_AGENT_API_KEY`。
2. 在任意页面打开助手面板，例如在创作页说：
   - 「合并 a1daad59 和 925281d5 两个分镜，并重新生成该分镜的后续流程」
   - 「我想使用自定义模型配置」
   - 「这个任务最近一次构建为什么失败？」
3. 面板会随消息发送当前页面上下文（创作页为任务、方案、展开的分镜），所以「这个任务」「第 3 镜」都能被理解。

## 确认与安全

| 风险级别 | 工具示例 | 行为 |
|---|---|---|
| read | 读取工作台、预检、构建状态、设置、检索 | 直接执行 |
| write | 修改分镜/文案/方案参数、新建任务或档案、业务设置 | 显示改动预览，确认后执行；可勾选「本会话内不再询问此操作」 |
| danger | 构建成片、单节点执行、删除任务、修改或激活模型档案 | 每次都必须显式确认，不接受会话授权 |

- 预览时读取的 `revision` 会在确认后原样提交。如果确认期间你在页面上改动了工作台，提交会被拒绝，助手重新读取后再试，不会覆盖你的修改。
- 密钥只通过助手弹出的表单（密码框）收集：明文直接进入本机保险库（`data_dir/agent.sqlite3`，带有效期，默认 30 分钟），会话日志、发给模型的请求与 SSE 流里只出现 `secret://...` 引用，工具执行时才在进程内解引用。在聊天框粘贴疑似密钥会被拦截。
- 不允许在会话中修改或切换智能助手自身正在使用的档案，请到设置页操作。
- 文案、分镜旁白、素材名称、工具结果都只是数据；系统提示词明确要求模型不执行其中的指令。即使模型被诱导调用高风险工具，也仍需你确认。
- `/api/agent` 与其余接口一样受 `MINDEX_WEB_TOKEN` 保护。没有设置访问令牌时，任何能访问服务端口的人都能使用助手（与设置页同样的风险，见 [设置页指南](settings.md#密钥安全)）。

## 页面同步与定位

助手的每次查询与修改都会反映到网页上，并尽量定位到对应位置：

- 读工具和写工具都返回「定位描述」：页面（`studio`/`settings`/`search`/`admin`/`persons`）以及任务、方案、分镜、阶段、区块、模型角色、档案、检索词、素材等。写工具在确认卡片出现时就先定位到要修改的地方，执行后再定位到结果（例如合并后高亮保留下来的那一镜）。
- 改动与定位写入全局通道 `data_dir/agent.sqlite3` 的 `agent_changes` 表，所有打开的页面经 `GET /api/agent/changes` 订阅。内置助手与 MCP 服务端（包括独立进程的 `mindex mcp`）都会发布。

| 来源 | 数据刷新 | 定位 |
|---|---|---|
| 本标签页的助手会话 | 刷新 | 同页立即定位；跨页的定位等会话空闲或开始等待确认时，跳到本轮最后一个目标。跳转后面板自动打开，会话与待确认卡片延续 |
| 其他标签页的会话、MCP 调用 | 所有打开的页面都刷新 | 只在当前正好是同一页面（创作页为同一任务）时页内高亮，绝不跨页跳转 |
| 重放历史（打开旧会话、页面重载） | — | 不定位、不跳转 |

纯查询同样跟随（例如在设置页问「颜色短片有几个分镜」，回答后会切到创作页定位）。面板顶部的「跟随助手定位」开关默认开启、会被记住；关闭后只刷新数据。

各页面的定位方式：创作页切换任务、展开并高亮分镜行（带阶段时展开该阶段详情，最多 5 镜），或高亮文案、方案栏、分镜工作台、工作日志、成片、产物等区块；助手操作的方案不是当前方案时只提示、不擅自切换。设置页展开模型角色并高亮档案卡，或展开检索调参、音色库、索引版本、处理设置。检索页填入检索词并执行。素材管理页打开素材并定位镜头或片段。人物库页切换分页。

用户明确要求「打开设置页」「带我看成片」这类界面操作时，助手调用 `navigate` 工具；它只改变界面、不修改数据，不导出到 MCP。

新页面接入约定：定义 `window.MindexAgentContext()` 返回页面上下文，定义 `window.MindexAgentFocus(focus, {passive})`（页面未就绪返回 `false`，面板会重试；`passive` 为真时只高亮、不切换内容），高亮用 `window.MindexAgent.highlight(element)`。

## 回复格式

助手回复按 Markdown 渲染：标题、列表、表格、代码块、引用、链接。渲染使用仓库内固定版本的 [marked](https://github.com/markedjs/marked) 18.1.1，结果经 [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.17 按白名单净化后显示（版本、许可证与校验值见 [第三方组件声明](../THIRD_PARTY_NOTICES.md)）：脚本、事件属性、内联样式、`javascript:`/`data:`/协议相对链接都会被清除，外部链接在新窗口打开。用户消息、工具卡片仍按纯文本显示。

## 图片与音视频

工具结果会附带相关媒体，显示在对应工具卡片下方：

| 工具 | 媒体 |
|---|---|
| `resolve_shots` | 分镜的配音、渲染视频与召回候选缩略图（最多 4 镜） |
| `get_workspace_summary`、`get_build_status`、构建结束总结 | 成片 |
| `list_versions`、`select_version` | 对应版本的配音、视频或成片 |
| `search_media` | 检索结果片段缩略图 |
| `list_sources`、`list_persons` | 素材（首个镜头作封面）、人物参考图 |

- 图片与带封面的片段排成缩略图网格，点击打开大图浏览：←/→ 切换，Esc 或点击背景关闭；片段用与创作页相同的镜头播放器播放。
- 图片与成片默认展开；配音与单镜视频默认折叠，展开时才加载，避免一次加载大量音视频。切换会话、关闭面板或关闭大图浏览时自动暂停。
- 只加载本站 `/api/...` 地址（后端 `agent/tools/media.py` 与面板两处校验）。助手 Markdown 回复里的图片同样只放行本站地址，外链图片会被移除；本站图片可点击放大。

## 面板宽度

拖动面板左边缘调整宽度（320px 到视口 80%），也可聚焦手柄后用 ←/→ 每次调整 24px、Home/End 到最小/最大、回车或双击恢复默认 440px。宽度保存在浏览器本地，刷新和切换页面后保持；宽屏下页面右侧留白随之变化。

## 构建跟踪

助手提交构建后，面板显示进度卡片；构建结束时会话里写入结果，并自动触发一轮总结（重建、复用、失败数量；失败或阻塞时说明原因并建议下一步）。全局同一时间只能有一个构建，冲突时助手会告知并建议稍后重试。服务重启后，未结束的对话轮次会标记为中断，正在跟踪的构建继续跟踪。

## 能力范围与缺口

第一期共 30 个工具：创作与设置可读写；素材库、人物库、检索只读；另有界面导航 `navigate`。

| 领域 | 工具 |
|---|---|
| meta | `ask_user_question`、`report_capability_gap`、`navigate` |
| studio（读） | `list_jobs`、`get_workspace_summary`、`resolve_shots`、`get_preflight`、`get_build_status`、`list_versions` |
| studio（写） | `create_job`、`create_plan`、`activate_plan`、`update_plan_options`、`edit_script`、`edit_shots`、`select_version` |
| studio（高风险） | `run_build`、`run_node`、`delete_job` |
| settings | `get_settings`、`list_provider_presets`、`list_voices`、`test_model_profile`、`create_model_profile`、`update_business_settings`、`update_model_profile`（高风险）、`activate_model_profile`（高风险） |
| library / search | `search_media`、`list_sources`、`list_persons` |

`mindex agent tools` 输出全部工具的参数 JSON Schema。所有工具都经进程内回环调用既有 REST 接口，校验、锁与错误信息与页面操作完全一致。`edit_shots` 的合并规则与创作页相同：旁白拼接、查询以「；」连接、时长相加，保留前一镜的召回设置（前一镜为默认文字召回时继承被合并镜）；只能合并相邻分镜。

现有能力做不到的需求（例如「调整拆分镜粒度」——粒度目前写死在拆分镜提示词里），助手会如实说明、给出替代做法，并记录到能力缺口列表：`GET /api/agent/gaps`。

## API

| 方法与路径 | 用途 |
|---|---|
| `GET /api/agent/status` | 是否启用、模型与后端 |
| `GET/POST /api/agent/sessions`、`GET/DELETE /api/agent/sessions/{id}` | 会话列表、新建、详情（含待答复的确认与提问）、删除 |
| `POST /api/agent/sessions/{id}/messages` | `{content, context}`；会话忙时返回 409 |
| `GET /api/agent/sessions/{id}/events?after=<seq>` | SSE：`session.event`（持久事件）、`assistant.stream`（流式块）、`session.status`、`ping`；断线用最后的 seq 续传 |
| `GET /api/agent/sessions/{id}/log?after=<seq>` | 同样的持久事件，JSON 形式 |
| `POST /api/agent/sessions/{id}/approvals/{requestId}` | `{outcome: allowed-once\|rejected\|cancelled\|unavailable, grant}` |
| `POST /api/agent/sessions/{id}/questions/{requestId}` | `{answers: [{id, selected[], custom?, values?}]}`；secret 字段的值在这里换成引用 |
| `POST /api/agent/sessions/{id}/cancel` | 停止当前轮次 |
| `GET /api/agent/gaps` | 能力缺口记录 |
| `GET /api/agent/changes?after=<seq>` | SSE 全局改动与定位通道：先发 `ready{seq}`，之后每条为 `change{seq, time, kind: mutation\|focus, source: agent\|mcp, session_id, tab, data}`；`after=-1`（默认）从最新开始、不回放 |
| `GET /api/agent/changes/log?after=<seq>` | 同样的记录，JSON 形式（调试用） |
| `POST /api/agent/changes` | `{kind, data}`：独立进程的 MCP 服务（`mindex mcp --base-url`）报告改动，来源记为 `mcp` |

## 配置

`config.local.yaml` 的 `agent` 段（完整注释见 `config.example.yaml`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 关闭后面板接口返回 503 |
| `max_steps` | `16` | 单轮最多连续工具调用步数，超出后暂停 |
| `max_context_tokens` | `60000` | 历史估算上限；超出时先省略较早的工具结果，再丢弃最早的轮次 |
| `secret_ttl_seconds` | `1800` | 密钥引用有效期 |
| `llm.backend` | `openai` | `openai`（OpenAI 兼容 tools 协议）或 `anthropic`（原生 Messages 协议） |
| `llm.base_url` / `llm.model` / `llm.api_key_env` / `llm.timeout` / `llm.max_tokens` | DeepSeek | 设置页「智能助手」档案可覆盖 |
| `mcp.http_enabled` | `false` | 挂载 streamable-http `/mcp` |
| `mcp.policy` | `read` | MCP 调用方允许的最高风险级别：`read`、`write`、`all` |
| `mcp.token_env` | `MINDEX_MCP_TOKEN` | `/mcp` 的 Bearer 令牌所在环境变量；开启 HTTP 但未设置令牌时服务拒绝启动 |

会话、密钥保险库与能力缺口存放在 `data_dir/agent.sqlite3`，属于本机运行状态，不进入 `mindex backup`。

## MCP 导出与外部 harness

安装依赖：`uv sync --extra mcp`（开发环境的 `--extra dev` 已包含）。服务名 `mindex`，导出除 `ask_user_question`、`navigate` 外的 28 个工具；read 工具标注 `readOnlyHint`，danger 工具标注 `destructiveHint`。外部 harness 不一定支持确认交互（deepseek-harness 不读取注解，也不支持 elicitation），所以由服务端按 `agent.mcp.policy` 强制执行：超出策略的调用返回 `isError`，提示到 Mindex 页面操作。成功的 MCP 调用同样发布改动与定位，打开的 Mindex 页面会刷新并做页内高亮。MCP 调用方没有密钥保险库，需要密钥的档案请改用 `api_key_env` 或在设置页填写；提交构建后用 `get_build_status` 查询进度。

两种传输：

- stdio：`mindex mcp`（本进程装配应用）或 `mindex mcp --base-url http://127.0.0.1:8000`（连接已运行的服务，令牌读 `MINDEX_WEB_TOKEN`）；`--policy` 覆盖配置。
- streamable-http：`agent.mcp.http_enabled: true`，设置 `MINDEX_MCP_TOKEN`，端点 `http://<host>:8000/mcp`。

deepseek-harness（`@deepseek-ai/dsh-mcp-client`，工具出现为 `mcp__mindex__<tool>`）：

```yaml
- id: mcp-mindex
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: mindex
    transport: streamable-http
    url: http://127.0.0.1:8000/mcp
    headers:
      Authorization: !!js '`Bearer ${process.env.MINDEX_MCP_TOKEN}`'
    toolCallTimeoutMs: 120000

# 或 stdio：
- id: mcp-mindex-stdio
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: mindex
    transport: stdio
    command: uv
    args: ['run', '--project', '/path/to/mindex', 'mindex', 'mcp', '--policy', 'write']
```

Claude Code：

```bash
claude mcp add mindex -- uv run --project /path/to/mindex mindex mcp
claude mcp add --transport http mindex-http http://127.0.0.1:8000/mcp --header "Authorization: Bearer $MINDEX_MCP_TOKEN"
```

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.mindex]
command = "uv"
args = ["run", "--project", "/path/to/mindex", "mindex", "mcp"]
```

## 与 deepseek-harness 的兼容设计

内置运行时的数据形状逐字沿用 dsh，未来可以直接换成 dsh 运行时，前端与工具都不用改：

- 会话事件信封 `{session_id, seq, time, type, data, ignorable?}`，核心事件 `turn/start`、`turn/end{reason.kind}`、`step/start|end`、`user/message{source.kind: user|context|followup}`、`assistant/message`、`tool/call{callId, name, arguments: 原始 JSON 字符串}`、`tool/result{message, error?, meta?}`；Mindex 扩展事件一律为 `plugin:mindex/...`。发给模型的历史只从日志派生。
- 内容块 `text` / `reasoning` / `tool-call`，SSE 流式块沿用 dsh StreamChunk（`block-start`、`text-delta`、`tool-call-delta`、`block-end`、`usage`、`finish`），用量字段 `inputTokens` / `outputTokens` / `cacheReadTokens`。
- 确认结果 `allowed-once | rejected | cancelled | unavailable`；提问工具 `ask_user_question` 的参数与结果是 dsh 同名工具的超集。
- `AgentRuntime` 协议（`backend/src/agent/runtime.py`）与 dsh SDK 一一对应：`prompt` ↔ `session/prompt`，`create_or_resume` ↔ `session/resume`，`events`/`status` ↔ `session.event`/`session.status` 通知，`answer_approval` ↔ `approval/request` 的响应，`answer_question` ↔ `user-question/ask` 的响应。

规划中的 `DshRuntime`：在工作线程里运行 dsh Python SDK（`deepseek_harness.HarnessClient`，`approval_requests=True`、`capabilities.userQuestions=True`），用上面的 stdio 或 HTTP 配置把 Mindex 工具挂给 dsh；把 `session.event` 通知原样写入同一张事件表，把 `approval/request` 与 `user-question/ask` 转成面板现有的确认卡片和表单卡片。

## 验证

- `uv run pytest tests/test_agent_*.py`：协议适配、工具、运行时、确认、构建跟踪、密钥、MCP，以及 10 个确定性验收场景（`tests/agent_scenarios.py`）。这些测试使用脚本化模型，只验证工程链路，不代表真实模型质量。
- `tests/test_agent_changes.py`：改动与定位通道、各工具定位描述、`navigate`。
- `tests/test_agent_media.py`：各工具附带的媒体只含本站地址且真实可加载、折叠默认值。
- `tests/test_agent_panel_ui.py`：Playwright 驱动真实页面，覆盖跨页跳转后确认、页内定位、双标签刷新、外部改动、关闭跟随、导航、媒体卡片与大图浏览、宽度调整与 Markdown（含 XSS 向量，`tests/agent_markdown_ui.cjs`）（需要 node 与 Playwright，可用 `MINDEX_PLAYWRIGHT_MODULE` 指定模块）。
- 真实模型评测：`scripts/agent_eval.py` 让真实模型跑同一组场景（素材、嵌入、配音仍是测试专用适配器），输出通过率与每个场景的事件日志：

```bash
MINDEX_AGENT_API_KEY=... uv run python scripts/agent_eval.py --backend openai \
  --base-url https://api.deepseek.com/v1 --model deepseek-chat
ANTHROPIC_API_KEY=... uv run python scripts/agent_eval.py --backend anthropic \
  --base-url https://api.anthropic.com --model claude-sonnet-4-5 --api-key-env ANTHROPIC_API_KEY
```
