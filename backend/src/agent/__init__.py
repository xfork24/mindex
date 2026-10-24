"""智能助手：自然语言驱动 Mindex 核心能力的 harness。

结构（与运行方式无关的工具层 + 可替换的运行时）：

- ``llm/``：与 deepseek-harness（dsh）对齐的消息/内容块/流式块词汇，以及 OpenAI 兼容与
  Anthropic 原生两种协议适配器；``scripted`` 是测试专用的脚本化模型。
- ``registry``：工具注册表（名称、领域、风险级别、参数模型、预览与执行）。
- ``tools``：第一期工具集，全部经 ``loopback`` 回环调用既有 REST 接口，不复制业务逻辑。
- ``policy``：分级确认策略（read 直接执行 / write 预览后确认 / danger 必须显式确认）。
- ``session_store``：dsh 形状的会话事件日志（SQLite），模型可见内容只从日志派生。
- ``runtime``：``AgentRuntime`` 协议与内置实现 ``BuiltinRuntime``；未来的 ``DshRuntime``
  可在不改前端与工具的前提下替换它。
- ``api``：``/api/agent`` 路由（会话、消息、SSE、确认、提问、取消、能力缺口）。
- ``mcp_server``：把同一工具注册表导出为 MCP 服务（streamable-http ``/mcp`` 与 stdio）。
"""
