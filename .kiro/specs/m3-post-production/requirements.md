# Requirements Document

M3 后期与第三方工具（ROADMAP M3）

## Introduction

M1 完成后，成片仍是硬切拼接，字幕只能配字体与字号，没有文字层、品牌、封面与发布包，也无法接入外部生成服务；背景音乐只有 AI 生成与翻唱。本功能分三期补齐：

1. 第一期 成片质感：转场、字幕样式、文字层（ASS 与 HTML 透明叠加）、品牌模板（片头片尾、水印）；
2. 第二期 平台交付：多 final 阶段、多画幅导出、平台预设、封面、发布元数据、发布包；
3. 第三期 扩展能力：智能曲库（分析、推荐、节拍对齐）与第三方工具来源（HTTP 插件、MCP 客户端）。

两条贯穿全部需求的约束（见需求 21、22）：

- Web 端与桌面 App 端（`desktop/`，Electron 外壳 + 冻结的 `mindex serve`）兼容，且每项能力在两端都可用；
- 每项能力都在 Web 页面、CLI、agent 工具、MCP 四个入口体现（底层统一经 REST），并补齐基础 `mindex studio` CLI。

协作约束：改动保持未提交，除非用户要求提交；沿用 AGENT.md 约定（中文消息、`messages.json` 唯一文案源、strict 配置、不可变版本、确定性测试替身）。

## Glossary

- **Post_Pipeline**：成片后期管线 `workspace/post.py`，拼接 → 片头片尾 → 水印 → 字幕与 ASS 文字层 → 封装。
- **In_Transition**：镜内转场，只作用于单镜开头或结尾，不改变时长。
- **Overlap_Transition**：重叠转场（xfade），由前一镜 `transition.next` 声明，靠尾部余量实现。
- **Handle**：尾部余量，render 阶段在本镜帧数之外多产出的画面帧，供重叠转场与节拍对齐消耗。
- **Overlay**：文字层，`engine` 为 `ass`（字幕引擎烧录）或 `html`（透明录制叠加）。
- **Brand_Template**：品牌模板，含字幕样式、文字层样式、片头、片尾、水印；内置只读，用户模板复制修改。
- **Final_Variant**：final 阶段的变体键（画幅或平台），节点键为 `stage:variant`。
- **Platform_Preset**：平台预设（bilibili、douyin、youtube）。
- **Music_Track**：曲库曲目，带授权元数据与分析结果。
- **Tool_Source**：第三方工具登记（HTTP manifest 或 MCP 服务），含工具白名单。
- **Signature_Compat**：签名兼容规则：新字段为默认值时不进 dump 与签名，旧版本号与签名逐字节不变。

## Requirements

### Requirement 1: 后期管线与兼容

**User Story:** 作为维护者，我希望成片合成按步骤组织，加入新后期能力时旧成片不受影响。

#### Acceptance Criteria

1. THE compose 阶段 SHALL 经 Post_Pipeline 执行；未使用任何 M3 能力的方案，其 compose 签名、ffmpeg 参数与输出 SHALL 与改动前一致。
2. 所有新增的分镜字段与方案选项 SHALL 遵守 Signature_Compat。
3. WHEN 任一 M3 后期步骤参与时，THE compose 签名 SHALL 包含该步骤的参数与版本常量。

### Requirement 2: studio CLI

**User Story:** 作为终端用户，我希望不打开页面也能完成创作任务的查看、构建与下载。

#### Acceptance Criteria

1. THE CLI SHALL 提供 `mindex studio` 命令组：`jobs`、`plans`、`preflight`、`build`（可 `--watch`）、`status`、`download`。
2. THE CLI SHALL 默认在进程内调用 REST（与 agent 工具同一客户端），`--url` 与令牌连接运行中的服务。
3. WHEN 执行器忙碌（409）时，THE CLI SHALL 给出中文提示并以非零退出码结束。
4. 后续每项 M3 能力 SHALL 在该命令组下提供对应子命令；`--json` 输出机器可读结果。

### Requirement 3: 镜内转场

#### Acceptance Criteria

