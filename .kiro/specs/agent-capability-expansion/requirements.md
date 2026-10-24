# Requirements Document

智能助手能力扩展：创作深化、素材处理、检索增强、人物库、索引管理，以及动态加载、附件通道、视觉输入和全任务可取消

## Introduction

Mindex 主应用有 140 个 REST 端点，智能助手（`mindex.agent`）注册了 31 个工具，只用到其中 22 个端点。以下能力还没有交给助手：素材处理、人物库写操作、索引版本管理、人工选片、所有需要上传文件的操作。此外有三个结构性限制：

- 工具每一步都全量下发给模型，工具数量无法继续增长。
- 模型看不到图片。
- 只有部分后台任务可以中断。

本功能分为几部分：

1. 基础设施：按领域动态加载工具；附件通道 `attachment://`；模型视觉输入；所有构建类任务都能取消。
2. 新增和补齐若干 REST 接口：按片段找相似、区间抽帧、索引版本管理、素材报告、识别演员、创作诊断、取消接口、删除方案。
3. 把创作、素材、检索、索引、人物、设置六个领域的能力封装为工具。内置助手和 MCP 导出共用这些工具。

所有工具继续通过进程内回环调用既有 REST 接口，校验、锁和错误信息与页面操作一致。

范围之外：磁盘清理预览、全库批量处理。删除素材、删除演员表导入图片这两个会删除物理文件的接口不开放给助手。

## Glossary

- **Agent**：内置智能助手运行时（`BuiltinRuntime`，`backend/src/agent/runtime.py`）。
- **Tool_Registry**：工具注册表（`ToolRegistry` / `ToolSpec`，`backend/src/agent/registry.py`），内置助手与 MCP 导出共用。
- **Domain**：工具领域。取值为 `meta`、`studio`、`studio_detail`、`library`、`search`、`index`、`persons`、`settings`。
- **Loaded_Domains**：某个会话当前已加载的非 meta 领域，按最近使用排序，最多 3 个。
- **Page_Domain**：当前页面对应的领域，对应关系为：studio→studio，settings→settings，search（检索页 `/`）→search，admin→library，persons→persons。
- **MCP_Server**：把注册表导出为 MCP 工具的服务（`backend/src/agent/mcp_server.py`），支持进程内 stdio、远程 stdio（`--base-url`）、streamable-http `/mcp` 三种模式。
- **Loopback_Client**：回环客户端 `MindexClient`（`backend/src/agent/loopback.py`）。
- **Attachment**：通过 `POST /api/agent/attachments` 暂存的文件，用 `attachment://<token>` 引用。
- **Attachment_Owner**：附件归属。`session` 表示归属某个面板会话，`mcp` 表示由持 MCP 令牌的调用方上传。
- **Vision**：智能助手档案上的视觉输入开关（`agent.llm.vision`）。
- **Background_Task**：素材任务（`source_tasks`：shots / analysis / publish）、对白任务（`dialogue_tasks`）、人物任务（`person_tasks`）。
- **Studio_Build**：创作版本工作台中的一次构建（`workspace.builds[]`）；旧任务的步骤执行（`/steps`、`/run`）也归入此类。
- **Physical_Delete**：会删除磁盘文件的接口。本功能中指 `DELETE /api/admin/sources/{id}` 和 `DELETE /api/persons/imports/{id}/images/{image_id}`。`DELETE /api/studio/jobs/{id}`（`delete_job`）经用户确认保留。
- **Record_Delete**：只删除数据库记录、文件留给 `mindex clean` 回收的删除，包括人物实体、参考图、检索组合、模型档案、音色、方案。
- **Risk**：工具风险级别。`read` 直接执行；`write` 先预览、确认后执行，可按会话授权；`danger` 每次都要显式确认。

## Requirements

### Requirement 1: 按领域动态加载工具

**User Story:** 作为用户，我希望助手在工具变多以后仍然能准确选对工具，响应也不变慢。所以每一步只下发当前需要的工具，需要别的工具时再由助手自行展开。

#### Acceptance Criteria

