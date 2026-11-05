# Mindex 文档中心

Mindex 定位为 **one-stop video creation agent**（一站式视频创作智能体）：从选题、文案、分镜、配音、选片到成片后期与平台交付的完整创作工作流，底座是中文视频片段检索与人物库。本目录收录全部机制与用法文档；设计决策与实施计划放在 [`plans/`](../plans/README.md)，带日期的验收结论追加在[实现验证记录](verification.md)。

## 按需求导航

| 我想… | 先读 | 再读 |
|---|---|---|
| 了解整体架构与数据流 | [架构与模块详解](architecture.md) | [共享基础设施](shared-infrastructure.md) |
| 跑通离线处理管线 | [离线管线详解](pipeline.md) | [电影处理指南](film-workflow.md) |
| 查某个命令怎么用 | [CLI 完全参考](cli-reference.md) | [配置参考](config-reference.md) |
| 理解检索排名怎么算 | [检索机制详解](search.md) | [三渠道独立处理与召回](recall-enhancement.md) |
| 管理演员、角色，按出镜查找片段 | [人物库与证据审核](persons.md) | [人物配置](config-reference.md#person--人物服务) |
| 换模型 / 回退索引 | [索引版本与空间身份](index-versions.md) | [部署说明](deployment.md) |
| 做真实模型端到端验证 | [e2e 实验框架](e2e-framework.md) | [端到端操作手册](e2e-runbook.md)、[验收测试计划](../plans/e2e-acceptance-test-plan.md) |
| 管理素材（改名/收藏/隐藏/上传/处理任务） | [素材后台指南](admin.md) | — |
| 回收磁盘空间、清退旧切片 | [磁盘清理](cleanup.md) | [旧切片维护](clips-maintenance.md) |
| 备份、迁移合并或全新 clone 恢复 | [数据备份与迁移](backup.md) | [CLI 参考](cli-reference.md#数据备份与迁移) |
| 部署持久 worker、跨节点创作或可恢复上传 | [持久创作、目录发布与上传会话](scaling-durable-delivery.md) | [水平扩展交付矩阵](../plans/horizontal-scaling-delivery-matrix.md)、[验证记录](scaling-enhancement-verification.md) |
| 切换线上/自建模型、克隆音色、调检索权重 | [设置页指南](settings.md) | [配置参考](config-reference.md) |
| 按领域发现选题、打分并建成创作任务 | [选题挖掘](topics.md) | [创作工作流](studio.md) |
| 从文案生成视频（创作工作台） | [创作工作流](studio.md) | [设置页：视频生成与动画生成角色](settings.md) / [部署：ComfyUI 与动画录制](deployment.md#comfyui可选的-aigc-视频生成服务) |
| 用自然语言操作 Mindex，或接入外部 harness（MCP） | [智能助手](agent.md) | [设置页指南](settings.md) |
| 安装或构建桌面版（macOS/Windows/Linux） | [Mindex 桌面版](desktop.md) | [设置页：本机服务](settings.md#本机服务) |
| 了解前端结构与 UI 约定 | [前端结构](frontend.md) | [界面文案与多语言](i18n.md) |
| 新增或修改界面文案 | [界面文案与多语言](i18n.md) | [前端结构](frontend.md#资源版本) |
| 了解鉴权（访问令牌、MCP 令牌、桌面注入） | [Web/API 访问鉴权](web-auth.md) | — |
| 写测试、跑回归、更新快照 | [测试约定](testing.md) | — |
| 了解锁、迁移与后台任务机制 | [共享基础设施](shared-infrastructure.md) | [架构](architecture.md) |
| 标注评测查询 | [评测标注说明](../eval/README.md) | [e2e 实验框架](e2e-framework.md) |
| 了解后续规划与最近一次审查 | [功能路线图](../plans/roadmap.md) | [审查报告](#审查报告) |

## 文档一览

**核心机制**：

- [architecture.md](architecture.md) — 按页面划分的源码目录、模块职责、离线/在线数据流、存储布局、SQLite 表、五条核心不变量
- [pipeline.md](pipeline.md) — detect/split/transcribe/align/analyze/index 各阶段机制、检查点与断点续跑、指纹键
- [search.md](search.md) — 三渠道召回（新版）与五路召回（旧版）、加权 RRF、视频查询窗口化、筛选器、分数语义
- [recall-enhancement.md](recall-enhancement.md) — 画面、对白、分析三渠道的独立处理、增量发布与召回
- [index-versions.md](index-versions.md) — 索引版本指纹、发布检查、激活与回退、能力门槛
- [persons.md](persons.md) — 人物库、参考图、出镜审核、发布与人物检索（含文献指称规则）、镜头详情

**操作与运维**：

- [cli-reference.md](cli-reference.md) — `mindex`（含 `agent`、`mcp`、`topics`、`studio`）、`mindex-model-server` 与 `mindex-person-server` 的命令、参数、默认值、示例
- [config-reference.md](config-reference.md) — 全部配置键、类型、默认值、影响与常用组合
- [admin.md](admin.md) — 素材后台功能与管理 API（上传、四项处理工作台、素材组/音乐组、复用规则）
- [settings.md](settings.md) — 设置页：十二个模型角色的档案热切换、线上厂商支持、音色克隆、检索调参、索引版本、密钥安全
- [cleanup.md](cleanup.md) — 磁盘清理：三级分类（每次删/策略过期/只报告）、保留规则、锁与并发
- [clips-maintenance.md](clips-maintenance.md) — 旧切片迁移、归档校验、边界修复与清退（`mindex clips`）
- [backup.md](backup.md) — 离线备份、部分迁移、已有库合并、切换中断恢复和全新 clone 恢复流程
- [deployment.md](deployment.md) — GPU 服务部署（各角色环境、启动脚本、HTTP 协议、ComfyUI、TransNetV2）
- [desktop.md](desktop.md) — Mindex 桌面版（macOS/Windows/Linux）：安装与首次打开、本机模型服务、后台运行与每日选题、随包组件、构建约定

**创作**：

- [studio.md](studio.md) — 创作工作流：文案模板与参考文稿、拆分镜、七种分镜画面来源、画幅、成片后期、曲库与品牌模板、多画幅导出与交付模板/平台、版本与构建、阶段注册表、API
- [topics.md](topics.md) — 选题挖掘：领域档案、数据源、挖掘与打分、素材覆盖、建创作任务、每日定时、四个入口
- [agent.md](agent.md) — 智能助手：全局面板、分级确认、工具领域与动态加载、附件、MCP 导出与 deepseek-harness 兼容

**平台与工程**：

- [frontend.md](frontend.md) — 前端结构：无框架原生 JS、页面划分、请求封装、创作页按序脚本、资源版本、UI 约定
- [i18n.md](i18n.md) — 界面文案与多语言：键规则、中文唯一源、生成链路、语言切换、后端消息反向映射
- [web-auth.md](web-auth.md) — Web/API 访问鉴权：令牌、会话 Cookie、MCP 令牌、桌面与回环凭证
- [testing.md](testing.md) — 测试约定：集成式哲学、分层与标记、浏览器回归与布局回归复现、快照与 parity 测试
- [shared-infrastructure.md](shared-infrastructure.md) — 共享基础设施：具名文件锁与顺序、SQLite 版本化迁移、后台任务三件套
- [scaling-durable-delivery.md](scaling-durable-delivery.md) — Studio 持久账本、目录 manifest、修订冲突、外部调用核对和上传会话

**参考记录**：

- [e2e-framework.md](e2e-framework.md) — probe/pin 信任模型、11 个 phase 状态机、标注冻结、评测 profile
- [e2e-runbook.md](e2e-runbook.md) — 真实模型端到端验证操作手册
- [verification.md](verification.md) — 实现验证记录（带日期追加的验收档案）
- [third-party-notices.md](third-party-notices.md) — 第三方组件声明（仓库内固定版本脚本与随包组件的许可证、版本与校验值）

## 审查报告

2026-10 的全面审查报告（铺平在本目录，`review-2026-10-*`），取代关系：~~project-review-2026-10-22（已删除）~~ → 2026-10 专项审查 → [review-2026-10-26-project-analysis.md](review-2026-10-26-project-analysis.md)（综合分析）→ [review-1102.md](review-1102.md)（综合分析，最新）。各报告的"当前可做"表已回填完成状态；其中 architecture 与 code-quality 报告仍含未解决的遗留项（A10/A11、Q21/Q22），以报告内标注为准：

| 文档 | 内容 |
|---|---|
| [review-1102.md](review-1102.md) | 综合项目分析（2026-11-02：规模实测、架构与规范评价、13 条潜在问题） |
| [review-2026-10-26-project-analysis.md](review-2026-10-26-project-analysis.md) | 综合项目分析（规模、功能全览、演进与 7 条建议） |
| [features-roadmap.md](review-2026-10-features-roadmap.md) | 功能集缺口与本期（M1）创作能力扩展 |
| [architecture.md](review-2026-10-architecture.md) | 仓库结构与架构审查（runner、迁移、锁、执行器、拆包） |
| [code-quality.md](review-2026-10-code-quality.md) | 代码质量（鉴权、IO、日志、前端轮询与 lint） |
| [docs-consistency.md](review-2026-10-docs-consistency.md) | 文档一致性偏差清单与修正状态 |

长期路线图（M1–M4）见 [plans/roadmap.md](../plans/roadmap.md)。

## 任务导向文档

| 文档 | 用途 |
|---|---|
| [README.md](../README.md) | 项目概览与快速上手 |
| [film-workflow.md](film-workflow.md) | 整部电影「分镜 → 切片 → 画面索引」最短流程 |
| [deployment.md](deployment.md) | GPU 服务部署（自定义 mindex 服务 / llama.cpp 路径）、HTTP 协议 |
| [e2e-runbook.md](e2e-runbook.md) | 真实模型端到端验证操作手册 |
| [eval/README.md](../eval/README.md) | 评测查询标注说明与模板生成器 |

计划与设计决策文档在 [`plans/`](../plans/README.md)（含按主题分类的索引）：

| 文档 | 用途 |
|---|---|
| [retrieval-design-plan.md](../plans/retrieval-design-plan.md) | 检索的设计决策、验收标准（40 条测试集 Hit@5 ≥85%） |
| [e2e-acceptance-test-plan.md](../plans/e2e-acceptance-test-plan.md) | 端到端验收测试设计与通过条件 |
| [studio-initial-plan.md](../plans/studio-initial-plan.md) / [studio-refinement-plan.md](../plans/studio-refinement-plan.md) | 创作工作流初始设计与修订 |
| [roadmap.md](../plans/roadmap.md) | 功能路线图（M1–M4：创作能力、选题与 agent、后期、桌面分发） |
| [model-identity-rehash-plan.md](../plans/model-identity-rehash-plan.md) | 模型身份与索引兼容性重构（三层身份模型） |

双路径上传与浏览器 S3 endpoint/CORS 配置见 [scaling-upload.md](scaling-upload.md)。
