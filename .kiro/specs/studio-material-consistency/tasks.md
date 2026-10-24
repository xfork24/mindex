# Implementation Plan: studio-material-consistency

## Overview

按以下顺序实现（Python 后端 + 原生 JS 前端）：先做 visual 字段的存储与透传，再做分镜表和智能助手对 visual 的支持；然后实现素材摘要模块与预览，让文案模型参考摘要；接着做拆分镜 v7 和按画面意象重写召回词；最后做全链路评测、文档与整体验证。

全程约束：
- 工作区 `data/` 与 `config.local.yaml` 只读，唯一例外是新建评测报告 `data/eval-split-<时间>.json`。不修改任务 20261023-174141-5f6104。
- 测试只使用 `tests/conftest.py` 提供的临时数据目录和配置。
- 文档、日志与错误消息用中文。
- 界面文字以 `frontend/src/shared/messages.json` 的中文条目为唯一源，改完运行 `python scripts/build-messages.py` 生成 `messages.js`。
- 不创建任何 git commit。

属性测试使用 Hypothesis（`@settings(max_examples=100, deadline=None)`），每个属性测试注明 `# Feature: studio-material-consistency, Property N: …`。

## Tasks

- [x] 1. 后端支持 visual 字段的存储与透传
  - [x] 1.1 在分镜模型中加入 visual 字段及规范化函数
    - `shared/types.py`：`ShotDraft` 新增 `visual: str = ""` 作为第一个字段（strip，不设长度上限）
    - `studio/studio.py`：新增 `VISUAL_LIMIT = 120`、`normalize_visual`、`clip_visual`、`visual_error`（中文消息，指出第一个超长分镜序号与字数）；`Shot.visual: str | None = None`，带 field_validator（strip，空值转 None，超过 120 抛中文 ValueError），并用 wrap 型 `model_serializer` 在 visual 为 None 时去掉该键
    - `studio/studio_models.py`：`ShotBody.visual: str | None = None`（只约束类型）
    - 在 `pyproject.toml` 的 dev 依赖中加入精确固定版本的 hypothesis，并同步 `uv.lock`
    - _Requirements: 7.3, 7.5, 7.7_

  - [x] 1.2 编写 clip_visual 的属性测试（tests/test_studio_visual.py）
    - **Property 15: 模型输出 visual 的截断**
    - **Validates: Requirements 7.2**

  - [x] 1.3 编写 visual_error 的属性测试（tests/test_studio_visual.py）
    - **Property 16: 人工提交的超长 visual 被拒绝**
    - **Validates: Requirements 7.4, 8.10**

  - [x] 1.4 编写 visual 往返的属性测试（tests/test_studio_visual.py）
    - **Property 17: visual 往返**
    - **Validates: Requirements 7.5, 7.6**

  - [x] 1.5 编写空 visual 不改变老分镜 dump 的属性测试（tests/test_studio_visual.py）
    - **Property 18: 空 visual 不改变老分镜的 dump**
    - **Validates: Requirements 7.3, 7.7**

  - [x] 1.6 在各条路径中透传 visual
    - `studio_workspace.execute` 的 split 阶段与 `studio_steps.run_split`：`Shot(..., visual=clip_visual(d.visual)[0])`，发生截断时记录 `split-visual-truncated` 诊断（中文），不让拆分失败
    - `studio_api.patch_storyboard` 构造 `Shot` 时带上 `visual`；`patch_storyboard` 和 `patch_plan` 在写入前用 `visual_error` 检查，超长时整份请求返回 422 中文错误，不 persist
    - 确认 `select_version`（content）整体透传 visual；`studio.save_storyboard` 只比较 text/query 来决定失效
    - `tests/conftest.py::FixtureStoryboarder` 输出确定性的 visual
    - _Requirements: 7.2, 7.4, 7.8, 7.9, 7.10, 7.11_

  - [x] 1.7 编写 visual 不进签名的属性测试（tests/test_studio_visual.py）
    - **Property 19: visual 不进签名**
    - **Validates: Requirements 7.9, 7.10**

  - [x] 1.8 编写 visual 后端集成测试（tests/test_studio_visual_flow.py）
    - 保存后回读：execute split、run_split、patch_storyboard（旧任务）、patch_plan、select_version(content) 各一例，读回的 visual 逐字符一致
    - 老分镜（无 visual）重新保存后 content 版本号不变；拆分产物带 visual
    - 只改 visual 时 preflight 中 recall/tts/select/render 均复用、conflicts 为空
    - 超长 visual 返回 422，workspace.json 逐字节不变；monkeypatch SPLIT_VERSION 后已有方案的 board 与 content 不变
    - _Requirements: 7.4, 7.6, 7.7, 7.8, 7.10, 7.11_