1. THE Tool_Registry SHALL 接受 Domain 取值 `meta`、`studio`、`studio_detail`、`library`、`search`、`index`、`persons`、`settings`，并拒绝注册其他领域的工具。
2. WHEN Agent 为一个步骤构造模型请求时，THE Agent SHALL 只下发 meta 领域工具，加上该会话 Loaded_Domains 中各领域的工具。
3. WHEN 一轮对话开始且页面上下文能映射到某个 Page_Domain 时，THE Agent SHALL 把该 Page_Domain 加入 Loaded_Domains，并标记为最近使用。
4. WHEN Agent 成功执行了某个非 meta 领域的工具时，THE Agent SHALL 把该工具所在领域标记为最近使用。
5. WHEN 加入新领域后 Loaded_Domains 超过 3 个时，THE Agent SHALL 淘汰最久未使用的领域，使 Loaded_Domains 恰好剩 3 个。
6. THE Agent SHALL 把 Loaded_Domains 持久化到会话记录。同一会话的后续轮次以及服务重启后，SHALL 沿用已保存的 Loaded_Domains。
7. WHEN 模型调用 `load_tools` 并传入 1 至 3 个合法领域时，THE Agent SHALL 把这些领域加入 Loaded_Domains，返回加载后的领域列表和各领域工具名，并追加一条 ignorable 事件 `plugin:mindex/tools-loaded`。从下一步起，模型请求 SHALL 包含这些领域的工具。
8. IF `load_tools` 收到未知领域或 `meta`，THEN THE Agent SHALL 返回 `invalid_arguments` 错误，且不改变 Loaded_Domains。
9. IF 模型调用的工具在注册表里存在、但所在领域未加载，THEN THE Agent SHALL 不执行该工具，并返回 code 为 `TOOL_NOT_LOADED` 的可恢复错误，错误文本中写明应调用 `load_tools` 加载的领域名。
10. THE Agent SHALL 在系统提示词中列出每个非 meta 领域的一句说明、代表性工具名，以及当前是否已加载。

### Requirement 2: MCP 领域过滤与远程模式补齐

**User Story:** 作为外部 harness 的使用者，我希望能控制 MCP 导出哪些领域，并且远程模式下的安全保护与网页助手一致。

#### Acceptance Criteria

1. THE MCP_Server SHALL 读取配置 `agent.mcp.domains`。值为 `null` 时导出全部领域；值为领域列表时，只导出这些领域的工具，外加 `report_capability_gap`。
2. THE MCP_Server SHALL 始终不导出 `load_tools`、`navigate`、`ask_user_question`。
3. WHEN 运行中的 `agent.mcp.domains` 发生变化时，THE MCP_Server SHALL 在下一次 `tools/list` 与 `tools/call` 时按新值过滤，无需重启。
4. WHEN `mindex mcp` 带一个或多个 `--domain` 选项启动时，THE MCP_Server SHALL 用这些选项覆盖 `agent.mcp.domains`；IF 出现未知领域，THEN 启动 SHALL 失败并给出中文错误。
5. IF `tools/call` 请求的工具不在当前导出范围内，THEN THE MCP_Server SHALL 返回 `isError` 结果，说明该工具未导出。
6. WHILE 以远程 stdio 模式（`--base-url`）运行时，THE MCP_Server SHALL 通过远端设置接口解析智能助手当前激活档案，并拒绝修改或激活该档案，行为与进程内模式一致。
7. WHILE 以远程 stdio 模式运行时，THE MCP_Server SHALL 通过新增端点 `POST /api/agent/gaps` 记录能力缺口，使 `report_capability_gap` 可以正常使用。
8. THE 配置模型 SHALL 拒绝 `agent.mcp.domains` 中的未知领域；`config.example.yaml` SHALL 给出该键的注释示例。

### Requirement 3: 附件通道

**User Story:** 作为用户，我希望把图片、视频、字幕、音频样本交给助手，让它用来检索、登记素材、上传字幕、做参考图或克隆音色，不必自己切换到页面去上传。

#### Acceptance Criteria

