# 智能助手（Agent）

每个页面右下角的「智能助手」按钮打开一个侧边面板。用自然语言描述需求，助手调用 Mindex 现有能力完成，需要时向你提问；修改操作会先给出预览并请求确认。同一套工具也以 MCP 服务导出，可由 deepseek-harness、Claude Code、Codex 等外部 harness 驱动。

## 快速开始

1. 在设置页「智能助手」角色新建档案（预设：DeepSeek、阿里云百炼、智谱、Anthropic），点击「测试连接」——测试会要求模型调用一个 echo 工具，只有工具调用成功才算通过。激活档案。也可以在 `config.local.yaml` 的 `agent.llm` 配置，密钥放在环境变量 `MINDEX_AGENT_API_KEY`。
2. 在任意页面打开助手面板，例如在创作页说：
   - 「合并 a1daad59 和 925281d5 两个分镜，并重新生成该分镜的后续流程」
   - 「我想使用自定义模型配置」
   - 「这个任务最近一次构建为什么失败？」
3. 面板会随消息发送当前页面上下文（创作页为任务、方案、详情区打开的分镜与阶段），所以「这个任务」「第 3 镜」都能被理解。

## 确认与安全

| 风险级别 | 工具示例 | 行为 |
|---|---|---|
| read | 读取工作台、预检、诊断、检索、找相似、抽帧、索引检查、识别演员 | 直接执行 |
| write | 修改分镜/文案/方案参数、新建任务或档案、整理信息、上传素材与字幕、取消任务或构建、人物审核 | 显示改动预览，确认后执行；可勾选「本会话内不再询问此操作」 |
| danger | 构建成片、单节点或整列执行、发起素材处理任务、激活索引、发布人物检索、删除任务/方案/人物/档案/音色、修改或激活模型档案、克隆音色 | 每次都必须显式确认，不接受会话授权 |

