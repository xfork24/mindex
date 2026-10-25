# Requirements Document

Mindex 全面审查（2026-10）、架构改造与创作能力扩展

## Introduction

近期前端页面、后端 API、CLI 与智能助手新增了大量功能。本功能做三件事：

1. 从功能集与路线图、仓库结构与架构、实现质量、文档一致性四个方面审查项目，每个方面形成一份报告，附"当前可做"任务清单；
2. 落实这些任务：安全与质量修复、架构重构（激进方案）；
3. 扩展创作能力：文案模板（现有文案生成迁移为基础模板）、参考文稿、方案级画幅、五种分镜来源（素材召回、临时片段、静态图片、AIGC 视频、AI 动画），以及"视频生成""动画生成"两个模型角色。

协作约束：spec `agent-capability-expansion` 正在执行。它完成之前，本功能只新增文件（报告、路线图、本 spec），不修改任何已跟踪文件；它完成后先单独提交它的改动，再开始本功能的代码改动，本功能的改动保持未提交。

## Glossary

- **Shot_Source**：分镜画面来源，取值 `recall`、`clip`、`image`、`aigc`、`animation`。
- **Script_Template**：文案模板。内置模板随代码发布（只读）；用户模板由内置模板复制修改，存 SQLite。
- **Base_Template**：基础模板"通用解说"，承接现有文案生成。
- **Reference_Text**：参考文稿（访谈稿、谈话文稿等），存任务 `inputs/<sha256>.txt`。
- **Canvas**：方案级画幅，`16:9`、`9:16`、`1:1`，适配方式 `pad` 或 `crop`。
- **Video_Generator**：视频生成器协议，后端 `minimax`、`dashscope`、`comfyui`。
- **Animator**：动画生成模型，输出自包含单文件 HTML/JS 动画。
- **Capture_Sandbox**：动画录制沙箱（Playwright 无头 Chromium + 虚拟时钟）。
- **Stage_Registry**：创作阶段注册表，替代按阶段名的 if/elif 分支。
- **Gate**：第二阶段开始条件，见需求 2。

## Requirements

### Requirement 1: 审查报告与路线图

**User Story:** 作为产品负责人，我希望得到四份审查报告和一份长期维护的路线图，以便决定后续投入。

#### Acceptance Criteria

1. THE 系统 SHALL 在 `docs/reviews/2026-10/` 下提供四份报告：`features-roadmap.md`、`architecture.md`、`code-quality.md`、`docs-consistency.md`。
2. 每份报告 SHALL 先给结论，再按严重度或优先级列发现（带 `文件:行` 证据），末尾附"当前可做"任务清单，并对应本 spec 的任务编号。
3. THE `plans/ROADMAP.md` SHALL 给出里程碑 M1–M4：M1 本期创作地基；M2 垂直领域选题挖掘；M3 后期与第三方工具；M4 桌面端（macOS、Windows）。
4. 路线图 SHALL 以垂直领域（如金融、科技、历史）为前提设计选题挖掘；桌面端方案 SHALL 以"性能不劣于当前 Web 版"为验收标准，并给出性能基线指标。
5. 报告与路线图 SHALL 链接到 `docs/README.md` 与 `plans/README.md` 索引；链接检查 SHALL 通过。

### Requirement 2: 与 agent-capability-expansion 的协作门槛

**User Story:** 作为维护者，我希望另一个 spec 的改动先单独提交，本功能的改动保持未提交，便于分别审阅。

#### Acceptance Criteria

1. WHILE Gate 未满足时，THE 执行者 SHALL 只新增本 spec 目录、`docs/reviews/2026-10/` 下的文件和 `plans/ROADMAP.md`，SHALL NOT 修改任何已被 git 跟踪的文件。
2. THE Gate SHALL 在以下任一条件满足时判定为满足（用户确认，不做统一检查）：
   1. `agent-capability-expansion/tasks.md` 中没有状态为 `[ ]`、`[-]`、`[~]` 的任务（可选任务的 `[ ]` 除外）；
   2. 报告与路线图写完后开始轮询，已累计等待 2 小时。
