# 前端结构

前端是**无框架的原生 HTML/JS/CSS**：没有打包器、没有构建步骤，页面由后端直接服务。约定集中在 `frontend/src/shared/`，页面按功能目录划分。改前端不需要编译，但文案与资源版本要跑两个生成脚本（见下文）。

## 页面结构

| 目录 | 页面 |
|---|---|
| `search/` | `/` 视频检索 |
| `admin/` | `/admin` 素材处理工作台（子页面 `/admin/music` 曲库） |
| `studio/` | `/studio` 创作（子页面 `/studio/topics` 选题挖掘、`/studio/templates` 文案模板、`/studio/brands` 风格模板、`/studio/delivery-templates` 交付模板） |
| `persons/` | `/persons` 人物库 |
| `settings/` | `/settings` 设置 |
| `shared/` | 公共资源：api.js、i18n.js、messages.json/js、agent-panel.js、multiselect.js、样式等 |

顶部导航固定顺序：**视频检索、创作、素材管理、人物库、设置**；当前页绿色选中并标记 `aria-current`。创作页内的标签顺序为：创作任务、选题挖掘、文案模板、风格模板、交付模板。

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

## UI 约定（控件尺寸与布局）

全站控件尺寸与布局统一（`shared/control-layout.js` + 共享样式）：

- 控件高度三档：**普通控件 32px，主操作 40px，紧凑操作 28px**；人物多选默认宽 260px，素材范围 320px/260px。
- 多行输入（textarea）双向可调整宽高，最大宽度受容器限制。
- 页头刷新按钮靠左（创作与设置页左边距为 0）；素材页刷新保持原有刷新逻辑。
- 响应式支持 1440→320px：窄屏单列，横向内容（表格、时间轴、分镜条）内部滚动。
- 布局正确性由全页面布局回归保障（复现方法见[测试约定](testing.md#全页面布局回归复现方法)）。