1. WHEN 已鉴权调用方以 multipart 方式向 `POST /api/agent/attachments` 上传单个文件时，THE 系统 SHALL 把文件流式写入 `data_dir/agent-attachments/`，记录文件名、内容类型、字节数、SHA256、Attachment_Owner、可选会话 ID 和过期时间，并返回 `attachment://` 引用及上述元数据。
2. IF 上传文件超过 `agent.attachment_max_mb`，THEN THE 系统 SHALL 返回 413 并删除已写入的部分；IF 文件为空，THEN SHALL 返回 422。
3. THE 附件端点 SHALL 接受网页鉴权凭证（Cookie、Bearer `MINDEX_WEB_TOKEN`、进程内部凭证），这类上传的 Attachment_Owner 为 `session`，且必须带有效会话 ID。附件端点 SHALL 还接受 Bearer `MINDEX_MCP_TOKEN`（常量时间比较），这类上传的 Attachment_Owner 为 `mcp`。IF 已启用网页令牌或 MCP 令牌，但请求不带任何有效凭证，THEN SHALL 返回 401。
4. WHEN 工具通过 Loopback_Client 提交带附件引用的 multipart 请求时，THE Loopback_Client SHALL 按以下规则解析引用，并以文件字段提交给目标上传接口：
   - 面板会话的工具调用，只能使用归属本会话的 `session` 附件；
   - MCP 工具调用，只能使用 `mcp` 附件。
5. IF 附件引用不存在、已过期或不属于当前调用方，THEN THE Loopback_Client SHALL 返回 `{ok: false, status: 400}` 的可恢复错误，且不向目标接口发送请求。
6. WHILE 以远程 stdio 模式运行时，THE Loopback_Client SHALL 先通过 `GET /api/agent/attachments/{id}/content` 取回文件内容，再提交给目标接口。
7. WHEN 面板消息带有 `attachments` 列表时，THE Agent SHALL 校验每个附件都归属本会话，然后把附件引用、文件名、类型、大小写入 `user/message` 事件，供模型和界面使用。
8. WHEN 删除会话时，THE 系统 SHALL 在同一流程中删除该会话的附件记录和文件。
9. THE `mindex clean` SHALL 提供 B 级规则“过期助手附件”，回收两类内容：已过期的附件记录及其文件；没有记录、且超过宽限期的孤儿文件。附件有效期由 `agent.attachment_ttl_hours` 配置，默认 168 小时。
10. THE `mindex backup` SHALL 排除 `agent-attachments/` 目录。

### Requirement 4: 面板附件交互

**User Story:** 作为用户，我希望在助手面板里通过拖放、粘贴或选择文件来添加附件，能看到上传进度，发送后在消息里能看到附件。

#### Acceptance Criteria

1. THE 助手面板 SHALL 提供“添加文件”按钮，支持选择多个文件。
2. WHEN 用户把文件拖放到面板，或粘贴剪贴板里的文件时，THE 助手面板 SHALL 把这些文件加入待发送附件列表并开始上传。粘贴文本时，原有的疑似密钥提示 SHALL 保持不变。
3. WHILE 附件正在上传时，THE 助手面板 SHALL 显示每个附件的文件名、大小和上传进度，并提供带无障碍标签的移除按钮。
4. WHEN 用户发送消息时，THE 助手面板 SHALL 先确保会话已创建，等全部附件上传完成后，再在消息体中携带附件引用列表。IF 任一附件上传失败，THEN SHALL 阻止发送，并显示失败原因。
5. WHEN 渲染带附件的用户消息时，THE 助手面板 SHALL 显示附件 chip；图片附件 SHALL 能在现有的大图浏览器里打开。
6. THE 面板新增文案 SHALL 只写入 `frontend/src/shared/messages.json`，并通过 `scripts/build-messages.py` 生成 `messages.js`，同时提供中英双语。

### Requirement 5: 模型视觉输入

**User Story:** 作为用户，我希望在使用支持视觉的模型时，助手能直接看到我上传的图片，以及它自己抽取的画面，并据此判断和回答。

#### Acceptance Criteria

1. THE 智能助手配置与设置页的智能助手档案 SHALL 提供 `vision` 布尔开关，默认 `false`。档案保存后热生效。
2. WHERE `vision` 为 `true` 时，THE Agent SHALL 在模型请求中包含两类图片：用户消息里的图片附件，以及工具结果中声明的图片（`ToolOutput.images`）。
3. THE Agent SHALL 在事件日志和 SSE 中只保存图片引用，即 `attachment://` 或本站 `/api/` 地址，SHALL NOT 写入 base64 图片数据。
4. WHEN 为一个步骤构造模型请求时，THE Agent SHALL 把图片引用解析为最长边不超过 1024 像素的 JPEG，并按协议编码：
   - OpenAI 兼容协议：用户消息使用 `image_url` 数据 URL；工具返回的图片放在该批工具结果之后的一条合成用户消息中。
   - Anthropic 协议：使用 `tool_result` 或用户消息中的 `image` 块。