1. THE Shot SHALL 支持可选 `transition`，含 `enter`、`leave`（kind：fade 黑场、white 白场、zoom 缩放、slide 滑动；duration 0.1–2 秒）。
2. THE render 阶段 SHALL 在画面上实现 In_Transition，镜头帧数与配音对齐不变。
3. WHEN 转场时长超过镜头时长一半时，THE 系统 SHALL 截短到一半并在版本数据中给出提示。

### Requirement 4: 重叠转场

#### Acceptance Criteria

1. THE `transition.next` SHALL 声明本镜与下一镜之间的 Overlap_Transition（xfade 类型与时长）。
2. THE render 阶段 SHALL 为声明了 `next` 的镜头产出等于转场时长的 Handle；源素材不足时冻结末帧补足。
3. THE compose 阶段 SHALL 用 xfade 消耗 Handle（过渡从切点开始，在下一镜开头完成），成片总帧数、配音、字幕与背景音乐时间线 SHALL 与无转场时一致；声明了 next 时本镜 leave 与下一镜 enter 被取代。
4. 最后一镜的 `next` SHALL 被忽略并提示。

### Requirement 5: 字幕样式模板

#### Acceptance Criteria

1. THE 系统 SHALL 提供字幕样式：位置（底部、居中、顶部）、字号、主色、描边色与宽度、阴影、背景条、高亮色。
2. THE Shot SHALL 支持可选 `highlights`（字幕中需要高亮的词）。
3. WHEN 设置了字幕样式或高亮时，THE compose SHALL 生成并烧录 ASS；否则沿用原 `force_style` 路径。SRT 与 VTT SHALL 继续导出。
4. 竖屏与方形画幅 SHALL 按短边缩放字号，规则与现有 `canvas_subtitle_style` 一致。

### Requirement 6: ASS 文字层

#### Acceptance Criteria

1. THE Shot SHALL 支持可选 `overlays[]`；`engine=ass` 的图层类型为 title（标题卡）、lower_third（下三分之一信息条）、data_card（数据卡片：标签与数值列表）。
2. 每个图层 SHALL 有相对镜头的起止时间（缺省整镜）与入场效果（none、fade、slide）。
3. THE compose SHALL 把图层换算到成片时间并写入同一 ASS；`burn=false` 时文字层 SHALL 仍被烧录，字幕保持软字幕。

### Requirement 7: 品牌模板

#### Acceptance Criteria

1. THE 系统 SHALL 提供内置只读品牌模板与用户品牌模板（复制后修改，存 SQLite），接口与文案模板一致：列表、获取、复制、修改、删除。
2. 品牌模板 SHALL 包含字幕样式、文字层样式、片头视频、片尾视频、水印（图片、位置、透明度、缩放、边距）。
3. THE 方案 SHALL 以 `brand_id` 选用模板，以 `brand_overrides` 覆盖其中几项；被方案引用的模板 SHALL 拒绝删除（409）。
4. 品牌素材 SHALL 按内容寻址存储于 `data_dir/brand/`，读取时校验哈希。
5. 模板内容哈希 SHALL 只覆盖影响成片的字段，并进入 compose 签名。

### Requirement 8: 水印、片头、片尾

#### Acceptance Criteria

1. WHEN 品牌模板含片头或片尾时，THE compose SHALL 按画幅与帧率规范化后拼接，成片时长 = 片头 + 正文 + 片尾。
2. 字幕、文字层、背景音乐与章节时间 SHALL 随片头整体偏移；背景音乐只覆盖正文。
3. THE 水印 SHALL 在烧录阶段叠加于正文（可选覆盖片头片尾），`burn=false` 时同样生效。

### Requirement 9: HTML 透明叠加层

#### Acceptance Criteria

1. `engine=html` 的图层 SHALL 由逐镜 `overlay` 阶段产出 HTML：内置模板填数据，或由动画生成模型按透明背景约束生成。
2. 人工修改的叠加层源码 SHALL 钉住，只有强制重建替换。
3. THE render 阶段 SHALL 透明录制（PNG 帧，`omit_background`）并 overlay 到画面；Playwright 不可用时预检 SHALL 阻塞并说明原因。

### Requirement 10: 多 final 阶段与变体

#### Acceptance Criteria

