# Requirements Document

## Introduction

本特性让创作工作台的全链路——文案创作 → 拆分镜（画面意象与召回词）→ 召回——参照同一份"可用素材摘要"。摘要的用词即召回实际比对的画面分析文字（views.analysis），从而让文案写出素材里真实存在的画面、分镜按可召回的具体意象切分、召回词贴近素材画面词表，减少"文案有、素材没有"或"召回词与画面描述用词不一致"造成的召回失败。

本轮范围：素材摘要模块与预览接口、文案模型 v2、拆分镜提示词 v7（含画面意象字段 visual 与软校验）、按画面意象重写召回词、全链路评测与文档。演员/角色名进入摘要列为后续工作，不在本轮范围。

## Glossary

- **Studio**：创作工作台后端（backend/src/studio/），负责任务、方案、分镜、各阶段产物与签名。
- **Studio_Frontend**：创作工作台前端（frontend/src/studio/studio.js、studio.html 及 messages.json/messages.js）。
- **Material_Digest**：可用素材摘要模块（backend/src/studio/material_digest.py），从生效内容索引快照中为某任务生成素材镜头信息摘要。
- **Digest**：Material_Digest 输出的摘要结果，包含素材条目、画面词表、统计与指纹。
- **Digest_Budget**：配置项 studio.material_digest_chars，摘要总字符预算，默认 12000，0 表示关闭。
- **Material_Scope**：任务素材范围，即 studio.material_scope(state) 的结果；None 表示不限。
- **Effective_Hidden**：经 Library.overlays 与 decorate 计算后片段的生效隐藏状态。
- **Analyzed_Segment**：annotation_status == "complete" 的片段。
- **Effective_Snapshot**：生效内容索引快照 store.snapshots_shared。
- **Content_Index_ID**：生成 Digest 所用生效内容索引的标识。
- **Digest_Fingerprint**：由 Digest 内容确定性计算出的指纹。
- **Visual_Vocabulary**：Digest 中 shot/moment 的场景、物体、动作高频词集合（每素材前 30 个），供拆分镜与召回词参考。
- **Writer**：文案模型客户端（backend/src/models/writer.py），提示词版本 WRITER_PROMPT_VERSION。
- **Script_Version**：文案阶段产生的文案版本，其 data 字段承载文案与元数据。
- **Storyboarder**：拆分镜模型协议，含 QwenStoryboarder（真实服务）与 FixtureStoryboarder（测试替身）实现；提示词由 backend/src/models/storyboard.py 的 split_instructions() 生成。
- **Split_Version**：拆分镜提示词版本常量 SPLIT_VERSION（backend/src/studio/studio.py），当前为 6。
- **Shot**：分镜数据模型（Shot、ShotBody 均为 StrictModel，extra=forbid；草稿模型 ShotDraft 位于 shared/types.py）。
- **Visual**：分镜的画面意象字段 visual，一句中文画面意象，上限 120 字。
- **Query**：分镜召回词字段，用于召回比对。
- **Concrete_Imagery**：具体意象，指地点、建筑、景点、食物、活动等可直接找到素材的主体。
- **Abstract_Enumeration**：抽象或修辞性列举，指 nature、culture、modern life 等类别或品质，以及欢迎语、感叹。
- **Shot_Editor**：分镜表中单个分镜的编辑器（renderShotEditor），含"文案与查询"格。
- **Agent_Tools**：智能助手工具（agent/tools/studio_write.py 与 agent/tools/common.py）。
- **Signature**：各阶段产物的构建签名，用于判断产物是否失效。
- **Split_Eval**：全链路评测脚本 eval/split_eval.py 及其用例集 eval/split_cases.jsonl。
- **Reference_Job**：新加坡任务 20261023-174141-5f6104，仅作只读验证对象。

## Requirements

### Requirement 1: 素材摘要的内容范围

**User Story:** 作为创作者，我希望文案模型只看到当前任务真正可用的素材镜头信息，以便文案不会引用超出范围、已隐藏或未分析的素材。

#### Acceptance Criteria

1. WHEN Material_Digest 为某任务生成 Digest，THE Material_Digest SHALL 仅从生成时刻的 Effective_Snapshot 读取片段数据，不读取其他内容索引版本，且不修改 Effective_Snapshot 及任务数据（只读）。
2. WHILE 任务的 Material_Scope 不为 None，THE Material_Digest SHALL 仅包含 Material_Scope 内素材的片段，范围外素材的名称、时长、故事线与 Visual_Vocabulary 均不出现在 Digest 中。
3. WHEN Material_Digest 生成 Digest，THE Material_Digest SHALL 排除生成时刻 Effective_Hidden 为真的片段，被隐藏片段的描述文字与词汇均不计入故事线、Visual_Vocabulary 与镜头数统计。
4. THE Material_Digest SHALL 仅包含 Analyzed_Segment，annotation_status 不等于 "complete" 的片段（含未分析、分析中、分析失败）的内容均不出现在 Digest 中，且不计入镜头数统计。
5. THE Material_Digest SHALL 仅使用画面分析文字（views.analysis）作为故事线与 Visual_Vocabulary 的内容来源，对白、字幕及其他非 views.analysis 字段的文字均不出现在 Digest 中。
6. THE Material_Digest SHALL 在本轮不在 Digest 中输出任何演员名与角色名字段或演员/角色识别结果。
7. IF 某素材经范围、隐藏与分析状态过滤后不剩任何 Analyzed_Segment，THEN THE Material_Digest SHALL 在 Digest 中省略该素材条目，且该素材不计入素材数统计。
8. IF 不存在 Effective_Snapshot，THEN THE Material_Digest SHALL 返回空 Digest（素材条目为空、素材数与镜头数均为 0），并标明为空原因为"无生效索引"。
9. IF 经范围、隐藏与分析状态过滤后不剩任何 Analyzed_Segment（含 Material_Scope 为空集合的情况），THEN THE Material_Digest SHALL 返回空 Digest（素材条目为空、素材数与镜头数均为 0），并标明为空原因为"无已分析镜头"。

