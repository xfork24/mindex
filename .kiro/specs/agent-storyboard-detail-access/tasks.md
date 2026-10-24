# Implementation Plan: 智能助手读取分镜工作台分镜详情

## Overview

使用 Python 实现。先在 `backend/src/agent/tools/shot_details.py` 中实现不依赖 I/O 的纯函数（详情构建、字幕与配音、分页、引用解析、页面定位、渲染与预算），再在 `studio_read.py` 中新增 `ShotDetailsArgs` 与 `get_shot_details` 处理函数并注册到 `studio` 领域，随后更新 `board_text`、`get_workspace_summary`、`edit_shots`、`SYSTEM_PROMPT` 与 MCP `INSTRUCTIONS` 文本，最后更新既有工具数量断言并补齐示例、集成与 MCP 测试。性质测试使用项目已有的 `hypothesis`，集中在 `tests/test_agent_shot_details.py`。

## Tasks

- [x] 1. 实现分镜详情纯函数（数据部分）
  - [x] 1.1 新建 `backend/src/agent/tools/shot_details.py`，实现 `plan_header`、`shot_detail`、`subtitle_info`、`speech_info`、`page_details`
    - `plan_header(view)` 返回 `{job_id, plan_id, plan_name, revision, active, total}`，`revision` 取 `view.revision`，`total = len(view.order)`
    - `shot_detail(view, sid, index)` 按 Data Models 构建 Shot_Detail：`text`/`query`/`estimated_seconds` 原样取自内容版本；`visual`、`recall` 空值为 `None`；`granularity`、`recall_limit` 缺省为 `None`；`manual_range` 取 `plan["ranges"].get(sid)`；`short_id` 用 `short(sid)`；`stages` 用 `view.stage_state` 覆盖 `STAGES` 四个阶段
    - `subtitle_info(view, sid, narration)`：选用选片版本 `status == "complete"` 且 `data.event.subtitle` 为含 `text/start/end` 的字典时返回 `{"available": True, text, start, end, "stale": text != narration}`，否则只返回 `{"available": False}`；全程 `.get()` 容错
    - `speech_info(view, sid)`：选用配音版本 `status == "complete"` 且 `data.duration` 为数值（排除 bool）时返回 `{"available": True, duration, "version": short(vid)}`，否则只返回 `{"available": False}`
    - `page_details(view, offset, limit)` 只为 `view.order[offset:offset + limit]` 构建详情，`index` 为全局 1 起序号
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.7, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.6, 7.3_

  - [x]* 1.2 新建 `tests/test_agent_shot_details.py`，编写 hypothesis 生成器（`shot_ids`、`contents`、`versions`、`workspace`、`refs`）与性质测试：列表详情完整且与工作台数据一致
    - **Property 1: 列表详情完整且与工作台数据一致**
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.5, 3.6**

  - [x]* 1.3 编写性质测试：字幕信息可用性与过期标记
    - **Property 3: 字幕信息可用性与过期标记**
    - **Validates: Requirements 3.1, 3.3, 3.5**

  - [x]* 1.4 编写性质测试：配音时长可用性
    - **Property 4: 配音时长可用性**
    - **Validates: Requirements 3.2, 3.4**

  - [x]* 1.5 编写性质测试：分页窗口与头部信息
    - **Property 5: 分页窗口与头部信息**
    - **Validates: Requirements 1.7, 4.6, 7.3, 7.4**

  - [x] 1.6 在 `shot_details.py` 中实现 `resolve_refs` 与 `details_focus`
    - `resolve_refs(view, refs)` 逐项调用既有 `resolve_shot(view.order, ref)`，按提供顺序返回；`missing` 映射为 `not_found`；ok 项含 `ref/status/index/shot_id/detail`（`detail` 复用 `shot_detail`）；ambiguous 项含全部 `candidates`（`index`、`shot_id`）且不含 `detail`；not_found 项含 `reason`，超范围 `#N` 时说明方案分镜总数
    - `details_focus(view, items)`：列表模式或无 ok 项 → `page="studio"`、`job_id`、`plan_id`、`section="storyboard"`；否则 `shots` 为按引用顺序去重后的 ok `shot_id` 前 20 个
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 7.1, 7.2_

  - [x]* 1.7 编写性质测试：引用解析与 resolve_shot 一致
    - **Property 6: 引用解析与 resolve_shot 一致**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

  - [x]* 1.8 编写性质测试：按引用读取与按列表读取结果相同
    - **Property 7: 按引用读取与按列表读取结果相同**
    - **Validates: Requirements 2.6**

  - [x]* 1.9 编写性质测试：页面定位
    - **Property 9: 页面定位**
    - **Validates: Requirements 7.1, 7.2**