1. THE 注册表 SHALL 支持多个链式 final 阶段及变体；`p["final"]` 继续指向 compose，其余存 `p["finals"]`。
2. 预检 SHALL 按变体展开节点并给下游 final 阶段写占位；阻塞 SHALL 传播；选用版本 SHALL 支持 final 变体。
3. 只有 compose 的方案，预检、构建与签名 SHALL 不变；旧工作台加载时补 `finals={}`。
4. agent 工具与前端中写死的 compose SHALL 改为由注册表驱动。

### Requirement 11: 多画幅导出

#### Acceptance Criteria

1. THE 方案 SHALL 支持 `exports[]`（canvas、fit、mode：derive 或 rerender）。
2. derive：由主成片的无字幕版本裁切或加边，按新画幅重新排版并烧录字幕、文字层与水印。
3. rerender：维护关联子方案（共享分镜内容、画幅覆盖），复用配音等未变产物，重新渲染。
4. "构建全部画幅" SHALL 串行构建主方案与子方案，可整体取消。

### Requirement 12: 平台预设

#### Acceptance Criteria

1. THE 系统 SHALL 内置 bilibili、douyin、youtube 预设：成片画幅、封面尺寸与格式、标题/简介/标签长度与数量、章节规则。
2. 预设数值 SHALL 在执行时核对平台现行规则并在文件中注明来源与核对日期。

### Requirement 13: 封面

#### Acceptance Criteria

1. THE `cover:<platform>` 阶段 SHALL 支持三种来源：成片挑帧（按清晰度给候选）、AIGC 生图、上传。
2. THE 封面 SHALL 叠加标题与副标题（品牌字体、自动换行、描边或底条），按平台尺寸导出。
3. 没有可用生图模型时，AIGC 来源 SHALL 在预检阻塞。

### Requirement 14: 发布元数据

#### Acceptance Criteria

1. THE `metadata:<platform>` 阶段 SHALL 由 LLM 生成标题、简介、标签与章节，并按预设校验与修正。
2. 章节 SHALL 来自成片 boundaries（含片头偏移）；曲库音乐署名 SHALL 自动写入简介。
3. 人工修改的元数据 SHALL 钉住。

### Requirement 15: 发布包

#### Acceptance Criteria

1. THE `package:<platform>` 阶段 SHALL 输出目录与 zip：对应画幅成片、封面、srt/vtt、metadata.json 与 metadata.txt。
2. 上游任一产物变化后，发布包 SHALL 需要重建。

### Requirement 16: 曲库

#### Acceptance Criteria

1. THE 系统 SHALL 提供曲库：导入时授权来源、许可、署名必填，有效期可选；文件按内容寻址存储，重复导入去重。
2. 后台分析 SHALL 产出 BPM、节拍时间、能量与亮度；LLM 从固定情绪词表打标签，用户可修改。合成 120 BPM 节拍器音频的 BPM 误差 SHALL ≤ 2%。
3. THE ShotMusic SHALL 支持 `kind=library`（`track_id`）；授权过期时预检 SHALL 阻塞。
4. 推荐接口 SHALL 按段落文案推断情绪与节奏区间，按情绪、BPM、时长与授权排序。
5. 被方案引用的曲目 SHALL 拒绝删除。

### Requirement 17: 节拍对齐

#### Acceptance Criteria

1. WHEN 方案开启 `beat_align` 时，THE compose SHALL 把每个切点推迟到容差内最近的节拍，画面用 Handle 补足、配音补静音。
2. 对齐后切点与节拍距离 SHALL ≤ 1 帧；配音 SHALL 不被截断；关闭时签名不变。

### Requirement 18: 第三方工具登记

#### Acceptance Criteria

1. THE 系统 SHALL 提供 Tool_Source 登记：协议（http、mcp-stdio、mcp-http）、连接信息、密钥引用、工具白名单（每个工具的 allow_upload、output、timeout、max_bytes）、产物下载主机白名单。
2. 只能调用白名单中的工具；未勾选 allow_upload 的工具 SHALL 拒绝携带素材。
3. 密钥 SHALL 不回显；产物下载 SHALL 限制大小与主机。
4. 登记与修改 Tool_Source 的 agent 工具 SHALL 为 danger 级；其中会在本机启动进程的 `mcp-stdio` 登记 SHALL 不经 MCP 暴露（外部 MCP 客户端不能让 Mindex 执行任意命令），工具列表、测试连接与 http/mcp-http 登记 SHALL 经 MCP 暴露并受 `agent.mcp.policy` 约束。