### Requirement 2: 素材摘要的结构、预算与确定性

**User Story:** 作为创作者，我希望摘要按素材组织、长度受控且结果稳定，以便文案模型获得有代表性的画面信息并让产物签名可复现。

#### Acceptance Criteria

1. THE Material_Digest SHALL 为每个素材输出一个素材条目，条目包含素材名称与以秒为单位的素材时长；素材条目之间的先后顺序只由素材本身决定，与片段读取顺序无关。
2. THE Material_Digest SHALL 为每个素材输出一条故事线。故事线由该素材的 scene 级描述组成，并按片段开始时间升序排列。去重时先去掉首尾空白再比较，文本完全相同的描述只保留开始时间最早的一条。如果去重后的条数超过该素材配额能容纳的条数，就在去重序列中按等间距位置抽样，抽样结果包含序列的第一条和最后一条。
3. IF 某素材没有任何 scene 级描述，THEN THE Material_Digest SHALL 按准则 2 的排序、去重与抽样规则，用该素材的 shot 级描述构成故事线；IF 该素材既没有 scene 级描述也没有 shot 级描述，THEN THE Material_Digest SHALL 仍输出该素材的名称、时长与 Visual_Vocabulary，故事线为空。
4. THE Material_Digest SHALL 将每个素材 shot/moment 中场景、物体、动作三类词合并统计频次，按频次降序排列（频次相同时按词的字典序升序），取前 30 个作为该素材的 Visual_Vocabulary；IF 该素材的不同词少于 30 个，THEN THE Material_Digest SHALL 输出全部词。
5. WHEN Digest_Budget 大于 0，THE Material_Digest SHALL 使 Digest 的总字符数（按 Unicode 字符计）小于或等于 Digest_Budget，并按各素材 Analyzed_Segment 数量的比例分配各素材的字符配额，每个素材的配额不低于 200 字符。
6. IF Digest_Budget 小于 200 乘以素材数，THEN THE Material_Digest SHALL 将 Digest_Budget 向下取整后平均分配给各素材，Digest 总字符数仍小于或等于 Digest_Budget。
7. IF Digest_Budget 为 0，THEN THE Material_Digest SHALL 返回空 Digest：不含任何素材条目，素材数与镜头数均为 0。
8. IF Digest_Budget 未配置，THEN THE Material_Digest SHALL 使用默认值 12000；配置值为负数、非整数或超出范围时按 Requirement 3 第 2 条在加载配置时拒绝。
9. FOR ALL 相同的 Effective_Snapshot、Material_Scope、Effective_Hidden 状态与 Digest_Budget 输入，THE Material_Digest SHALL 在多次调用中产生逐字节相同的 Digest 与相同的 Digest_Fingerprint（确定性属性）；IF 任何被纳入 Digest 的片段的画面分析文字发生变化，THEN THE Material_Digest SHALL 产生不同的 Digest_Fingerprint。
10. THE Digest SHALL 包含以下统计信息：素材数（Digest 中的素材条目数）、镜头数（范围内纳入的 Analyzed_Segment 总数，按抽样前计）、Digest_Fingerprint，以及 Content_Index_ID。

### Requirement 3: 素材摘要配置项与预览

**User Story:** 作为创作者，我希望能配置摘要预算并在任务元信息中预览摘要，以便了解文案模型实际参考了什么。

#### Acceptance Criteria

