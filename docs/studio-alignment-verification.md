# 连续 UI 调整最终验收

日期：2026-10-19。已完成 [计划](../plans/UI_ELEM_LAYOUT_PLAN.md) 及后续补充要求；“删除刷新素材”已由后续“恢复”要求取代。

## 要求与实现对应

| 要求 | 最终实现 | 本次验证 |
|---|---|---|
| 多行输入能调整宽高 | 共享 textarea 双向 resize，默认宽度按所在表单和编辑器排列，最大宽度受容器限制 | 全页面逐控件计算样式；真实拖动、输入保留 |
| 人物过滤框与列表统一 | 同一边框容器保留原输入和 select；列表右下角可拉高 | 人物筛选、搜索、多选、清除和拖拽；正常及长内容布局 |
| 默认控件宽度与布局 | 普通控件 32px，主操作 40px，紧凑操作 28px；人物多选默认 260px，素材范围 320px／260px，窄屏单列 | 五页及弹窗、动态编辑器、两种导出页面；完整覆盖见下方记录 |
| 文 / En 图标 | 两个 SVG 图标按钮，保留语言状态与可访问名称 | 五页切换、节点／输入／文件／焦点／播放保留、语言持久化与跨标签页同步 |
| 刷新靠左及恢复素材刷新 | 五页保留刷新；创作与设置刷新左边距为 0；素材刷新重新绑定原逻辑 | 60 个页头布局；素材列表刷新、选中素材详情刷新、加载禁用、503 提示、恢复并重试 |
| 召回增加其他筛选 | 场景、人物情绪、景别及最短／最长时长 | 保存／取消、模式切换、真实召回请求与结果约束 |
| 两处素材范围多选 | 检索页和分镜文本编辑器共用可过滤多选列表，兼容原单素材值 | 多素材查询、过滤不丢选择、全选／清除、刷新保留、拖动列表、保存及取消 |
| 图片／视频控件对齐 | 检索上传面板采用一致尺寸；分镜文件框和上传按钮同排 40px | 两语言六屏宽下对应面板几何尺寸一致，同行偏差不超过 1px；真实文件上传 |
| 召回演员—角色去重与参考图 | 每个候选同一组合一次，显示实际匹配的参考图，缺失参考时保留文字 | 每候选四个不同组合及重复证据，URL 与匹配参考一致；后端匹配分数来源测试 |
| 排名与电影／时间排版 | 排名在候选画面左上角 6px；电影名和秒数分两行，不加分隔点 | 12 个召回布局，排名、图像位置和分行实际坐标；预览及设为锚点 |
| 选片阶段去除重复锚点 | 不再插入参考锚点标题、自动选择按钮和召回列表；召回阶段仍保留 | 未生成选片及完成选片两种状态；窗口键盘移动与保存返回 200，召回预览正常 |
| 单元格底部与历史操作对齐 | 摘要单元格采用等高纵向布局；历史下拉及按钮居中并移除额外 margin | 12 个摘要布局与四阶段共 48 个历史布局；同行中心偏差不超过 1px，历史查看／选用正常 |
| 顶部 tab 始终保留 | 固定顺序为视频检索、创作、素材管理、人物库、设置；当前页绿色选中并标记 aria-current；移除左侧重复标题 | 五页 × 两语言 × 六屏宽共 60 案例，实际点击全循环，顺序、选中、可见、无重叠／溢出 |

顶部导航由五页静态 HTML 和共享样式实现；链接路径及刷新 DOM 标识保留。更新了共享 CSS 缓存版本。召回多选的可选 `source_ids` 扩展已在 [召回筛选报告](recall-filters-verification.md) 说明，兼容原字段，无数据库迁移。

## 最终验证结果

本次重新运行，全部通过：

