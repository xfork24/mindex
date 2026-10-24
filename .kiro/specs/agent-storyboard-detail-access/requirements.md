# Requirements Document

智能助手读取分镜工作台分镜详情

## Introduction

当前智能助手只能通过 `get_workspace_summary` 与 `resolve_shots` 了解分镜工作台：分镜表文本中旁白被截断到 60 字、查询截断到 30 字、画面意象截断到 60 字，`resolve_shots` 也只返回 60 字摘要；字幕时间轴（选片版本 `event.subtitle`）、配音时长（配音版本 `data.duration`）等信息完全不可见。而 `edit_shots` 的 `split` 操作要求「各段旁白按顺序拼接须等于原旁白」，助手因读不到完整旁白而无法正确拆分分镜。

本功能新增只读的「分镜详情」能力：一次读取当前方案全部分镜，或按短 ID / 完整 ID / #序号读取指定分镜，返回不截断的旁白、查询、画面意象、预估时长、召回设置、人工区间、各阶段状态，以及字幕与配音信息。该能力作为工具注册到既有工具注册表（`mindex.agent.registry`），经进程内回环调用既有 Studio REST 接口，同时供内置智能助手与 MCP 导出使用。

## Glossary

- **Agent**：Mindex 内置智能助手（`mindex.agent.runtime`），通过工具读写系统。
- **Tool_Registry**：与运行方式无关的工具注册表（`ToolRegistry` / `ToolSpec`），内置执行与 MCP 导出共用。
- **MCP_Server**：把 Tool_Registry 中 `mcp=True` 的工具导出给外部 harness 的服务（`mindex.agent.mcp_server`）。
- **Shot_Detail_Tool**：本功能新增的只读工具，读取分镜详情。
- **Workspace_API**：既有接口 `GET /api/studio/jobs/{job_id}/workspace`，返回任务、方案、版本与分镜内容。
- **Job**：创作任务。
- **Plan**：任务内的方案；每个 Job 有一个当前激活方案（active plan）。
- **Shot**：方案中的一个分镜，内容字段为 ShotBody 形状：`id`、`text`、`query`、`estimated_seconds`、`visual`、`recall`、`granularity`、`recall_limit`。
- **Shot_Reference**：用户或模型对分镜的指代：完整 ID、界面短 ID（如 `a1daad59` 或 `shot-a1daad59`）或 `#序号`，由既有 `resolve_shot` 解析。
- **Narration**：分镜旁白，即 Shot 的 `text` 字段（1–200 字），用于配音与字幕。
- **Subtitle_Cue**：分镜当前选用的选片版本中 `data.event.subtitle` 的 `{text, start, end}`，时间以秒计、相对于该分镜起点。
- **Speech_Info**：分镜当前选用的配音版本的状态与 `data.duration`（秒）。
- **Stage_State**：分镜在召回（recall）、配音（tts）、选片（select）、渲染（render）各阶段选用版本的状态（complete / failed / missing 等）、版本短 ID 与过期标记。
- **Shot_Detail**：Shot_Detail_Tool 为单个分镜返回的结构化记录。

## Requirements

### Requirement 1: 读取全部分镜详情

**User Story:** 作为用户，我希望智能助手能一次读取分镜工作台中全部分镜的完整信息，以便它基于真实内容拆分、合并或改写分镜。

#### Acceptance Criteria

