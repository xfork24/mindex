# 设计与实施计划索引

`plans/` 保存各功能的设计资料和实施计划，是历史设计依据，不代表当前代码的行为。以源码、配置模型和 CLI 帮助为准；机制说明见 [文档中心](../docs/README.md)。

名称相近的几份计划主题不同，不是重复文件，请按下表区分。

## 总体与测试

| 文件 | 主题 |
|---|---|
| [retrieval-design-plan.md](retrieval-design-plan.md) | 视频片段检索的实施计划与设计决策（原 `PLAN.md`） |
| [roadmap.md](roadmap.md) | 功能路线图（M1–M5）；对应审查报告见 [docs/review-2026-10-features-roadmap.md](../docs/review-2026-10-features-roadmap.md) |
| [e2e-acceptance-test-plan.md](e2e-acceptance-test-plan.md) | 端到端验收测试计划 |
| [horizontal-scaling-plan.md](horizontal-scaling-plan.md) | 素材容量水平扩展：瓶颈评估与 PostgreSQL/多后端存储/分布式锁队列的分阶段方案（已设计、未实施） |

## 检索与索引

| 文件 | 主题 |
|---|---|
| [recall-channels-enhancement-plan.md](recall-channels-enhancement-plan.md) | 画面、对白、分析三渠道独立处理与混合召回 |
| [model-identity-rehash-plan.md](model-identity-rehash-plan.md) | macOS 模型身份与索引兼容性重构（三层身份模型） |
| [shot-media-refactor-plan.md](shot-media-refactor-plan.md) | 以 shot 为唯一业务媒体来源的改造与数据迁移 |

## 人物库

| 文件 | 主题 |
|---|---|
| [person-recognition-plan.md](person-recognition-plan.md) | 人物库、演员表导入与人物出镜召回（最初设计） |
| [person-library-ui-plan.md](person-library-ui-plan.md) | 演员表导入：多图全流程改造 |
| [person-library-ui-plan-v2.md](person-library-ui-plan-v2.md) | 人物与参考图交互改造、全局可搜索多选框（与上一份是不同需求，`v2` 指第二轮改造） |
| [person-frames-migration-plan.md](person-frames-migration-plan.md) | `persons/frames` 展示帧存储改造 |
| [person-role-only-processing-plan.md](person-role-only-processing-plan.md) | 人物处理改为仅处理角色出镜 |

## 创作（Studio）

| 文件 | 主题 |
|---|---|
| [studio-initial-plan.md](studio-initial-plan.md) | 文本生成视频工作流的最初实施计划 |
| [studio-refinement-plan.md](studio-refinement-plan.md) | Studio 静态修复与集成验收 |
| [studio-version-workbench-plan.md](studio-version-workbench-plan.md) | 分镜与版本构建工作台改造 |
| [studio-window-selection-ui-plan.md](studio-window-selection-ui-plan.md) | 召回候选选择与定长窗口选片 |
| [studio-shot-recall-plan.md](studio-shot-recall-plan.md) | 创作页分镜四类召回与镜头详情人物信息（已实施） |
| [studio-save-window-perf-plan.md](studio-save-window-perf-plan.md) | 修复"保存窗口"耗时问题 |
| [delivery-template-platform-plan.md](delivery-template-platform-plan.md) | 交付模板与平台管理（平台目录、规则快照） |

## 素材管理、界面与运维

| 文件 | 主题 |
|---|---|
| [upload-and-processing-workbench-plan.md](upload-and-processing-workbench-plan.md) | 素材上传与四项处理工作台 |
| [ui-layout-normalization-plan.md](ui-layout-normalization-plan.md) | 全页面 UI 检查与统一修正 |
| [i18n-hot-switch-plan.md](i18n-hot-switch-plan.md) | 中英文多语言与热切换 |
| [disk-cleanup-plan.md](disk-cleanup-plan.md) | `mindex clean` 磁盘清理 |
| [backup-migration-plan.md](backup-migration-plan.md) | CLI 数据备份与迁移工具 |
| [third-party-harness-plan.md](third-party-harness-plan.md) | MCP 外部 harness 接入与 deepseek-harness 托管运行时 |
| [macos-native-plan.md](macos-native-plan.md) | macOS 桌面端（Electron 外壳 + Python sidecar）决策与实施计划 |
| [windows-linux-desktop-plan.md](windows-linux-desktop-plan.md) | 桌面版 Windows x64 / Linux x64 跨平台决策与实施计划 |
