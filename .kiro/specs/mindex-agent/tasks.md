# 实施计划

- [x] 1. 智能助手模型角色与 LLM 适配层
  - 新增 `agent/llm/types.py`、`openai_compat.py`、`anthropic.py`、`scripted.py`、`factory.py`
  - `AgentConfig`；runtime_settings 增加 `agent` 角色、`anthropic` 厂商与预设
  - 设置接口 `_effective` 与测试连接（echo 工具探测）支持 agent；settings.js 显示该角色
  - 补充文案并重新生成 messages.js；测试两种协议的请求构造、流式解析与档案增删改
  - _需求: 1.1, 1.2, 1.3, 1.4_

- [x] 2. 工具注册表、回环客户端、ID 解析与 Studio 只读工具
  - `registry.py`、`loopback.py`（内部凭证，web_auth 放行）、`ids.py`
  - 工具：`list_jobs`、`get_workspace_summary`、`resolve_shots`、`get_preflight`、`get_build_status`、`list_versions`
  - CLI `mindex agent tools` 输出工具 schema
  - _需求: 2.1, 2.2, 2.3, 2.4_

- [x] 3. 会话存储、BuiltinRuntime 与 SSE 接口
  - SQLite 表与 dsh 形状事件、`derive_messages`、上下文预算
  - `AgentRuntime` 协议与 `BuiltinRuntime`；重启恢复；系统提示词
  - `/api/agent` 路由（会话、消息、SSE、取消）；`create_app(agent_llm=...)`
  - _需求: 4.1, 4.2, 4.3, 4.4, 4.5, 10.1_

- [x] 4. 前端智能助手面板 MVP
  - `shared/agent-panel.js` 与 `agent-panel.css`，五个页面引入
  - 各页面 `MindexAgentContext`；流式渲染、工具卡片、会话切换
  - 文案进入 messages.json；更新 `test_page_assets.py`
  - _需求: 7.1, 7.2, 7.4_

- [x] 5. 确认策略与 Studio 写操作工具
  - `policy.py`、确认事件、会话授权、失败关闭
  - 工具：`create_job`、`create_plan`、`activate_plan`、`update_plan_options`、`edit_script`、`edit_shots`、`select_version`（均带预览）
  - 面板确认卡片；`mindex:agent-mutation` 触发 studio 页刷新
  - _需求: 3.1, 3.2, 3.3, 3.4, 7.3_

- [x] 6. 构建工具、BuildWatcher 与自动总结
  - 工具：`run_build`（预览为预检节点统计）、`run_node`、`delete_job`
  - `watcher.py`：终态写入 build-finished 并排队 followup 轮次；重启后继续跟踪
  - 面板构建进度卡片
  - _需求: 6.1, 6.2, 6.3_

- [x] 7. 提问工具、密钥保险库与设置工具
  - `ask_user_question`（dsh 超集，支持 secret 字段）与表单卡片
  - `agent_secrets`：会话绑定、TTL、回环解引用、全链路脱敏；前端粘贴拦截
  - 工具：`get_settings`、`list_provider_presets`、`list_voices`、`test_model_profile`、`create_model_profile`、`update_business_settings`、`update_model_profile`、`activate_model_profile`；自身档案保护
  - _需求: 3.5, 5.1, 5.2, 5.3, 5.4, 5.5_

- [x] 8. 跨领域只读工具、能力缺口与提示词加固
  - 工具：`search_media`、`list_sources`、`list_persons`、`report_capability_gap`
  - `agent_gaps` 表与 `GET /api/agent/gaps`；按页面上下文限定领域
  - 提示注入测试：素材名或文案里的指令不触发工具调用
  - _需求: 2.5, 8.1, 8.2_

- [x] 9. MCP 服务导出（兼容 dsh）
  - `mcp_server.py`：注册表导出、注解、`agent.mcp.policy` 强制执行
  - streamable-http `/mcp`（Bearer，默认关闭）与 `mindex mcp` stdio
  - 测试：list_tools/call_tool 往返、策略拦截、名称符合 `mcp__mindex__<tool>`
  - _需求: 9.1, 9.2, 9.3_