1. THE Studio SHALL 提供整数配置项 studio.material_digest_chars（即 Digest_Budget），取值范围 0 至 100000，未配置时取默认值 12000，并在 config.example.yaml 与配置文档中同步说明其含义、默认值、取值范围以及 0 表示关闭。
2. IF studio.material_digest_chars 不是整数或超出 0 至 100000 范围，THEN THE Studio SHALL 在加载配置时拒绝该配置，沿用现有配置项的字段校验方式报出校验错误，错误中包含该配置项路径（studio.material_digest_chars）与违反的取值范围。
3. WHEN 收到 GET /api/studio/jobs/{job}/material-digest 请求且该任务存在，THE Studio SHALL 按当前 Effective_Snapshot、Material_Scope、Effective_Hidden 与 Digest_Budget 生成该任务的 Digest 并返回，响应包含 Digest 正文以及素材数、镜头数、Digest_Fingerprint 与 Content_Index_ID 统计信息。
4. WHEN 处理 GET /api/studio/jobs/{job}/material-digest 请求，THE Studio SHALL 不加锁、不修改任务、方案、分镜、版本或任何已有产物与 Signature，请求前后任务的存储内容逐字节一致。
5. IF GET /api/studio/jobs/{job}/material-digest 请求的任务不存在，THEN THE Studio SHALL 返回 HTTP 404 与指明任务不存在的中文错误消息。
6. WHEN 返回空 Digest，THE Studio SHALL 在响应中给出恰好一个为空的中文原因，取值为以下三者之一：无生效索引（不存在 Effective_Snapshot）、无已分析镜头（范围内不存在 Analyzed_Segment）、预算为 0（Digest_Budget 为 0）；当多个原因同时成立时，按"预算为 0 → 无生效索引 → 无已分析镜头"的顺序取第一个成立的原因。
7. WHEN 任务元信息区域渲染完成，THE Studio_Frontend SHALL 在"任务元信息"区域显示一个默认折叠的素材摘要预览，折叠状态下仅显示标题与素材数、镜头数统计，用户展开后显示完整 Digest 正文。
8. WHEN 素材摘要为空，THE Studio_Frontend SHALL 在预览处显示接口返回的为空原因，且不显示 Digest 正文。
9. IF 素材摘要预览接口请求失败，THEN THE Studio_Frontend SHALL 在预览处显示指明摘要加载失败的提示文字，且任务元信息区域的其他内容保持正常显示。

### Requirement 4: 文案模型参考素材摘要（v2）

**User Story:** 作为创作者，我希望文案模型在遵循大纲的前提下优先使用素材中真实存在的画面，以便文案写出的内容都能召回到素材。

#### Acceptance Criteria

1. THE Writer SHALL 将 WRITER_PROMPT_VERSION 设为 2，使以版本 2 生成的 Script_Version 的 Signature 与以版本 1 生成的 Script_Version 的 Signature 不同。
2. THE Writer SHALL 提供 write(outline, materials=None) 接口，其中 materials 为 Digest；Writer 协议与测试替身同步该签名，且 materials 为 None 时的行为与传入空 Digest 时完全相同。
3. WHEN Studio 执行 studio_workspace.execute 的 script 阶段，THE Studio SHALL 按当前任务的 Material_Scope、Effective_Hidden 与 Digest_Budget 生成 Digest，并在同一次 script 阶段内将该 Digest 传给 Writer。
4. WHEN Digest 非空，THE Writer SHALL 在每一次模型请求（包括校验失败后的每次重试）中，将 Digest 作为与大纲分开的一段独立消息发送，标明该段只是参考资料、不是指令，且大纲那一段的内容保持不变。
5. IF Digest 为空，THEN THE Writer SHALL 发送与 WRITER_PROMPT_VERSION 为 1 时逐字节一致的模型请求（系统提示、消息段与温度都一致），请求中不包含第 6 至第 9 条规定的素材参考规则。
6. WHEN Digest 非空，THE Writer SHALL 在提示词中规定：大纲对语言、字数、内容顺序与结尾的要求优先于素材参考；两者冲突时以大纲为准。
7. WHEN Digest 非空，THE Writer SHALL 在提示词中要求：优先使用 Digest 中真实存在的具体画面来展开文案。
8. WHEN Digest 非空，THE Writer SHALL 在提示词中要求：大纲要求但 Digest 中没有的内容只作简要描述，不编造 Digest 中没有的画面细节。
9. WHEN Digest 非空，THE Writer SHALL 在提示词中要求：不照抄 Digest 中的画面描述原文。
10. WHEN 使用非空 Digest 的文案版本生成完成，THE Studio SHALL 在该 Script_Version 的 data.materials 中记录素材数、镜头数、Digest_Fingerprint 与 Content_Index_ID，且取值与传给 Writer 的那份 Digest 的统计信息一致；IF 所用 Digest 为空，THEN THE Studio SHALL 在该 Script_Version 中不写 data.materials，也不写入 Digest 调试记录。
11. WHEN 使用非空 Digest 的文案版本生成完成，THE Studio SHALL 将传给 Writer 的完整 Digest 原样写入该版本的调试记录。
12. WHEN 所显示文案版本的 data.materials 存在，THE Studio_Frontend SHALL 在文案区显示"参考了 N 个素材、M 个镜头描述"，其中 N 为 data.materials 的素材数，M 为镜头数；IF 所显示文案版本没有 data.materials，THEN THE Studio_Frontend SHALL 不显示该提示。
13. IF script 阶段生成 Digest 时出错，THEN THE Studio SHALL 改用空 Digest 继续生成文案，按第 5 条发送请求，不让文案阶段因此失败，并按第 10 条不写 data.materials。

### Requirement 5: 拆分镜粒度规则