- [x] 2. 分镜表显示和编辑画面意象，智能助手同步支持
  - [x] 2.1 前端分镜表支持 visual
    - `studio.js`：`renderShotEditor` 的"文案与查询"格第一项加入 visual 文本框，标签为双语"画面意象"；输入为空白时删除 `draft.data.visual`，有改动时标记未保存
    - `cellSummary` 在 visual 非空时显示完整 visual，为空时不显示该行
    - `saveShot` 发出整份 PATCH，每镜都带当前 visual；被拒时保留草稿和未保存标记，并显示服务端返回的中文 detail
    - 新增顶层函数 `joinVisual`，`changeStructure("merge")` 用它合并 visual：跳过空值，用"；"拼接，按码点截断到 120 字，结果为空时删键
    - 在 `messages.json` 新增 `ui.visual_label` 等中文条目并提供英文译文，然后运行 `python scripts/build-messages.py`
    - `studio.html` 中被改动资源的 `?v=` 更新为 `20261026-material-digest`
    - _Requirements: 7.12, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 12.6_

  - [x] 2.2 编写分镜表 visual 的前端测试（tests/studio_visual_ui.cjs + tests/test_studio_visual_ui.py 包装）
    - 参照 `tests/studio_script_ui.cjs`，用 vm 截取 studio.js 的真实代码运行；node 不可用时由 pytest 包装 skip
    - 覆盖：编辑器预填 visual 和双语标签；输入后标记未保存、改回原值后取消标记；cellSummary 有 visual 时显示、为空时不显示；PATCH 中每镜都带 visual；保存被拒后草稿保留；合并用"；"拼接并截断到 120 字（含 emoji 与恰好 120/121 字的边界），全为空时没有 visual
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 13.5_

  - [x] 2.3 智能助手工具支持 visual
    - `agent/tools/studio_write.py`：`ShotFields.visual`；`LABELS` 加入"画面意象"；新增 `join_visual`，并在 `merge_into` 中使用；update 时 `visual=""` 删除该键；split 操作中未提供 visual 的段继承原镜 visual；`plan_shots` 在发出 PATCH 前调用 `visual_error`，超长时抛 `ToolError(code="invalid_shot")`
    - `agent/tools/common.py`：`shot_rows` 输出 visual（为空时输出 None）；`board_text` 在 visual 非空时附一行"画面意象：…"
    - _Requirements: 8.9, 8.10, 8.11_

  - [x] 2.4 编写合并 visual 的属性测试（tests/test_agent_write.py）
    - **Property 20: 合并 visual 的拼接与截断**
    - **Validates: Requirements 8.6, 8.9**

  - [x] 2.5 编写智能助手 visual 测试（tests/test_agent_write.py）
    - 覆盖：update 设置和清空 visual；split 时继承 visual；超长 visual 返回 ToolError 且不发 PATCH、方案不变；`shot_rows` 有/无 visual 两种情况
    - _Requirements: 8.9, 8.10, 8.11_

