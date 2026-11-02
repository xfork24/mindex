# 前端结构

前端是**无框架的原生 HTML/JS/CSS**：没有打包器、没有构建步骤，页面由后端直接服务。约定集中在 `frontend/src/shared/`，页面按功能目录划分。改前端不需要编译，但文案与资源版本要跑两个生成脚本（见下文）。

## 页面结构

| 目录 | 页面 |
|---|---|
| `search/` | `/search` 视频检索（旧的 `/index.html` 308 重定向到 `/search`） |
| `admin/` | `/admin` 素材处理工作台（子页面 `/admin/music` 曲库） |
| `studio/` | `/`（主页）创作（同时挂在 `/studio`；子页面 `/studio/topics` 选题挖掘、`/studio/templates` 文案模板、`/studio/brands` 风格模板、`/studio/delivery-templates` 交付模板） |
| `persons/` | `/persons` 人物库 |
| `settings/` | `/settings` 设置 |
| `shared/` | 公共资源：api.js、i18n.js、messages.json/js、agent-panel.js、multiselect.js、样式等 |

主页（`/`）是创作页：顶栏 logo 与「创作」标签都指向 `/`，桌面版启动即落在创作页。

顶部导航固定顺序：**视频检索、创作、素材管理、人物库、设置**；当前页绿色选中并标记 `aria-current`。创作页内的标签顺序为：创作任务、选题挖掘、文案模板、风格模板、交付模板。顶栏右上角的刷新、主题、语言三枚按钮收在 `.header-actions` 组里：整体靠右，组内间距与左侧导航标签一致（8px）。

## 请求封装（shared/api.js）

所有请求统一经 `window.MindexApi`：各页面的 `api()` 都是它的薄封装。

- `request(url, options)` 原样交给 fetch；`json(url, method, body, options)` 普通对象按 JSON、FormData 原样发送。
- 失败抛 Error：message 为 `MindexI18n.system(响应体)`（随语言渲染的错误文案），并附带 `status`、`detail`、`data`；网络错误与中止原样抛出。

## 创作页的按序经典脚本

`studio/studio.html` 用一串带 `defer` 的经典脚本按依赖顺序加载：共享层（messages/i18n/api/control-layout/group-controls/marked/purify/agent-markdown/agent-panel/feedback/multiselect）→ `studio-state.js` → `studio-jobs.js` → `studio-workspace.js` → `studio-board.js` → `studio-editor.js` → `studio-stages.js` → `studio-post.js` → `delivery-editor.js` → `studio-delivery.js` → `studio-plan.js` → `studio-recall.js` → `studio-build.js` → `studio-template-text.js` → `studio-templates.js`，**启动代码在最后的 `studio-main.js`**。新增脚本必须插在 `studio-main.js` 之前并保持依赖顺序；不要用 ES 模块。

## 资源版本

页面里 `/assets/…` 的地址带 `?v=<内容哈希>`（SHA-256 前 10 位），由 `scripts/build-assets.py` 改写；**文案改动后先 `build-messages.py` 再 `build-assets.py`**（详见 [界面文案与多语言](i18n.md#生成链路勿手编镜像)）。CI 用 `--check` 校验一致性。

## 智能助手面板

`shared/agent-panel.js` 被九个页面（五个主页面与选题/模板/风格/曲库子页面）引入：Markdown 经 `agent-markdown.js` + 仓库内固定版本的 `marked.umd.js`/`purify.min.js` 渲染（版本与许可证见[第三方组件声明](third-party-notices.md)），支持附件、媒体网格与页面定位（`window.MindexAgentFocus`），机制见[智能助手](agent.md)。

## 主题系统（浅色/深色）

颜色全部收敛为 `shared/style.css` `:root` 里的主题 token（表面/线条/文字/品牌/状态/遮罩），浅色是默认值，`[data-theme=dark]` 覆盖为深色值；改 token 须同步两处。`shared/theme.js` 在 head 内同步加载（防闪烁）：三态 **跟随系统 ◐ / 浅色 ☀ / 深色 ☾** 循环切换，偏好持久化在 `localStorage['mindex.theme']`（缺省 auto，由 `prefers-color-scheme` 解析并监听系统变化）；aria-label 双语随 `mindex:languagechange` 联动。切换按钮位于顶栏右侧、语言切换左侧（login 页只加载脚本不加按钮）。媒体叠层色（视频底、缩略图角标、选中工具的青色系）与 logo 点、阴影保留字面量，不参与主题。回归：`tests/test_theme_ui.py`；`ui_layout_browser.cjs` 末尾有 dark-* 审计组。

## UI 约定（控件尺寸与布局）

全站控件尺寸与布局统一（`shared/control-layout.js` + 共享样式）：

- 控件高度：**普通控件 28px（`--control-size`），主操作 40px（`.submit` 等）**；面板/卡片圆角两级 `--panel-radius:10px`/`--card-radius:8px`，控件圆角 6px；dialog 宽度两级 `--dialog-width-m:720px`/`--dialog-width-l:900px`。
- 页面头为紧凑单行（`.intro/.studio-intro/.admin-intro`：18px 标题 + 单行简介，基线对齐；类名是 `feedback.js` 与回归测试的锚点，勿改名）。素材管理页头右端为主操作按钮。
- 顶栏右侧动作组统一为 **刷新 + 主题切换 + 语言切换**；二级标签（`.studio-tabs`/`.tab`/`.kind-tabs`/`.clone-mode`）统一 34px/13px 规格，规范定义在 style.css。
- 内容区无最大宽度上限：窗口拉大时网格（`auto-fit/minmax`）自动增列，表单控件按内容宽度经 `control-layout.js` 横排铺开；布局审计覆盖 320→2560px。
- 多行输入（textarea）双向可调整宽高，最大宽度受容器限制。
- 布局正确性由全页面布局回归保障（复现方法见[测试约定](testing.md#全页面布局回归复现方法)）。