**User Story:** 作为创作者，我希望分镜按可召回的具体意象切分，而抽象修辞保持整句，以便每镜都能召回到对应画面，且不会产生大量无法召回的碎镜。

#### Acceptance Criteria

1. THE Storyboarder SHALL 在 split_instructions() 生成的提示词中固定写入本需求第 2–6 条所述的全部粒度规则（具体意象单独成镜、最短长度、抽象列举不拆、超长再拆、单一意象长句不拆），并将 Split_Version 由 6 递增为 7。
2. THE Storyboarder SHALL 将每个 Concrete_Imagery 单独成镜，使每镜包含的 Concrete_Imagery 不超过 1 个；即使该意象文本短于第 3 条的最短长度，也不与另一个 Concrete_Imagery 合并为同一镜。
3. THE Storyboarder SHALL 使每镜 text 不少于 3 个英文词（按空白分隔计数）或 5 个中文字（按汉字计数，不含标点），约合 1.2 秒；短于该长度的片段 SHALL 并入相邻分镜，且优先并入不包含其他 Concrete_Imagery 的相邻分镜（如连接词、修饰语所在分镜）。
4. THE Storyboarder SHALL 对 Abstract_Enumeration 不按项拆分，而以其所在的整句或整个从句作为一镜，使同一 Abstract_Enumeration 的全部列举项出现在同一镜 text 中。
5. IF 一镜 text 超过 20 个英文词或 32 个中文字（约 8 秒），且包含 2 个及以上 Concrete_Imagery，THEN THE Storyboarder SHALL 将该镜再拆分，使拆分后每镜包含的 Concrete_Imagery 不超过 1 个。
6. IF 一镜 text 超过 20 个英文词或 32 个中文字，且只包含 0 个或 1 个 Concrete_Imagery，THEN THE Storyboarder SHALL 保持该长句为一镜，不再拆分。
7. WHEN 输入句子为 "From world-class airport marvels and tranquil UNESCO greenery to savory local dishes and dazzling night views, Singapore delivers an unforgettable blend of experiences for every traveler."，THE Storyboarder SHALL 切分为 5 镜：机场、绿植、美食、夜景、总结。
8. WHEN 输入句子为 "Welcome to Singapore — the Lion City, where nature, culture, and modern life come together in the most beautiful ways."，THE Storyboarder SHALL 切分为 2 镜："Welcome to Singapore — the Lion City," 与 "where nature, culture, and modern life come together in the most beautiful ways."
9. THE Storyboarder SHALL 在提示词示例中使用恰好一对对照示例，两者均不含 "Singapore"、"新加坡" 或其地标名称，其中一个演示具体列举（Concrete_Imagery）需按项拆分，另一个演示抽象修辞（Abstract_Enumeration）保持整句不拆分。
10. THE Storyboarder SHALL 在提示词中要求按以下顺序产出：先规划每镜的 Visual，再切分 text，最后编写 Query。
11. THE Storyboarder SHALL 使同一输入文案切分后各镜 text 按顺序拼接（忽略镜与镜之间的空白差异）与原文案一致，不得遗漏、重复或改写原文字词。

### Requirement 6: 拆分镜校验与重试

**User Story:** 作为创作者，我希望拆分结果在硬性要求上始终合格，并在粒度问题上尽量修正，以便既不丢原文，也不因模型偶发问题拿不到结果。

#### Acceptance Criteria

1. THE Studio SHALL 对每次拆分输出执行三项硬校验：（a）输出能解析为合法 JSON 并通过分镜草稿模型校验；（b）去除全部空白字符后，各分镜 text 按顺序拼接的结果与原文完全一致（无遗漏、重复或改写）；（c）分镜数量在 1 至 studio.max_shots 之间。
2. THE Studio SHALL 对每次通过硬校验的拆分输出执行两项软校验，并将每个命中的分镜计为一个软校验问题：（a）过短分镜，即分镜文本少于 3 个英文词或 5 个中文字，且该分镜文本不是一个完整句子单独成镜；（b）缺少 Visual 的分镜，即 Visual 为空或仅含空白字符。
3. IF 某次拆分输出未通过硬校验或存在至少 1 个软校验问题，且已发起的尝试次数小于 split_retries + 1（split_retries 为拆分镜档案的重试次数，即 studio.generator.split_retries 或设置页激活的"文本拆分镜"档案中的同名值，默认 2，最小 0），THEN THE Studio SHALL 再次发起拆分请求，并将本次未通过的具体硬校验项或软校验问题（含涉及的分镜序号）作为反馈附加到请求中。
4. WHEN 某次拆分输出通过全部硬校验且软校验问题数为 0，THE Studio SHALL 立即返回该次结果，不再发起后续尝试。
5. IF 已发起 split_retries + 1 次尝试且没有一次的软校验问题数为 0，THEN THE Studio SHALL 返回所有通过硬校验的尝试中软校验问题数最少的那一次结果；问题数相同时返回最早的那一次。
6. IF 已发起 split_retries + 1 次尝试且没有一次通过硬校验，THEN THE Studio SHALL 使拆分失败，给出指明最后一次硬校验失败原因的中文错误消息，并保持已有方案与分镜不变。
7. THE Storyboarder SHALL 提供 split(text, vocabulary=None) 接口，vocabulary 为 Visual_Vocabulary；QwenStoryboarder 与 FixtureStoryboarder 都 SHALL 接受该签名，vocabulary 为 None 时拆分请求中不包含词表内容。