- [x] 3. Checkpoint - 确保所有测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. 素材摘要模块、配置项、预览接口与任务元信息折叠块
  - [x] 4.1 新增摘要数据模型与配置项
    - `shared/types.py`：新增 `DigestSource`、`MaterialDigest`（`empty`、`stats()`、`public()`、`empty_of()`），并保持不变式 `empty ⇔ source_count==0 ⇔ text=="" ⇔ empty_reason 非空`
    - `shared/config.py`：`StudioConfig.material_digest_chars: int = Field(default=12000, ge=0, le=100000, strict=True)`
    - 同步 `config.example.yaml`（在 max_shots 后加入并配中文注释）和 `docs/config-reference.md`（说明含义、默认值 12000、取值范围、0 表示关闭）
    - _Requirements: 2.8, 2.10, 3.1, 3.2, 13.2_

  - [x] 4.2 实现 `backend/src/studio/material_digest.py`
    - 纯函数：`parse_analysis`、`allocate`、`sample`、`compose_digest`（过滤、按 `(name, id)` 排序素材、故事线排序/去重/抽样并在无 scene 时回退到 shot、前 30 个画面词、按配额渲染、指纹、统计）、`merged_vocabulary`
    - I/O 外壳：`build_digest(config, store, state)` 只读生效快照与覆盖层；空原因优先级为"预算为 0 → 无生效索引 → 无已分析镜头"
    - `safe_digest(config, job_id)`：捕获异常后返回空 Digest，并记录中文诊断
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.9, 2.10, 3.6_

  - [x] 4.3 编写范围过滤的属性测试（tests/test_material_digest.py）
    - **Property 1: 范围过滤**
    - **Validates: Requirements 1.2, 1.7, 1.9**

  - [x] 4.4 编写不合格片段不影响摘要的属性测试（tests/test_material_digest.py）
    - **Property 2: 不合格片段与非分析文字不影响 Digest**
    - **Validates: Requirements 1.3, 1.4, 1.5, 1.6, 2.10**

  - [x] 4.5 编写确定性的属性测试（tests/test_material_digest.py）
    - **Property 3: 确定性与顺序无关**
    - **Validates: Requirements 1.1, 2.1, 2.9**

  - [x] 4.6 编写指纹敏感性的属性测试（tests/test_material_digest.py）
    - **Property 4: 指纹对分析文字敏感**
    - **Validates: Requirements 2.9**

  - [x] 4.7 编写预算分配的属性测试（tests/test_material_digest.py）
    - **Property 5: 预算分配与总长度上界**
    - **Validates: Requirements 2.5, 2.6**

  - [x] 4.8 编写故事线抽样的属性测试（tests/test_material_digest.py）
    - **Property 6: 故事线排序、去重与等间距抽样**
    - **Validates: Requirements 2.2**

  - [x] 4.9 编写画面词表的属性测试（tests/test_material_digest.py）
    - **Property 7: 画面词表与参考实现一致**
    - **Validates: Requirements 2.4**

  - [x] 4.10 编写素材摘要示例测试（tests/test_material_digest_examples.py）
    - 覆盖：范围、隐藏、未分析、预算（含 0 和小于 200×n）、指纹稳定、无生效索引、scope 为空集、只有 shot 描述、只有 moment 描述，以及空原因优先级的各种组合
    - 配置测试：默认值 12000；负数、`1.5`、`"12000"`、超过 100000 被拒绝，错误中包含 `studio.material_digest_chars`；`config.example.yaml` 加载后为 12000
    - _Requirements: 1.2, 1.3, 1.4, 1.8, 1.9, 2.3, 2.5, 2.6, 2.7, 2.8, 2.9, 3.1, 3.2, 3.6_

  - [x] 4.11 实现 GET /api/studio/jobs/{job}/material-digest
    - 在 `studio_api.py` 中实现：用 `studio.load_job` 加载任务，不存在时返回 404"找不到创作任务"；调用 `build_digest` 并返回 `public()`；全程不加锁、不 persist
    - _Requirements: 3.3, 3.4, 3.5, 3.6, 12.4_

  - [x] 4.12 前端任务元信息中的素材摘要折叠块
    - `studio.js`：`loadMaterialDigest(jobId)` 按任务缓存结果；`renderJobMeta` 追加默认折叠的 `<details class="material-digest">`，summary 显示标题和计数，展开后显示正文；摘要为空时显示原因、不显示正文；请求失败时显示"素材摘要加载失败"，其余元信息正常显示
    - 在 `messages.json` 新增 `ui.material_digest_*` 条目，然后运行 build-messages
    - _Requirements: 3.7, 3.8, 3.9, 12.6_

  - [x] 4.13 编写预览接口与折叠块测试
    - `tests/test_studio_material_api.py`：返回字段、404；持有 `studio.lock` 时仍返回 200；请求前后任务目录文件哈希一致
    - `tests/studio_material_ui.cjs`（并在 pytest 包装中调用）：默认折叠并显示计数、为空时显示原因、失败时显示提示且其余元信息正常
    - _Requirements: 3.3, 3.4, 3.5, 3.7, 3.8, 3.9, 12.4_