3. WHEN Gate 满足时，THE 执行者 SHALL 暂存 agent-capability-expansion 当时的全部改动（已修改与新增的路径，排除本功能新增的文件），用中文提交说明创建一次提交，不使用 `--no-verify`、不推送。
4. IF 提交时该 spec 尚未完成，THEN 交付说明 SHALL 写明这一点，以及之后其改动可能与本功能改动混在工作区。
5. AFTER 提交后，本功能的全部改动 SHALL 保持未提交。

### Requirement 3: 安全与鉴权

**User Story:** 作为部署者，我希望服务在网络暴露时默认安全。

#### Acceptance Criteria

1. IF `mindex serve` 监听非回环地址且没有配置 `MINDEX_WEB_TOKEN`，THEN THE 命令 SHALL 拒绝启动并输出中文原因，除非显式传入 `--allow-insecure`。
2. THE `/login` SHALL 只接受站内相对路径作为 `next`：拒绝带 netloc、反斜杠或控制字符的值，回落到 `/`。
3. THE `/logout` SHALL 只接受 POST；页面登出入口同步改为 POST。

### Requirement 4: IO、资源与错误处理

**User Story:** 作为用户，我希望服务在并发、大文件与失败场景下稳定，且失败可诊断。

#### Acceptance Criteria

1. THE JSON/字节原子写 SHALL 只有 `shared/jsonio` 一份实现：同目录唯一临时文件、fsync、`os.replace`、UTF-8；`studio.save`/`save_bytes` SHALL 改用它。
2. 读写中文文本 SHALL 显式使用 `encoding="utf-8"`。
3. THE 声音克隆接口 SHALL 不在事件循环中执行阻塞网络或 ffmpeg 调用；样本上传 SHALL 有大小上限，超限返回 413 且不残留文件；克隆失败 SHALL 删除待定样本；失败记录的 voice_id SHALL 唯一。
4. THE `save_upload` SHALL 在失败时自行删除半成品；附件落盘与哈希 SHALL 在线程池执行；备份子进程 SHALL 有超时。
5. 后台线程顶层异常处理 SHALL 记录 traceback；查找构建记录失败 SHALL 不掩盖原始异常。
6. 返回前端的连接测试错误 SHALL 脱敏，完整错误写入服务端日志。
7. 前端轮询与 SSE 重连 SHALL 指数退避并有上限，页面隐藏时暂停轮询；接口失败 SHALL 提示用户而不是静默吞掉。

### Requirement 5: 工程规范与 CI

#### Acceptance Criteria

1. THE ruff SHALL 启用 `ASYNC`、`BLE` 与 `RUF100`；保留的宽泛异常捕获 SHALL 带说明性 noqa。
2. THE 仓库 SHALL 提供 `.github/workflows/ci.yml`，运行 ruff、链接检查、`build-messages --check`、`build-assets --check`、`node --check` 与 `pytest -m "not slow"`。
3. THE 导入边界测试 SHALL 递归覆盖全部包；现存上层包互相引用以白名单冻结，新增违规依赖 SHALL 使测试失败；`maintenance` SHALL 不依赖 CLI 层；`agent` SHALL 只依赖 `shared` 与显式允许的 studio 纯函数。
4. 智能助手专用常量（领域说明、页面领域映射）SHALL 位于 `agent/`，`shared` 只保留领域名元组。

### Requirement 6: 架构重构

#### Acceptance Criteria

1. 素材、对白、人物三类后台任务 SHALL 共用 `BackgroundTaskRunner`（启动、重试、取消、终态、对外行格式）；对外 API 响应形状 SHALL 不变。
2. `metadata.sqlite3` 与 `agent.sqlite3` SHALL 使用 `PRAGMA user_version` 加有序迁移列表；旧库 SHALL 幂等升级；备份往返 SHALL 通过。
3. 文件锁 SHALL 由 `shared/locks.py` 统一提供，并按规范顺序（pipeline → index-write → library/register → studio）获取；逆序获取 SHALL 被拒绝。
4. 创作执行状态（线程、锁、取消）SHALL 由 `StudioExecutor` 管理；取消 SHALL 统一为进程内事件加哨兵文件一套 API。
5. `studio_workspace.py` SHALL 拆为 `studio/workspace/` 包，单文件不超过 700 行，不留转发层；固定 fixture 的构建产物 SHALL 与拆分前一致。
6. 阶段 SHALL 由 Stage_Registry 注册；既有任务的版本签名 SHALL 逐字节不变。
7. `studio.js` SHALL 按职责拆为多个有序脚本；五个页面 SHALL 共用 `shared/api.js`；HTML 资源版本参数 SHALL 由 `scripts/build-assets.py` 生成并可 `--check`。

