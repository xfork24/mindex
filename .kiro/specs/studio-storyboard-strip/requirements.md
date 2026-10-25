# Requirements Document

## Introduction

创作页的分镜工作台目前是固定五列的表格（最小宽度 1300px，每列 220px），阶段详情只能放进一个跨列单元格，受表头限制难以布局。本特性把它改为「顶部横向分镜条 + 下方整块阶段详情」：分镜条上每张卡片按该分镜的有效阶段显示摘要与产物；鼠标悬停卡片时在卡片下方竖排展开全部阶段；点击卡片直接打开它所显示的阶段，点击展开列表中的阶段打开该阶段；阶段详情铺满分镜条下方的整块区域。

本轮范围：前端布局与交互、卡片产物嵌入、选片原片区间的只读播放列表接口、相关测试与文档。阶段详情内部（分镜编辑器、召回候选、选片窗口编辑器等）的内容与行为保持不变。

## Glossary

- **Board**：分镜工作台，`#storyboard-section` 中的分镜条、阶段列表与阶段详情区。
- **Shot_Strip**：顶部横向滚动的分镜条 `#shot-strip`。
- **Shot_Card**：分镜条中的一张分镜卡片 `.shot-card`。
- **Stage_Popover**：悬停或展开时出现在卡片下方的竖排阶段列表 `#stage-popover`。
- **Stage_Area**：分镜条下方的阶段详情区 `#shot-stage-area`。
- **Bar_Stages**：某分镜可打开的阶段序列：文案与查询（content）、画面阶段（该分镜实际的 recall/draw/generate/animate，没有画面阶段时为上传素材预览 source）、以及该分镜包含的配音（tts）、选片与对齐（select）、分镜渲染（render）。背景音乐（music）并入配音阶段显示。
- **Stage_State**：某分镜某阶段的状态：`none`（无版本）、`complete`（已选版本且未过期）、`stale`（已选版本但 `conflicts` 非空）、`failed`（最近一次执行失败）、`running`（运行中构建里待执行或执行中）。
- **Card_Stage**：卡片显示的阶段：Bar_Stages 中第一个 `stale` 阶段；没有时为最后一个已有版本的阶段；都没有时为 content。
- **Selection**：当前打开的 `{分镜, 阶段}`，按「任务/方案」分别记录。
- **Source_Playlist**：选片版本对应原片区间的播放列表接口 `GET /api/studio/jobs/{job_id}/versions/{version_id}/source-playlist`。

## Requirements

### Requirement 1: 分镜条布局

**User Story:** 作为创作者，我希望分镜横向排在顶部、一次看到约 5 个，以便快速浏览并在分镜间切换。

#### Acceptance Criteria

1. THE Board SHALL 用 Shot_Strip 替换原分镜表格，Shot_Strip 按方案分镜顺序横向排列 Shot_Card。
2. WHILE 工作区宽度足以容纳，THE Shot_Strip SHALL 一屏显示约 5 张 Shot_Card；卡片宽度下限 220px，窄屏时一屏显示的卡片数随之减少。
3. THE Shot_Strip SHALL 支持横向滚动：触控板横向滚动、鼠标滚轮纵向滚动转为横向（已滚到尽头时交还页面滚动），以及两端的上一组/下一组按钮（到达尽头时禁用）。
4. THE Board SHALL 在 1440、1000、800、540、390、320px 宽度、中英文两种语言下不产生页面横向滚动。
5. WHEN 方案没有分镜，THE Shot_Strip SHALL 显示无分镜说明与「新增分镜」按钮。

### Requirement 2: 阶段状态与卡片显示阶段

**User Story:** 作为创作者，我希望每张卡片显示该分镜最值得关注的阶段，以便一眼看出哪里过期、做到了哪一步。

#### Acceptance Criteria

1. THE Board SHALL 按 Bar_Stages 计算每个阶段的 Stage_State，优先级为 running > failed > stale > complete > none；stale 依据 `state.data.conflicts["{stage}:{sid}"]` 非空，failed/running 依据该阶段最近一次构建节点的状态。
2. THE Shot_Card SHALL 显示 Card_Stage：存在 stale 阶段时为第一个 stale 阶段并标「已过期」；否则为最后一个已有选定版本的阶段；都没有时为文案与查询。
3. THE Shot_Card SHALL 为 Bar_Stages 的每个阶段显示一个状态点，颜色区分五种 Stage_State，并提供可被读屏读取的阶段名与状态文字。
4. THE Shot_Card SHALL 显示序号、分镜短 ID、Card_Stage 名称，以及与原表格单元格一致的阶段摘要（版本徽标、摘要文字、冲突、执行状态与失败原因）。
5. WHILE 该分镜的文案与查询存在未保存修改，THE Shot_Card SHALL 显示「未保存」标记，无论 Card_Stage 是哪个阶段。