- [x] 5. 文案模型参考素材摘要（v2）
  - [x] 5.1 实现 Writer v2
    - `models/writer.py`：`WRITER_PROMPT_VERSION = 2`，新增 `MATERIAL_RULES`、`MATERIAL_HEADER`；`write(outline, materials=None)` 在 Digest 非空时，每次尝试都附上规则和独立的摘要段；Digest 为空或为 None 时请求与 v1 逐字节一致
    - 同步 `adapters.Writer` 协议，以及 `tests/conftest.py::FixtureWriter` 的签名（记录收到的 materials）
    - _Requirements: 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_

  - [x] 5.2 编写空摘要请求一致性的属性测试（tests/test_studio_writer.py）
    - **Property 8: 空摘要时请求与 v1 一致**
    - **Validates: Requirements 4.2, 4.5**

  - [x] 5.3 编写非空摘要独立段的属性测试（tests/test_studio_writer.py）
    - **Property 9: 非空摘要在每次尝试中作为独立段发送**
    - **Validates: Requirements 4.4, 4.6, 4.7, 4.8, 4.9**

  - [x] 5.4 script 阶段接入 Digest 并在前端显示参考提示
    - `studio_workspace.execute`（script）：用 `safe_digest` 生成 Digest 并传给 `write(..., materials=digest)`；Digest 非空时写入 `data.materials` 和调试记录 `material-digest`（全文与统计），为空或出错时都不写
    - `studio.js`：`renderScript` 在当前版本有 `data.materials` 时显示"参考了 N 个素材、M 个镜头描述"；在 messages.json 新增 `ui.script_materials_note`，然后运行 build-messages
    - _Requirements: 4.3, 4.10, 4.11, 4.12, 4.13_

  - [x] 5.5 编写 script 阶段与提示显示测试
    - `tests/test_studio_script_materials.py`：v2 使 script 签名变化；FixtureWriter 收到的 Digest 与 `build_digest` 一致；`data.materials` 与调试记录在 Digest 非空时存在、为空时不存在；`build_digest` 抛错时文案仍生成且没有 `data.materials`；非空时提示词包含 4.6–4.9 的规则文字
    - `tests/studio_material_ui.cjs`：补充"参考了 N 个素材"提示在有/无 `data.materials` 时的显示与隐藏
    - _Requirements: 4.1, 4.3, 4.6, 4.7, 4.8, 4.9, 4.10, 4.11, 4.12, 4.13_