1. WHEN Agent 调用 Shot_Detail_Tool 且未指定 Shot_Reference，THE Shot_Detail_Tool SHALL 按 Workspace_API 返回的目标 Plan（按 Requirement 4 规则确定）分镜列表顺序，为每个 Shot 恰好返回一个 Shot_Detail，不遗漏、不重复（受 Requirement 7 的 `offset`/`limit` 分页约束）。
2. THE Shot_Detail_Tool SHALL 在每个 Shot_Detail 中包含以下字段：序号（从 1 开始，与 `get_workspace_summary` 分镜表及 `#序号` Shot_Reference 的序号一致）、短 ID（与 `get_workspace_summary` 分镜表显示的短 ID 相同）、完整 ID、Narration、`query`、`visual`、`estimated_seconds`、`granularity`、`recall_limit`、`recall` 设置与人工区间。
3. IF 某个 Shot 的 `query`、`visual`、`recall` 设置或人工区间为空或不存在，THEN THE Shot_Detail_Tool SHALL 在该 Shot_Detail 的结构化值中保留该字段并标记为空，且在渲染文本中显示为「（无）」，而不是省略该字段。
4. THE Shot_Detail_Tool SHALL 在 Shot_Detail 的结构化值与给模型的渲染文本中逐字保留 Narration、`query` 与 `visual` 的完整内容（不截断、不添加省略号、不合并或删除换行与空白）。
5. FOR ALL 目标 Plan 中的 Shot，Shot_Detail 中的 Narration、`query`、`visual`、`estimated_seconds`、`granularity`、`recall_limit` SHALL 与 Workspace_API 返回的该 Shot 内容版本中对应字段的值完全相等。
6. WHEN 目标 Plan 的分镜数为 0，THE Shot_Detail_Tool SHALL 成功返回（不报错）空的 Shot_Detail 列表与分镜总数 0，并在渲染文本中说明当前方案还没有分镜。
7. THE Shot_Detail_Tool SHALL 在结果中包含 Plan ID、Plan 名称、方案 revision 与分镜总数，其中 revision 与本次读取时 Workspace_API 返回的方案 revision 相等，分镜总数等于目标 Plan 的全部分镜数（不受分页影响）。

### Requirement 2: 读取指定分镜详情

**User Story:** 作为用户，我希望说「把 #3 拆成两镜」或「看看 a1daad59 的旁白」时，智能助手能准确读取那个分镜的完整信息。

#### Acceptance Criteria

1. WHEN Agent 调用 Shot_Detail_Tool 并提供 1 至 50 个 Shot_Reference，THE Shot_Detail_Tool SHALL 用既有 `resolve_shot` 规则（完整 ID 精确匹配；短 ID 前缀匹配，带或不带 `shot-` 前缀，不区分大小写；`#N` 表示方案内第 N 个分镜，N 从 1 开始）解析每个 Shot_Reference，并为每个 Shot_Reference 按提供顺序各返回一项结果，每项包含原始 Shot_Reference 文本与解析状态（ok / ambiguous / not_found）。
2. WHEN 某个 Shot_Reference 解析状态为 ok，THE Shot_Detail_Tool SHALL 在该项结果中返回该分镜的序号、完整 ID 与 Shot_Detail。
3. IF 某个 Shot_Reference 匹配到 2 个及以上分镜，THEN THE Shot_Detail_Tool SHALL 在该项结果中标记 ambiguous 并列出全部候选分镜的序号与完整 ID，不为该项返回 Shot_Detail，且在渲染文本中提示需向用户确认。
4. IF 某个 Shot_Reference 未匹配到任何分镜（含 `#N` 中 N 小于 1 或大于方案分镜总数），THEN THE Shot_Detail_Tool SHALL 在该项结果中标记 not_found，不为该项返回 Shot_Detail；对超出范围的 `#N`，原因 SHALL 说明方案的分镜总数。
5. IF 至少 1 个 Shot_Reference 解析状态为 ambiguous 或 not_found，THEN THE Shot_Detail_Tool SHALL 仍以成功结果（而非工具错误）返回其余解析状态为 ok 的分镜的 Shot_Detail；全部 Shot_Reference 均解析失败时 SHALL 同样返回成功结果且 Shot_Detail 数量为 0。
6. FOR ALL 解析状态为 ok 的 Shot_Reference，通过 Shot_Reference 读取的 Shot_Detail SHALL 与同一 Plan revision 下读取全部分镜时同一分镜的 Shot_Detail 逐字段相等；同一分镜被多个 Shot_Reference 指向时，每个 Shot_Reference 的结果项 SHALL 各自包含相同的 Shot_Detail。
7. IF Agent 提供的 Shot_Reference 列表为空列表、超过 50 个或含空字符串或仅含空白字符的项，THEN THE Shot_Detail_Tool SHALL 返回 code 为 `invalid_arguments` 的参数无效错误，不返回任何 Shot_Detail，且不读取 Workspace_API。

### Requirement 3: 字幕与配音信息

**User Story:** 作为用户，我希望智能助手能看到每个分镜实际使用的字幕与配音时长，以便它判断拆分或改写对成片时间轴的影响。