5. THE Agent SHALL 在单次模型请求中最多保留 `agent.max_images`（默认 8）张图片，只保留最近的。更早的图片 SHALL 替换为说明图片已省略的文字。
6. WHERE `vision` 为 `false` 时，THE Agent SHALL 不向模型发送任何图片；返回图片的工具，其结果文本 SHALL 注明“图片仅在面板展示”。
7. IF 某个图片引用无法解析，THEN THE Agent SHALL 用说明文字代替该图片，并继续本步请求。

### Requirement 6: 检索增强

**User Story:** 作为用户，我希望助手能用检索页的全部能力帮我找素材，包括按条件过滤、指定召回渠道、以图搜视频、以视频搜相似，以及查看系统是否就绪。

#### Acceptance Criteria

1. THE `search_media` 工具 SHALL 支持以下参数，并原样透传给 `POST /api/search/text`：`granularity`（shot / moment / scene）、`limit`、`source_ids`、`person_ids`、`person_mode`、`person_relation`、`channels`、`filters`（min_duration、max_duration、scene、emotion、shot_size）。
2. WHEN 检索结果带有 `degraded` 或 `warnings` 时，THE `search_media` 工具 SHALL 在结果文本中如实说明。
3. WHEN `search_by_media` 收到图片或视频附件时，THE 工具 SHALL 把附件以 `file` 字段提交给 `/api/search/image` 或 `/api/search/video`，并以 JSON 字符串形式提交 `filters`。附件类型 SHALL 从内容类型推断，也可以由参数显式指定。
4. THE `get_system_status` 工具 SHALL 返回活动索引、渠道覆盖、素材数量与片段数量，数据来自 `/api/status?summary=true`。WHEN 请求包含嵌入状态时，SHALL 额外返回嵌入身份是否与活动索引匹配。
5. THE 检索类工具 SHALL 返回检索页的定位（focus）和片段缩略图媒体。

### Requirement 7: 按片段找相似

**User Story:** 作为用户，我希望对已有检索结果说“再找几个类似的”，助手就能直接用这个片段找相似画面，不需要我重新上传。

#### Acceptance Criteria

1. WHEN 调用 `POST /api/search/similar` 并传入一个已发布片段 ID 时，THE SearchEngine SHALL 从当前活动索引（或固定索引）读出该片段的全部 `video` 表示向量，每个向量做一次近邻检索，再按等权 RRF 融合，返回与 `/api/search/text` 相同结构的结果，并附上 `seed_segment_id`。
2. THE SearchEngine SHALL 从候选中排除种子片段本身，以及与种子同一素材、时间区间有交集的片段。
3. THE SearchEngine SHALL 对候选应用隐藏过滤、粒度过滤（默认为种子片段的粒度）和请求中的 `filters`。
4. THE SearchEngine SHALL 只使用库内同一索引的向量完成检索。查询嵌入服务离线时，本接口 SHALL 仍然可用。
5. IF 种子片段不在当前索引中，或尚未完成，THEN THE 接口 SHALL 返回 404；IF 种子没有画面表示，THEN SHALL 返回 422；IF 种子的画面向量在向量库中缺失，THEN SHALL 返回 409。
6. THE `find_similar` 工具 SHALL 调用该接口，并返回检索页定位和缩略图媒体。

### Requirement 8: 区间抽帧

**User Story:** 作为用户，我希望助手能把某个镜头或区间的若干帧画面展示给我看；开启视觉后，助手自己也能看到这些画面。

#### Acceptance Criteria

1. WHEN 调用 `GET /api/admin/sources/{id}/frame?t=<秒>&width=<像素>` 时，THE 系统 SHALL 返回该时间点之前最近一帧真实画面的 JPEG，宽度为请求值，最大 1024，默认 768。
2. IF `t` 不是有限数，或不在 `[0, 素材时长]` 范围内，THEN THE 接口 SHALL 返回 422；IF 素材不存在，THEN SHALL 返回 404。
3. THE 接口 SHALL 提供基于镜头文件修订、`t`、`width` 计算的 ETag。WHEN 请求带有匹配的 `If-None-Match` 时，SHALL 返回 304。
4. THE `sample_frames` 工具 SHALL 接受 `source_id` 加可选区间，或 `segment_id`，在区间内均匀生成 1 至 12 个时间点，以图片媒体卡片返回这些帧的地址，并在 `ToolOutput.images` 中声明这些地址。

