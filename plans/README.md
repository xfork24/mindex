# 设计与实施计划索引

`plans/` 保存各功能的设计资料和实施计划，是历史设计依据，不代表当前代码的行为。以源码、配置模型和 CLI 帮助为准；机制说明见 [文档中心](../docs/README.md)。

名称相近的几份计划主题不同，不是重复文件，请按下表区分。

## 总体与测试

| 文件 | 主题 |
|---|---|
| [PLAN.md](PLAN.md) | 视频片段检索的实施计划与设计决策 |
| [ROADMAP.md](ROADMAP.md) | 功能路线图（M1–M4）；对应审查报告见 [docs/reviews/2026-10/](../docs/reviews/2026-10/features-roadmap.md) |
| [E2E_TEST_PLAN.md](E2E_TEST_PLAN.md) | 端到端测试计划 |

## 检索与索引

| 文件 | 主题 |
|---|---|
| [RECALL_ENHANCEMENT_PLAN.md](RECALL_ENHANCEMENT_PLAN.md) | 画面、对白、分析三渠道独立处理与混合召回 |
| [REHASH_REFINEMENT_PLAN.md](REHASH_REFINEMENT_PLAN.md) | macOS 模型身份与索引兼容性重构 |
| [CLIP_REFACTOR_PLAN.md](CLIP_REFACTOR_PLAN.md) | 以 shot 为唯一业务媒体来源的改造与数据迁移 |

## 人物库

| 文件 | 主题 |
|---|---|
| [PERSON_RECO_PLAN.md](PERSON_RECO_PLAN.md) | 人物库、演员表导入与人物出镜召回（最初设计） |
| [PERSON_LIB_UI_PLAN.md](PERSON_LIB_UI_PLAN.md) | 演员表导入：多图全流程改造 |
| [PERSON_LIB_UI_PLAN_v2.md](PERSON_LIB_UI_PLAN_v2.md) | 人物与参考图交互改造、全局可搜索多选框（与上一份是不同需求，`v2` 指第二轮改造） |
| [PERSON_REFACTOR_PLAN.md](PERSON_REFACTOR_PLAN.md) | `persons/frames` 展示帧存储改造 |
| [PERSON_PROCESS_REFACTOR_PLAN.md](PERSON_PROCESS_REFACTOR_PLAN.md) | 人物处理改为仅处理角色出镜 |

## 创作（Studio）

| 文件 | 主题 |
|---|---|
| [STUDIO_PLAN.md](STUDIO_PLAN.md) | 文本生成视频工作流的最初实施计划 |
| [STUDIO_REFINEMENT_PLAN.md](STUDIO_REFINEMENT_PLAN.md) | Studio 静态修复与集成验收 |
| [STUDIO_UI_PLAN.md](STUDIO_UI_PLAN.md) | 分镜与版本构建工作台改造 |
| [STUDIO_REFINEMENT_UI_PLAN.md](STUDIO_REFINEMENT_UI_PLAN.md) | 召回候选选择与定长窗口选片 |
| [RECALL_MIGRATION_PLAN.md](RECALL_MIGRATION_PLAN.md) | 创作页分镜四类召回与镜头详情人物信息（已实施） |
| [CLIP_WINDOW_ENHANCEMENT_PLAN.md](CLIP_WINDOW_ENHANCEMENT_PLAN.md) | 修复"保存窗口"耗时问题 |

## 素材管理、界面与运维

| 文件 | 主题 |
|---|---|
| [UPLOAD_VIDEO_PLAN.md](UPLOAD_VIDEO_PLAN.md) | 素材上传与四项处理工作台 |
| [UI_ELEM_LAYOUT_PLAN.md](UI_ELEM_LAYOUT_PLAN.md) | 全页面 UI 检查与统一修正 |
| [I18N_PLAN.md](I18N_PLAN.md) | 中英文多语言与热切换 |
| [MINDEX_CLEAN_PLAN.md](MINDEX_CLEAN_PLAN.md) | `mindex clean` 磁盘清理 |
| [DATA_MIGRATION_PLAN.md](DATA_MIGRATION_PLAN.md) | CLI 数据备份与迁移工具 |
| [MACOS_NATIVE_PLAN.md](MACOS_NATIVE_PLAN.md) | macOS 桌面端（Electron 外壳 + Python sidecar）决策与实施计划；任务进度见 `.kiro/specs/macos-desktop/` |
| [WIN_LINUX_NATIVE_PLAN.md](WIN_LINUX_NATIVE_PLAN.md) | 桌面版 Windows x64 / Linux x64 跨平台决策与实施计划；任务进度见 `.kiro/specs/desktop-cross-platform/` |
