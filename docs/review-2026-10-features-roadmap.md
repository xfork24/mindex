# 功能集审查与路线图（2026-10）

范围：2026-10-25 的工作区（含进行中的 agent-capability-expansion，按已完成计）。长期路线图见 [plans/roadmap.md](../plans/roadmap.md)。本文是带日期的审查记录。

## 结论

Mindex 已经把"素材管理 → 多模态检索 → 文案驱动的混剪成片"打通，并通过智能助手和 MCP 把这些能力交给外部 harness。要成为 AI 时代的视频创作 agent，还缺三块：

1. **工作流两端不完整**。上游没有选题，文案只能从一段大纲出发，没有领域模板，也不能利用谈话文稿等文本素材。下游只有"拼接 + 配音 + 字幕"，没有 BGM、转场、文字层、封面和多画幅。
2. **画面只能来自素材召回**。分镜、选片、渲染的数据模型都假设画面是素材库里某段原片区间，AIGC、动画、临时素材都接不进来。
3. **只能以源码方式运行**。没有桌面分发形态，私有部署要求用户自己装 Python、uv、FFmpeg 和模型服务。

版本、签名与复用这套机制是通用的，扩展的主要工作是把"按阶段名写死的分支"改成注册表，并让时间线支持文件型画面。

## 现状盘点

| 环节 | 已有 | 证据 |
|---|---|---|
| 素材 | 导入去重、分镜、切片、对白（字幕优先/ASR）、视觉分析、人物识别与审核、整理（显示名/备注/收藏/隐藏）、清理与备份 | `processing/`、`admin/`、`persons/`、`maintenance/` |
| 检索 | 三渠道 RRF、人物与台词复合检索、图/视频找相似、按片段找相似、索引版本与回退 | `search/search.py`、`admin/indexes.py` |
| 文案 | 按大纲生成（单一通用提示词，可参考素材摘要），手动编辑保存为版本 | `models/writer.py:24-41`、`studio_workspace.py` `add_script` |
| 分镜 | 按"具体意象"拆分，逐字覆盖原文，画面意象 visual，召回词规则 | `models/storyboard.py:249-310` |
| 画面 | 逐镜召回（文字/人物/图片/视频）、定长窗口选片、同源延长、配音加速、冻结帧兜底 | `studio_workspace.py` `execute` recall/select 分支 |
| 渲染 | 统一画布 letterbox、配音响度归一、可选原声 ducking、硬切拼接、软/烧录字幕（仅字体与字号） | `studio/studio_render.py` |
| 版本 | 多方案、逐节点重做、版本依赖树、按签名复用、取消构建 | `studio_workspace.py` `preflight`/`match` |
| 助手 | 8 个领域约 57 个工具、动态加载、附件、视觉输入、MCP 导出 | `agent/` |

## 功能缺口

### 一、选题（缺失）

没有任何选题相关的数据或工具。对垂直领域频道来说，选题决定了播放量的上限，也决定了素材库该往哪个方向积累。缺少的环节：

- 领域方向配置：领域、子方向、受众、账号定位；
- 热点与时效数据源：平台热榜、RSS、行业新闻、财报或发布会日历，以及历史上的今天；
- 选题与素材库覆盖度的匹配：素材是否撑得起这个选题，缺什么画面；
- 选题打分与去重：时效、受众、素材覆盖、和已做视频是否重复；
- 从选题生成大纲，交给文案阶段。

### 二、文案（薄弱）

| 缺口 | 影响 |
|---|---|
| 只有一个通用提示词 | 历史解说、热点评论、影视解说、知识科普的结构和语气差别很大，现在只能靠在大纲里描述 |
| 不能输入参考文稿 | 访谈、播客、讲座文稿无法改写为视频文案；拆分镜要求逐字覆盖原文，也不能直接拿文稿去拆 |
| 协作粒度只有整段 | 没有选段改写、扩写、缩写、换语气，也没有"大纲 → 段落结构 → 正文"的分步生成 |
| 没有时长目标 | 只有字数上限，没有"目标 3 分钟"这类约束 |