### Requirement 3: 卡片嵌入产物

**User Story:** 作为创作者，我希望在卡片上直接看到或听到该阶段的产物，以便不打开详情就能判断结果。

#### Acceptance Criteria

1. WHEN Card_Stage 有产物，THE Shot_Card SHALL 嵌入产物：配音为音频；分镜渲染为带配音与该镜字幕的视频（不单独显示音频，卡片与详情区一致；字幕底边对齐可视画面底边，不含 pad 黑边）；生成画面为视频；生成图片为图片；召回为最多 3 张候选缩略图；上传素材为素材预览；选片为原片区间静音播放（见 Requirement 4）。生成动画只显示摘要，不嵌入 iframe。
2. THE Shot_Card SHALL 让嵌入的播放器带控件、不自动播放、只预加载元数据，视频默认静音（分镜渲染视频带配音，不静音）；音频不静音，只在用户点击播放时发声。
3. THE Shot_Card SHALL 仅在卡片进入 Shot_Strip 可视区域后才创建媒体元素；WHEN 卡片离开可视区域，THE Shot_Card SHALL 暂停其中的播放。
4. WHEN 轮询刷新但 Card_Stage 与产物地址未变化，THE Shot_Card SHALL 保留现有媒体元素，不打断正在进行的播放。
5. WHEN 用户在媒体区域内点击或悬停，THE Board SHALL 不打开阶段详情，也不展开 Stage_Popover。

### Requirement 4: 选片原片区间播放

**User Story:** 作为创作者，我希望选片卡片能播放所选的原片区间，同时不触发耗时的预览生成。

#### Acceptance Criteria

1. THE Studio SHALL 提供只读接口 Source_Playlist：只接受视频来源为素材库（`event.video.kind` 缺省或为 `library`）的已完成选片版本，返回该区间的镜头播放列表（`duration` 等于区间长度）。
2. THE Source_Playlist SHALL 不调用 ffmpeg、不持有变更锁、不修改任务数据。
3. IF 版本不存在、不属于该任务、不是选片阶段、未完成、来源不是素材库或区间包含隐藏素材，THEN THE Source_Playlist SHALL 返回 4xx 与中文错误。
4. WHEN Card_Stage 为素材库选片，THE Shot_Card SHALL 先显示首个镜头缩略图与播放按钮，用户点击后才加载 Source_Playlist 并静音播放。
5. WHEN Card_Stage 为上传片段或生成画面的选片，THE Shot_Card SHALL 直接播放对应文件并以 `#t=start,end` 限定区间。

### Requirement 5: 阶段列表与打开详情

**User Story:** 作为创作者，我希望悬停卡片就能看到所有阶段并直接点选，点击卡片则直接打开它显示的阶段。

#### Acceptance Criteria

1. WHEN 鼠标在卡片（媒体区域之外）上停留 120ms，THE Board SHALL 在卡片正下方显示 Stage_Popover，竖排列出该分镜 Bar_Stages，每项带状态点与状态文字，当前打开的阶段高亮。
2. WHILE 鼠标位于卡片或 Stage_Popover 内，THE Stage_Popover SHALL 保持显示；WHEN 鼠标离开两者 200ms，THE Stage_Popover SHALL 收起。显示 Stage_Popover 不改变 Stage_Area。
3. WHEN 用户点击 Stage_Popover 中的阶段，THE Board SHALL 收起 Stage_Popover 并在 Stage_Area 打开该阶段详情。
4. WHEN 用户点击卡片空白处（非媒体、非按钮），THE Board SHALL 收起 Stage_Popover 并在 Stage_Area 打开该卡片的 Card_Stage。
5. THE Stage_Popover SHALL 渲染在 Shot_Strip 之外，不被分镜条的滚动区域裁剪；WHEN Shot_Strip 滚动或窗口尺寸变化，THE Stage_Popover SHALL 重新贴到卡片下方；WHEN 卡片滚出 Shot_Strip 可视区域，THE Stage_Popover SHALL 收起。
6. THE Shot_Card SHALL 提供「阶段 ▾」按钮：在不支持悬停的设备上或卡片获得键盘焦点时可见，点击后展开或收起 Stage_Popover。触屏的 pointer 事件不触发悬停展开。
7. THE Board SHALL 支持键盘操作：Tab 聚焦卡片时展开 Stage_Popover；Enter/空格打开 Card_Stage；下方向键进入 Stage_Popover；在列表中上下方向键切换、Enter 打开、Esc 收起并把焦点还给卡片；点击页面其他位置收起。
8. THE Shot_Card 与 Stage_Popover SHALL 使用正确的 ARIA 属性（卡片 `aria-haspopup="menu"`、`aria-expanded`、`aria-controls`；列表 `role="menu"`，阶段 `role="menuitem"`）。

