# 文档一致性审查（2026-10）

范围：所有活文档对照 2026-10-25 的源码。带日期的验证记录（`docs/history/`、`VERIFICATION.md` 等）不在修正范围内。本文是带日期的审查记录。"状态"列在任务 26 完成后回填。

## 结论

链接检查通过：91 个 Markdown 文件中没有失效链接。偏差集中在三类内容：

1. 新功能（智能助手、索引接口、文案角色）上线后，数量和模块清单没有同步；
2. 少数默认值或取值写错；
3. 智能助手相关文档滞后于 agent-capability-expansion，这一部分属于预期内的滞后。

## 偏差清单

| # | 文档 | 现状 | 代码事实 | 状态 |
|---|---|---|---|---|
| D1 | README 功能表与 06 节 | "七类模型角色"，列举中缺智能助手 | `settings/runtime_settings.py` `ROLES` 有 8 个，本期新增 2 个后为 10 个 | 待修 |
| D2 | `docs/settings.md` | 标题写"八个模型角色"，表格只有 7 行（缺人物识别）；API 表写"五角色档案" | 8 个，本期后为 10 个 | 待修 |
| D3 | `docs/architecture.md`、CLAUDE.md | "七个 Protocol"，后端模块列表缺 `writer` | 已有 8 个（含 `Writer`），本期新增 `VideoGenerator`、`Animator` | 待修 |
| D4 | `docs/config-reference.md` asr.backend | 取值列为 mindex/llama_cpp/openai | 还有 `minimax`（`shared/config.py`） | 待修 |
| D5 | `docs/config-reference.md` | `subtitle.burn` 默认值写成 true | `SubtitleConfig.burn = False` | 待修 |
| D6 | `docs/config-reference.md`、`docs/agent.md` | 缺 `attachment_ttl_hours`、`attachment_max_mb`、`max_images`、`mcp.domains`，以及环境变量 `MINDEX_AGENT_API_KEY`、`MINDEX_MCP_TOKEN` | `AgentConfig`、`AgentMcpConfig` 中都有 | 待修 |
| D7 | `docs/cli-reference.md` | 命令组表缺 `agent` | `cli.py` 注册了 `agent`（`tools`）和顶层 `mcp` | 待修 |
| D8 | `docs/architecture.md` 模块表与目录树 | 缺整个 `agent/` 包，以及 `cli_agent.py`、`models/writer.py`、`models/deployment.py`、`admin/indexes.py`、`shared/task_cancel.py`、`shared/agent_domains.py`、`studio/material_digest.py`、`persons/profiling.py`、`search/warmup.py` | 这些文件都存在 | 待修 |
| D9 | `docs/architecture.md` | `studio/studio.py` 写作"五步：split/recall/tts/select/render" | 实际流程是 script → split → recall/tts → select → render → compose | 待修 |
| D10 | `docs/architecture.md` 存储与表 | 缺 `agent.sqlite3` 及其 6 张表、`agent-attachments/` | 见 `agent/session_store.py`、`changes.py`、`attachments.py` | 待修 |
| D11 | `docs/architecture.md` 在线服务 | 页面列表缺 `/persons`，也没有 `/api/agent`、`/mcp`、`/login`、`/api/indexes` | 都已注册 | 待修 |
| D12 | `docs/agent.md` | 写"第一期共 30 个工具"，并称素材、人物、检索领域只读；MCP 导出写 28 个 | 注册表有 57 个工具，MCP 导出 54 个；多个领域有 write 和 danger 工具 | 待修（agent spec 的 20.3 可能已处理） |
| D13 | CLAUDE.md | "保持总数约 30 以内" | 已改为按领域动态加载 | 待修 |
| D14 | CLAUDE.md | messages.json "约 1800 键" | 约 2,045 键 | 待修 |
| D15 | CLAUDE.md、AGENT.md | cli 注册列表缺 `agent`/`mcp`，Web/API 路由列表缺 `agent/api.py`、`admin/indexes.py`，存储布局缺 agent 数据 | 见代码 | 待修 |
| D16 | `docs/settings.md` 热生效 | 只写了"`mindex e2e` 除外" | `clips`、`backup` 也不读取覆盖层（与 cli-reference 一致） | 待修 |
| D17 | README 与 `docs/README.md` | 文档表缺 `agent.md`；README 没有介绍智能助手与 MCP | 功能已上线 | 待修 |
| D18 | `docs/README.md` | `studio.md` 的描述仍是"五步状态机"；`cli-reference` 的描述缺 `mindex-person-server` 和 agent | 见代码 | 待修 |
| D19 | `docs/admin.md` 或 `docs/index-versions.md` | 没有 `/api/indexes` 的列表、检查、激活接口 | `admin/indexes.py` | 待修 |
| D20 | README、AGENT.md | 测试数与通过记录停留在 2026-10-22 的 966 项 | 需要重跑；保留日期并标注为历史记录 | 待修 |

本期新功能对应的文档（模板、参考文稿、画幅、五种来源、两个角色、`--allow-insecure`、CI、迁移、锁）在任务 26 中一并补齐，届时追加到本表。