#### Acceptance Criteria

1. WHEN Shot_Detail_Tool 生成某分镜的 Shot_Detail 且该分镜当前选用的选片版本状态为 complete 并含 Subtitle_Cue，THE Shot_Detail_Tool SHALL 在该 Shot_Detail 中返回 Subtitle_Cue 的 `text`（逐字保留、不截断）、`start` 与 `end`（单位秒，数值与选片版本中存储的值完全相等，不重新取整）。
2. WHEN Shot_Detail_Tool 生成某分镜的 Shot_Detail 且该分镜当前选用的配音版本状态为 complete，THE Shot_Detail_Tool SHALL 在该 Shot_Detail 中返回 Speech_Info 的配音时长（单位秒，数值与配音版本 `data.duration` 完全相等）及该配音版本的短 ID。
3. IF 分镜当前没有选用的选片版本、选用的选片版本状态不是 complete，或状态为 complete 但不含 Subtitle_Cue，THEN THE Shot_Detail_Tool SHALL 在该 Shot_Detail 中把字幕标记为缺失、不返回任何 `text`/`start`/`end` 值，并给出选片阶段的 Stage_State（状态为 missing、failed 或其他非 complete 状态，存在版本时含版本短 ID）。
4. IF 分镜当前没有选用的配音版本、选用的配音版本状态不是 complete，或状态为 complete 但不含 `data.duration`，THEN THE Shot_Detail_Tool SHALL 在该 Shot_Detail 中把配音时长标记为缺失、不返回时长数值，并给出配音阶段的 Stage_State（状态为 missing、failed 或其他非 complete 状态，存在版本时含版本短 ID）。
5. WHEN Shot_Detail_Tool 返回 Subtitle_Cue，THE Shot_Detail_Tool SHALL 按逐字符比较（不忽略空白、换行或标点差异）判断 Subtitle_Cue 的 `text` 与当前 Narration 是否相等，相等时把字幕过期标记设为否，不相等时把字幕过期标记设为是（表示字幕按旧旁白生成）。
6. THE Shot_Detail_Tool SHALL 在每个 Shot_Detail 中包含召回、配音、选片、渲染四个阶段的 Stage_State，FOR ALL 同一 Plan 中的 Shot，每个阶段的状态、版本短 ID 与过期标记 SHALL 与同一时刻 `get_workspace_summary` 分镜表中该分镜对应阶段的值相等。
7. THE Shot_Detail_Tool SHALL 在给模型的渲染文本中为每个分镜写出字幕 `text`、`start`、`end` 与配音时长（以秒为单位），缺失时写明缺失及对应阶段状态，字幕过期时写明字幕已过期。

### Requirement 4: 任务与方案定位

**User Story:** 作为用户，我希望智能助手默认读取我当前打开的任务与方案，也能读取我指定的其他方案。

#### Acceptance Criteria

1. WHEN Agent 调用 Shot_Detail_Tool 且未提供 `job_id`（参数缺省或为空），THE Shot_Detail_Tool SHALL 使用当前页面上下文中的 Job ID 作为目标 Job；WHEN Agent 提供了 `job_id`，THE Shot_Detail_Tool SHALL 使用所提供的 `job_id`，即使它与页面上下文中的 Job 不同。
2. WHEN Agent 调用 Shot_Detail_Tool 且未提供 `plan_id`，THE Shot_Detail_Tool SHALL 仅在页面上下文的 Job ID 与目标 Job ID 相同且页面上下文含 Plan ID 时使用该页面 Plan；其他情况（页面上下文无 Plan，或页面上下文的 Job 与目标 Job 不同）下 SHALL 使用目标 Job 的当前激活 Plan。
3. IF Agent 未提供 `job_id` 且页面上下文中没有 Job，THEN THE Shot_Detail_Tool SHALL 不调用 Workspace_API，返回 code 为 `missing_job` 的可恢复工具错误，错误信息提示提供 `job_id` 或先用 `list_jobs` 查找，且不返回任何 Shot_Detail。
4. IF 最终确定的 Plan ID（Agent 提供的 `plan_id`，或取自页面上下文的 Plan ID）不在目标 Job 的方案列表中，THEN THE Shot_Detail_Tool SHALL 返回 code 为 `missing_plan` 的可恢复工具错误，错误信息包含该 Plan ID 以及目标 Job 全部可用方案的名称与 ID，且不返回任何 Shot_Detail。
5. IF Workspace_API 返回非成功状态（包括所提供的 `job_id` 不存在），THEN THE Shot_Detail_Tool SHALL 按既有 `check` 规则返回可恢复工具错误：code 为 `http_<HTTP 状态>`，错误信息包含 HTTP 状态与 Workspace_API 返回的错误信息，且不返回任何 Shot_Detail。
6. WHEN Shot_Detail_Tool 成功定位目标 Job 与 Plan，THE Shot_Detail_Tool SHALL 在结果中包含实际使用的 Job ID、Plan ID，以及该 Plan 是否为该 Job 当前激活 Plan 的标记。
7. IF Shot_Detail_Tool 返回 `missing_job`、`missing_plan` 或 `http_<状态>` 错误，THEN THE Shot_Detail_Tool SHALL 保持 Job、Plan 与版本数据不变（错误前后 Workspace_API 返回的方案 revision 相同）。

