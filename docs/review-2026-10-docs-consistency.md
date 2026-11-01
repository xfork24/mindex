# 文档一致性审查（2026-10）

范围：所有活文档对照 2026-10-25 的源码。带日期的验证记录（`docs/history/`、`verification.md` 等）不在修正范围内。本文是带日期的审查记录。"状态"列已在任务 26 回填。

## 结论

链接检查通过：91 个 Markdown 文件中没有失效链接。偏差集中在三类内容：

1. 新功能（智能助手、索引接口、文案角色）上线后，数量和模块清单没有同步；
2. 少数默认值或取值写错；
3. 智能助手相关文档滞后于 agent-capability-expansion，这一部分属于预期内的滞后。

## 偏差清单

| # | 文档 | 现状 | 代码事实 | 状态 |
|---|---|---|---|---|
| D1 | README 功能表与 06 节 | "七类模型角色"，列举中缺智能助手 | `settings/runtime_settings.py` `ROLES` 有 8 个，本期新增 2 个后为 10 个 | 已修：功能表改为"十类模型角色"并新增"智能助手与 MCP"行；06 节列出十个角色（`ROLES` 实为 analyzer、writer、generator、embedding、asr、tts、video_generator、animator、person、agent） |
| D2 | `docs/settings.md` | 标题写"八个模型角色"，表格只有 7 行（缺人物识别）；API 表写"五角色档案" | 8 个，本期后为 10 个 | 已修：标题与表格均为十个角色（补人物识别），API 表"十个角色档案"，并补 `embedding-status`、音色目录接口 |
| D3 | `docs/architecture.md`、CLAUDE.md | "七个 Protocol"，后端模块列表缺 `writer` | 已有 8 个（含 `Writer`），本期新增 `VideoGenerator`、`Animator` | 已修：architecture 与 CLAUDE.md 改为十个 Protocol（含 `Writer`、`VideoGenerator`、`Animator`），工厂表补 `make_writer`、`make_video_generator`、`make_animator` 与 ASR `minimax`；AGENT.md 后端模块列表补 `writer`、`video_generators`、`animator` |
| D4 | `docs/config-reference.md` asr.backend | 取值列为 mindex/llama_cpp/openai | 还有 `minimax`（`shared/config.py`） | 已修（并说明 minimax 返回词级时间戳、无需 align） |
| D5 | `docs/config-reference.md` | `subtitle.burn` 默认值写成 true | `SubtitleConfig.burn = False` | 已修：默认 false，默认输出软字幕轨道 |
| D6 | `docs/config-reference.md`、`docs/agent.md` | 缺 `attachment_ttl_hours`、`attachment_max_mb`、`max_images`、`mcp.domains`，以及环境变量 `MINDEX_AGENT_API_KEY`、`MINDEX_MCP_TOKEN` | `AgentConfig`、`AgentMcpConfig` 中都有 | 已修：config-reference 的 agent 段与 agent.md 配置表都列出四个键；环境变量表补 `MINDEX_AGENT_API_KEY`、`MINDEX_MCP_TOKEN`（另见 D21） |
| D7 | `docs/cli-reference.md` | 命令组表缺 `agent` | `cli.py` 注册了 `agent`（`tools`）和顶层 `mcp` | 已修：命令组表补 `agent`（`tools`）与顶层 `mcp`；逐个叶子命令的 `--help` 选项与文档对照无遗漏 |
| D8 | `docs/architecture.md` 模块表与目录树 | 缺整个 `agent/` 包，以及 `cli_agent.py`、`models/writer.py`、`models/deployment.py`、`admin/indexes.py`、`shared/task_cancel.py`、`shared/agent_domains.py`、`studio/material_digest.py`、`persons/profiling.py`、`search/warmup.py` | 这些文件都存在 | 已修：模块表与目录树补 `agent/` 全部模块、`cli_agent.py`、`models/writer.py`、`models/deployment.py`、`models/video_generators.py`、`models/animator.py`、`admin/indexes.py`、`shared/task_cancel.py`、`shared/comfyui.py`、`studio/material_digest.py`、`studio/studio_diagnostics.py`、`studio/animation_capture.py`、`persons/profiling.py`、`search/warmup.py`；`shared/agent_domains.py` 只列领域名 |
| D9 | `docs/architecture.md` | `studio/studio.py` 写作"五步：split/recall/tts/select/render" | 实际流程是 script → split → recall/tts → select → render → compose | 已修：`studio/studio.py` 改述为任务持久化与旧任务五步状态机；数据流改为 大纲/模板/参考文稿 → 文案 → 拆分镜 → 按五种来源的逐镜阶段 → 成片合成 |
| D10 | `docs/architecture.md` 存储与表 | 缺 `agent.sqlite3` 及其 6 张表、`agent-attachments/` | 见 `agent/session_store.py`、`changes.py`、`attachments.py` | 已修：目录树补 `agent.sqlite3`、`agent-attachments/`、`locks/`（含 `cancel/`）与 studio `inputs/`；列出 agent 库 6 张表；SQLite 表补 `script_templates`（迁移 v2，`METADATA_STEPS` 共 2 步） |
| D11 | `docs/architecture.md` 在线服务 | 页面列表缺 `/persons`，也没有 `/api/agent`、`/mcp`、`/login`、`/api/indexes` | 都已注册 | 已修：页面列表补 `/persons`，并列出全部 `/api/*` 前缀、`/mcp`、`/login` 与 `/logout`（仅 POST） |
| D12 | `docs/agent.md` | 写"第一期共 30 个工具"，并称素材、人物、检索领域只读；MCP 导出写 28 个 | 注册表有 57 个工具，MCP 导出 54 个；多个领域有 write 和 danger 工具 | 已由 agent-capability-expansion 处理；本次按注册表复核：92 个工具、89 个导出 MCP，8 个领域的工具名与风险级别与 agent.md 各表逐项一致；24 个验收场景与 `tests/agent_scenarios.py` 一致 |
| D13 | CLAUDE.md | "保持总数约 30 以内" | 已改为按领域动态加载 | 已由先前修改处理：CLAUDE.md 已改为按领域动态加载的指引（目前最大的 studio 领域 28 个） |
| D14 | CLAUDE.md | messages.json "约 1800 键" | 约 2,045 键 | 已修：约 2290 键（实测 2,287） |
| D15 | CLAUDE.md、AGENT.md | cli 注册列表缺 `agent`/`mcp`，Web/API 路由列表缺 `agent/api.py`、`admin/indexes.py`，存储布局缺 agent 数据 | 见代码 | 已修：cli 注册列表补 `agent`/`mcp`；Web/API 列表补 `agent/api.py`、`admin/indexes.py`、`shared/web_auth.py`；存储布局补 agent 数据、`locks/` 与 studio 新目录 |
| D16 | `docs/settings.md` 热生效 | 只写了"`mindex e2e` 除外" | `clips`、`backup` 也不读取覆盖层（与 cli-reference 一致） | 已修：热生效例外写为 `e2e`、`clips`、`backup`（与 `cli.py` 一致）；CLAUDE.md 同步 |
| D17 | README 与 `docs/README.md` | 文档表缺 `agent.md`；README 没有介绍智能助手与 MCP | 功能已上线 | 已修：README 与 docs/README 的文档表加入 agent.md 与 ROADMAP；README 功能表、工作原理图加入智能助手与 MCP |
| D18 | `docs/README.md` | `studio.md` 的描述仍是"五步状态机"；`cli-reference` 的描述缺 `mindex-person-server` 和 agent | 见代码 | 已修：studio.md 描述改为文案模板、参考文稿、五种画面来源、画幅、阶段注册表；cli-reference 描述补 `agent`、`mcp` 与 `mindex-person-server` |
| D19 | `docs/admin.md` 或 `docs/index-versions.md` | 没有 `/api/indexes` 的列表、检查、激活接口 | `admin/indexes.py` | 已修：index-versions.md 新增 `/api/indexes` 列表、检查、激活接口说明 |
| D20 | README、AGENT.md | 测试数与通过记录停留在 2026-10-22 的 966 项 | 需要重跑；保留日期并标注为历史记录 | 已修：README、AGENT.md 改为"2026-10 审查期间完整运行 1732 passed、13 skipped（历史记录，以本次运行为准）"，不再声称当前全部通过；README 开发检查补齐 CI 中的全部检查 |