### Requirement 7: 文案模板

**User Story:** 作为创作者，我希望按领域模板写文案，并能定制模板。

#### Acceptance Criteria

1. THE 系统 SHALL 内置六个模板：通用解说（Base_Template）、历史解说、热点评论、影视解说、知识科普、访谈改写；字段含 id、名称、分类、kind（`outline`/`rewrite`）、提示词、输出约束。
2. 现有文案生成 SHALL 迁移为 Base_Template，提示词可改进，`WRITER_PROMPT_VERSION` 递增为 3。
3. 已有文案版本 SHALL 保留且不被自动重写；新方案缺省使用 Base_Template。
4. 用户 SHALL 能复制内置模板并修改，用户模板存 `script_templates` 表；内置模板只读；被方案引用的用户模板 SHALL 不可删除。
5. 文案阶段签名 SHALL 包含模板内容哈希；修改所选模板后 SHALL 提示需要重新生成，但不自动重写。
6. 创作页 SHALL 提供模板选择与管理；智能助手 SHALL 能列出模板并为方案设置模板。

### Requirement 8: 参考文稿

#### Acceptance Criteria

1. 方案 SHALL 能附加参考文稿（上传或粘贴），存 `inputs/<sha256>.txt`，执行前校验摘要，总字数有配置上限，超限返回 422。
2. writer SHALL 把参考文稿作为"仅供参考"的独立段落发送。
3. `rewrite` 类模板 SHALL 允许大纲为空但要求至少一份参考文稿，缺失时预检阻塞。
4. 文案签名 SHALL 包含参考文稿摘要。

### Requirement 9: 方案级画幅

#### Acceptance Criteria

1. 方案 SHALL 提供 `canvas`（16:9、9:16、1:1）与 `fit`（pad、crop），分辨率映射来自配置。
2. render 与 compose 的签名 SHALL 包含画幅；切换画幅只重建 render 与 compose，召回与配音 SHALL 复用。
3. 字幕字号与位置 SHALL 按画幅缩放。

### Requirement 10: 分镜来源

**User Story:** 作为创作者，我希望分镜画面不只来自素材召回。

#### Acceptance Criteria

1. THE `Shot` SHALL 有 `source` 字段，缺省为 `recall`；未设置 source 的既有分镜签名 SHALL 不变。
2. THE 时间线视频 SHALL 支持素材区间与文件区间两种形态。
3. 每种来源 SHALL 在 Stage_Registry 注册自己的依赖图；预检 SHALL 按来源判断阻塞（例如 clip 不需要索引，aigc 需要激活的视频生成档案）。
4. `clip`：用户上传视频（复用查询文件通道并校验摘要），在文件内按配音时长取定长窗口，可设人工起点；ducking 使用片段原声；没有活动索引时纯 clip 任务 SHALL 能完成构建。
5. `image`：静态图片按 motion（zoom-in、zoom-out、pan-left、pan-right，强度）生成与配音等长、帧数确定的画面；签名含图片摘要与 motion。
6. 混合来源的成片 SHALL 正确合成；清理与备份 SHALL 识别新增的输入引用。
7. 创作页分镜卡 SHALL 提供来源切换与对应设置；智能助手 SHALL 能设置来源。

### Requirement 11: AIGC 视频来源与"视频生成"角色

#### Acceptance Criteria