### Requirement 6: 阶段详情区

**User Story:** 作为创作者，我希望阶段详情占满分镜条下方的整块区域，以便宽内容（选片窗口、候选网格）有足够空间。

#### Acceptance Criteria

1. WHILE 没有 Selection，THE Stage_Area SHALL 显示「请从上方选择分镜和阶段」提示。
2. WHEN 打开某分镜某阶段，THE Stage_Area SHALL 以全宽显示与原展开面板相同的详情内容（分镜编辑器、阶段详情或上传素材预览），标题为「分镜 N · 阶段 · 短 ID」，带关闭按钮；关闭后回到提示。
3. THE Shot_Card SHALL 对当前打开的分镜显示选中样式与 `aria-current`，并标出打开的阶段。
4. THE Board SHALL 按「任务/方案」分别记住 Selection；切换方案再切回后恢复。
5. IF Selection 的分镜已被删除或阶段已不属于该分镜，THEN THE Board SHALL 清除 Selection。
6. WHEN 轮询刷新但详情依赖的数据未变化，THE Stage_Area SHALL 不重建详情，保留未提交输入与媒体播放；版本历史、执行结果提示照常增量更新。
7. WHEN 新增分镜成功，THE Board SHALL 打开新分镜的文案与查询阶段并把它的卡片滚动到可视区域。

### Requirement 7: 批量操作

**User Story:** 作为创作者，我希望仍能对全部分镜批量执行或重新生成某个阶段。

#### Acceptance Criteria

1. THE Board SHALL 在 Shot_Strip 上方提供「批量操作」：阶段下拉（文案与查询/拆分镜、召回（画面）、配音、选片与对齐、分镜渲染）以及「全部执行/复用」「全部重新生成」两个按钮，行为与原各列表头按钮一致。
2. WHILE 有构建运行中或存在未保存修改，THE Board SHALL 禁用批量操作并给出与原表头按钮相同的说明；无分镜时除拆分镜外禁用。
3. THE Board SHALL 移除「展开所有行」按钮。

### Requirement 8: 智能助手定位与兼容

**User Story:** 作为使用智能助手的创作者，我希望助手定位分镜时页面仍能准确展示。

#### Acceptance Criteria

1. WHEN 智能助手定位到分镜（非被动）且给出阶段，THE Board SHALL 打开第一个目标分镜的对应阶段（画面阶段映射为该分镜实际的画面阶段，music 映射为配音）。
2. WHEN 智能助手定位到分镜，THE Board SHALL 高亮全部目标分镜卡片，并把第一个滚动到可视区域。
3. WHEN 删除任务或切换任务，THE Board SHALL 暂停并清空卡片与详情中的全部媒体，收起 Stage_Popover。

### Requirement 9: 测试与文档

#### Acceptance Criteria

1. THE 测试 SHALL 用纯脚本单元测试覆盖 Stage_State、Card_Stage、Bar_Stages 的各种组合（无版本、只有文案、多阶段完成、中间阶段过期、失败、执行中、图片/上传素材/AIGC/动画分镜）。
2. THE 测试 SHALL 用 pytest 覆盖 Source_Playlist 的成功与各类拒绝情形。
3. THE 现有引用分镜表格的前端测试 SHALL 迁移到新结构并通过。
4. THE 文档 `docs/studio.md` SHALL 更新分镜工作台的说明。
5. THE 静态检查（`node --check`、`build-messages.py --check`、`build-assets.py --check`、`ruff check`）SHALL 通过，新增界面文字中英双语。
