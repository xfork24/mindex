# Mindex 文档中心

本目录收录项目的全部文档，讲清**机制与用法**。同一目录内既有机制文档（架构、管线、检索），也有按步骤执行的操作手册，两者互补、互相链接。设计决策与计划类文档放在 [`plans/`](../plans/README.md)，带日期的验证与审计记录放在 [`history/`](history/)。

## 按需求导航

| 我想… | 先读 | 再读 |
|---|---|---|
| 了解整体架构与数据流 | [架构与模块详解](architecture.md) | [索引版本与空间身份](index-versions.md) |
| 跑通离线处理管线 | [离线管线详解](pipeline.md) | [电影处理指南](FILM_WORKFLOW.md) |
| 查某个命令怎么用 | [CLI 完全参考](cli-reference.md) | [配置参考](config-reference.md) |
| 理解检索排名怎么算 | [检索机制详解](search.md) | [三渠道独立处理与召回](recall-enhancement.md) |
| 管理演员、角色，按出镜查找片段 | [人物库与证据审核](persons.md) | [人物配置](config-reference.md#person--人物服务) |
| 迁移到 macOS、建立 v2 空间 | [macOS 模型身份迁移](macos-model-migration.md) | [实际迁移记录](REHASH_MIGRATION.md) |
| 换模型 / 回退索引 | [索引版本与空间身份](index-versions.md) | [部署说明](DEPLOYMENT.md) |
| 做真实模型端到端验证 | [e2e 实验框架](e2e-framework.md) | [端到端操作手册](E2E_RUNBOOK.md)、[测试计划](../plans/E2E_TEST_PLAN.md) |
| 管理素材（改名/收藏/隐藏） | [素材后台指南](admin.md) | — |
| 回收磁盘空间 | [磁盘清理](cleanup.md) | [CLI 参考](cli-reference.md#磁盘清理) |
| 备份、迁移合并或全新 clone 恢复 | [数据备份与迁移](backup.md) | [CLI 参考](cli-reference.md#数据备份与迁移) |
| 切换线上/自建模型、克隆音色、调检索权重 | [设置页指南](settings.md) | [配置参考](config-reference.md) |
| 从文案生成视频（创作工作台） | [创作工作流](studio.md) | [设置页：视频生成与动画生成角色](settings.md) / [部署：ComfyUI 与动画录制](DEPLOYMENT.md#comfyui可选的-aigc-视频生成服务) |
| 用自然语言操作 Mindex，或接入外部 harness（MCP） | [智能助手](agent.md) | [设置页指南](settings.md) |
| 用哔哩哔哩缓存转 MP4 | [m4s-converter](../tools/m4s-converter/README.md) | — |
| 标注评测查询 | [评测标注说明](../eval/README.md) | [e2e 实验框架](e2e-framework.md) |
| 了解后续规划与最近一次审查 | [功能路线图](../plans/ROADMAP.md) | [2026-10 审查报告](#审查报告) |

## 文档一览

- [architecture.md](architecture.md) — 按页面划分的源码目录、模块职责、离线/在线数据流、存储布局、SQLite 表、五条核心不变量
- [pipeline.md](pipeline.md) — detect/split/transcribe/align/analyze/index 各阶段机制、检查点与断点续跑、指纹键
- [cli-reference.md](cli-reference.md) — `mindex`（含 `agent`、`mcp`）、`mindex-model-server` 与 `mindex-person-server` 的命令、参数、默认值、示例
- [config-reference.md](config-reference.md) — 全部配置键、类型、默认值、影响与常用组合
- [search.md](search.md) — 三渠道召回（新版）与五路召回（旧版）、加权 RRF、视频查询窗口化、筛选器、分数语义
- [recall-enhancement.md](recall-enhancement.md) — 画面、对白、分析三渠道的独立处理、增量发布与召回
- [persons.md](persons.md) — 人物库、参考图、出镜审核、发布与人物检索、镜头详情
- [index-versions.md](index-versions.md) — 索引版本指纹、发布检查、激活与回退、能力门槛
- [e2e-framework.md](e2e-framework.md) — probe/pin 信任模型、11 个 phase 状态机、标注冻结、评测 profile
- [admin.md](admin.md) — 素材后台功能与管理 API
- [settings.md](settings.md) — 设置页：十个模型角色的档案热切换、线上厂商支持、音色克隆、检索调参、索引版本、密钥安全
- [studio.md](studio.md) — 创作工作流：文案模板与参考文稿、拆分镜、五种分镜画面来源（召回/片段/图片/AIGC/AI 动画）、画幅、版本与构建、阶段注册表、API
- [agent.md](agent.md) — 智能助手：全局面板、分级确认与删除策略、工具领域与动态加载、附件、视觉输入、密钥表单、页面同步与定位、Markdown 回复、图片与音视频、可调宽度、任务跟踪与取消、全部工具表、MCP 导出（领域过滤）与 deepseek-harness 兼容
- [cleanup.md](cleanup.md) — 磁盘清理：三级分类（每次删/策略过期/只报告）、保留规则、锁与并发
- [backup.md](backup.md) — 离线备份、部分迁移、已有库合并、切换中断恢复和全新 clone 恢复流程

## 验证与审计记录

带日期的历史档案，位于 [`docs/history/`](history/)，记录当时的验证范围与结果，不代表当前代码的测试结论。引用的 `artifacts/` 为本地产物，不在仓库中。

| 文档 | 内容 |
|---|---|
| [project-review-2026-10-22.md](project-review-2026-10-22.md) | 项目架构与规范审查：发现、建议与当时已修正项（后续见[审查报告](#审查报告)） |
| [source-layout-verification.md](history/source-layout-verification.md) | 前后端目录重组、打包与验证范围，含既有失败和未完成检查 |
| [implementation-review.md](history/implementation-review.md) | 三份计划的实现审查与端到端验收 |
| [recall-enhancement-verification.md](history/recall-enhancement-verification.md) | 三渠道改造验证 |
| [persons-verification.md](history/persons-verification.md) / [person-reference-recall-verification.md](history/person-reference-recall-verification.md) | 人物召回、镜头详情、对白提及人物召回修复 |
| [studio-workspace-verification.md](history/studio-workspace-verification.md) / [studio-refinement-verification.md](history/studio-refinement-verification.md) / [studio-row-ui-verification.md](history/studio-row-ui-verification.md) / [studio-loading-verification.md](history/studio-loading-verification.md) / [studio-alignment-verification.md](history/studio-alignment-verification.md) | Studio 工作台、候选与定长窗口、逐镜行、加载优化、对齐 UI 的验收 |
| [candidate-identities-verification.md](history/candidate-identities-verification.md) / [recall-filters-verification.md](history/recall-filters-verification.md) | 召回人物标记与排名、召回筛选与素材多选 |
| [ui-layout-verification.md](history/ui-layout-verification.md) / [ui-layout-coverage.md](history/ui-layout-coverage.md) / [ui-layout-final-coverage.md](history/ui-layout-final-coverage.md) | 全页面 UI 布局修正与覆盖记录 |
| [upload-verification.md](history/upload-verification.md) / [source-detail-loading.md](history/source-detail-loading.md) / [source-work-audit-2026-10-20.md](history/source-work-audit-2026-10-20.md) | 素材上传、详情加载、素材工作台全量检查 |
| [wemm-short-clip-verification.md](history/wemm-short-clip-verification.md) | WeMM 极短分镜采样修复 |
| [person-review-perf-baseline.md](history/person-review-perf-baseline.md) | 人物库出镜审核读取耗时基线（`scripts/profile_person_review.py` 生成） |

## 审查报告

2026-10 的全面审查（spec `mindex-review-2026-10`），位于 [`reviews/2026-10/`](reviews/2026-10/)，各报告的"当前可做"表已回填完成状态：

| 文档 | 内容 |
|---|---|
| [features-roadmap.md](reviews/2026-10/features-roadmap.md) | 功能集缺口与本期（M1）创作能力扩展：文案模板、参考文稿、画幅、五种画面来源 |
| [architecture.md](reviews/2026-10/architecture.md) | 仓库结构与架构：后台任务 runner、迁移、锁、执行器、工作台拆包、阶段注册表、前端拆分 |
| [code-quality.md](reviews/2026-10/code-quality.md) | 代码质量：鉴权、IO、日志、前端轮询与 lint |
| [docs-consistency.md](reviews/2026-10/docs-consistency.md) | 文档一致性偏差清单、修正状态与删除的文档 |

长期路线图（M1–M4）见 [plans/ROADMAP.md](../plans/ROADMAP.md)。

## 任务导向文档

| 文档 | 用途 |
|---|---|
| [README.md](../README.md) | 项目概览与快速上手 |
| [FILM_WORKFLOW.md](FILM_WORKFLOW.md) | 整部电影「分镜 → 切片 → 画面索引」最短流程 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | GPU 服务部署（自定义 mindex 服务 / llama.cpp 路径）、HTTP 协议 |
| [macos-model-migration.md](macos-model-migration.md) / [REHASH_MIGRATION.md](REHASH_MIGRATION.md) | macOS 模型身份迁移：操作手册与实际执行记录 |
| [E2E_RUNBOOK.md](E2E_RUNBOOK.md) | 真实模型端到端验证操作手册（当前实验：trailer-20261013） |
| [E2E_RESULTS.md](E2E_RESULTS.md) | e2e 实验结果汇总 |
| [VERIFICATION.md](VERIFICATION.md) | 实现验证记录（带日期的历史档案） |
| [QWEN_BENEFIT_EVALUATION.md](QWEN_BENEFIT_EVALUATION.md) | 换用 Qwen 系模型的收益评估 |
| [studio-verification.md](studio-verification.md) | 创作工作流验收记录 |
| [eval/README.md](../eval/README.md) | 评测查询标注说明与模板生成器 |

计划与设计决策文档在 [`plans/`](../plans/README.md)（含按主题分类的索引）：

| 文档 | 用途 |
|---|---|
| [PLAN.md](../plans/PLAN.md) | 设计决策、验收标准（40 条测试集 Hit@5 ≥85%） |
| [E2E_TEST_PLAN.md](../plans/E2E_TEST_PLAN.md) | 端到端测试设计与通过条件 |
| [STUDIO_PLAN.md](../plans/STUDIO_PLAN.md) / [STUDIO_REFINEMENT_PLAN.md](../plans/STUDIO_REFINEMENT_PLAN.md) | 创作工作流设计与修订 |
| [ROADMAP.md](../plans/ROADMAP.md) | 功能路线图（M1–M4：创作能力、选题与 agent、后期、桌面分发） |
| [REHASH_REFINEMENT_PLAN.md](../plans/REHASH_REFINEMENT_PLAN.md) | 模型身份与索引兼容性重构计划 |