### Requirement 9: 索引版本管理

**User Story:** 作为用户，我希望助手能列出索引版本、检查一致性，并能发布或回退到指定版本，安全约束与 CLI 相同。

#### Acceptance Criteria

1. THE `GET /api/indexes` SHALL 按创建时间倒序返回全部内容索引版本（ID、状态、模式、格式、创建时间），以及当前活动版本 ID 和检索组合 ID。
2. THE `GET /api/indexes/{id}/check` SHALL 在线程池中执行一致性检查，返回 `{index_id, ok, issues}`。
3. WHEN 调用 `POST /api/indexes/{id}/activate` 时，THE 系统 SHALL 在持有处理锁和索引写锁的前提下依次完成以下检查，全部通过后才激活：
   1. 一致性检查无问题；
   2. 对 schema 2 的空间，当前嵌入身份与目标索引空间一致，且通过能力门槛；
   3. 当前视频预处理配置与目标索引一致。
4. IF 一致性检查有问题，THEN THE 激活接口 SHALL 返回 422 并列出问题；IF 空间或预处理不一致，THEN SHALL 返回 503；IF 处理锁或索引写锁在超时内拿不到，THEN SHALL 返回 409。以上情况都不改变活动索引。
5. WHEN 激活成功时，THE 系统 SHALL 返回新的活动版本和检索组合，并在后台触发检索预热。
6. THE `activate_index` 工具 SHALL 为 danger 级。它的预览 SHALL 列出目标版本的模式与格式、空间是否匹配，以及将要切换到的检索组合。

### Requirement 10: 素材浏览、整理与报告

**User Story:** 作为用户，我希望助手能告诉我素材处理到了哪一步、某个片段的转写、字幕、分析和人物结果，也能帮我修改显示名、备注、收藏和隐藏状态。

#### Acceptance Criteria

1. THE `get_source` 工具 SHALL 返回素材基本信息，以及分镜、对白、分析、人物四项工作的状态、最近任务 ID 和可发布性，数据来自 `GET /api/admin/sources/{id}?detail=work`。
2. THE `list_segments` 工具 SHALL 支持 `kind`、`parent_id`、`visibility` 和分页参数，返回片段列表与缩略图媒体。
3. THE `get_segment_details` 工具 SHALL 返回该片段的 ASR、字幕、LLM 分析和人物结果；长文本按工具结果预算截断，并注明已截断。
4. THE `update_curation` 工具 SHALL 为 write 级，只提交调用方显式给出的整理字段。WHEN 修改涉及隐藏状态时，预览 SHALL 提示隐藏会让对应内容不再出现在检索结果中。
5. THE `GET /api/admin/sources/{id}/report` SHALL 在线程池中生成素材报告（计划镜头数、时长统计、缺失文件明细、布局是否一致、运行记录）。THE `get_source_report` 工具 SHALL 调用该接口。

### Requirement 11: 素材处理任务与上传

**User Story:** 作为用户，我希望让助手发起分镜、对白、分析、人物处理和发布任务，任务结束时主动告诉我结果；也希望通过附件登记新素材和上传字幕。

#### Acceptance Criteria

1. THE `start_source_task` 工具 SHALL 为 danger 级，按 `work` 分派到对应接口：
   - shots → `/api/admin/sources/{id}/shots`
   - analysis → `/api/admin/sources/{id}/analysis`
   - dialogue → `/api/admin/sources/{id}/dialogue`
   - persons → `/api/persons/sources/{id}/process`
   - publish → `/api/admin/sources/{id}/publish`

   各接口原有的选项 SHALL 原样透传。
2. WHEN 任务提交成功时，THE Agent SHALL 跟踪该任务。WHEN 任务进入终态（complete、failed、cancelled、interrupted）时，SHALL 发送 followup 通知。
3. THE `get_task_status` 与 `retry_task` 工具 SHALL 按任务类别（source / dialogue / person）路由到对应接口；接口返回的 409 与 422 SHALL 原样转述给模型。
4. THE `upload_source` 工具 SHALL 把附件以 `file` 字段提交给 `/api/admin/sources/upload`。THE `upload_subtitle` 工具 SHALL 把附件提交给 `/api/admin/sources/{id}/subtitles`，并返回字幕 ID，供 `start_source_task(dialogue, method=subtitle, subtitle_file=ID)` 使用。
5. THE 助手 SHALL NOT 提供删除素材的工具。