### 三、画面来源（单一）

只支持素材召回。下面几处把"画面 = 素材区间"写死了：

- `studio.py` 的 `TimelineVideo` 必须有 `source_id` 和 `segment_ids`；
- `studio_workspace.py` 的 `STAGES`、`node_dependencies`，以及 `specification`、`execute`、`preflight` 里按阶段名写的 if/elif；
- `render_node` 经 `ShotMedia.materialize` 取原片区间；
- 拆分镜提示词明确写着"每个分镜会各自用 query 去素材库检索一段画面"。

缺少的来源：临时导入片段、静态图片（推拉）、AIGC 视频（扩散模型）、AI 动画（LLM 生成 HTML/JS 后录制）、第三方工具产物。

### 四、后期与成片（基础）

- 画幅：只有全局 `studio.render` 一个分辨率，用 letterbox 补边，没有 9:16 和 1:1，也不能按方案选择；
- 音乐：没有 BGM、音乐库和节拍对齐，也没有淡入淡出；
- 转场：`concat_media` 用 `-c copy` 硬切；
- 文字层：没有标题卡、字幕样式（位置、描边、高亮）、花字、下三分之一信息条；
- 封面、片头片尾、水印、品牌模板：都没有；
- 多语种：配音与字幕只有单一语言。

### 五、分发与运维

- 没有桌面应用。私有部署要求懂 Python 与 FFmpeg，而且源码明文分发；
- 模型服务要手工启停，没有托管和健康面板；
- 没有发布到平台的能力（导出规格、标题、简介、标签）。

### 六、Agent

助手能驱动现有流水线，但没有跨"选题 → 文案 → 成片"的长程计划和项目记忆，也没有选题类工具。这是 M2 要补的。

## 当前可做（本期 M1，对应 spec `mindex-review-2026-10`）

| 任务 | 内容 | 状态 |
|---|---|---|
| 17 | 文案模板：6 个内置模板（通用解说为基础模板，承接现有生成）+ 用户自定义模板 | 已完成。模块名为 `studio/script_templates.py`（设计稿写作 `studio/templates.py`，因与 `studio/templates/` 目录同名而改名），路由在 `studio/studio_templates_api.py`；用户模板表 `script_templates` 为 metadata 迁移 v2；`WRITER_PROMPT_VERSION=3` |
| 18 | 参考文稿：访谈稿等文本改写为视频文案 | 已完成（`studio/workspace/references.py`，`studio.max_reference_chars`）。智能助手的 `remove_reference_text` 为 danger（删除登记），`add_reference_text` 为 write |
| 19 | 方案级画幅：16:9 / 9:16 / 1:1，pad / crop | 已完成（`studio.render.canvases`；只进 render/compose 签名，16:9 + pad 签名与旧版逐字节一致） |
| 20 | 分镜来源抽象 + 临时导入片段 | 已完成（`Shot.source`、`TimelineVideo` 判别联合、`SHOT_SOURCES`；`studio.max_clip_seconds`/`max_clip_mb`） |
| 21 | 静态图片来源（Ken Burns 推拉） | 已完成（zoompan，`motion`/`strength`）；21.5 补齐了 17–21 的智能助手工具与场景 |
| 22–23 | "视频生成"模型角色 + AIGC 视频来源（MiniMax、通义万相、ComfyUI） | 已完成（`models/video_generators.py`、`shared/comfyui.py`、`generate` 阶段）；测试连接不提交生成 |
| 24–25 | "动画生成"模型角色 + AI 动画来源（沙箱逐帧录制） | 已完成（`models/animator.py`、`studio/animation_capture.py`、`animate` 阶段，可选 extra `animation`）；真实浏览器录制需安装 Playwright Chromium，自动化测试中有未安装时跳过的用例 |

状态于 2026-10 任务 26 回填，依据 spec `mindex-review-2026-10` 的 tasks.md。

M2–M4 见 [路线图](../plans/roadmap.md)。