- [x] 6. Checkpoint - 确保所有测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. 拆分镜提示词 v7 与校验重试
  - [x] 7.1 重写拆分镜提示词
    - `models/storyboard.py`：新增 `QUERY_RULES` 公共常量和 `VOCABULARY_RULE`；`CONCRETE_EXAMPLE`、`ABSTRACT_EXAMPLE` 两个对照示例都不涉及新加坡；`split_instructions(max_shots, *, vocabulary=False)` 依次包含产出顺序（visual → text → query）、粒度规则（具体意象单独成镜、最短长度、抽象列举不拆、超长再拆、单一意象长句不拆）、text 规则、visual 规则、召回词规则、示例和 Schema；新增 `query_instructions(*, vocabulary=False)`
    - `studio/studio.py`：`SPLIT_VERSION = 7` 并更新注释
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.9, 5.10, 5.11, 7.1, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8_

  - [x] 7.2 实现拆分校验纯函数
    - `models/storyboard.py`：`measure`、`is_short`、`is_long`、`whole_sentence`、`hard_check`（复用 coverage_warning，并检查数量 1..max_shots）、`soft_issues`（每镜最多计 1 个问题）、`choose_attempt`、`feedback`（中文，包含分镜序号）
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 7.3 实现 `split(text, vocabulary=None)` 的重试与最佳尝试选择
    - `QwenStoryboarder.split`：有词表时附加独立的词表段；循环 split_retries+1 次，硬校验失败或有软问题时带反馈重试；软问题为 0 时立即返回；否则返回最佳尝试；全部失败时抛中文 `ModelError`
    - 同步 `adapters.Storyboarder` 协议、`FixtureStoryboarder`，以及测试中只接受 `split(self, text)` 的替身（如 Divergent）
    - `studio_workspace.execute` 的 split 阶段用 `merged_vocabulary(safe_digest(...))` 生成词表并以关键字参数传入；`run_split` 不传词表
    - _Requirements: 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x] 7.4 编写硬校验的属性测试（tests/test_studio_split_props.py）
    - **Property 10: 硬校验接受覆盖原文的切分并拒绝其他**
    - **Validates: Requirements 5.11, 6.1**

  - [x] 7.5 编写软校验的属性测试（tests/test_studio_split_props.py）
    - **Property 11: 软校验按镜计数**
    - **Validates: Requirements 6.2**

  - [x] 7.6 编写重试与最佳尝试的属性测试（tests/test_studio_split_props.py）
    - **Property 12: 重试与最佳尝试选择**
    - **Validates: Requirements 6.3, 6.4, 6.5, 6.6**

  - [x] 7.7 编写召回词规则公共常量的属性测试（tests/test_studio_split_props.py）
    - **Property 14: 召回词规则是公共常量**
    - **Validates: Requirements 9.5**

  - [x] 7.8 改写 tests/test_studio_split.py 的提示词断言
    - 去掉 `樟宜机场 旅行 室内`、"不设最低字数"等旧断言，改为断言新规则：提示词包含全部粒度规则，`SPLIT_VERSION == 7`；示例恰好一对且不含新加坡相关词表中的任一词；每个示例都通过 `hard_check`、没有软问题；`ShotDraft` 的第一个字段是 visual；提示词按 visual → text → query 的顺序描述；拆分失败时 board 不变
    - _Requirements: 5.1, 5.9, 5.10, 6.6, 7.3_