### Requirement 12: 后台任务取消

**User Story:** 作为用户，我希望任何正在运行或排队中的素材、对白、人物、发布任务都能被取消；取消后再重试，能从断点继续。

#### Acceptance Criteria

1. THE 系统 SHALL 提供以下接口：`POST /api/admin/source-tasks/{id}/cancel`、`POST /api/admin/dialogue-tasks/{id}/cancel`、`POST /api/persons/tasks/{id}/cancel`。
2. WHEN 取消一个 queued 状态的任务时，THE 系统 SHALL 直接把它置为 `cancelled` 状态。
3. WHEN 取消一个 running 状态的任务时，THE 系统 SHALL 设置取消标记。任务 SHALL 在下一次上报进度时，或在等待 GPU 准入时停止执行，然后置为 `cancelled`、错误文本为“已取消”，并释放全部锁。
4. IF 任务状态不是 queued 或 running，THEN THE 取消接口 SHALL 返回 409。
5. THE 取消 SHALL 跨进程生效：取消标记 SHALL 同时写入进程内事件和 `data_dir/locks/cancel/<task_id>` 哨兵文件。
6. THE 重试接口 SHALL 允许重试 `cancelled` 状态的任务，并复用已有检查点。
7. WHEN 索引构建在发布任务中被取消时，THE 系统 SHALL 保持当前活动索引不变；该索引版本 SHALL 保持可续建状态。
8. THE 取消父任务 SHALL NOT 自动取消已链式创建的发布子任务。
9. THE 素材工作台和人物库页面 SHALL 为 queued 和 running 状态的任务显示“取消”按钮，并显示 `cancelled` 状态文案；`cancelled` 任务 SHALL 显示“重试”按钮。
10. THE `cancel_task` 工具 SHALL 为 write 级，按任务类别调用对应的取消接口。

### Requirement 13: 创作构建取消与方案删除

**User Story:** 作为用户，我希望能取消正在执行的创作构建或旧任务的步骤执行，也能删除不再需要的方案，并且不丢失任何产物文件。

#### Acceptance Criteria

1. WHEN 调用 `POST /api/studio/jobs/{job}/builds/{bid}/cancel` 且该构建处于 running 状态时，THE 系统 SHALL 设置取消标记，标记同时写入进程内事件和任务目录下的哨兵文件。IF 构建不是 running 状态，THEN SHALL 返回 409。
2. WHEN 构建执行器在开始下一个节点之前检测到取消标记时，THE 执行器 SHALL 依次完成以下操作：
   1. 把剩余的 pending 节点置为 `blocked`，错误文本为“已取消”；
   2. 把构建置为 `cancelled`，并写入 `finished_at` 和 `board_result`；
   3. 释放创作锁。
   
   已经开始执行的节点 SHALL 正常执行完毕。
3. THE 服务重启恢复逻辑 SHALL NOT 改变已取消的构建。
4. THE 旧任务步骤执行器 SHALL 在步骤之间检查同样的取消信号，并提供对应的取消接口。
5. WHEN 调用 `DELETE /api/studio/jobs/{job}/plans/{pid}` 时，THE 系统 SHALL 在创作修改锁内从工作台删除该方案记录，SHALL NOT 删除任何版本或文件。IF 被删除的是当前方案，THEN SHALL 切换到其余方案中创建时间最新的一个。
6. IF 存在 running 状态的构建，或该方案是任务中唯一的方案，THEN THE 删除方案接口 SHALL 返回 409 或 422，且不做任何修改。
7. THE 创作页 SHALL 为运行中的构建显示“取消执行”按钮，为方案提供“删除方案”按钮（需二次确认），显示 `cancelled` 状态文案；遇到未知状态时 SHALL 回退显示原始值。
8. THE `cancel_build` 工具 SHALL 为 write 级，`delete_plan` 工具 SHALL 为 danger 级。

### Requirement 14: 创作写操作扩展

**User Story:** 作为用户，我希望用对话完成分镜召回设置（人物、限定素材、图片或视频召回、粒度、数量），并能修改任务标题。

