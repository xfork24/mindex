"""选题挖掘（路线图 M2）：领域档案 → 数据源抓取 → 聚类与 LLM 归纳 → 打分与素材覆盖 → 大纲 → 创作任务。

业务逻辑只在本包（服务层）；``api.py`` 是 REST 薄封装，``mindex.cli_topics`` 直接调用服务层，
智能助手工具（``mindex.agent.tools.topics``）经回环调用 REST，MCP 原样导出这些工具。
能力与入口的对照表见 ``capabilities.py``，由 ``tests/test_topics_parity.py`` 校验。详见 docs/topics.md。
"""