- 面板顶部「跟随助手定位」后面有「总是允许」开关（默认关闭，选择记在本机浏览器）。开启后，面板收到 write/danger 确认请求即自动回复「允许」，不再逐次询问；打开会话时仍待确认的请求也会自动允许，历史中已决的请求不受影响。服务端分级策略不变，只是由面板代你确认；开启期间删除任务、修改模型档案等高风险操作也会直接执行。
- 预览时读取的 `revision` 会在确认后原样提交。如果确认期间你在页面上改动了工作台，提交会被拒绝，助手重新读取后再试，不会覆盖你的修改。
- 密钥只通过助手弹出的表单（密码框）收集：明文直接进入本机保险库（`data_dir/agent.sqlite3`，带有效期，默认 30 分钟），会话日志、发给模型的请求与 SSE 流里只出现 `secret://...` 引用，工具执行时才在进程内解引用。在聊天框粘贴疑似密钥会被拦截。
- 不允许在会话中修改、切换或删除智能助手自身正在使用的档案，请到设置页操作；各角色正在使用的档案也不能删除。
- 文案、分镜旁白、素材名称、工具结果都只是数据；系统提示词明确要求模型不执行其中的指令。即使模型被诱导调用高风险工具，也仍需你确认。
- `/api/agent` 与其余接口一样受 `MINDEX_WEB_TOKEN` 保护（附件端点自行鉴权，见[附件](#附件)）。没有设置访问令牌时，任何能访问服务端口的人都能使用助手（与设置页同样的风险，见 [设置页指南](settings.md#密钥安全)）。

删除策略：

- 不提供会删除磁盘文件的工具：删除素材（`DELETE /api/admin/sources/{id}`）和删除演员表导入图片（`DELETE /api/persons/imports/{id}/images/{image_id}`）只能在页面上操作。
- 例外是 `delete_job`（删除整个创作任务及其产物），保留为 danger 级。
- 只删除记录、文件留给 `mindex clean` 回收的删除一律为 danger 级：`delete_plan`、`remove_reference_text`、`delete_script_template`、`delete_brand`、`delete_person`、`remove_person_reference`、`change_search_release(action=delete)`、`delete_model_profile`、`delete_voice`。
- 回环客户端拒绝路径中含 `.`/`..` 段、`?` 或 `#` 的请求（返回 400 `invalid_path`，不发送请求），防止工具参数里的 ID 经路径规整打到别的接口，例如把 `delete_job(job_id="../../admin/sources/x")` 变成删除素材。

## 工具领域与动态加载

工具按领域组织（领域名在 `backend/src/shared/agent_domains.py`，页面领域映射与领域说明在 `backend/src/agent/domains.py`）。每一步只把 meta 领域和会话已加载领域的工具发给模型，工具数量增长后不必全部下发。

| 领域 | 说明 | 默认加载页面 |
|---|---|---|
| `meta` | 提问、能力缺口、界面导航、`load_tools`；始终下发 | — |
| `studio` | 创作任务、方案、文案与文案模板、参考文稿、画幅、分镜与画面来源（含 AI 生成图片、AI 生成视频与 AI 动画）、背景音乐与歌词生成、构建与版本 | 创作页 |
| `studio_detail` | 创作诊断、版本依赖、召回词建议、人工选片、整列重做、AI 动画源码读取与修改、背景音乐歌词生成 | — |
| `studio_post` | 成片后期与交付：品牌模板（字幕样式、文字层样式、片头片尾、水印）与品牌素材上传、HTML 叠加层源码、多画幅导出、平台发布（预设、封面、发布元数据、发布包）；音乐库与第三方工具也在这里 | 素材管理 · 音乐库（`/admin/music`） |
| `library` | 素材与片段浏览、整理信息、素材报告、处理任务、上传素材与字幕 | 素材管理页 |
| `search` | 文字/图片/视频检索、找相似、区间抽帧、系统状态 | 检索页 |
| `index` | 内容索引版本列表、一致性检查、激活与回退 | — |
| `persons` | 演员/角色/参考图、识别演员、出镜审核、人物发布、检索组合、演员表导入 | 人物库页 |
| `settings` | 模型档案、业务设置、音色库与音色克隆 | 设置页 |
| `topics` | 选题挖掘：领域档案、数据源（RSS、热榜、Brave 搜索、日历）、挖掘任务、选题打分与素材覆盖、大纲、从选题建创作任务、任务档案同步、素材采集清单、每日定时（见[选题挖掘](topics.md)） | 选题挖掘页（`/studio/topics`） |

加载规则：

- 每轮开始时，当前页面对应的领域自动加载并标为最近使用。
- 模型需要未加载领域的工具时调用 `load_tools(domains=[...])`（1–3 个领域，未知领域或 `meta` 返回 `invalid_arguments`，已加载领域不变），从下一步起生效；同时追加一条可忽略事件 `plugin:mindex/tools-loaded`。
- 成功执行某个非 meta 工具会把它所在领域标为最近使用。
- 已加载领域最多 3 个，超出时淘汰最久未使用的。列表持久化在 `agent.sqlite3` 的 `agent_sessions.loaded_domains`，同一会话的后续轮次和服务重启后沿用。
- 直接调用未加载领域的工具不会执行，返回 code 为 `TOOL_NOT_LOADED` 的可恢复错误，文本写明应调用 `load_tools(domains=["<领域>"])`。
- 系统提示词的「工具领域」一节列出每个可加载领域的说明、前 4 个代表工具和是否已加载，并说明以上规则。

`mindex agent tools` 输出全部工具的参数 JSON Schema（含 `domain`、`risk`、`mcp`）；`--domain` 只列指定领域，`--names-only` 只输出名称、风险和领域。

## 附件

交给助手的文件先暂存，再由工具以 `attachment://<token>` 引用。用到附件的工具：`search_by_media`、`upload_source`、`upload_subtitle`、`attach_query_media`、`add_reference_text`、`add_person_reference`、`identify_person`、`import_cast`、`clone_voice`。

面板操作：

- 点击「添加文件」可多选文件，也可以把文件拖放到面板，或粘贴剪贴板里的文件；粘贴文本时仍保留原有的疑似密钥拦截。
- 添加后立即上传（会先创建会话），chip 显示文件名、大小和进度条；移除按钮会中止上传，已上传的则撤销暂存（`DELETE /api/agent/attachments/{token}`）。
- 发送时等待全部附件上传完成；任一附件失败会阻止发送并显示原因。每条消息最多 12 个附件。
- 服务端校验每个附件都归属本会话（否则 422），把引用、文件名、类型、大小写入 `user/message` 事件，并在正文末尾逐行附上「附件：attachment://… 名称（类型，大小）」供模型引用。历史消息里以 chip 显示附件，图片可在大图浏览器中打开。

存储与生命周期：

- `POST /api/agent/attachments`（multipart：`file`，可选 `session_id`）流式写入 `data_dir/agent-attachments/<token>/`，元数据（文件名、类型、字节数、SHA256、归属、会话、过期时间）记在 `agent.sqlite3` 的 `agent_attachments` 表。
- 单个上限 `agent.attachment_max_mb`（默认 500 MiB），超出返回 413 并删除已写入部分；空文件返回 422。
- 有效期 `agent.attachment_ttl_hours`（默认 168 小时）。删除会话时同时删除其附件；过期附件由 `mindex clean` 的 B 级规则 `agent_attachments` 回收（见[磁盘清理](cleanup.md#b-级策略过期可能要用超期才删)）。附件目录不进入 `mindex backup`。

归属与鉴权：带 `session_id` 上传的附件归属该会话（owner=`session`），面板会话的工具调用只能使用本会话的附件；不带 `session_id` 的归属 mcp（owner=`mcp`），只有 MCP 工具调用能使用。引用不存在、过期或归属不符时，回环客户端返回 400 `attachment_invalid`，不向目标接口发送请求。附件端点不经网页鉴权中间件，按下表自行鉴权（MCP 令牌用常量时间比较）：

| 已设置的令牌 | 带 `session_id` 上传（归属会话） | 不带 `session_id` 上传（归属 mcp） |
|---|---|---|
| 都未设置（本机默认） | 允许 | 允许 |
| 只有 `MINDEX_WEB_TOKEN` | 需要网页凭证，否则 401 | 没有 mcp 归属：带网页凭证返回 422，不带返回 401 |
| 只有 `MINDEX_MCP_TOKEN` | 允许（网页侧未启用令牌） | 需要 Bearer MCP 令牌，否则 422 |
| 两者都设置 | 需要网页凭证；只持 MCP 令牌返回 401 | 需要 Bearer MCP 令牌 |

外部 harness 先上传、再把返回的 `id`（`attachment://…`）作为工具参数：

```bash
curl -s -H "Authorization: Bearer $MINDEX_MCP_TOKEN" -F file=@shot.png http://127.0.0.1:8000/api/agent/attachments
```

内存占用：上传端点与进程内回环（内置助手、进程内 MCP）都按块流式处理——请求体按 64 KiB 分块发送，服务端超过 1 MiB 即落盘，实测 64 MiB 附件转交给上传接口时 Python 堆峰值约 2 MiB，与文件大小无关。远程模式（`mindex mcp --base-url`）不同：客户端进程先经 `GET /api/agent/attachments/{token}/content` 把附件整个取回内存再提交，峰值约为文件大小的数倍。经远程 MCP 处理大视频时注意客户端内存，大体量原片建议直接在素材管理页上传。

## 视觉输入

`agent.llm.vision`（默认 `false`）表示模型是否接受图片输入；设置页「智能助手」档案有同名复选框可覆盖，保存后热生效。

开启后，每一步发请求前，运行时把两类图片发给模型：用户消息里的图片附件（JPEG/PNG/WebP/GIF/BMP/TIFF），以及工具结果声明的图片（`ToolOutput.images`，只接受 `attachment://` 或本站 `/api/` 地址）。处理规则（`agent/vision.py` 的 `materialize`）：

- 按出现顺序只保留最近 `agent.max_images`（默认 8）张；更早的替换为「（图片已省略）」。
- 每张解析为最长边不超过 1024 像素的 JPEG（quality 85）；无法读取或解码时替换为「（图片无法读取：原因）」，本步照常请求。
- OpenAI 兼容协议：用户图片为 `image_url` 数据 URL；工具图片放在该批工具结果之后合成的一条用户消息里（tool 消息只能是文本）。Anthropic 协议：图片作为 `image` 块直接放进用户消息或 `tool_result`。
- 上下文预算按每张图片 800 token 估算。
- 事件日志、SSE 与派生历史只保存图片引用；解析出的图片数据只存在于本次请求的内存中，不落库。

关闭时不向模型发送任何图片：系统提示追加一段说明「对话中的图片不会发送给你」，返回图片的工具在结果文本末尾注明「当前模型未开启视觉输入：图片未发送给模型，仅在面板展示」。

返回图片的工具：`sample_frames`（抽取的帧）、`search_by_media`（图片查询本身与前几个结果缩略图）、`identify_person`（上传图与获胜参考图）、`get_selection_candidates`（前 6 个候选缩略图）。

## 页面同步与定位

助手的每次查询与修改都会反映到网页上，并尽量定位到对应位置：

- 读工具和写工具都返回「定位描述」：页面（`studio`/`settings`/`search`/`admin`/`persons`）以及任务、方案、分镜、阶段、区块、模型角色、档案、检索词、素材等。写工具在确认卡片出现时就先定位到要修改的地方，执行后再定位到结果（例如合并后高亮保留下来的那一镜）。
- 改动与定位写入全局通道 `data_dir/agent.sqlite3` 的 `agent_changes` 表，所有打开的页面经 `GET /api/agent/changes` 订阅。内置助手与 MCP 服务端（包括独立进程的 `mindex mcp`）都会发布。

| 来源 | 数据刷新 | 定位 |
|---|---|---|
| 本标签页的助手会话 | 刷新 | 同页立即定位；跨页的定位等会话空闲或开始等待确认时，跳到本轮最后一个目标。跳转后面板自动打开，会话与待确认卡片延续 |
| 其他标签页的会话、MCP 调用 | 所有打开的页面都刷新 | 只在当前正好是同一页面（创作页为同一任务）时页内高亮，绝不跨页跳转 |
| 重放历史（打开旧会话、页面重载） | — | 不定位、不跳转 |

改动事件按 `domain` 区分：创作页刷新 `studio`，设置页刷新 `settings`（索引激活与检索组合切换也记为 `settings`，刷新设置页的索引版本区）。素材类工具发布 `library`、人物类工具发布 `persons`，但素材管理页和人物库页目前没有监听改动事件：助手修改整理信息、登记素材或审核人物后，需要手动刷新页面（或重新打开素材）才能看到；已打开素材的运行中任务仍按页面原有的 2 秒轮询更新。

纯查询同样跟随（例如在设置页问「颜色短片有几个分镜」，回答后会切到创作页定位）。面板顶部的「跟随助手定位」开关默认开启、会被记住；关闭后只刷新数据。

各页面的定位方式：创作页切换任务、高亮分镜卡片并滚动到第一个（带阶段时在详情区打开第一个分镜的该阶段），或高亮文案、方案栏、分镜工作台、工作日志、成片、产物等区块；助手操作的方案不是当前方案时只提示、不擅自切换。设置页展开模型角色并高亮档案卡，或展开检索调参、音色库、索引版本、处理设置。检索页填入检索词并执行。素材管理页打开素材并定位镜头或片段。人物库页切换分页。

用户明确要求「打开设置页」「带我看成片」这类界面操作时，助手调用 `navigate` 工具；它只改变界面、不修改数据，不导出到 MCP。

新页面接入约定：定义 `window.MindexAgentContext()` 返回页面上下文，定义 `window.MindexAgentFocus(focus, {passive})`（页面未就绪返回 `false`，面板会重试；`passive` 为真时只高亮、不切换内容），高亮用 `window.MindexAgent.highlight(element)`；需要随助手改动刷新时监听 `mindex:agent-mutation` 并按 `detail.domain` 过滤。

## 回复格式

助手回复按 Markdown 渲染：标题、列表、表格、代码块、引用、链接。渲染使用仓库内固定版本的 [marked](https://github.com/markedjs/marked) 18.1.1，结果经 [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.17 按白名单净化后显示（版本、许可证与校验值见 [第三方组件声明](../THIRD_PARTY_NOTICES.md)）：脚本、事件属性、内联样式、`javascript:`/`data:`/协议相对链接都会被清除，外部链接在新窗口打开。用户消息、工具卡片仍按纯文本显示。

## 图片与音视频

工具结果会附带相关媒体，显示在对应工具卡片下方：

| 工具 | 媒体 |
|---|---|
| `resolve_shots` | 分镜的配音、渲染视频与召回候选缩略图（最多 4 镜） |
| `get_workspace_summary`、`get_build_status`、构建结束总结 | 成片 |
| `list_versions`、`select_version`、`preview_version` | 对应版本的配音、视频或成片 |
| `get_selection_candidates`、`preview_selection` | 召回候选缩略图、带配音的选片预览视频 |
| `search_media`、`search_by_media`、`find_similar` | 检索结果片段缩略图 |
| `sample_frames` | 抽取的帧 |
| `list_sources`、`get_source`、`list_segments`、`get_segment_details` | 素材（首个镜头作封面）、片段缩略图 |
| `list_persons`、`get_person`、`add_person_reference`、`identify_person`、`list_review_shots`、`import_cast` | 人物参考图、识别用的图片、审核证据图、演员表截图 |

- 图片与带封面的片段排成缩略图网格，点击打开大图浏览：←/→ 切换，Esc 或点击背景关闭；片段用与创作页相同的镜头播放器播放。
- 图片与成片默认展开；配音与单镜视频默认折叠，展开时才加载，避免一次加载大量音视频。切换会话、关闭面板或关闭大图浏览时自动暂停。
- 只加载本站 `/api/...` 地址（后端 `agent/tools/media.py` 与面板两处校验）。助手 Markdown 回复里的图片同样只放行本站地址，外链图片会被移除；本站图片可点击放大。

## 面板宽度

拖动面板左边缘调整宽度（320px 到视口 80%），也可聚焦手柄后用 ←/→ 每次调整 24px、Home/End 到最小/最大、回车或双击恢复默认 440px。宽度保存在浏览器本地，刷新和切换页面后保持；宽屏下页面右侧留白随之变化。

## 任务跟踪与取消

助手提交创作构建（`run_build`、`run_node`、`run_column`）、后台任务（`start_source_task`、`retry_task`）或选题挖掘（`discover_topics`、`manage_topic_run` 重试）后，`agent/watcher.py` 的 `TaskWatcher` 轮询状态：

- 构建：跟踪事件 `plugin:mindex/build-watch`，结束写 `plugin:mindex/build-finished`，面板显示进度卡片。
- 素材、对白、人物任务与选题挖掘任务：跟踪事件 `plugin:mindex/task-watch`，结束写 `plugin:mindex/task-finished`；选题挖掘结束的总结附产出卡片数、大纲数与告警，并提示用 `list_topics` 汇报高分选题。

进入终态（complete、partial、failed、cancelled、interrupted）后自动触发一轮 followup 总结：构建给出重建、复用、失败数量，失败或阻塞时说明原因并建议下一步。全局同一时间只能有一个构建，冲突时助手会告知并建议稍后重试。服务重启后，未结束的对话轮次标记为中断，尚未结束的跟踪目标继续跟踪；重启期间已结束的立即补写结果并总结。

后台任务取消（`cancel_task`，write）：

- 接口：`POST /api/admin/source-tasks/{id}/cancel`、`POST /api/admin/dialogue-tasks/{id}/cancel`、`POST /api/persons/tasks/{id}/cancel`。
- queued 任务直接置为 `cancelled`；running 任务设置取消标记，在下一次上报进度时，或在等待 GPU 准入时停止，置为 `cancelled`（错误文本「已取消」）并释放全部锁。其他状态返回 409。
- 取消标记同时写入进程内事件和 `data_dir/locks/cancel/<task_id>` 哨兵文件，跨进程生效。
- `cancelled` 任务可以重试，复用已有检查点。
- 发布任务中途取消时活动索引不变，正在构建的索引版本保持可续建。
- 取消父任务不会连带取消已链式创建的发布子任务。

创作构建取消（`cancel_build`，write）：

- 版本工作台：`POST /api/studio/jobs/{job}/builds/{bid}/cancel`，只接受 running 构建（否则 409），返回 `{build_id, status: "cancelling"}`。标记同时写入进程内事件和哨兵文件 `studio/<job>/builds/<bid>.cancel`。执行器在下一个节点开始前停止：已开始的节点照常完成，其余 pending 节点置为 `blocked`、错误「已取消」，构建置为 `cancelled`，已生成的版本保留。重启恢复不改变已取消的构建。
- 旧任务的步骤执行：`POST /api/studio/jobs/{job}/cancel`，执行状态不是 running 时返回 409；worker 在步骤之间检查哨兵 `steps.cancel`，当前步骤完成后停止，执行状态记为 `cancelled`。`cancel_build` 省略 `build_id` 且任务是旧任务时走这条路径。

## 能力范围与缺口

共 148 个工具，其中 144 个导出到 MCP（`load_tools`、`ask_user_question`、`navigate` 与会在本机启动进程的 `save_stdio_tool_source` 不导出）。所有工具都经进程内回环调用既有 REST 接口，校验、锁与错误信息与页面操作完全一致。

### meta

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `load_tools` | read | 否 | 按领域加载工具，下一步起可用 |
| `ask_user_question` | read | 否 | 向用户提问或用表单收集信息；密钥用 secret 字段，模型只拿到 `secret://` 引用 |
| `report_capability_gap` | read | 是（始终） | 记录现有能力做不到的需求 |
| `navigate` | read | 否 | 在浏览器里切页并定位，不修改数据 |

### studio

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `list_jobs` | read | 是 | 列出创作任务 |
| `get_workspace_summary` | read | 是 | 工作台：方案、参数、revision、文案、分镜摘要、成片与最近构建 |
| `resolve_shots` | read | 是 | 把短 ID、完整 ID 或 #序号解析为分镜 ID，有歧义时返回候选 |
| `get_shot_details` | read | 是 | 分镜完整详情（旁白、查询、画面意象、召回设置、画面来源、各阶段状态、字幕时间轴；aigc 分镜附生成状态与生成画面媒体；aigc_image 分镜附生成图片版本；背景音乐提示点附提示点与生成版本，顺延分镜说明顺延自哪一镜；animation 分镜附动画源码版本、沙箱预览地址与录制画面） |
| `get_preflight` | read | 是 | 构建预检：重建、复用、阻塞、跳过的节点 |
| `get_build_status` | read | 是 | 构建状态、失败或阻塞的节点、最近执行日志 |
| `list_versions` | read | 是 | 产物版本历史，标出当前选用版本 |
| `list_script_templates` | read | 是 | 文案模板（内置与用户模板）的 ID、名称、分类、类型与说明，可附完整提示词 |
| `get_script_template` | read | 是 | 单个文案模板的完整信息与完整提示词 |
| `create_job` | write | 是 | 新建创作任务（视频大纲） |
| `create_plan` | write | 是 | 新建方案（可复制或从成片恢复）并设为当前 |
| `activate_plan` | write | 是 | 切换当前方案 |
| `update_plan_options` | write | 是 | 修改方案参数（含文案模板 `template_id`、画幅 `canvas` 与适配 `fit`）或名称；换模板不重写已有文案，换画幅提示重建渲染与成片 |
| `edit_script` | write | 是 | 用完整新文案替换方案文案 |
| `edit_shots` | write | 是 | 批量 update/insert/delete/move/merge/split 分镜，可设置或 `clear` 召回设置、粒度、数量与画面来源 `source`（recall / clip / image / aigc_image / aigc / animation；aigc_image 可设 `prompt`、主体参考图 `reference_id`、`seed`、`prompt_optimizer`、`motion`、`strength`；aigc 可设 `prompt`、`negative`、`seed`、首帧 `image_id`，以及 MiniMax H3 的 `mode`（text/first_frame/last_frame/first_last/reference）、`last_image_id`、`references`、`model`、`resolution`、`ratio`、`duration`；animation 可设画面说明 `brief`），以及背景音乐提示点 `music`（generate/cover/none，`prompt`、`lyrics`、`instrumental`、`audio_id`、`volume`；之后的分镜顺延）、转场 `transition`（`enter`/`leave` 镜内转场 fade/white/zoom/slide，`next` 到下一镜的重叠转场）、字幕高亮词 `highlights` 与文字层 `overlays`（title/lower_third/data_card/custom，`engine` 为 ass 或 html）；`clear` 可恢复硬切、清空高亮词与文字层 |
| `rename_job` | write | 是 | 修改任务标题 |
| `attach_query_media` | write | 是 | 把图片或视频附件设为分镜的召回查询（上传 + 改召回，一次确认）；`purpose=source` 时上传为画面文件并把分镜来源改为上传片段（视频）或静态图片（图片）；分镜已是 aigc 来源时按 `role` 作为首帧（缺省）、尾帧或主体参考（图片/视频/音频），aigc_image 来源时图片作为主体参考图；`role=cover` 的音频附件作为背景音乐翻唱参考并设 cover 提示点（同样一次确认） |
| `add_reference_text` | write | 是 | 为方案登记参考文稿（文本附件或粘贴正文）；预览显示名称与字数，不自动重写文案 |
| `remove_reference_text` | danger | 是 | 移除方案登记的参考文稿（只删登记，文件保留） |
| `select_version` | write | 是 | 选用历史版本作为当前方案的产物 |
| `copy_script_template` | write | 是 | 把内置或用户模板复制为新的用户模板；预览显示来源模板与新名称 |
| `update_script_template` | write | 是 | 修改用户模板（只提交给出的字段）；预览列出变化字段的前后对比（提示词截断） |
| `cancel_build` | write | 是 | 取消运行中的构建或旧任务步骤执行 |
| `run_build` | danger | 是 | 按预检清单后台构建成片 |
| `run_node` | danger | 是 | 执行单个节点；`stage=draw` 只用于 aigc_image 分镜（调用图片生成服务），`stage=music` 只用于背景音乐提示点分镜（调用音乐生成服务），`stage=generate` 只用于 aigc 分镜，预览提示调用视频生成服务可能产生费用；`stage=animate` 只用于 animation 分镜（force=true 让动画生成模型重写并替换源码，含人工修改；false 时保留可复用或人工源码），animation 分镜的 `stage=render` 即重新录制 |
| `delete_job` | danger | 是 | 永久删除创作任务及其全部产物 |
| `delete_plan` | danger | 是 | 删除方案记录（版本与文件保留） |
| `delete_script_template` | danger | 是 | 删除用户模板记录；内置模板 403、被方案引用 409，原因原样转述 |

`edit_shots` 的合并规则与创作页相同：旁白拼接、查询以「；」连接、时长相加，保留前一镜的召回设置（前一镜为默认文字召回时继承被合并镜）；只能合并相邻分镜。修改召回设置时预览提示该分镜的召回、选片、渲染及成片会在下次构建时重建；服务端拒绝（人物不存在、选择了作品、素材超出任务范围、查询文件缺失）的中文原因原样转述。修改画面来源（`source`，见 [分镜画面来源](studio.md#分镜画面来源)）时预览按新来源说明要重建的阶段：素材召回为召回、选片、渲染及成片，上传片段为片段选窗、渲染及成片，静态图片为渲染及成片，AIGC 生成为生成画面、选窗、渲染及成片（并提示会调用视频生成服务、可能产生费用），AI 动画为生成动画、录制渲染及成片；配音不受影响。`get_shot_details` 输出画面来源；aigc 分镜另输出生成状态（版本、时长、提示词或失败原因），并以媒体附上当前生成画面（`list_versions(stage=generate)` 同样附带）。`run_build` 预检中有要重建的生成画面节点时，预览提示将调用视频生成服务的分镜数。未激活视频生成档案时预检阻塞，原因原样转述。animation 分镜另输出动画源码状态（版本、模型生成或人工修改、字符数、时长与画布、沙箱预览地址、录制是否基于当前源码），并以媒体附上当前渲染的录制画面；`run_build` 预检中有要重建的生成动画节点时，预览提示将调用动画生成模型的分镜数。

文案模板工具（`agent/tools/studio_templates.py`，见 [文案模板](studio.md#文案模板)）：内置模板只读，要定制先 `copy_script_template`；`update_script_template` 修改类型、提示词或字数上限后，选用该模板的方案在创作页显示「模板已变化」，已有文案不会自动重写，需要时用 `run_node(stage=script)` 重新生成；只改名称、分类、说明不影响已有文案。模板改动发布 `{domain: "studio", templates: true}`，创作页据此重新读取模板目录。

### studio_detail

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `diagnose_plan` | read | 是 | 方案诊断：分镜检查、字幕滤镜与字体、已选节点状态与变化原因、预检摘要 |
| `get_version_lineage` | read | 是 | 产物版本的上游依赖链 |
| `suggest_shot_query` | read | 是 | 按画面意象重写召回词，只返回建议 |
| `get_material_digest` | read | 是 | 任务的可用素材摘要 |
| `preview_version` | read | 是 | 预览选片或渲染版本 |
| `get_selection_candidates` | read | 是 | 人工选片前查看召回候选、锚点、配音时长与就绪状态 |
| `preview_selection` | read | 是 | 生成选片预览与 preview_token（候选窗口或任意区间），不改选片 |
| `confirm_selection` | write | 是 | 确认选片预览，生成并选用新选片版本 |
| `run_column` | danger | 是 | 整列执行召回/配音/背景音乐/生成图片/生成画面/生成动画/选片/渲染（`force` 为整列重做；背景音乐、生成图片、生成画面与生成动画只绑定对应的分镜），并跟踪构建 |
| `generate_lyrics` | write | 是 | 调用音乐生成服务的歌词生成（`write_full_song` 或 `edit`），只返回歌名、风格标签与歌词，不改分镜；预览提示可能产生费用，ComfyUI 音乐档案不支持 |
| `get_animation_source` | read | 是 | 读取 AI 动画分镜的 HTML/JS 源码（按字符分页 `offset`/`limit`，单次 ≤10000 字符）、版本与来源（模型生成/人工修改）、时长与画布、沙箱预览地址与录制画面 |
| `edit_animation_source` | write | 是 | 修改 AI 动画源码：整份替换 `html` 或查找替换 `replacements`（每个 `find` 须恰好出现一次）；预览先以 `dry_run` 做服务端静态校验（未通过直接报告违规项，不出确认卡），显示源码大小前后对比与校验结果；保存为人工版本并选用，只重建该镜渲染与成片，之后的构建不会覆盖 |

### studio_post

成片后期工具与 Web 页面（创作页分镜详情的「后期」面板、`/studio/brands` 品牌模板页、方案参数）、`mindex studio` CLI 与 MCP 共用同一组 REST 接口，Web 与桌面版行为一致。转场、字幕高亮词与文字层是分镜字段，用 `studio` 领域的 `edit_shots` 设置（`transition`、`highlights`、`overlays`）；字幕样式、品牌与节拍对齐用 `update_plan_options`（`subtitle_style`、`brand_id`、`brand_overrides`、`beat_align`）；曲库选曲用 `edit_shots` 的 `music={kind: library, track_id}`，第三方工具画面用 `edit_shots` 的 `source={kind: tool, tool_id, tool, arguments}`。

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `list_brands` | read | 是 | 列出品牌模板：内置（简洁、新闻资讯、科技评测）与用户模板，是否有片头/片尾/水印 |
| `get_brand` | read | 是 | 读取品牌模板的字幕样式、文字层样式、片头、片尾与水印 |
| `copy_brand` | write | 是 | 复制为用户模板（内置模板只读） |
| `update_brand` | write | 是 | 修改用户模板，只传要改的字段；影响成片的字段传 null 去掉；使用它的方案下次构建只重新合成成片 |
| `delete_brand` | danger | 是 | 删除用户模板（只删记录）；被方案引用时拒绝（409） |
| `upload_brand_asset` | write | 是 | 上传品牌素材（水印图片或 ≤30 秒的片头片尾视频），返回可引用的 `asset` |
| `get_overlay_source` | read | 是 | 读取分镜 HTML 文字层（`engine=html`）的透明叠加层源码（按层序号，分页） |
| `set_exports` | write | 是 | 设置多画幅导出（整体替换）：derive 快速派生；rerender 维护关联子方案，用 `run_build(all_canvases=true)` 一起构建 |
| `list_platforms` | read | 是 | 平台发布预设：画幅、封面尺寸、标题/简介/标签/章节限制 |
| `set_publish` | write | 是 | 设置发布平台与封面（成片挑帧、AI 生成或上传，标题与副标题）；平台画幅缺导出时提示 |
| `cover_candidates` | read | 是 | 某平台成片的封面候选帧（按清晰度），附缩略图 |
| `get_publish_metadata` | read | 是 | 读取某平台的标题、简介（含章节与署名）、标签与校验修正说明 |
| `edit_publish_metadata` | write | 是 | 人工修改标题、简介正文与标签，按平台限制修正；保存后钉住 |
| `build_publish_package` | danger | 是 | 构建发布包（含导出、元数据、封面），有重新渲染导出时构建全部画幅，后台执行并跟踪 |
| `edit_overlay_source` | write | 是 | 整份替换某层叠加层源码；预览先做安全检查，保存后版本被钉住，只重建该镜渲染与成片 |
| `list_music_tracks` | read | 是 | 列出曲库曲目（BPM、情绪、授权是否过期），可按情绪或关键词筛选 |
| `get_music_track` | read | 是 | 读取单首曲目：授权、署名、节奏分析与情绪标签 |
| `import_music_track` | write | 是 | 把音频附件导入曲库（授权来源、许可、署名必填），自动分析并打情绪标签；相同音频去重 |
| `update_music_track` | write | 是 | 修改曲目的授权信息、标签或情绪标签 |
| `analyze_music_track` | write | 是 | 重新分析节奏，可选重新打情绪标签 |
| `recommend_music` | read | 是 | 按段落文案或提示点分镜推荐曲目（情绪、BPM、时长与理由） |
| `list_tool_sources` | read | 是 | 列出第三方工具源（协议、白名单工具、产物类型、上传许可、缺少的密钥） |
| `test_tool_source` | write | 是 | 测试工具源连接并列出服务端工具（不提交任务；mcp-stdio 会启动本机进程） |
| `save_tool_source` | danger | 是 | 登记或整体替换 http / mcp-http 工具源；密钥只接受 `secret://` 引用 |
| `save_stdio_tool_source` | danger | 否 | 登记本机 MCP 服务器（mcp-stdio，绝对路径命令）；外部 MCP 客户端不能让 Mindex 执行任意命令 |

### library

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `list_sources` | read | 是 | 列出素材 |
| `get_source` | read | 是 | 素材信息与分镜、对白、分析、人物四项工作的状态、最近任务、可发布性 |
| `list_segments` | read | 是 | 分页列出镜头或精细片段，附缩略图 |
| `get_segment_details` | read | 是 | 片段的 ASR、字幕、LLM 分析与人物结果（长文本截断） |
| `get_source_report` | read | 是 | 素材报告：计划镜头数、时长统计、缺失文件、布局一致性、运行记录 |
| `get_task_status` | read | 是 | 查询素材、对白或人物任务 |
| `update_curation` | write | 是 | 修改显示名、备注、收藏、隐藏（只提交给出的字段） |
| `retry_task` | write | 是 | 从断点重试失败、中断、已取消或发布失败的任务 |
| `cancel_task` | write | 是 | 取消排队中或运行中的任务 |
| `upload_source` | write | 是 | 把视频附件登记为新素材（不分镜） |
| `upload_subtitle` | write | 是 | 上传字幕附件，返回字幕 ID |
| `start_source_task` | danger | 是 | 发起 shots/analysis/dialogue/persons/publish 任务，选项原样透传，提交后跟踪 |

不提供删除素材的工具。

### search

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `search_media` | read | 是 | 文字和/或人物检索，可指定粒度、数量、素材、人物匹配方式、渠道与筛选；如实说明 `degraded`/`warnings` |
| `search_by_media` | read | 是 | 以图或视频附件检索（类型从内容类型推断或显式指定） |
| `find_similar` | read | 是 | 用库内片段找画面相似的片段，排除种子及同源重叠片段 |
| `sample_frames` | read | 是 | 在素材区间或片段内均匀抽 1–12 帧 |
| `get_system_status` | read | 是 | 活动索引、渠道覆盖、素材与片段数；可选检查嵌入身份是否匹配 |

### index

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `list_index_versions` | read | 是 | 内容索引版本与当前检索组合 |
| `check_index` | read | 是 | 一致性检查（省略 ID 检查活动索引） |
| `activate_index` | danger | 是 | 发布或回退到指定索引版本；预览列出模式、格式、空间是否匹配和将切换的检索组合 |

### persons

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `list_persons` | read | 是 | 列出演员、角色与作品 |
| `get_person` | read | 是 | 实体详情：别名、关系、参考图、关联镜头数与示例 |
| `identify_person` | read | 是 | 识别图片附件中的人，按角色与演员给出候选 |
| `list_review_shots` | read | 是 | 出镜审核分镜列表（按素材、状态、演员·角色、无候选筛选） |
| `list_search_releases` | read | 是 | 最近的检索组合与当前生效组合 |
| `save_person` | write | 是 | 新建或修改演员、角色、作品 |
| `assign_source_work` | write | 是 | 设置素材所属作品（会清除已有人物证据） |
| `add_person_reference` | write | 是 | 用附件或素材截图（可裁剪）添加参考图，会质检 |
| `review_shot` | write | 是 | 审核一个分镜；每项必须带角色，空列表表示整镜排除 |
| `import_cast` | write | 是 | 用 1–12 张演员表截图创建导入草稿并自动拆分 |
| `confirm_cast_import` | write | 是 | 确认演员表草稿导入到作品 |
| `delete_person` | danger | 是 | 删除演员或角色（作品不可删除；有关联镜头时被拒） |
| `remove_person_reference` | danger | 是 | 移除一张参考图（只删记录） |
| `publish_persons` | danger | 是 | 发布人物检索并切换当前生效的检索组合 |
| `change_search_release` | danger | 是 | 激活或删除检索组合（当前生效组合不可删除） |

不提供删除演员表导入图片的工具。

### settings

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `get_settings` | read | 是 | 各角色生效配置、档案（密钥掩码）与业务设置 |
| `list_provider_presets` | read | 是 | 厂商预设 |
| `list_voices` | read | 是 | 音色库、默认音色与语音合成模型 |
| `list_voice_catalog` | read | 是 | 当前语音合成后端的音色目录、音色库、默认音色与是否支持克隆 |
| `test_model_profile` | read | 是 | 测试档案连通性（视频/图片/音乐生成档案只做鉴权/可达性探测，不发起生成；动画生成档案只发一次最小对话） |
| `probe_model_deployment` | read | 是 | e2e probe：只读探测 llama_cpp 嵌入/ASR/多模态理解档案的服务，报告量化、维度与能力，自动定位本机 GGUF/mmproj，列出与当前部署一致、可复用的部署清单 |
| `pin_model_deployment` | write | 是 | e2e pin：优先复用一致的旧清单，否则计算 GGUF/mmproj SHA256 生成新 schema 2 清单（`data/deployments/`，不覆盖），并写入档案的 `deployment_manifest`；本机服务从监听进程命令行与工作目录自动定位文件，远程服务需给出路径或 SHA256 声明；嵌入档案返回 smoke 门槛状态 |
| `create_model_profile` | write | 是 | 新建档案（不自动激活；`api_key` 只能是 `secret://` 引用）；`video_generator`（backend 含 `minimax_h3`）、`image_generator`、`music_generator`（可带 `cover_model`）角色都支持 ComfyUI 工作流与映射；`video_generator` 角色可带 `durations`、`poll_interval`、`max_wait`、`fps`、`comfyui_workflow`、`comfyui_mapping`，ComfyUI 工作流与映射先在本地校验并给出中文错误；`animator` 角色可带 `max_tokens`、`retries`、`max_html_kb`、`max_capture_seconds`（激活即启用动画生成） |
| `update_business_settings` | write | 是 | 修改 GPU 互斥、检索权重、默认音色 |
| `register_voice` | write | 是 | 把已有厂商音色登记到音色库 |
| `update_model_profile` | danger | 是 | 修改档案（不能修改助手自身档案） |
| `activate_model_profile` | danger | 是 | 激活档案，立即热生效（不能切换助手自身档案） |
| `delete_model_profile` | danger | 是 | 删除档案（不能删除正在使用的档案或助手自身档案） |
| `reset_business_settings` | danger | 是 | 清除页面保存的业务设置覆盖 |
| `delete_voice` | danger | 是 | 删除音色记录；是默认音色时一并清除默认覆盖 |
| `clone_voice` | danger | 是 | 克隆音色（授权只来自页面表单，见下） |
| `list_local_services` | read | 是 | 本机模型服务（配置文件 `local_services` 定义）：运行状态、健康、端口与自启设置 |
| `start_local_service` | danger | 是 | 启动配置文件中定义的本机服务（不能改命令）；会占用本机内存/显存 |
| `stop_local_service` | danger | 是 | 停止由本应用启动的本机服务；正在使用它的任务会失败 |

`clone_voice` 在确认卡之后再弹出一张表单：音色名称、参考文本（可选）和声音授权确认项（dashscope 后端另有样本 URL）。只有你在表单中选择「我确认拥有该声音的合法使用授权」时，才以 `consent=true` 调用克隆接口；工具参数里没有授权字段，模型无法代为授权。openai/minimax 后端使用音频附件，dashscope 后端使用公网 URL。拒绝、取消表单或没有提问通道（MCP 调用）时不调用接口并说明原因。

现有能力做不到的需求（例如「调整拆分镜粒度」——粒度目前写死在拆分镜提示词里），助手会如实说明、给出替代做法，并记录到能力缺口列表：`GET /api/agent/gaps`。

### topics

参数名带 `topic_` 前缀（`topic_profile_id`、`topic_source_id`），与设置页模型档案的 `profile_id`、素材的 `source_id` 区分。改动事件为 `{domain: "topics", profile_id}`，选题页据此刷新；定位为 `{page: "topics", profile_id, card_id, section}`，建出创作任务后定位到创作页。

| 工具 | 风险 | MCP | 说明 |
|---|---|---|---|
| `list_topic_profiles` | read | 是 | 领域档案：领域、子方向、每日定时 |
| `save_topic_profile` | write | 是 | 新建或修改档案（受众、定位、禁区、关键词、权重、定时等） |
| `delete_topic_profile` | danger | 是 | 删除档案及其数据源、选题、采集清单与挖掘记录（只删记录） |
| `list_topic_sources` | read | 是 | 数据源：类型、状态、最近抓取与错误、密钥掩码 |
| `save_topic_source` | write | 是 | 新建或修改数据源（rss/hotlist/brave/calendar/local；密钥只接受 `secret://` 引用或明文，保存后只显示掩码） |
| `delete_topic_source` | danger | 是 | 删除数据源记录 |
| `test_topic_source` | read | 是 | 试抓并返回前 20 条，不入库 |
| `import_topic_calendar` | write | 是 | 为日历来源导入 ICS/CSV 文本或内置样例 |
| `fetch_topic_items` | write | 是 | 立即抓取并入库去重 |
| `list_topic_items` | read | 是 | 最近的候选条目 |
| `discover_topics` | danger | 是 | 开始挖掘（后台任务，调用嵌入服务与模型）；结束时 followup |
| `get_topic_run` | read | 是 | 挖掘任务的状态、进度、产出与告警 |
| `manage_topic_run` | write | 是 | 取消或重试挖掘任务 |
| `list_topics` | read | 是 | 按总分列出选题卡片 |
| `get_topic` | read | 是 | 卡片详情：分项分数、关键画面、大纲、推荐模板与来源条目 |
| `score_topic` | write | 是 | 重新打分（受众匹配调用模型，覆盖按当前索引检索） |
| `topic_coverage` | read | 是 | 素材覆盖报告：命中片段（附缩略图）与缺口 |
| `draft_topic_outline` | write | 是 | 生成或重新生成大纲与推荐模板 |
| `update_topic` | write | 是 | 修改标题、大纲、推荐模板，或忽略/恢复 |
| `create_job_from_topic` | write | 是 | 从选题建创作任务（模板、画幅、来源资料与档案快照预先设好） |
| `get_job_profile` | read | 是 | 任务的账号档案快照与是否已过期 |
| `sync_job_profile` | write | 是 | 用当前档案更新任务快照（文案只提示可重新生成） |
| `topic_gaps` | read | 是 | 素材采集清单 |
| `update_topic_gap` | write | 是 | 标记缺口已采集或重新打开 |
| `get_topic_schedule` | read | 是 | 每日定时：下次运行、今日运行与产出 |

## API

| 方法与路径 | 用途 |
|---|---|
| `GET /api/agent/status` | 是否启用、模型与后端 |
| `GET/POST /api/agent/sessions`、`GET/DELETE /api/agent/sessions/{id}` | 会话列表、新建、详情（含待答复的确认与提问）、删除（连同会话附件） |
| `POST /api/agent/sessions/{id}/messages` | `{content, context, attachments}`；`attachments` 为本会话附件引用（最多 12 个，归属不符返回 422）；会话忙时返回 409 |
| `GET /api/agent/sessions/{id}/events?after=<seq>` | SSE：`session.event`（持久事件）、`assistant.stream`（流式块）、`session.status`、`ping`；断线用最后的 seq 续传 |
| `GET /api/agent/sessions/{id}/log?after=<seq>` | 同样的持久事件，JSON 形式 |
| `POST /api/agent/sessions/{id}/approvals/{requestId}` | `{outcome: allowed-once\|rejected\|cancelled\|unavailable, grant}` |
| `POST /api/agent/sessions/{id}/questions/{requestId}` | `{answers: [{id, selected[], custom?, values?}]}`；secret 字段的值在这里换成引用 |
| `POST /api/agent/sessions/{id}/cancel` | 停止当前轮次 |
| `POST /api/agent/attachments` | multipart `file` + 可选 `session_id`；201 返回 `{id: "attachment://…", filename, content_type, bytes, sha256, expires_at, …}` |
| `GET /api/agent/attachments/{token}`、`GET /api/agent/attachments/{token}/content` | 附件元数据、内容；只持 MCP 凭证时只能访问 mcp 附件 |
| `DELETE /api/agent/attachments/{token}` | 撤销暂存（面板移除附件用，不对应工具） |
| `GET /api/agent/gaps` | 能力缺口记录 |
| `POST /api/agent/gaps` | `{request, reason, suggestion}`：远程 MCP 服务记录能力缺口，不关联会话 |
| `GET /api/agent/changes?after=<seq>` | SSE 全局改动与定位通道：先发 `ready{seq}`，之后每条为 `change{seq, time, kind: mutation\|focus, source: agent\|mcp, session_id, tab, data}`；`after=-1`（默认）从最新开始、不回放 |
| `GET /api/agent/changes/log?after=<seq>` | 同样的记录，JSON 形式（调试用） |
| `POST /api/agent/changes` | `{kind, data}`：独立进程的 MCP 服务（`mindex mcp --base-url`）报告改动，来源记为 `mcp` |

为助手补齐的业务接口（页面与 CLI 同样可用）：

| 方法与路径 | 用途 | 主要错误 |
|---|---|---|
| `POST /api/search/similar` | `{segment_id, granularity?, limit 1–50, filters}`：用种子片段的全部画面向量检索并等权 RRF 融合，结果结构同 `/api/search/text`，另带 `seed_segment_id`；只用库内向量，嵌入服务离线也可用 | 种子不在当前索引或未完成 404；种子无画面表示 422；画面向量缺失 409 |
| `GET /api/admin/sources/{id}/frame?t=&width=768` | 该时间点的 JPEG 帧，宽度最大 1024；带 ETag，`If-None-Match` 命中返回 304 | `t` 非有限数或超出时长 422；素材不存在 404 |
| `GET /api/admin/sources/{id}/report` | 素材报告（线程池生成） | 404 |
| `GET /api/indexes` | 全部内容索引版本（新到旧）与当前活动版本、检索组合 | — |
| `GET /api/indexes/{id}/check` | 一致性检查 `{index_id, ok, issues}` | — |
| `POST /api/indexes/{id}/activate` | 持处理锁与索引写锁，依次检查一致性、嵌入空间与能力门槛、视频预处理，全部通过才激活，并在后台预热检索 | 检查有问题 422；空间或预处理不符 503；锁超时 409；以上都不改变活动索引 |
| `POST /api/persons/identify` | multipart `file`（≤20 MiB）+ `top_k`（默认 5）：每张人脸按角色和按演员给出前 k 个候选，并返回未缓存特征的参考图数 | 人物服务不可用或空间不符 503 |
| `POST /api/admin/source-tasks/{id}/cancel`、`/api/admin/dialogue-tasks/{id}/cancel`、`/api/persons/tasks/{id}/cancel` | 取消后台任务 | 非 queued/running 409 |
| `POST /api/studio/jobs/{job}/builds/{bid}/cancel` | 取消运行中的构建 | 非 running 409 |
| `POST /api/studio/jobs/{job}/cancel` | 取消旧任务的步骤执行 | 非 running 409 |
| `DELETE /api/studio/jobs/{job}/plans/{pid}` | 删除方案记录 | 有构建执行 409；唯一方案 422 |
| `GET /api/studio/jobs/{job}/plans/{pid}/diagnostics` | 方案诊断（只读、不加锁） | 404 |

后台任务的 `status` 新增 `cancelled`，创作构建新增 `cancelled`，旧任务 `execution.status` 新增 `cancelled`。

## 配置

`config.local.yaml` 的 `agent` 段（完整注释见 `config.example.yaml`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 关闭后面板接口返回 503 |
| `context_window_steps` | `256` | 模型输入滚动保留最近的操作步骤（1–256），每步含该次模型回复及全部工具调用结果；执行步数不限 |
| `max_context_tokens` | `60000` | 历史估算上限；超出时先省略较早的工具结果，再丢弃最早的轮次 |
| `secret_ttl_seconds` | `1800` | 密钥引用有效期 |
| `attachment_ttl_hours` | `168` | 附件有效期，过期后由 `mindex clean` 回收 |
| `attachment_max_mb` | `500` | 单个附件上限（MiB，1–20480），超出返回 413 |
| `max_images` | `8` | 单次模型请求最多携带的图片数（0–32），只保留最近的 |
| `llm.backend` | `openai` | `openai`（OpenAI 兼容 tools 协议）或 `anthropic`（原生 Messages 协议） |
| `llm.base_url` / `llm.model` / `llm.api_key_env` / `llm.timeout` / `llm.max_tokens` | DeepSeek | 设置页「智能助手」档案可覆盖 |
| `llm.vision` | `false` | 模型是否接受图片输入；开启后附件图片与工具抽取的画面会发给模型（最多 `max_images` 张）。设置页「智能助手」档案可覆盖，保存后热生效 |
| `mcp.http_enabled` | `false` | 挂载 streamable-http `/mcp` |
| `mcp.policy` | `read` | MCP 调用方允许的最高风险级别：`read`、`write`、`all` |
| `mcp.token_env` | `MINDEX_MCP_TOKEN` | `/mcp` 与附件端点的 Bearer 令牌所在环境变量；开启 HTTP 但未设置令牌时服务拒绝启动 |
| `mcp.domains` | `null` | MCP 导出的领域；`null` 导出全部，列表只导出这些领域（另加 `report_capability_gap`）。只接受 `studio`、`studio_detail`、`studio_post`、`library`、`search`、`index`、`persons`、`settings`、`topics`，空列表或未知领域校验失败 |

单轮不再因步数耗尽而暂停；助手持续执行到完成、取消或出错，需要确认或提问时仍会等待答复。超过操作窗口时，较早步骤及其工具结果整体移出模型输入，保留窗口起点所属的用户指令和最近页面上下文；`max_context_tokens` 仍可进一步缩减历史。完整事件日志保留，面板回放与 SSE 续传不受滚动窗口影响。旧配置中的 `max_steps` 会被忽略，无需手动删除；`GET /api/agent/status` 返回 `max_steps: null` 和当前 `context_window_steps`。

会话（含已加载领域）、密钥保险库、附件记录与能力缺口存放在 `data_dir/agent.sqlite3`，附件文件在 `data_dir/agent-attachments/`，都属于本机运行状态，不进入 `mindex backup`。

## MCP 导出与外部 harness

安装依赖：`uv sync --extra mcp`（开发环境的 `--extra dev` 已包含）。服务名 `mindex`。导出规则：

- `load_tools`、`navigate`、`ask_user_question` 从不导出；MCP 不做动态加载，直接导出所选领域的全部工具。
- `agent.mcp.domains` 为 `null` 时导出全部 144 个工具；为领域列表时只导出这些领域，`report_capability_gap` 始终导出。
- `tools/list` 与 `tools/call` 每次重新读取配置，修改 `agent.mcp.domains` 无需重启（进程内 stdio 与 `/mcp`）。调用不在导出范围内的工具返回 `isError`，说明该工具未导出。
- `mindex mcp --domain <领域>` 可重复指定，覆盖配置值；出现未知领域时启动失败并列出可选领域。远程模式（`--base-url`）在启动时读取一次本地配置的 `agent.mcp.domains`。
- read 工具标注 `readOnlyHint`，danger 工具标注 `destructiveHint`。

外部 harness 不一定支持确认交互（deepseek-harness 不读取注解，也不支持 elicitation），所以由服务端按 `agent.mcp.policy` 强制执行：超出策略的调用返回 `isError`，提示到 Mindex 页面操作。其他差异：

- 成功的 MCP 调用同样发布改动与定位，打开的 Mindex 页面会刷新并做页内高亮。
- MCP 调用方没有密钥保险库，需要密钥的档案请改用 `api_key_env` 或在设置页填写。
- 没有提问通道：`clone_voice` 被拒绝，提示需要在 Mindex 页面助手中确认授权。
- 不自动跟踪任务：提交构建或后台任务后，结果文本提示改用 `get_build_status` 或 `get_task_status(kind=…)` 查询。
- 附件只能使用 owner=mcp 的附件，上传方式见[附件](#附件)。
- 自身档案保护与内置助手一致。远程模式下由 `RemoteAgentProfile` 调用远端 `GET /api/settings?probe_embedding=false` 解析智能助手当前激活档案（缓存 10 秒），拒绝修改、激活或删除它；`report_capability_gap` 经远端 `POST /api/agent/gaps` 记录。

两种传输：

- stdio：`mindex mcp`（本进程装配应用）或 `mindex mcp --base-url http://127.0.0.1:8000`（连接已运行的服务，令牌读 `MINDEX_WEB_TOKEN`）；`--policy` 覆盖 `agent.mcp.policy`，`--domain` 覆盖 `agent.mcp.domains`。
- streamable-http：`agent.mcp.http_enabled: true`，设置 `MINDEX_MCP_TOKEN`，端点 `http://<host>:8000/mcp`。

deepseek-harness（`@deepseek-ai/dsh-mcp-client`，工具出现为 `mcp__mindex__<tool>`）：

```yaml
- id: mcp-mindex
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: mindex
    transport: streamable-http
    url: http://127.0.0.1:8000/mcp
    headers:
      Authorization: !!js '`Bearer ${process.env.MINDEX_MCP_TOKEN}`'
    toolCallTimeoutMs: 120000

# 或 stdio（只导出检索与素材两个领域）：
- id: mcp-mindex-stdio
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: mindex
    transport: stdio
    command: uv
    args: ['run', '--project', '/path/to/mindex', 'mindex', 'mcp', '--policy', 'write',
           '--domain', 'search', '--domain', 'library']
```

Claude Code：

```bash
claude mcp add mindex -- uv run --project /path/to/mindex mindex mcp
claude mcp add --transport http mindex-http http://127.0.0.1:8000/mcp --header "Authorization: Bearer $MINDEX_MCP_TOKEN"
```

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.mindex]
command = "uv"
args = ["run", "--project", "/path/to/mindex", "mindex", "mcp"]
```

## 与 deepseek-harness 的兼容设计

内置运行时的数据形状逐字沿用 dsh，未来可以直接换成 dsh 运行时，前端与工具都不用改：

- 会话事件信封 `{session_id, seq, time, type, data, ignorable?}`，核心事件 `turn/start`、`turn/end{reason.kind}`、`step/start|end`、`user/message{source.kind: user|context|followup}`、`assistant/message`、`tool/call{callId, name, arguments: 原始 JSON 字符串}`、`tool/result{message, error?, meta?}`；Mindex 扩展事件一律为 `plugin:mindex/...`。发给模型的历史只从日志派生。
- 内容块 `text` / `reasoning` / `tool-call`，以及只含引用的 `image`（`{type: "image", ref}`，发请求前才解析）；SSE 流式块沿用 dsh StreamChunk（`block-start`、`text-delta`、`tool-call-delta`、`block-end`、`usage`、`finish`），用量字段 `inputTokens` / `outputTokens` / `cacheReadTokens`。
- 确认结果 `allowed-once | rejected | cancelled | unavailable`；提问工具 `ask_user_question` 的参数与结果是 dsh 同名工具的超集。
- `AgentRuntime` 协议（`backend/src/agent/runtime.py`）与 dsh SDK 一一对应：`prompt` ↔ `session/prompt`，`create_or_resume` ↔ `session/resume`，`events`/`status` ↔ `session.event`/`session.status` 通知，`answer_approval` ↔ `approval/request` 的响应，`answer_question` ↔ `user-question/ask` 的响应。

规划中的 `DshRuntime`：在工作线程里运行 dsh Python SDK（`deepseek_harness.HarnessClient`，`approval_requests=True`、`capabilities.userQuestions=True`），用上面的 stdio 或 HTTP 配置把 Mindex 工具挂给 dsh；把 `session.event` 通知原样写入同一张事件表，把 `approval/request` 与 `user-question/ask` 转成面板现有的确认卡片和表单卡片。

## 验证

- `uv run pytest tests/test_agent_*.py`：协议适配、工具、运行时、确认、任务跟踪、密钥、MCP、动态加载、附件、视觉，以及 26 个确定性验收场景（`tests/agent_scenarios.py`，含跨领域加载、附件检索、找相似、索引回退、取消构建、取消素材任务、人物审核与发布、视觉输入、访谈改写、竖屏方案、附件作画面、AI 生成视频分镜、AI 生成图片分镜、背景音乐与歌词生成、AI 动画分镜生成与源码修改）。这些测试使用脚本化模型，只验证工程链路，不代表真实模型质量。
- 后端接口：`tests/test_search_similar.py`、`test_source_frame.py`、`test_index_api.py`、`test_task_cancel.py`、`test_studio_cancel.py`、`test_studio_diagnostics.py`、`test_person_identify.py`。
- `tests/test_agent_no_physical_delete.py`：用记录请求的客户端执行全部工具，确认不会请求物理删除接口，且每个 write/danger 工具都有预览。
- `tests/test_agent_changes.py`：改动与定位通道、各工具定位描述、`navigate`。
- `tests/test_agent_media.py`：各工具附带的媒体只含本站地址且真实可加载、折叠默认值。
- `tests/test_agent_panel_ui.py`：Playwright 驱动真实页面，覆盖跨页跳转后确认、页内定位、双标签刷新、外部改动、关闭跟随、导航、媒体卡片与大图浏览、宽度调整、附件（选择、拖放、移除、失败阻止发送、消息中的附件）与 Markdown（含 XSS 向量，`tests/agent_markdown_ui.cjs`）（需要 node 与 Playwright，可用 `MINDEX_PLAYWRIGHT_MODULE` 指定模块）。
- 真实模型评测：`scripts/agent_eval.py` 让真实模型跑同一组场景（素材、嵌入、配音、视频生成、动画生成与动画录制仍是测试专用适配器，不调用付费生成接口与真实浏览器），输出通过率与每个场景的事件日志：

```bash
MINDEX_AGENT_API_KEY=... uv run python scripts/agent_eval.py --backend openai \
  --base-url https://api.deepseek.com/v1 --model deepseek-chat
ANTHROPIC_API_KEY=... uv run python scripts/agent_eval.py --backend anthropic \
  --base-url https://api.anthropic.com --model claude-sonnet-4-5 --api-key-env ANTHROPIC_API_KEY
```

未经自动化验证的范围：真实模型的视觉效果、真实人物服务的识别质量、真实 GPU 任务的取消时延。