- 全页面浏览器检查：**1,458** 个视口／状态／语言组合，**87,579** 个实际控件，0 布局违规、0 页面脚本错误。包含五页、动态编辑器、弹窗、长内容、空数据、受控加载／错误分支及两种生成复核页。[逐项覆盖清单](ui-layout-final-coverage.md) / [完整记录](../artifacts/ui-layout/final/coverage.json) / [日志](../artifacts/ui-layout/final/browser.log)。
- 顶部导航与刷新：**60** 个布局及成功、失败、重试操作。[记录](../artifacts/header-navigation/coverage.json) / [日志](../artifacts/header-navigation/browser.log)。
- 素材多选、召回筛选与媒体控件：**72** 个布局及实际保存／取消／上传。[记录](../artifacts/recall-filters/coverage.json) / [本次日志](../artifacts/recall-filters/final-browser.log)。
- 召回标记、排名及两行元数据：**12** 个布局及锚点选择／播放。[记录](../artifacts/candidate-identities/coverage.json) / [本次日志](../artifacts/candidate-identities/final-browser.log)。
- 分镜底部、历史控件及选片精简：**60** 个布局和真实窗口保存／历史选择／召回预览。[记录](../artifacts/studio-alignment/final-coverage.json) / [日志](../artifacts/studio-alignment/browser.log)。
- Python：**147 项通过**，覆盖国际化、分镜列、加载、窗口、召回、人物、检索管线及导出布局，仅现有 Starlette 弃用提示。[日志](../artifacts/header-navigation/pytest.log)。
- 现有 `i18n_ui.cjs`、`studio_window_ui.cjs` 和 `studio_loading_ui.cjs` 通过；包括草稿、过期响应、重试、受限播放及直接进入任务。[国际化日志](../artifacts/header-navigation/i18n.log)。
- 受影响 JavaScript 语法与 diff 空白检查通过。

测试由浏览器真实点击触发 API，验证响应状态及页面更新。模型服务使用确定性替身；没有操作工作区真实素材、任务或外部付费模型。测试脚本中提示框 ID、历史按钮名称及等待异步媒体重建的问题已修正，最后完整运行全部通过。

## 截图及范围限制

人工检查了桌面导航、素材与设置窄屏、候选参考图／排名、选片历史行和导出复核页。代表性对照：[素材页头前后](../artifacts/header-navigation/admin-comparison.png)、[创作页头前后](../artifacts/header-navigation/studio-comparison.png)。对照基线来自早先 UI 验收，没有为每个新增动态分支制作修改前截图。

最终截图：[素材 320px](../artifacts/header-navigation/admin-en-320.png)、[召回候选 320px](../artifacts/candidate-identities/recall-zh-320.png)、[选片桌面](../artifacts/studio-alignment/select-1440.png)、[选片 320px](../artifacts/studio-alignment/select-320.png)。其余截图在对应 artifacts 目录。

以上是 Chromium 响应式视口验收，屏宽为 1440、1000、800、540、390、320px；应用中英文，导出页按现有中文。没有验证 Safari、Firefox 或手机触屏原生 resize。并非每个错误／加载／空数据状态都交叉六屏宽和两语言；实际覆盖以 JSON 为准。外部模型真实生成、厂商连接和真实音色克隆没有发起调用。人物审核保存／发布由既有测试覆盖，本轮全页面浏览器主要验证打开、选择和取消；历史成片恢复验证弹窗打开和取消。保留表格／时间轴内部滚动与原有摘要截断，可展开完整内容。

## 复现

在 `/tmp` 隔离库启动 `tests/serve_fixture.py`；设置 `MINDEX_PLAYWRIGHT_MODULE` 后运行 `tests/header_navigation_ui.cjs` 和 `tests/studio_alignment_ui.cjs`，默认使用 18341。多素材及参考图测试需分别使用已准备的隔离库，参见 [召回筛选](recall-filters-verification.md) 和 [参考图报告](candidate-identities-verification.md)。全页面脚本的初始化和导出页面服务见 [原 UI 报告](ui-layout-verification.md)；完整运行不设置 `MINDEX_LAYOUT_RESUME`。