### Requirement 5: 工具注册与导出

**User Story:** 作为开发者，我希望分镜详情能力和其他工具一样注册、只读直接执行，并能通过 MCP 被外部 harness 使用。

#### Acceptance Criteria

1. THE Tool_Registry SHALL 在 `studio` 领域注册且仅注册一个 Shot_Detail_Tool，风险级别为 `read`，`mcp` 标记为 true，工具名符合 `^[A-Za-z0-9_-]{1,64}$` 且与 Tool_Registry 中既有工具名均不相同（注册时不触发重名错误）。
2. WHEN Agent 调用 Shot_Detail_Tool，THE Agent SHALL 在不产生审批请求事件（approval-asked）、不弹出确认卡片、不依赖会话授权记录的情况下直接执行 Shot_Detail_Tool。
3. WHEN 外部 harness 列出 MCP_Server 的工具，THE MCP_Server SHALL 返回 Shot_Detail_Tool，其参数 JSON Schema 与 Tool_Registry 中该工具给内置 Agent 的参数 JSON Schema 完全相等，且注解标记为只读（readOnlyHint 为 true、destructiveHint 为 false）。
4. WHEN 外部 harness 通过 MCP_Server 调用 Shot_Detail_Tool，THE MCP_Server SHALL 在 `read`、`write`、`all` 任一 MCP 风险策略下直接执行该工具，并返回与内置 Agent 相同参数调用时内容相等的渲染文本与结构化结果。
5. THE Shot_Detail_Tool SHALL 仅通过既有回环客户端（MindexClient：内置运行时为进程内调用，独立 MCP 进程为带 Bearer 令牌的 HTTP 调用）调用既有 Studio 只读 REST 接口读取数据，且不发起任何非 GET 请求。
6. WHEN Shot_Detail_Tool 执行完成，THE Shot_Detail_Tool SHALL 保持 Job、Plan 与版本数据不变（调用前后 Workspace_API 返回的方案 revision、分镜数量与各分镜选用版本 ID 均相同），且工具结果中不包含改动通知（mutation 为空）。
7. IF Shot_Detail_Tool 执行中回环调用失败或参数校验失败，THEN THE MCP_Server SHALL 返回 isError 为 true 的工具结果并附带说明失败原因的文本，且不向页面发布任何改动或定位通知。

### Requirement 6: 拆分与改写分镜的工作流指引

**User Story:** 作为用户，我希望智能助手在拆分、合并或改写分镜旁白前，总是先读取完整旁白，而不是基于截断的摘要猜测。

#### Acceptance Criteria