### Requirement 7: 画面意象字段 Visual

**User Story:** 作为创作者，我希望每个分镜都有一句可编辑的中文画面意象，以便理解该镜想要的画面并据此调整召回词。

#### Acceptance Criteria

1. WHEN Storyboarder 完成一次拆分镜，THE Storyboarder SHALL 为输出中的每个分镜给出一个 Visual，内容为 1 句中文画面意象，去除首尾空白后长度为 1 至 120 个字符（按 Unicode 字符计数），且描述的是 Concrete_Imagery 而非 Abstract_Enumeration。
2. IF Storyboarder 输出的某个分镜缺少 Visual 或 Visual 超过 120 个字符，THEN THE Studio SHALL 将该情况作为软校验警告记录，同时照常保留该分镜的其余字段，不让这次拆分镜失败；超过 120 个字符的 Visual 截断到前 120 个字符，缺少的 Visual 存为空。
3. THE Studio SHALL 在 Shot、ShotBody 与 ShotDraft 中都支持可选的 visual 字段，类型为字符串，缺省为空；ShotDraft 中 visual 为第一个字段。
4. IF 提交的 visual 去除首尾空白后超过 120 个字符（按 Unicode 字符计数），THEN THE Studio SHALL 以 HTTP 422 拒绝整个请求，返回说明 visual 超出 120 字上限的中文错误消息，并保持该分镜及同一请求中其他分镜的已存内容不变。
5. WHEN 创作者提交去除首尾空白后长度为 1 至 120 个字符的 visual，THE Studio SHALL 接受该请求并保存去除首尾空白后的值。
6. FOR ALL 包含长度为 1 至 120 个字符 visual 的分镜，保存后回读 SHALL 得到与保存值逐字符相同的 visual（往返属性）。
7. IF 分镜的 visual 缺失、为空字符串或只含空白字符，THEN THE Studio SHALL 在存储内容中省略 visual 键，使未含 visual 的老分镜再次保存时内容版本号与保存前相同。
8. THE Studio SHALL 在 studio_workspace.execute 的 split 阶段、studio_steps.run_split、studio_api.patch_storyboard、select_version 与 patch_plan 中透传 visual，使经过上述任一路径后，读回的 visual 与输入值逐字符相同。
9. THE Studio SHALL 将 visual 排除在所有 Signature 之外：召回签名仅使用 Query、粒度、数量与召回设置；配音与选片签名仅使用 text。
10. WHEN 创作者只修改分镜的 visual 并保存，THE Studio SHALL 保持该分镜的召回、配音、选片产物的 Signature 不变，并且不把这些产物标记为失效或重新生成。
11. WHEN Split_Version 由 6 递增为 7，THE Studio SHALL 保持已有工作台方案的分镜内容（包括分镜数量、text 与 Query）不变，不自动重新拆分；只有创作者主动触发拆分镜时才按新版本重新拆分。
12. WHEN 创作者在 Shot_Editor 中打开某个分镜，THE Shot_Editor SHALL 显示该分镜当前的 visual，允许编辑，并通过 studio_api.patch_storyboard 保存编辑后的值；visual 为空时显示空的输入区域。

### Requirement 8: 分镜表与智能助手支持 Visual

**User Story:** 作为创作者，我希望在分镜表和智能助手中都能查看和编辑画面意象，以便两种入口行为一致。

#### Acceptance Criteria