- [x] 10. 验收场景与文档
  - `tests/test_agent_scenarios.py`：10 个确定性场景
  - `scripts/agent_eval.py`：真实模型评测
  - `docs/agent.md`（含 dsh、Claude Code、Codex 配置与 DshRuntime 路径）、docs 索引、CLAUDE.md 导航
  - 全量 pytest、ruff、文档链接与文案校验
  - _需求: 9.4, 10.2, 10.3_

- [x] 11. 后端改动与定位通道
  - `ToolOutput.focus` / `Preview.focus`；`agent/changes.py` 的 `ChangeFeed`（`agent_changes` 表，跨进程可见）
  - 运行时与 MCP 服务端发布 mutation/focus；会话日志追加 `plugin:mindex/focus`（带 tab）
  - `GET /api/agent/changes`（SSE，after=-1 不回放）、`GET /api/agent/changes/log`、`POST /api/agent/changes`（远程 MCP 报告）
  - 测试：读写工具产生记录、预览阶段即定位、SSE 续传、MCP 来源、无明文密钥
  - _需求: 11.1, 11.2, 11.5_

- [x] 12. navigate 工具与全部工具的定位描述
  - `tools/navigate.py`（read 级，不导出 MCP，工具总数 30）；29 个工具按清单补齐 focus（含确认预览）
  - 系统提示词补充 Markdown 与 navigate 说明
  - 测试：逐类断言 focus、navigate 解析短 ID 与歧义、MCP 列表不含 navigate
  - _需求: 11.1, 11.3_

- [x] 13. 面板跟随定位与五个页面定位入口
  - 面板订阅全局通道；mutation 统一由此派发；tab_id；「跟随助手定位」开关
  - 本标签会话：页内立即定位，跨页在 idle/waiting 时跳转并自动恢复面板；MCP/其他来源只页内被动高亮
  - 五个页面实现 `window.MindexAgentFocus` 与高亮；新增文案
  - 测试：Playwright 覆盖跨页跳转后确认、页内定位、双标签刷新、外部修改刷新、关闭跟随、navigate
  - _需求: 11.2, 11.4, 11.6_

- [x] 14. Markdown 渲染（marked + DOMPurify）
  - 固定版本 vendor 文件（校验 npm integrity）与 THIRD_PARTY_NOTICES；`agent-markdown.js`；助手消息（含流式）渲染
  - 五个页面引入；样式；资源版本与 test_page_assets 更新
  - 测试：结构正确、XSS 向量被清除、面板出现表格
  - _需求: 12.1, 12.2_

- [x] 15. 文档与全量验证
  - docs/agent.md「页面同步与定位」与 Markdown；docs/README.md、CLAUDE.md
  - ruff、文档链接、文案校验、JS 语法、全量 pytest
  - _需求: 11, 12_

- [x] 16. 工具结果附带媒体（后端）
  - `ToolOutput.media`：`[{kind: image|audio|video, url, poster?, title, start?, end?, collapsed?}]`，只允许本站 `/api/...` 地址
  - search_media、get_workspace_summary、resolve_shots、list_versions、select_version、get_build_status、构建总结、list_sources、list_persons 附带缩略图、配音、渲染视频、成片、参考图
  - 测试：各工具媒体列表正确且只含本站地址
  - _需求: 13.1, 13.3_

- [x] 17. 面板媒体卡片与大图浏览
  - 图片缩略图网格 + 大图浏览（左右切换、Esc 关闭）；`<audio>`/`<video>` 与 shot-player 播放
  - 图片与成片默认展开，音频与单镜视频默认折叠；切换会话或关闭面板时暂停
  - Markdown 只放行本站图片
  - 测试：Playwright 覆盖大图浏览、音视频加载、外链拒绝、折叠默认值
  - _需求: 13.1, 13.2, 13.3_

- [x] 18. 可调整面板宽度
  - 左边缘拖拽手柄（鼠标/触屏/键盘 ←→，`role=separator`），320px–80vw，双击恢复 440px，宽度记住
  - 宽屏时页面留白跟随宽度
  - 测试：拖拽、键盘、双击恢复、刷新与跨页保留
  - _需求: 14.1_