#### Acceptance Criteria

1. THE `edit_shots` 工具的 update 和 insert 操作 SHALL 支持设置 `recall`（与 `ShotRecall` 结构一致）、`granularity`、`recall_limit`，并通过 `clear` 列表把 `recall`、`granularity`、`recall_limit`、`visual` 恢复为跟随方案或清空。
2. WHEN `edit_shots` 修改了召回相关字段时，预览 SHALL 提示该分镜的召回、选片、渲染及成片会在下次构建时重建。
3. IF 服务端拒绝召回设置（人物不存在、选择了作品、素材超出任务范围、查询文件缺失），THEN THE `edit_shots` 工具 SHALL 把服务端给出的中文原因原样转述给模型。
4. THE `attach_query_media` 工具 SHALL 为 write 级，按以下步骤处理，整个过程只需一次确认：
   1. 把附件上传到 `/plans/{pid}/query-media`，得到 `media_id`；
   2. 把目标分镜的召回设为对应的 image 或 video 模式，并引用该 `media_id`。
5. THE `rename_job` 工具 SHALL 为 write 级，调用 `PATCH /api/studio/jobs/{id}/title`。

### Requirement 15: 创作诊断与人工选片

**User Story:** 作为用户，我希望助手能解释“为什么这个分镜要重建”、检查构建环境，并能帮我人工指定某个分镜用哪个候选、从哪一秒开始。

#### Acceptance Criteria

1. THE `GET /api/studio/jobs/{job}/plans/{pid}/diagnostics` SHALL 返回以下内容：
   - 当前分镜的硬性检查和软性问题；
   - 方案开启烧录字幕时，字幕滤镜与字体检查的结果；
   - 各已选节点的变化原因（与 `conflicts` 一致）；
   - 各已选节点是否缺失、失败或文件损坏。
2. THE `diagnose_plan`、`get_version_lineage`、`suggest_shot_query`、`get_material_digest`、`preview_version` 工具 SHALL 为 read 级，分别调用诊断、版本依赖、召回词建议、素材摘要、版本预览接口。
3. THE `run_column` 工具 SHALL 为 danger 级，使用当前工作台的输入组装 `inputs_by_shot`，调用 execute-column 或 regenerate-column 接口，并跟踪构建进度。
4. THE `get_selection_candidates` 工具 SHALL 返回指定分镜的召回候选、当前锚点，以及选片上下文是否就绪和原因。
5. THE `preview_selection` 工具 SHALL 支持两种方式，并返回对齐结果、预览 token 和预览视频：
   - 候选 ID 加起点：窗口方式，必要时先设置人工锚点；
   - 素材 ID 加起止时间：区间方式。
6. THE `confirm_selection` 工具 SHALL 为 write 级，提交与预览一致的请求体和 token。IF 方案 revision 已变化或 token 不一致，THEN SHALL 提示重新预览。
7. IF 分镜尚无成功的配音，或有构建正在执行，THEN 选片类工具 SHALL 返回可恢复错误，并说明原因。

### Requirement 16: 人物库管理与识别演员

**User Story:** 作为用户，我希望助手能维护演员、角色和参考图，并能根据一张图片告诉我画面里的人是谁。

#### Acceptance Criteria

1. WHEN 调用 `POST /api/persons/identify` 并上传一张图片时，THE 系统 SHALL 依次处理：
   1. 把图片归一化后交给人物服务抽取特征，并校验空间；
   2. 对每张人脸，分别按角色和按演员给出前 k 个候选（相似度、是否达到接受条件、获胜参考图地址）；
   3. 返回人脸框、人脸质量，以及没有特征缓存的参考图数量。
2. IF 人物服务不可用或空间不匹配，THEN THE 识别接口 SHALL 返回 503。
3. THE `get_person`、`save_person`、`assign_source_work`、`add_person_reference`、`identify_person` 工具 SHALL 分别读取或写入实体、作品归属与参考图，或调用识别接口。`add_person_reference` SHALL 支持两种参考图来源：附件；素材 ID 加时间点加可选裁剪框。
4. THE `delete_person` 与 `remove_person_reference` 工具 SHALL 为 danger 级。IF 实体有关联镜头，THEN SHALL 把服务端返回的关联镜头示例转述给模型；作品实体 SHALL 不可删除。
5. THE 助手 SHALL NOT 提供删除演员表导入图片的工具。