1. WHEN Shot_Editor 渲染某分镜，THE Studio_Frontend SHALL 在"文案与查询"格中显示一个预填该分镜当前 Visual 的可编辑输入框（Visual 为空时输入框为空），并标注双语"画面意象"标签。
2. WHEN 用户修改 Shot_Editor 中的 Visual 输入框，THE Studio_Frontend SHALL 将修改记入该分镜的未保存草稿，并将该分镜标记为未保存。
3. WHEN 分镜摘要单元（cellSummary）渲染某个 Visual 非空的分镜，THE Studio_Frontend SHALL 显示该分镜的完整 Visual 文本；IF 分镜的 Visual 为空，THEN THE Studio_Frontend SHALL 在 cellSummary 中不显示 Visual 行，也不显示占位文字。
4. WHEN 用户保存分镜，THE Studio_Frontend SHALL 在整份 PATCH 中为每个分镜带上当前 Visual 值，包括未被编辑的分镜，使已有的 Visual 不会被清空。
5. IF 保存分镜的请求因 Visual 超过 120 字被 Studio 拒绝，THEN THE Studio_Frontend SHALL 显示 Studio 返回的中文错误消息，并保留该分镜未保存的草稿内容和未保存标记。
6. WHEN 用户合并 2 个或更多相邻分镜，THE Studio_Frontend SHALL 按分镜顺序用"；"拼接各分镜的非空 Visual（跳过空 Visual），并在结果超过 120 字时截断到前 120 字（按字符计）；IF 所有被合并分镜的 Visual 都为空，THEN THE Studio_Frontend SHALL 将合并后分镜的 Visual 设为空。
7. THE Studio_Frontend SHALL 将本需求新增的界面文字以中文 messages.json 条目为唯一源，并通过运行 scripts/build-messages.py 生成 messages.js 中对应的中文和英文文字，scripts/build-messages.py --check 检查通过。
8. THE Studio_Frontend SHALL 将 studio.html 中本特性改动资源（studio.js、messages.js 及其他被修改的样式或脚本）引用的缓存版本标记更新为与修改前不同的新标记，沿用现有"日期-说明"格式（如 20261024-retrieval-deps）。
9. THE Agent_Tools SHALL 在 ShotFields、update 字段名映射与 merge_into 中支持 visual，使通过智能助手设置、修改或合并 visual 的结果，与在分镜表中做相同操作的结果一致（合并时同样用"；"拼接非空 Visual、跳过空值，并截断到 120 字）。
10. IF 智能助手工具调用提交的 visual 超过 120 字，THEN THE Agent_Tools SHALL 返回中文错误消息，并保持该方案的所有分镜不变。
11. WHEN Agent_Tools 生成 shot_rows 输出，THE Agent_Tools SHALL 为每个分镜包含 visual 值，Visual 为空的分镜输出空值。

### Requirement 9: 召回词生成规则

**User Story:** 作为创作者，我希望召回词既反映本镜核心画面，又贴近素材实际用词，以便召回命中率更高。

#### Acceptance Criteria

1. THE Storyboarder SHALL 在提示词中要求每镜 Query 结合本镜 text、本镜所在整句及相邻分镜的上下文生成，且上下文只用于补全代词指代和专名，不引入原文未明示的人物、道具、天气、情绪等事实。
2. THE Storyboarder SHALL 在提示词中要求 Query 的核心画面词（主体、地点、动作或场景）取自本镜 text 或本镜 Visual，不包含仅属于相邻分镜的意象词，使相邻分镜的 Query 能互相区分。
3. WHEN 调用时传入非空 Visual_Vocabulary，THE Storyboarder SHALL 在提示词中附上该 Visual_Vocabulary，并要求：词表中有与本镜核心画面词同义或近义的词时优先使用词表中的词；与本镜内容无关的词表词不得写入 Query。
4. WHEN 本镜 Visual 非空，THE Storyboarder SHALL 在提示词中要求 Query 参考本镜 Visual，使 Query 至少包含一个与 Visual 描述的主体、地点或场景对应的词。
5. THE Storyboarder SHALL 将召回词规则抽取为一个公共常量，使 split_instructions() 生成的拆分提示词与 suggest_query 使用的重写召回词提示词都包含该常量的相同文本；修改该常量后两处提示词同步变化。
6. THE Storyboarder SHALL 在召回词规则中要求每个 Query 使用中文，由 2 至 5 个中文词组成，词与词之间用空格分隔，不写完整句子、解释或英文，与原文语言无关。
7. IF 调用时 Visual_Vocabulary 为 None 或为空，THEN THE Storyboarder SHALL 在提示词中不放词表段落，也不写"优先使用词表"的要求，其余召回词规则保持不变。
8. IF 本镜 Visual 为空，THEN THE Storyboarder SHALL 在提示词中要求仅根据本镜 text 和上下文生成 Query，不因缺少 Visual 而补写原文中没有的画面细节。

### Requirement 10: 按画面意象重写召回词

**User Story:** 作为创作者，我希望在修改画面意象后一键重写召回词并先预览，以便确认后再生效。

#### Acceptance Criteria

1. THE Storyboarder SHALL 提供 suggest_query(text, visual, context, vocabulary) 接口：text 为本镜文本，visual 为本镜 Visual，context 为同一方案中本镜前一镜与后一镜的文本（不存在时为空），vocabulary 为当前任务 Digest 的 Visual_Vocabulary（Digest 为空时为 None）；返回值为单条非空 Query 字符串；QwenStoryboarder 实现真实模型调用并使用 Requirement 9 的召回词公共常量；FixtureStoryboarder 对相同输入返回逐字节相同的 Query。
2. WHEN 收到 POST /api/studio/jobs/{job}/plans/{plan}/shots/{sid}/query-suggestion 请求，THE Studio SHALL 以请求中携带的 Visual 调用 suggest_query（请求未携带 Visual 时使用该分镜已保存的 Visual），并返回建议 Query；整个处理过程不获取任务锁，不修改分镜、方案、版本、产物或 Signature，请求前后该分镜的已存内容与内容版本号保持不变。
3. IF 请求中的任务、方案或分镜 sid 任一不存在，THEN THE Studio SHALL 返回 HTTP 404 与指明不存在对象的中文错误消息，且不调用 Storyboarder。
4. IF 拆分镜模型调用出错（包括调用超时、服务不可用或返回空 Query），THEN THE Studio SHALL 返回 HTTP 503 与说明召回词重写失败的中文错误消息，且不修改任何数据。
5. WHEN 用户在 Shot_Editor 点击"按画面意象重写召回词"按钮，THE Studio_Frontend SHALL 以该分镜草稿中当前的 Visual 发起请求，在请求返回前禁用该按钮；收到成功响应后，THE Studio_Frontend SHALL 用建议 Query 替换该分镜草稿中的 Query，保留草稿中的其他字段，并将该分镜标记为未保存。
6. WHEN 用户点击"保存此镜"，THE Studio_Frontend SHALL 将草稿中的建议 Query 随整份 PATCH 提交；保存成功前，已保存的 Query 与召回产物保持不变。
7. IF 用于重写的 Visual 为空或仅含空白字符，THEN THE Studio SHALL 返回 HTTP 422 与提示需先填写画面意象的中文错误消息，且不调用 Storyboarder。
8. IF 重写召回词请求失败（HTTP 404、422、503 或网络错误），THEN THE Studio_Frontend SHALL 在该 Shot_Editor 中显示对应的中文错误消息，保持草稿中的 Query 与未保存标记状态不变，并恢复按钮为可点击。