- [x] 8. 按画面意象重写召回词
  - [x] 8.1 实现 suggest_query
    - `models/storyboard.py`：`QwenStoryboarder.suggest_query(text, visual, context, vocabulary)` 使用 `query_instructions`，只调用一次；返回空 query 时抛 `ModelError`
    - `adapters.Storyboarder` 协议新增 `suggest_query`；`tests/conftest.py::FixtureStoryboarder.suggest_query` 按输入确定性地生成结果
    - _Requirements: 9.5, 10.1_

  - [x] 8.2 编写词表只在提供时进入请求的属性测试（tests/test_studio_split_props.py）
    - **Property 13: 词表只在提供时进入请求**
    - **Validates: Requirements 6.7, 9.3, 9.7**

  - [x] 8.3 实现 POST …/plans/{plan}/shots/{sid}/query-suggestion
    - `studio_models.py` 新增 `QuerySuggestionBody`
    - `studio_api.py`：不存在的任务/方案/分镜返回 404，分别为"找不到创作任务/构建方案/分镜"；visual 为空返回 422"请先填写画面意象，再重写召回词"，超长返回 422；上下文取前后镜的 text，词表取 `merged_vocabulary(safe_digest(...))`；`ModelError` 包装为"召回词重写失败：…"并返回 503；不加锁、不 persist，不使用 `read_workspace()`
    - _Requirements: 10.2, 10.3, 10.4, 10.7, 12.4_

  - [x] 8.4 前端"按画面意象重写召回词"按钮
    - `studio.js`：`renderShotEditor` 新增按钮和 `role="status"` 的错误提示区；顶层函数 `rewriteQuery` 在请求期间禁用按钮，成功后替换草稿 query、同步输入框并 `markDirty()`，失败时显示中文错误、草稿与未保存标记不变，结束后恢复按钮
    - 在 messages.json 新增 `ui.visual_rewrite_*` 条目，然后运行 build-messages
    - _Requirements: 10.5, 10.6, 10.8, 12.6_

  - [x] 8.5 编写重写召回词测试
    - `tests/test_studio_query_suggestion.py`：请求带 visual 和省略 visual 两种情况；持锁时仍返回 200 且任务目录哈希不变；三种 404、空白 visual 返回 422、超长返回 422 时替身调用次数都为 0；替身抛 `ModelError` 或返回空串时返回 503
    - `tests/studio_visual_ui.cjs`：补充请求期间按钮禁用、成功后替换 query 并标记未保存、失败时状态不变且按钮恢复
    - _Requirements: 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 13.5_

- [x] 9. Checkpoint - 确保所有测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. 全链路评测
  - [x] 10.1 编写 eval/split_cases.jsonl
    - 12–16 条用例，字段为 `id`、`text`、`expected`、`note`；必须包含需求 5.7 的例句（期望 5 镜）和 5.8 的例句（期望 2 镜）
    - _Requirements: 11.1_

  - [x] 10.2 实现 eval/split_eval.py
    - 纯函数：`case_matches`、`match_rate`（空集返回 0.0）、`short_count`（复用 `is_short`/`whole_sentence`）、`average_seconds`
    - 参数 `--cases`、`--materials JOB_ID`（任务不存在时打印中文错误、以退出码 2 退出且不拆分；Digest 为空时打印原因，不带 vocabulary 继续）、`--recall`（每镜取前 3 条结果，零结果时标注"该镜无召回结果"）、`--output`
    - 单条用例失败时标记为 failed 并记录中文原因，继续评测下一条；报告写到 `data/eval-split-YYYYMMDD-HHMMSS.json`，不读写任务 workspace，不加锁
    - _Requirements: 11.2, 11.3, 11.4, 11.5, 11.10, 11.11, 11.12, 12.1, 12.3_

  - [x] 10.3 编写评测指标的属性测试（tests/test_split_eval.py）
    - **Property 21: 评测指标**
    - **Validates: Requirements 11.2, 11.11**

  - [x] 10.4 编写评测单元测试（tests/test_split_eval.py）
    - 吻合度五种情形：完全一致、仅首尾空白不同、镜数不同、镜数相同但边界不同、空用例集
    - 用例集有 12–16 条且包含两句例句；`--materials` 把 vocabulary 传给替身；`--recall` 覆盖 ≥3、<3、0 条结果；报告文件名格式正确，测试中输出到临时目录；JOB_ID 不存在时退出码非零且未调用 split；Digest 为空时提示原因并以 vocabulary=None 继续
    - _Requirements: 11.1, 11.3, 11.4, 11.5, 11.6, 11.10, 11.12_

  - [x] 10.5 编写 eval/README.md 的"拆分镜全链路评测"一节
    - 中文说明用例格式（字段含义和一行示例）、运行命令示例，以及 `--materials`、`--recall` 的作用
    - _Requirements: 11.7, 12.5_

  - [x] 10.6 用真实服务运行评测并做只读带召回检查
    - 运行前对 `data/studio/20261023-174141-5f6104/` 计算 sha256 清单，并导出 `index_members`、`segment_curation`、`source_curation` 的行哈希
    - 用 GLM-5.3-Flash 运行 `uv run python eval/split_eval.py`，再运行 `uv run python eval/split_eval.py --materials 20261023-174141-5f6104 --recall`
    - 运行后重新计算哈希，必须完全一致；`data/` 下只允许新增 `eval-split-*.json`
    - 如果吻合度低于 80%，或两句例句中任一句不吻合，最多对 `models/storyboard.py` 的提示词做两轮针对性微调，每轮保留报告并记录三项指标和提示词改动；提示词改动后重跑 7.8 中的测试
    - _Requirements: 11.8, 11.9, 12.1, 12.3_