1. THE 系统 SHALL 定义 `VideoGenerator` 协议（提交、轮询、下载、可选取消）与 `make_video_generator` 工厂，后端 `minimax`、`dashscope`、`comfyui`。
2. 设置页 SHALL 新增"视频生成"角色：档案、预设（MiniMax、阿里复用已有密钥）、测试连接；测试连接 SHALL 不发起付费生成。
3. ComfyUI 档案 SHALL 支持导入 API 格式工作流 JSON 与节点映射（prompt、negative、seed、frames、width、height），保存时校验并给出中文错误。
4. `aigc` 分镜 SHALL 经 `generate` 阶段异步提交、轮询（退避、总超时）、下载；轮询之间检查构建取消；结果不可变留档；签名含 prompt、模型、参数、seed、参考图摘要。
5. 生成时长缺省取配音时长并按后端允许值取整；片段不足时以冻结帧兜底。
6. 没有激活视频生成档案时，预检 SHALL 阻塞并说明原因。
7. 审计与诊断记录 SHALL 不包含密钥。

### Requirement 12: AI 动画来源与"动画生成"角色

#### Acceptance Criteria

1. 设置页 SHALL 新增"动画生成"角色，配置段 `studio.animator` 缺省复制 `agent.llm`。
2. Animator SHALL 生成自包含单文件 HTML；生成或人工保存的源码 SHALL 经静态校验，拒绝外部 URL、`fetch`、`XMLHttpRequest`、`WebSocket`、动态 `import()`，并限制大小。
3. Capture_Sandbox SHALL：拦截全部网络请求（只放行 `data:`、`blob:`）；使用临时 profile；以虚拟时钟接管 `requestAnimationFrame`、`performance.now`、`Date`、定时器与 `document.timeline`；逐帧截图经管道交给 ffmpeg；有总时长上限与单帧超时。
4. 同一份源码、画布与 fps 的两次录制 SHALL 逐帧一致。
5. 录制依赖 SHALL 为可选 extra `animation`；未安装时 SHALL 给出中文安装指引。
6. 动画源码 SHALL 版本化（origin `generated`/`manual`），可人工编辑并重新录制；修改只重建该分镜 render 与成片。
7. 页面预览 SHALL 使用 `sandbox="allow-scripts"`（不含 `allow-same-origin`）的 iframe 与严格 CSP。

### Requirement 13: 文档一致性

#### Acceptance Criteria

1. 所有活文档 SHALL 与代码一致：模型角色数（10）、Protocol 数、模块与包、SQLite 表、数据目录、配置键与默认值、环境变量、CLI 命令组与选项、API、工具数量。
2. `docs-consistency.md` SHALL 列出每条偏差及其修正状态；报告 1–3 SHALL 回填各任务完成状态。
3. 最终统一检查 SHALL 全部通过；交付说明 SHALL 写明未验证范围。

### Requirement 14: 新功能暴露给智能助手

**User Story:** 作为通过助手面板或外部 harness（MCP）工作的用户，我希望本期新增的每项创作能力都能用对话完成，助手知道这些能力并会主动使用。

#### Acceptance Criteria

1. 本期新增的每项页面能力 SHALL 有对应工具（经回环调用 REST，写操作有预览与 focus，带媒体时有 media），并随 MCP 导出：
   - 文案模板：列出、查看、复制、修改、删除用户模板（删除为 danger），为方案选择模板；
   - 参考文稿：添加（附件或文本）、移除；
   - 画幅：设置 canvas/fit；
   - 分镜来源：设置 recall/clip/image/aigc/animation 来源及其参数，上传附件直接用作画面；
   - AIGC：设置提示词、负面提示词、种子、参考图，单独执行 generate 节点，查看生成状态与结果；
   - AI 动画：生成、读取与修改动画源码（修改走预览确认），单独重新录制；
   - 设置：视频生成与动画生成两个角色的档案增删改、测试连接、激活（沿用现有档案工具的角色取值）。
2. THE 系统提示词与领域说明 SHALL 描述模板、参考文稿、画幅与五种分镜来源，以及何时选用哪种来源。
3. `tests/agent_scenarios.py` SHALL 新增确定性场景覆盖：按模板改写访谈稿、竖屏方案、用附件作画面、AIGC 分镜生成、动画分镜生成与修改；`scripts/agent_eval.py` 同步。
4. 工具数量相关断言与 `docs/agent.md` 工具表 SHALL 与注册表一致。