### Requirement 17: 人物审核、发布与演员表导入

**User Story:** 作为用户，我希望助手能帮我完成出镜审核、发布人物检索、切换或删除检索组合，并能从演员表截图导入演员与角色。

#### Acceptance Criteria

1. THE `list_review_shots` 工具 SHALL 支持按素材、状态、演员、角色、是否有候选筛选并分页，返回证据图媒体。
2. THE `review_shot` 工具 SHALL 为 write 级，提交 `selections`；空列表表示整镜排除。每个选择 SHALL 带角色。
3. THE `publish_persons` 工具 SHALL 为 danger 级，预览说明发布会切换当前生效的检索组合。IF 内容索引未就绪或有未完成片段，THEN SHALL 转述服务端原因。
4. THE `change_search_release` 工具 SHALL 为 danger 级，支持 `activate` 和 `delete` 两种动作。IF 要删除的是当前生效组合，THEN SHALL 转述服务端拒绝原因。
5. THE `import_cast` 工具 SHALL 为 write 级，接受 1 至 12 个图片附件，依次创建导入草稿、执行自动拆分，返回条目与警告。THE `confirm_cast_import` 工具 SHALL 提交确认。

### Requirement 18: 设置与音色库扩展

**User Story:** 作为用户，我希望助手能删除不用的模型档案、重置业务设置、管理音色库；在我本人确认授权后，还能克隆音色。

#### Acceptance Criteria

1. THE `delete_model_profile` 工具 SHALL 为 danger 级。IF 目标档案是当前激活档案，或是智能助手自身档案，THEN SHALL 拒绝执行且不发送删除请求。
2. THE `reset_business_settings`、`delete_voice` 工具 SHALL 为 danger 级；`register_voice` 工具 SHALL 为 write 级；`list_voice_catalog` 工具 SHALL 为 read 级。
3. WHEN 执行 `clone_voice` 时，THE 工具 SHALL 先通过 `ask_user_question` 向用户展示表单，表单含音色名称、参考文本和声音授权确认项。只有用户在表单中确认授权后，才 SHALL 以 `consent=true` 调用克隆接口。模型参数 SHALL 无法设置授权。
4. THE `clone_voice` 工具 SHALL 按当前语音合成后端选择样本来源：openai 和 minimax 后端使用音频附件；dashscope 后端使用公网 URL。
5. IF 用户拒绝授权、取消表单，或当前运行环境没有提问通道（例如 MCP），THEN THE `clone_voice` 工具 SHALL 不调用克隆接口，并说明原因。

### Requirement 19: 风险分级与删除策略

**User Story:** 作为用户，我希望所有新增操作都有合理的确认级别，不可恢复的文件删除不会经由助手发生。

#### Acceptance Criteria

1. THE Tool_Registry SHALL 为每个 write 和 danger 工具提供预览，以及执行后的页面定位；带媒体的工具 SHALL 提供媒体卡片。
2. THE 助手 SHALL NOT 提供调用 Physical_Delete 接口的工具；`delete_job` SHALL 保留为 danger 级。
3. THE 只删除记录（Record_Delete）的工具 SHALL 一律为 danger 级。
4. THE 新增的 GPU 任务、发布、激活、构建类工具 SHALL 为 danger 级。

### Requirement 20: 文档与一致性

**User Story:** 作为维护者，我希望文档、注释、CLI 帮助和测试断言中的工具数量与领域说明都和代码一致。

#### Acceptance Criteria

1. THE `docs/agent.md` SHALL 描述以下内容：领域与工具表、动态加载规则、附件通道、视觉输入、取消、新增 API、新增配置、MCP 导出范围与 `agent.mcp.domains`。
2. THE `docs/cleanup.md`、`docs/studio.md`、`docs/admin.md`、`config.example.yaml`、`CLAUDE.md` SHALL 同步更新新增的清理规则、取消与删除方案说明，以及新增配置键。
3. THE `mindex agent tools --domain` 的帮助文本 SHALL 列出全部领域。
4. THE 测试中关于工具总数和 MCP 导出数的断言 SHALL 与注册表实际数量一致。
5. THE 统一检查 SHALL 全部通过：ruff、文档链接检查、`build-messages --check`、前端 JS 语法检查、完整 pytest。