- [x] 2. 实现分镜详情纯函数（渲染部分）
  - [x] 2.1 在 `shot_details.py` 中实现 `render_detail`、`fit_blocks`、`render_list`、`render_refs`
    - `render_detail` 按设计的渲染格式输出分镜块：旁白、查询、画面意象、字幕文本放在「」内原样输出（不经 `clip`）；空值显示「（无）」；`recall` 与人工区间用 `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`；`start`/`end`/`duration` 用 `f"{value!r}"`；字幕/配音缺失写明缺失与对应阶段状态及版本短 ID；字幕过期写「字幕已过期」
    - `fit_blocks(head, blocks, tail, budget=MAX_TEXT)` 按完整块累加，blocks 非空时至少返回 1
    - `render_list(header, details, offset)` 返回 `(text, shown)`：头部含任务、方案名与 ID、当前/非当前方案、revision、总数与本次范围；空方案写「当前方案还没有分镜。」；`offset ≥ total > 0` 写超出范围说明；有后续时末尾写 `offset=<offset+shown>`
    - `render_refs(header, items)` 返回 `(text, rendered)`：ok 项输出分镜块，ambiguous 项列候选并提示向用户确认，not_found 项写原因；超预算时末尾列出未展开的引用
    - _Requirements: 1.3, 1.4, 1.6, 2.3, 2.4, 3.7, 7.4, 7.6, 7.7_

  - [x]* 2.2 编写性质测试：渲染文本逐字保留并完整呈现各字段
    - **Property 2: 渲染文本逐字保留并完整呈现各字段**
    - **Validates: Requirements 1.3, 1.4, 3.7**

  - [x]* 2.3 编写性质测试：渲染预算与结构化结果一一对应
    - **Property 10: 渲染预算与结构化结果一一对应**
    - **Validates: Requirements 7.6, 7.7, 1.4**

- [x] 3. Checkpoint - 确保纯函数测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. 注册 `get_shot_details` 工具
  - [x] 4.1 在 `backend/src/agent/tools/studio_read.py` 中新增 `ShotDetailsArgs` 与 `get_shot_details`，并在 `specs()` 中 `resolve_shots` 之后注册
    - `ShotDetailsArgs`：`refs: list[str] | None`（1–50，`no_blank` 校验）、`offset: StrictInt | None`（≥0）、`limit: StrictInt | None`（1–100）、`job_id`、`plan_id`；`model_validator` 拒绝 `refs` 与 `offset`/`limit` 同时提供
    - 处理函数经 `load_view` 只发一次 GET；列表模式组装 `mode/offset/returned/has_more/next_offset/shots`（`shots` 截为渲染输出的前 `shown` 个）；指定模式组装 `mode/returned/unrendered_refs/results`；`focus` 用 `details_focus`；`meta={"kind": "shot_details", ...}`；`mutation` 为 `None`，不返回 `media`
    - `ToolSpec("get_shot_details", "studio", <设计中的描述>, ShotDetailsArgs, "read", get_shot_details)`，`mcp` 为 true
    - _Requirements: 1.1, 1.6, 1.7, 2.1, 2.5, 2.7, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 5.2, 5.5, 5.6, 7.3, 7.5, 7.7_

  - [x]* 4.2 编写性质测试：非法参数一律拒绝
    - 通过 `ToolSpec.parse` 验证非法组合抛 `invalid_arguments`、合法组合解析成功
    - **Property 8: 非法参数一律拒绝**
    - **Validates: Requirements 2.7, 7.5**

  - [x] 4.3 更新既有工具数量断言
    - `tests/test_agent_tools.py` 的 `len(registry) <= 30` 与 `tests/test_agent_changes.py` 的 `len(registry) == 30` 改为 31；`tests/test_agent_mcp.py` 的 MCP 导出数 28 改为 29
    - 在 `tests/test_agent_tools.py` 中断言 `build_registry()` 恰好有一个 `get_shot_details`，`domain == "studio"`、`risk == "read"`、`mcp is True`
    - _Requirements: 5.1_

  - [x]* 4.4 在 `tests/test_agent_shot_details.py` 中编写示例与集成测试（`board` 夹具 + `run_tool` 进程内回环）
    - 列表模式与 `refs=["#2", "a1daad59", "925281d", "#9"]` 的文本与结构化结果（ok / ambiguous / not_found）
    - 带 `event.subtitle` 的选片版本与带 `data.duration` 的配音版本：值在结构化与文本中相等；修改旁白后出现字幕过期提示
    - 任务与方案定位：页面同任务含 `plan_id`、页面为其他任务、页面无 plan；`missing_plan` 消息含方案列表；不存在的 `job_id` 返回 `http_404`
    - 记录调用的假客户端：`missing_job` 与参数非法时零次请求，正常执行仅一次 `get`
    - 只读不变性：执行前后 revision、分镜数量与各分镜选用版本 ID 不变，`mutation is None`
    - 用 `ScriptedLlm` 调用时会话事件中没有 approval-asked
    - _Requirements: 1.6, 2.5, 3.1, 3.2, 3.5, 4.1, 4.2, 4.3, 4.4, 4.5, 4.7, 5.2, 5.5, 5.6_

