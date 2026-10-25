# Implementation Plan: studio-storyboard-strip

## Overview

先实现阶段状态纯函数并单测；再一次性替换表格为分镜条 + 详情区（含批量操作、助手定位、编辑器刷新迁移）；然后补齐悬停浮层与键盘交互、卡片媒体、选片播放列表接口；最后迁移测试、更新文档并做全量验证。

全程约束：
- 界面文字以 `frontend/src/shared/messages.json` 中文为唯一源，改完运行 `python scripts/build-messages.py`。
- 改动的静态资源运行 `python scripts/build-assets.py` 刷新 `?v=`。
- 阶段详情内部（编辑器、召回、选片窗口）的行为不变。
- 不创建 git commit。

## Tasks

- [x] 1. 阶段状态模型
  - [x] 1.1 在 `studio-state.js` 实现 `barStages`、`stageState`、`cardStage`，`state` 新增 `selection`/`shotCards`/`boardScope`/`stagePanel`，移除 `expanded`/`matrixRows`/`matrixScope`
    - _Requirements: 2.1, 2.2_
  - [x] 1.2 编写 `tests/studio_card_stage_unit.cjs` 与 `tests/test_studio_card_stage_ui.py`
    - 覆盖：无版本、只有文案、多阶段完成、中间阶段过期、失败、执行中、图片/上传素材/AIGC/动画分镜
    - _Requirements: 9.1_

- [x] 2. 分镜条与详情区替换表格
  - [x] 2.1 `studio.html`：删除表格与「展开所有行」，加入批量操作栏、`#shot-strip`、`#shot-stage-area`、`#stage-popover`
    - _Requirements: 1.1, 7.1, 7.3_
  - [x] 2.2 `studio-board.js`：`stageSummaryNodes`/`cellSummary`、`renderBoard`（卡片增量重建、详情面板、Selection 校正）、`openStage`/`closeStage`/`toggleCell`、`refreshShotCard`、`updateLocks` 新作用域、空方案提示
    - _Requirements: 1.5, 2.3, 2.4, 2.5, 6.1–6.6, 7.2_
  - [x] 2.3 迁移调用方：`studio-editor.js`（4 处卡片刷新、新增分镜后选中并滚动）、`studio-stages.js`（素材预览「编辑」按钮）、`studio-jobs.js`、`studio-main.js`（批量按钮、删除任务重置、移除 ResizeObserver、`MindexAgentFocus`）
    - _Requirements: 6.7, 7.1, 8.1, 8.2, 8.3_
  - [x] 2.4 `studio.css`：分镜条、卡片、状态点、详情区、浮层样式；`.shot-matrix` 规则迁移到新容器；窄屏适配
    - _Requirements: 1.2, 1.4_
  - [x] 2.5 `messages.json` 新增文案并生成 `messages.js`
    - _Requirements: 9.5_

- [x] 3. 阶段浮层与键盘交互
  - [x] 3.1 悬停 120ms 展开、移入浮层保持、离开 200ms 收起；点击阶段打开；点击卡片打开 Card_Stage 并收起浮层
    - _Requirements: 5.1–5.4_
  - [x] 3.2 浮层 fixed 定位、分镜条滚动/窗口缩放重定位、卡片滚出可视区收起、点击外部收起
    - _Requirements: 5.5_
  - [x] 3.3 「阶段 ▾」按钮（hover:none / focus-within 可见）、触屏忽略悬停、键盘（Tab 展开、Enter 打开、方向键、Esc）、ARIA
    - _Requirements: 5.6, 5.7, 5.8_
  - [x] 3.4 分镜条两端按钮与滚轮转横向
    - _Requirements: 1.3_

- [x] 4. 卡片嵌入产物
  - [x] 4.1 `cardMedia` 按 Card_Stage 生成媒体；IntersectionObserver 懒加载与离开暂停；媒体区事件隔离；`mediaKey` 不变时保留
    - _Requirements: 3.1–3.5_

- [x] 5. 选片原片区间播放
  - [x] 5.1 后端 `GET /jobs/{job_id}/versions/{version_id}/source-playlist`
    - _Requirements: 4.1, 4.2, 4.3_
  - [x] 5.2 编写 `tests/test_studio_source_playlist.py`
    - _Requirements: 9.2_
  - [x] 5.3 前端选片卡片：缩略图 + 播放按钮 → ShotPlayer（静音）；文件来源 `#t=`
    - _Requirements: 4.4, 4.5_

- [x] 6. 测试迁移、文档与验证
  - [x] 6.1 迁移 `studio_visual_ui.cjs`、`studio_catalogue_loading_unit.cjs` 及浏览器脚本（`ui_layout_browser.cjs`、`studio_alignment_ui.cjs`、`recall_filters_ui.cjs`、`i18n_ui.cjs`、`studio_candidate_identities_ui.cjs`）的选择器
    - _Requirements: 9.3_
  - [x] 6.2 新增浏览器脚本 `tests/studio_board_ui.cjs` 并在隔离 fixture 上运行
    - _Requirements: 1.3, 1.4, 3.2, 3.5, 5.1–5.7, 6.1, 6.2_
  - [x] 6.3 更新 `docs/studio.md`
    - _Requirements: 9.4_
  - [x] 6.4 `build-assets.py`、`node --check`、`build-messages.py --check`、`build-assets.py --check`、`ruff check`、`pytest -m "not slow"` 与相关 slow 用例
    - _Requirements: 9.5_