本期新功能对应的文档（模板、参考文稿、画幅、五种来源、两个角色、`--allow-insecure`、CI、迁移、锁）已在各任务中写入 studio.md、config-reference.md、settings.md、deployment.md、cli-reference.md 与 architecture.md，任务 26 复核后补齐了下表的其余偏差。

### 任务 26 追加

| # | 文档 | 现状 | 代码事实 | 状态 |
|---|---|---|---|---|
| D21 | `docs/config-reference.md` 环境变量表 | 缺 `MINDEX_VIDEO_API_KEY`、`MINDEX_PLAYWRIGHT_MODULE`（以及 D6 的两个） | `VideoGeneratorConfig.api_key_env`；测试与 UI 回归读取 `MINDEX_PLAYWRIGHT_MODULE` | 已修 |
| D22 | `docs/settings.md` 语音识别行与厂商要点 | 线上 ASR"只出纯文本，时间戳由本地对齐补齐" | `MiniMaxTranscriber` 请求 `timestamp_level=word`，`transcribe` 的 minimax 分支直接得到带时间戳对白 | 已修 |
| D23 | `docs/deployment.md`、`docs/pipeline.md`、`docs/e2e-runbook.md` | 安装命令引用 `deploy/embedding.txt`、`asr.txt`、`detector.txt`、`requirements-alignment.txt` | 这些文件已在 9fe2794 删除，`deploy/` 只剩 `detector-local.txt`、`person.txt` | 已修：改为内联原文件中的依赖清单 |
| D24 | `docs/studio.md` | 构建依赖仍为"内容 → 召回/配音 → 选片 → 渲染 → 成片"；删除方案说"不再被引用的产物由 `mindex clean` 回收"；文件树缺 `inputs/` 与取消哨兵；`POST /jobs` 写"原文" | 依赖从文案起并按来源展开；`clean` 没有回收 `versions/` 的规则；`CreateJobBody.text` 是视频大纲；执行由 `StudioExecutor` 管理 | 已修 |
| D25 | `docs/cleanup.md` 永不清理 | 未列 studio `versions/`、`inputs/` 与 `agent.sqlite3` | `clean_studio.py` 只有 previews、attempts、exports 三条规则 | 已修 |
| D26 | `docs/backup.md` | 未说明用户文案模板、任务 `inputs/` 与旧快照的结构升级 | `backup_db.TABLES` 含 `script_templates`；导入暂存副本先 `migrate_metadata` | 已修 |
| D27 | `docs/persons.md` API | 缺 `POST /api/persons/identify` 与 `POST /tasks/{id}/cancel` | `person_api.py` 已注册 | 已修 |
| D28 | `docs/search.md` | 没有按片段找相似 | `POST /api/search/similar` | 已修：新增"找相似片段"一节 |
| D29 | `docs/film-workflow.md` | "素材录入仍使用 CLI" | 素材管理页支持上传与分步处理 | 已修 |
| D30 | `third-party-notices.md` | 升级时手工更新 `?v=` | `?v=` 由 `scripts/build-assets.py` 生成 | 已修；两个文件的 SHA-256 已复核一致 |
| D31 | `docs/deployment.md` 访问配置 | "该 Demo 不提供多用户认证" | 有 `MINDEX_WEB_TOKEN` 单令牌认证，非回环无令牌拒绝启动 | 已修 |
| D32 | `docs/architecture.md` 前端说明 | 未提 `shared/api.js` 与创作页的多脚本加载 | `window.MindexApi`；`studio/studio-*.js` 按 `studio.html` 顺序加载 | 已修；CLAUDE.md、AGENT.md 已有同样说明 |
| D33 | CLAUDE.md 创作工作流 | 开头仍是"五步状态机，状态即 job.json" | 新任务走版本工作台与按来源的阶段序列；补 `studio_templates_api.py`、`studio_diagnostics.py`、`animation_capture.py`、`workspace/sources.py` | 已修 |
| D34 | `plans/RECALL_MIGRATION_PLAN.md` | "状态：已批准，待实施" | 已实施（`docs/history/implementation-review.md`） | 已加历史记录说明 |
| D35 | `plans/roadmap.md` | M1 标为"本期" | spec 任务 17–25 均已完成 | 已标为已完成 |
| D36 | `docs/README.md`、`plans/README.md` | 索引缺审查报告、ROADMAP 与 `history/person-review-perf-baseline.md` | 文件存在但无入口 | 已修：新增"审查报告"一节 |
| D37 | 带日期的记录（`project-review-2026-10-22.md`、`verification.md`、`E2E_RESULTS.md`、`QWEN_BENEFIT_EVALUATION.md`、`studio-verification.md`、`REHASH_MIGRATION.md`、`history/person-review-perf-baseline.md`） | 没有标明以哪份文档为准 | — | 已在标题下加一行历史记录说明，正文不改 |

## 删除的文档

| 文档 | 理由 |
|---|---|
| `docs/tools.md` | 内容是 `tools/m4s-converter/README.md` 的旧摘抄，已被后者完整覆盖，且已经过时（写"中间文件留在缓存子目录"，实际 `convert.sh` 默认清理并提供 `--keep-intermediates`；含本机绝对路径）；不是带日期的决策或验证记录。入口改为直接链接 `tools/m4s-converter/README.md` |

其余候选均保留：`plans/` 下的计划是解释现状成因的设计记录；`docs/history/` 与带日期的验证、实验、迁移记录有存档价值；`ui-layout-coverage.md` 与 `ui-layout-final-coverage.md` 是同一项工作不同阶段的覆盖记录，不重复。

## 仍存在的已知不一致

- `docs/history/`、`plans/` 与带日期的记录按约定不改正文，其中的模块名（如 `studio_workspace.py`、`cli_common.py`、`studio.js`）、测试数与默认值是当时的状态。
- 测试数量只引用任务 16 的完整运行结果；任务 26 没有重新运行完整测试。