### Requirement 19: 第三方工具分镜来源

#### Acceptance Criteria

1. THE ShotSource SHALL 支持 `kind=tool`（tool_id、arguments、uploads、output）；视频产物阶段为 tts → invoke → select → render，图片产物为 tts → invoke → render。
2. THE invoke 签名 SHALL 包含工具 id、工具配置哈希、参数与上传文件哈希。
3. 产物 SHALL 经 ffprobe 或图片解码校验后登记为不可变版本文件。
4. HTTP 适配器 SHALL 支持提交、轮询、下载；MCP 适配器 SHALL 支持 stdio 与 streamable-http，经 list_tools 发现、call_tool 调用，`mcp` 缺失时提示安装。

### Requirement 20: 文档与验证

#### Acceptance Criteria

1. THE `docs/studio.md`、`docs/cli-reference.md`、`docs/agent.md`、`docs/desktop.md` 与 `plans/ROADMAP.md` SHALL 同步更新。
2. 每期结束 SHALL 运行 ruff、文档链接、messages、assets 检查、`node --check`、`pytest -m "not slow"`，以及入口矩阵测试（需求 22.4）与桌面检查（需求 21.6）。

### Requirement 21: Web 端与桌面 App 端兼容

**User Story:** 作为用户，我希望在浏览器和桌面 App 里都能使用全部后期能力，行为一致。

#### Acceptance Criteria

1. 所有能力 SHALL 只在后端经 REST 实现，前端两端共用；不新增仅桌面可用的业务逻辑，也不依赖 `window.MindexDesktop`（检测不到时保持 Web 行为）。
2. 新增的包数据（品牌、平台预设、叠加层模板等）SHALL 声明在 `pyproject.toml` 的 package-data，并通过 `scripts/check-desktop-sidecar.py` 的打包核对；新模块 SHALL 不以自身源码哈希作指纹（改用显式版本常量），否则登记到 `desktop/sidecar/mindex.spec` 的 `SOURCE_MODULES`。
3. ffmpeg/ffprobe 调用 SHALL 一律经 `shared/media.py`（`command()`、`ffmpeg_bin()`、`ffprobe_bin()`）；封面等 Pillow 文字排版 SHALL 使用与字幕相同的字体解析（桌面随包 CJK 字体）。
4. 新文件存储 SHALL 位于 `data_dir`（桌面为系统用户数据目录），文件锁 SHALL 经 `shared/locks.py`；新依赖 SHALL 可被 PyInstaller 冻结（曲目分析只用 numpy，不新增原生依赖）。
5. 下载（发布包 zip、封面、导出成片）SHALL 带 `Content-Disposition`，在桌面经外壳的下载处理保存；上传 SHALL 用标准文件输入，两端一致。HTML 叠加层录制在桌面 SHALL 复用"可选 Chromium 组件"，未安装时预检阻塞并提示安装。
6. MCP stdio 工具 SHALL 以用户登记的绝对路径启动并可配置环境变量（桌面 GUI 进程 PATH 精简）；`mcp` 依赖 SHALL 随桌面后端打包。每期 Checkpoint SHALL 在冻结后端上做一次 M3 冒烟（`check-desktop-sidecar.py` 增加对应步骤）；无法本机构建时 SHALL 说明未验证范围。

### Requirement 22: 四入口体现

**User Story:** 作为用户和自动化调用方，我希望每项能力在 Web、CLI、agent、MCP 都能使用。

#### Acceptance Criteria

1. 每项能力 SHALL 有 Web 页面入口（创作页或设置页面板，中英双语文案）。
2. 每项能力 SHALL 有 `mindex studio …` CLI 子命令，支持 `--json`。
3. 每项能力 SHALL 有 agent 工具（ToolSpec），并经 `agent/mcp_server.py` 导出为 MCP 工具（除需求 18.4 的本机进程登记外），读写风险分级与 `agent.mcp.policy` 一致。
4. THE 测试 SHALL 维护一张能力入口矩阵（能力 → REST 路由、CLI 命令、agent 工具名、MCP 是否导出、页面元素），`tests/test_m3_entrypoints.py` 逐项断言存在。
5. `docs/agent.md` 工具表与 `docs/cli-reference.md` SHALL 列出全部新增工具与命令。