- [x] 11. 文档与整体验证
  - [x] 11.1 更新 docs/studio.md
    - 分节说明：Material_Digest（范围、结构、预算）；visual 字段（120 字上限、不进签名、合并规则）；拆分镜粒度规则与 SPLIT_VERSION 7；按画面意象重写召回词的操作流程；两个新接口的用途、只读性和 404/422/503 响应。同时替换文末旧的"不设最低字数"描述
    - _Requirements: 13.1, 12.5_

  - [x] 11.2 运行全部检查并修复问题
    - 运行 `uv run ruff check backend/src tests`、`python scripts/build-messages.py --check`、`python scripts/check-doc-links.py`、对 `frontend/src/*/*.js` 中每个文件运行 `node --check`、相关 pytest 文件（含改写后的 tests/test_studio_split.py），以及新增的全部 .cjs 测试
    - 所有检查都要以退出码 0 结束；失败时修复后重跑，不得用 skip、xfail 或删除测试的方式绕过；确认 messages.js 与 messages.json 一致；不创建 git commit
    - _Requirements: 12.7, 13.3, 13.4, 13.6_

- [x] 12. Final checkpoint - 确保所有测试通过
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标 `*` 的子任务是可选的属性测试，跳过可以更快得到 MVP。用户明确要求的示例测试、集成测试与 cjs 测试没有标为可选。
- 每个任务都标注了对应的需求条目，便于追溯。
- 属性测试与 design.md 的 Correctness Properties 一一对应。为了能和示例测试并行，拆分镜相关的属性测试放在 `tests/test_studio_split_props.py`，`tests/test_studio_split.py` 只保留改写后的示例断言。
- 需求 7.12 实际走的是 `patch_plan` 保存路径（见 design.md"与需求的出入"），两条路径都透传 visual。
- 只有任务 10.6 会访问真实服务与真实 data/，而且只读（评测报告除外）。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.6", "4.1"] },
    { "id": 2, "tasks": ["1.3", "1.8", "2.1", "2.3", "4.2", "5.1"] },
    { "id": 3, "tasks": ["1.4", "2.2", "2.4", "4.3", "4.10", "4.11", "5.2", "7.1"] },
    { "id": 4, "tasks": ["1.5", "2.5", "4.4", "4.12", "5.3", "7.2"] },
    { "id": 5, "tasks": ["1.7", "4.5", "4.13", "5.4"] },
    { "id": 6, "tasks": ["4.6", "5.5", "7.3"] },
    { "id": 7, "tasks": ["4.7", "7.4", "7.8", "8.1"] },
    { "id": 8, "tasks": ["4.8", "7.5", "8.3"] },
    { "id": 9, "tasks": ["4.9", "7.6", "8.4"] },
    { "id": 10, "tasks": ["7.7", "8.5", "10.1"] },
    { "id": 11, "tasks": ["8.2", "10.2"] },
    { "id": 12, "tasks": ["10.3", "10.5"] },
    { "id": 13, "tasks": ["10.4"] },
    { "id": 14, "tasks": ["10.6"] },
    { "id": 15, "tasks": ["11.1"] },
    { "id": 16, "tasks": ["11.2"] }
  ]
}
```