1. THE Agent 系统提示 SHALL 指示：在提交包含 `split` 操作、`merge` 操作或设置 `text` 字段的 `update` 操作的 `edit_shots` 调用前，先调用 Shot_Detail_Tool 读取该操作涉及的全部分镜（`split` 与 `update` 的目标分镜、`merge` 的每个被合并分镜）的完整 Narration，且不得以 `get_workspace_summary` 或 `resolve_shots` 返回的截断摘要作为新旁白的依据。
2. THE `get_workspace_summary` 工具描述与分镜表渲染文本 SHALL 注明旁白（截断到 60 字）、查询（截断到 30 字）与画面意象（截断到 60 字）为截断摘要，并写明 Shot_Detail_Tool 的工具名作为获取完整内容的途径。
3. THE `edit_shots` 工具描述 SHALL 注明 `split` 的各段旁白须由 Shot_Detail_Tool 返回的完整 Narration 按顺序切分，且各段按顺序拼接后（忽略空白字符）须等于原 Narration。
4. FOR ALL 由 Shot_Detail_Tool 返回、长度 L ≥ 2 的 Narration，在 1 至 L−1 个互不相同的字符位置切开得到的 2 至 L 个非空段，按顺序拼接 SHALL 等于该 Narration，且以这些段作为 `parts` 旁白提交的 `edit_shots` `split` 操作 SHALL 通过旁白一致性校验（往返性质）。
5. IF `edit_shots` 的 `split` 各段旁白按顺序拼接（忽略空白字符）后不等于原 Narration，THEN THE `edit_shots` 工具 SHALL 返回 code 为 `invalid_operation` 的可恢复工具错误，并保持该 Plan 的分镜与方案 revision 不变。
6. IF `edit_shots` 因旁白一致性校验失败或工作台在确认期间被改动而被拒绝，THEN THE Agent 系统提示 SHALL 指示 Agent 重新调用 Shot_Detail_Tool 读取涉及分镜的当前 Narration 后再重新提交，而不是基于之前读到的旁白重试。

### Requirement 7: 页面定位与输出规模

**User Story:** 作为用户，我希望助手读取分镜详情时界面定位到相关分镜，且长分镜表不会挤爆对话上下文。

#### Acceptance Criteria

1. WHEN Shot_Detail_Tool 按 Shot_Reference 返回至少 1 个解析成功的分镜，THE Shot_Detail_Tool SHALL 在结果的页面定位中包含 Studio 页面（`page=studio`）、Job ID、Plan ID，以及解析成功分镜的完整 ID：按 Shot_Reference 提供顺序去重后取前 20 个，不含 ambiguous 或 not_found 项。
2. WHEN Shot_Detail_Tool 返回全部分镜，或按 Shot_Reference 读取但没有任何一项解析成功，THE Shot_Detail_Tool SHALL 在结果的页面定位中包含 Studio 页面、Job ID、Plan ID，并指向分镜工作台区块（`section=storyboard`），且不包含分镜 ID 列表。
3. WHEN Agent 未提供 Shot_Reference 调用 Shot_Detail_Tool，THE Shot_Detail_Tool SHALL 接受可选的整数参数 `offset`（≥ 0，默认 0）与 `limit`（1–100，默认 100），只返回按方案分镜顺序、从第 `offset` 个分镜（从 0 计）开始的最多 `limit` 个 Shot_Detail，并在结果中给出分镜总数、本次返回数量，以及是否还有后续分镜（当 `offset` + 本次返回数量 < 分镜总数时为是）。
4. IF `offset` 大于等于目标 Plan 的分镜总数且分镜总数大于 0，THEN THE Shot_Detail_Tool SHALL 返回空的 Shot_Detail 列表、分镜总数与「无后续分镜」标记，并在渲染文本中说明 `offset` 已超出分镜范围。
5. IF `offset` 不是大于等于 0 的整数、`limit` 不是 1–100 内的整数，或 Agent 同时提供 Shot_Reference 与 `offset`/`limit` 参数，THEN THE Shot_Detail_Tool SHALL 返回参数无效错误（code `invalid_arguments`），且不返回任何 Shot_Detail。
6. WHEN 渲染文本加入下一个完整分镜后会超过 Tool_Registry 文本上限（12000 字符），THE Shot_Detail_Tool SHALL 在上一个完整分镜之后结束渲染文本（每次至少输出 1 个分镜），在末尾说明下一次读取应使用的 `offset`（= 本次 `offset` + 已输出分镜数），且不截断任一已输出分镜的 Narration、`query` 与 `visual`。
7. WHEN Shot_Detail_Tool 因文本上限提前结束输出，THE Shot_Detail_Tool SHALL 使结构化结果中的 Shot_Detail 列表与渲染文本中已输出的分镜一一对应，并在结构化结果中给出相同的下一次 `offset` 与「有后续分镜」标记。