### Requirement 11: 全链路评测

**User Story:** 作为开发者，我希望有可复跑的评测集和脚本，以便用真实服务量化拆分粒度和召回效果。

#### Acceptance Criteria

1. THE Split_Eval SHALL 在 eval/split_cases.jsonl 中提供 12 至 16 条用例，每条用例包含输入文本与按顺序排列的期望分镜文本列表，并且其中包含需求 5 第 7、8 条中的两句例句及其期望切分（分别为 5 镜和 2 镜）。
2. WHEN 运行 eval/split_eval.py，THE Split_Eval SHALL 输出以下三项指标：切分吻合度（实际分镜文本列表与期望分镜文本列表在去除每镜首尾空白后逐镜完全一致的用例数占总用例数的百分比，取值 0% 至 100%）；过短分镜数（文本少于 3 个英文词或少于 5 个中文字、且不是整句单独成镜的分镜总数）；平均分镜时长（按英文每秒 2.5 词、中文每秒 4 字估算，单位为秒，保留 1 位小数）。
3. WHERE 指定 --materials JOB_ID，THE Split_Eval SHALL 生成该任务的 Digest，并把其中的 Visual_Vocabulary 作为 vocabulary 参数传给 Storyboarder 的 split 接口完成拆分。
4. WHERE 指定 --recall，THE Split_Eval SHALL 对每个分镜用其 Query 执行召回，并列出排名前 3 条结果（不足 3 条时列出全部结果）的素材名、起止时间与画面描述；零结果时标注该镜无召回结果。
5. WHEN 评测运行完成，THE Split_Eval SHALL 将报告写入 data/eval-split-<时间>.json，<时间> 为本地时间 YYYYMMDD-HHMMSS 格式；报告包含第 2 条中的三项指标、每条用例的期望切分与实际切分（含 Visual 与 Query），以及启用 --recall 时第 4 条中的召回结果。
6. THE Split_Eval SHALL 为切分吻合度计算提供单元测试，至少覆盖：完全一致、仅首尾空白不同（判为吻合）、分镜数量不同（判为不吻合）、分镜数量相同但边界不同（判为不吻合）、用例集为空（吻合度为 0% 且不报错）这五种情形。
7. THE Split_Eval SHALL 在 eval/README.md 中用中文说明用例格式（字段含义与一行示例）、运行命令示例，以及 --materials 与 --recall 两个参数的作用。
8. WHEN 开发者完成实现，THE Split_Eval SHALL 使用真实拆分镜服务（当前 GLM-5.3-Flash）对全部用例运行一遍，并以 --materials 20261023-174141-5f6104 与 --recall 对 Reference_Job 的素材执行带召回的检查，整个过程只读，不修改 Reference_Job 的方案与分镜。
9. IF 真实服务运行得到的切分吻合度低于 80%，或需求 5 第 7、8 条两句例句中任一句未吻合，THEN THE 开发者 SHALL 最多进行两轮针对性提示词微调，并保存每轮的评测报告文件，记录该轮的三项指标与改动的提示词内容。
10. IF --materials 指定的 JOB_ID 不存在，THEN THE Split_Eval SHALL 在不执行任何拆分的前提下输出指明该任务不存在的中文错误信息，并以非零退出码结束。
11. IF 某条用例调用拆分镜服务失败或返回结果未通过需求 6 第 1 条的硬校验，THEN THE Split_Eval SHALL 在报告中把该用例标记为失败并记下中文失败原因，将其计为不吻合，继续评测剩余用例。
12. IF 运行时同时指定了 --recall 和 --materials，且该任务的 Digest 为空，THEN THE Split_Eval SHALL 输出指明 Digest 为空原因的中文提示（无生效索引、无已分析镜头或预算为 0），并在不使用 vocabulary 的情况下继续拆分。

### Requirement 12: 数据安全与语言约束