- [x] 5. 更新既有文本与工作流指引
  - [x] 5.1 更新 `backend/src/agent/tools/common.py` 的 `board_text` 与 `studio_read.py` 中 `get_workspace_summary` 描述
    - 有分镜时在分镜表末尾追加「（旁白与画面意象截断到 60 字、查询截断到 30 字；完整内容请用 get_shot_details 读取）」；空方案保持原文本
    - `get_workspace_summary` 描述注明截断并指向 `get_shot_details`
    - _Requirements: 6.2_

  - [x] 5.2 更新 `backend/src/agent/tools/studio_write.py` 中 `edit_shots` 工具描述与 `ShotOperation.parts` 字段描述
    - 注明 split 各段旁白须由 `get_shot_details` 返回的完整旁白按顺序切分，拼接（忽略空白）须等于原旁白；不改校验逻辑
    - _Requirements: 6.3, 6.5_

  - [x] 5.3 更新 `backend/src/agent/prompts.py` 的 `SYSTEM_PROMPT` 与 `backend/src/agent/mcp_server.py` 的 `INSTRUCTIONS`
    - `SYSTEM_PROMPT` 第 2 条后插入设计中的第 3 条（split/merge/update text 前先读完整旁白；被拒后重新读取再提交），后续条目顺延编号
    - `INSTRUCTIONS` 补充：拆分或改写旁白前用 `get_shot_details` 读取完整旁白
    - _Requirements: 6.1, 6.6_

  - [x]* 5.4 编写性质测试：按完整旁白切分的 split 往返
    - 直接调用 `studio_write.plan_shots(view, [ShotOperation(op="split", ...)])`
    - **Property 11: 按完整旁白切分的 split 往返**
    - **Validates: Requirements 6.4**

  - [x]* 5.5 编写性质测试：旁白不一致的 split 被拒绝
    - **Property 12: 旁白不一致的 split 被拒绝**
    - **Validates: Requirements 6.5**

  - [x]* 5.6 在 `tests/test_agent_tools.py` 中编写文本检查测试
    - `SYSTEM_PROMPT` 含 `get_shot_details` 及 split、merge、重读旁白指引；`get_workspace_summary`、`edit_shots` 描述与 `board_text` 输出含截断说明与工具名；空方案 `board_text` 不含截断说明
    - _Requirements: 6.1, 6.2, 6.3, 6.6_

- [x] 6. MCP 导出验证
  - [x]* 6.1 在 `tests/test_agent_mcp.py` 中编写 MCP 测试
    - `list_tools` 中 `get_shot_details` 的 `inputSchema == spec.parameters()`，`readOnlyHint=True`、`destructiveHint=False`
    - `read`/`write`/`all` 三种策略下调用成功，文本与 `structuredContent` 与内置调用相同
    - 参数非法时 `isError=True` 且未调用 `publish`
    - _Requirements: 5.3, 5.4, 5.7_

- [x] 7. Final checkpoint - 确保全部测试通过
  - 运行 `.venv/bin/python -m pytest tests/test_agent_shot_details.py tests/test_agent_tools.py tests/test_agent_write.py tests/test_agent_mcp.py tests/test_agent_changes.py -q`
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选测试任务，可为更快完成 MVP 而跳过
- 每个任务引用具体验收标准以便追溯
- 性质测试使用 `@settings(max_examples=100, deadline=None)`，并注释 `# Feature: agent-storyboard-detail-access, Property <n>: <性质标题>`
- `tests/test_agent_shot_details.py` 中的测试任务写入同一文件，依赖图中按不同波次顺序执行

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "5.2", "5.3"] },
    { "id": 1, "tasks": ["1.6", "1.2"] },
    { "id": 2, "tasks": ["2.1", "1.3"] },
    { "id": 3, "tasks": ["4.1", "1.4"] },
    { "id": 4, "tasks": ["5.1", "1.5", "4.3"] },
    { "id": 5, "tasks": ["1.7", "5.6", "6.1"] },
    { "id": 6, "tasks": ["1.8"] },
    { "id": 7, "tasks": ["1.9"] },
    { "id": 8, "tasks": ["2.2"] },
    { "id": 9, "tasks": ["2.3"] },
    { "id": 10, "tasks": ["4.2"] },
    { "id": 11, "tasks": ["4.4"] },
    { "id": 12, "tasks": ["5.4"] },
    { "id": 13, "tasks": ["5.5"] }
  ]
}
```