**User Story:** 作为用户，我希望开发与验证过程不改动我的真实数据，且文档与提示信息为中文，以便安全、易读。

#### Acceptance Criteria

1. WHILE 执行本特性的开发与验证（包括 pytest、cjs 前端测试与 Split_Eval），THE Studio SHALL 对工作区 data/ 目录与 config.local.yaml 仅做只读访问，不在其中新建、修改或删除任何文件；唯一例外是需求 11 规定的评测报告文件 data/eval-split-<时间>.json 的新建。
2. THE Studio SHALL 让本特性新增或修改的自动化测试只读写测试临时目录中的数据目录与配置，不读写工作区 data/ 与 config.local.yaml。
3. WHEN Split_Eval 以 --materials 或 --recall 对 Reference_Job 执行检查，THE Split_Eval SHALL 保持该任务的方案列表、各方案分镜（text、visual、Query）、内容版本号与各阶段产物 Signature，与检查前逐字节相同。
4. WHEN 收到 GET /api/studio/jobs/{job}/material-digest 请求或 POST .../shots/{sid}/query-suggestion 请求，THE Studio SHALL 保持该任务的方案、分镜、内容版本号与已有产物不变。
5. THE Studio SHALL 使用中文编写本特性新增或修改的文档（docs/studio.md、配置文档、eval/README.md）、日志消息与面向用户的错误消息；代码标识符、字段名、配置键、接口路径、命令行参数与模型名可保留英文原文。
6. THE Studio_Frontend SHALL 为本特性新增的每条界面文字在 messages.json 中以中文文案作为唯一源条目，并为每条提供英文译文。
7. IF messages.js 与 messages.json 不一致，THEN THE Studio_Frontend SHALL 使 scripts/build-messages.py --check 以退出码 1 结束并输出提示重新生成 messages.js 的中文消息。

### Requirement 13: 文档与整体验证

**User Story:** 作为开发者，我希望文档同步更新并通过全部检查，以便特性可维护且无回归。

#### Acceptance Criteria

1. THE Studio SHALL 在 docs/studio.md 中分别说明以下 5 项内容：Material_Digest 的内容范围、结构与 Digest_Budget；Visual 字段（120 字上限、不参与任何 Signature）；拆分镜粒度规则与 Split_Version 7；按画面意象重写召回词的操作流程；GET /api/studio/jobs/{job}/material-digest 与 POST /api/studio/jobs/{job}/plans/{plan}/shots/{sid}/query-suggestion 两个新接口的用途、只读性与错误响应。
2. THE Studio SHALL 在 docs/config-reference.md 中说明配置项 studio.material_digest_chars 的含义、默认值 12000 与取值 0 表示关闭，且说明与 config.example.yaml 一致。
3. WHEN 实现完成，THE 开发者 SHALL 确认以下检查全部以退出码 0 结束：uv run ruff check backend/src tests；python scripts/build-messages.py --check；python scripts/check-doc-links.py；对 frontend/src/*/*.js 中每个文件执行 node --check；覆盖 Material_Digest、Writer、Storyboarder、Visual、召回词重写与 Split_Eval 的 pytest 测试文件（含改写后的 tests/test_studio_split.py）；本特性新增的全部 .cjs 前端测试。
4. IF 第 3 条中任一检查以非 0 退出码结束，THEN THE 开发者 SHALL 修复问题后重新运行该检查直至通过，且不得以跳过、xfail 或删除测试的方式使其通过。
5. THE Studio_Frontend SHALL 参照 tests/studio_script_ui.cjs 的方式编写前端测试：用 Node 的 vm 模块加载 studio.js 中的真实代码片段，不依赖 Playwright 或真实浏览器，至少覆盖 Visual 的显示与编辑、保存时 PATCH 包含 Visual、合并分镜时用"；"拼接 Visual 并截断到 120 字，以及重写召回词后将草稿标记为未保存。
6. THE 开发者 SHALL 在本特性开发与验证过程中不创建任何 git commit，所有改动保留在工作区中。

## 实现约束与备注（来自已核对代码，供设计阶段参考）

- 拆分镜提示词位于 backend/src/models/storyboard.py 的 split_instructions()；当前 SPLIT_VERSION=6（backend/src/studio/studio.py）；文案模型在 backend/src/models/writer.py，由 studio_workspace.execute 的 script 阶段调用。
- Shot（studio/studio.py）与 ShotBody（studio/studio_models.py）均为 StrictModel（extra=forbid），visual 必须同时添加。
- set_board 以 content["data"] != shot.model_dump() 判断是否新建内容版本，因此 visual 为空时需去掉该键。
- match() 不复用 script/split 旧版本。
- 前端改动点：studio.js 的 renderShotEditor、cellSummary、saveShot（整份 PATCH）、changeStructure（合并）。
- 后端测试需覆盖：visual 保存回读、老分镜版本号不变、拆分带 visual、只改 visual 不失效、超长 422；素材摘要的范围、隐藏、未分析、预算、指纹稳定、无索引；改写 tests/test_studio_split.py 中的提示词断言。
