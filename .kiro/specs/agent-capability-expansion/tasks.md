# Implementation Plan: 智能助手能力扩展

## Overview

使用 Python（后端）与原生 JS（前端）实现，按依赖分为以下几层：

1. 先搭基础设施：领域常量、动态加载、空工具模块骨架；
2. 再做 MCP、附件、取消、视觉等横向能力，以及找相似、抽帧、索引、诊断、识别等后端接口；
3. 最后按领域封装工具，补齐页面按钮、场景与文档。

所有新工具都通过回环调用 REST 接口。会改动 `messages.json`、`config.py`、`runtime.py` 的任务不并行。标记 `*` 的子任务是可选测试。

## Tasks

- [x] 1. 领域常量与动态加载基础
  - [x] 1.1 新建 `backend/src/shared/agent_domains.py`，定义 `AGENT_DOMAINS`、`LOADABLE_DOMAINS`、`PAGE_DOMAIN`、`DOMAIN_INFO`；`agent/registry.py` 的 `DOMAINS` 改为从这里导入；`ToolRegistry.schemas` 增加 `domains` 过滤参数，并新增 `domain_tools()`
    - _Requirements: 1.1, 1.2_
  - [x] 1.2 新建 `backend/src/agent/domains.py`，实现纯函数 `touch(loaded, domains, limit=3)`
    - _Requirements: 1.3, 1.4, 1.5_
  - [x]* 1.3 编写性质测试：领域 LRU（`tests/test_agent_domains.py`）
    - **Property 1: 领域 LRU**
    - **Validates: Requirements 1.3, 1.4, 1.5**
  - [x] 1.4 修改 `agent/session_store.py`：`agent_sessions` 增加 `loaded_domains` 列（先用 PRAGMA 检查再 ALTER），`get`/`update` 时做 JSON 编解码
    - _Requirements: 1.6_
  - [x] 1.5 修改 `agent/runtime.py`，接入动态加载：
    - 每轮开始时 touch 页面领域；
    - 每一步按 `{"meta", *loaded}` 下发工具；
    - `_call_tool` 遇到未加载领域的工具返回 `TOOL_NOT_LOADED`，工具执行成功后 touch 其领域；
    - 新增 `load_domains(sid, domains)`；
    - `PAGE_DOMAINS` 改为读取 `PAGE_DOMAIN`，删除过时的计数注释
    - _Requirements: 1.2, 1.3, 1.4, 1.6, 1.9_
  - [x] 1.6 在 `agent/tools/meta.py` 实现 `load_tools`（read，`mcp=False`，追加 ignorable 事件 `plugin:mindex/tools-loaded`）；在 `agent/prompts.py` 中让 `system_prompt(page, loaded)` 输出领域目录与加载规则
    - _Requirements: 1.7, 1.8, 1.10_
  - [x] 1.7 创建工具模块骨架：新建 `tools/search.py`（把 `search_media` 从 library.py 迁入）、`tools/index.py`、`tools/library_tasks.py`、`tools/studio_detail.py`、`tools/persons.py`（把 `list_persons` 迁入并改为 persons 领域）、`tools/persons_review.py`，其余模块的 `specs()` 先返回空列表；在 `tools/__init__.py` 统一注册；更新 `cli_agent.py` 中 `--domain` 的帮助文本
    - _Requirements: 1.1, 20.3_
  - [x]* 1.8 编写性质测试：每步工具集合与未加载拦截（使用 `ScriptedLlm.requests`）
    - **Property 2: 每步工具集合**
    - **Property 3: 未加载即不执行**
    - **Validates: Requirements 1.2, 1.7, 1.9**
  - [x] 1.9 同步更新既有测试：工具数量断言（`test_agent_changes.py`、`test_agent_tools.py`、`test_agent_mcp.py`）、`test_agent_library.py` 首步工具断言，并在 `tests/agent_scenarios.py` 中按需插入 `load_tools` 调用，使原有 10 个场景全部通过
    - _Requirements: 20.4_
  - [x]* 1.10 编写示例测试：load_tools 的非法领域、跨轮保持、服务重启后恢复、系统提示词中的领域目录
    - _Requirements: 1.6, 1.8, 1.10_

- [x] 2. MCP 领域过滤与远程模式补齐
  - [x] 2.1 `shared/config.py` 的 `AgentMcpConfig` 新增 `domains: list[str] | None`，校验取值属于 `LOADABLE_DOMAINS`；在 `config.example.yaml` 加注释
    - _Requirements: 2.1, 2.8_
  - [x] 2.2 修改 `agent/mcp_server.py`：`exported(registry, domains)`；`build_server` 接收 `domains` 可调用对象，`list_tools`/`call_tool` 每次重新计算；支持可 await 的 `agent_profile`/`record_gap`；调用未导出工具时返回 isError
    - _Requirements: 2.1, 2.2, 2.3, 2.5_
  - [x] 2.3 修改 `agent/api.py`，新增 `POST /api/agent/gaps`；修改 `cli_agent.py`：`mindex mcp --domain` 可重复指定，遇到未知领域报错；远程模式接入 `RemoteAgentProfile` 与 `remote_gap_recorder`
    - _Requirements: 2.4, 2.6, 2.7_
  - [x]* 2.4 编写性质测试：MCP 导出过滤
    - **Property 4: MCP 导出过滤**
    - **Validates: Requirements 2.1, 2.2**
  - [x]* 2.5 扩展 `tests/test_agent_mcp.py`：热生效、`--domain` 覆盖、远程模式下的自身档案保护与能力缺口记录
    - _Requirements: 2.3, 2.4, 2.6, 2.7_

- [x] 3. 按片段找相似（后端）
  - [x] 3.1 修改 `search/search.py`：从 `_context` 中拆出 `_active()` 与 `_snapshots()`（原有 text/image/video 行为不变）；实现 `SearchEngine.similar`（读取库内 video 向量，多窗口等权融合，排除种子片段和同源重叠片段，不做空间校验）；在 `shared/types.py` 新增 `SimilarSearch`
    - _Requirements: 7.1, 7.2, 7.3, 7.4_
  - [x] 3.2 修改 `search/api.py`，新增 `POST /api/search/similar`，错误码映射为 404/422/409
    - _Requirements: 7.5_
  - [x]* 3.3 编写性质测试：相似检索排除规则（`tests/test_search_similar.py`）
    - **Property 8: 相似排除**
    - **Validates: Requirements 7.2, 7.3**
  - [x]* 3.4 编写集成测试：多窗口融合、隐藏过滤、嵌入服务离线时仍可用、只有对白渠道的种子返回 422、向量缺失返回 409，以及 text/image/video 回归
    - _Requirements: 7.1, 7.4, 7.5_

- [x] 4. 区间抽帧（后端）
  - [x] 4.1 在 `shared/shot_media.py` 新增 `frame_jpeg(source_id, t, width)`；在 `admin/admin.py` 新增 `GET /api/admin/sources/{id}/frame`：参数校验、ETag/304、信号量限流
    - _Requirements: 8.1, 8.2, 8.3_
  - [x]* 4.2 编写测试（`tests/test_source_frame.py`）：越界返回 422、素材不存在返回 404、ETag 命中返回 304、返回内容为 JPEG 且宽度不超过上限
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 5. 索引版本管理
  - [x] 5.1 新建 `backend/src/admin/indexes.py`，在 `api.py` 注册；实现列表、一致性检查（线程池执行），以及激活接口（处理锁加带超时的索引写锁，依次执行 check、空间与门槛校验、预处理校验、`store.activate`，最后后台 `warm_up`）
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
  - [x] 5.2 在 `agent/tools/index.py` 实现 `list_index_versions`、`check_index`、`activate_index`（danger，预览包含空间是否匹配与将切换的检索组合）
    - _Requirements: 9.6_
  - [x]* 5.3 编写测试（`tests/test_index_api.py`）：检查失败返回 422、空间不符返回 503、锁冲突返回 409 且活动索引不变、激活后检索命中新索引；附带工具预览测试
    - _Requirements: 9.3, 9.4, 9.5, 9.6_

- [x] 6. 附件通道后端
  - [x] 6.1 在 `shared/config.py` 增加 `attachment_ttl_hours`、`attachment_max_mb`、`max_images`；新建 `agent/attachments.py`，实现 `AttachmentStore`（建表、put/get/resolve/delete/delete_session/expired）
    - _Requirements: 3.1, 3.2, 3.9_
  - [x] 6.2 修改 `agent/api.py`：新增附件端点（上传、元数据、内容、删除）及 `attachment_caller` 鉴权依赖；在 `shared/web_auth.py` 为附件路径放行，改由端点自行鉴权；`MessageBody.attachments`；删除会话时级联删除附件
    - _Requirements: 3.1, 3.2, 3.3, 3.8_
  - [x] 6.3 修改 `agent/loopback.py`：增加 `resolve_attachment`、`with_resolvers`、`upload()`、`fetch_bytes()`，远程模式先 GET content 再上传；修改 `agent/runtime.py`：`prompt` 校验附件并写入 `user/message`，`client_factory` 注入附件解析器；`mcp_server.server_for_app` 注入 owner=mcp 的解析器
    - _Requirements: 3.4, 3.5, 3.6, 3.7_
  - [x] 6.4 在 `maintenance/clean.py` 注册 B 级规则 `agent_attachments`；在 `maintenance/backup_files.py` 中排除 `agent-attachments/`；同步 `config.example.yaml` 与 `docs/cleanup.md`
    - _Requirements: 3.9, 3.10_
  - [x]* 6.5 编写性质测试：附件隔离
    - **Property 5: 附件隔离**
    - **Validates: Requirements 3.4, 3.5**
  - [x]* 6.6 编写集成测试（`tests/test_agent_attachments.py`）：三种令牌情形、超限返回 413、空文件返回 422、两种回环模式的 multipart、会话删除级联、clean 规则预览与执行、备份排除；另外记录进程内上传大文件时的内存行为，如果请求体会被整包读入内存，就下调默认上限并在文档中说明
    - _Requirements: 3.1, 3.2, 3.3, 3.6, 3.8, 3.9, 3.10_

- [x] 7. 后台任务取消
  - [x] 7.1 新建 `shared/task_cancel.py`（`TaskCancelled`、`CancelRegistry`、`registry_for`）；`shared/task_progress.save_task_progress` 中调用 check；`PublicationLock.admit` 循环中也检查；确认分析阶段有逐片段进度上报，没有则补上
    - _Requirements: 12.3, 12.5_
  - [x] 7.2 修改 `admin/source_tasks.py`、`admin/dialogue_tasks.py`、`persons/person_processing.py`：run 中捕获 `TaskCancelled` 并置为 `cancelled`；新增 `cancel(task_id)`（queued 直接取消、running 设标记、其他状态返回 409）；retry 允许重试 cancelled 任务；在 `person_api.py` 等处新增三个 cancel 端点
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.6, 12.7, 12.8_
  - [x] 7.3 前端：`frontend/src/admin/admin.js`、`frontend/src/persons/persons.js` 增加“取消”按钮与 cancelled 状态文案，cancelled 任务显示“重试”；文案写入 `messages.json` 后运行 `build-messages.py`
    - _Requirements: 12.9_
  - [x]* 7.4 编写测试（`tests/test_task_cancel.py`）：每类任务在两次进度上报之间取消、取消后续跑、queued 任务直接取消、等待 GPU 的发布任务可取消、索引构建中途取消后活动索引不变且版本可续建、interrupted 恢复不受影响；补充 UI 回归
    - _Requirements: 12.2, 12.3, 12.4, 12.6, 12.7, 12.9_

- [x] 8. Checkpoint - 基础设施与后端接口测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. 面板附件 UI
  - [x] 9.1 修改 `frontend/src/shared/agent-panel.js` 与 `agent-panel.css`：附件 chip 列表、添加文件按钮、拖放、粘贴文件、XHR 进度、移除、发送前等待上传完成、消息中的附件渲染与大图查看；文案写入 `messages.json` 后运行 `build-messages.py`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_
  - [x]* 9.2 扩展 Playwright 用例（`tests/agent_panel_ui.cjs` / `test_agent_panel_ui.py`）：setInputFiles、模拟拖放、移除、上传失败时阻止发送、发送后显示附件
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 10. 检索领域工具
  - [x] 10.1 在 `agent/tools/search.py` 扩展 `search_media` 的参数；实现 `search_by_media`（附件走 multipart，filters 以 JSON 字符串传）、`find_similar`、`sample_frames`（均匀生成时间点，返回 media 与 images）、`get_system_status`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 7.6, 8.4_
  - [x]* 10.2 编写性质测试：抽帧时间点
    - **Property 9: 抽帧时间点**
    - **Validates: Requirements 8.4**
  - [x]* 10.3 编写工具测试（`tests/test_agent_search_tools.py`）：参数透传、附件链路、degraded 与 warnings 的提示、嵌入服务未就绪时的报告、focus 与 media
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

- [x] 11. 素材领域工具与任务跟踪
  - [x] 11.1 在 `admin/admin.py` 新增 `GET /api/admin/sources/{id}/report`（线程池执行）；在 `agent/tools/library.py` 实现 `get_source`、`list_segments`、`get_segment_details`、`get_source_report`、`update_curation`（write，预览中提示隐藏的影响）
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_
  - [x] 11.2 把 `agent/watcher.py` 泛化为 `TaskWatcher`（支持 build/source/dialogue/person 四类，终态时发 followup），并在 `ToolOutput` 中增加 `watch_task`
    - _Requirements: 11.2_
  - [x] 11.3 在 `agent/tools/library_tasks.py` 实现 `start_source_task`（danger）、`get_task_status`、`retry_task`、`cancel_task`、`upload_source`、`upload_subtitle`
    - _Requirements: 11.1, 11.3, 11.4, 11.5, 12.10_
  - [x]* 11.4 编写测试（`tests/test_agent_library_tools.py`）：字段透传、隐藏后检索结果被过滤、报告结构、GPU 锁 409 的转述、终态 followup、重试状态限制、上传的后缀与大小限制
    - _Requirements: 10.1, 10.4, 10.5, 11.1, 11.2, 11.3, 11.4_

- [x] 12. 创作构建取消与删除方案
  - [x] 12.1 修改 `studio/studio_workspace.py`：增加 `build_cancelled`；`_run_build` 在每个节点开始前和 auto_split 重新预检后检查取消，命中则剩余节点置为 blocked「已取消」，构建状态置为 cancelled；补全两张标签映射
    - _Requirements: 13.2, 13.3_
  - [x] 12.2 修改 `studio/studio_api.py`：新增构建取消端点（进程内 Event 加哨兵文件）、旧任务执行器的步骤间取消与 `POST /jobs/{job}/cancel`、`DELETE /jobs/{job}/plans/{pid}`
    - _Requirements: 13.1, 13.4, 13.5, 13.6_
  - [x] 12.3 修改 `frontend/src/studio/studio.js`：STATUS 增加 cancelled，未知状态回退显示原值；增加“取消执行”按钮（版本工作台与旧任务都要有）、“删除方案”按钮（二次确认，只剩一个方案时禁用）；文案写入 `messages.json` 后运行 `build-messages.py`
    - _Requirements: 13.7_
  - [x] 12.4 在 `agent/tools/studio_build.py` 实现 `cancel_build`（write）与 `delete_plan`（danger）
    - _Requirements: 13.8_
  - [x]* 12.5 编写性质测试：取消后状态、删除方案不丢版本（`tests/test_studio_cancel.py`）
    - **Property 10: 取消后状态**
    - **Property 11: 删除方案不丢版本**
    - **Validates: Requirements 13.2, 13.5**
  - [x]* 12.6 编写集成测试：已开始执行的节点照常完成、重启恢复不改变已取消的构建、非 running 状态取消返回 409、删除方案的边界情况、旧执行器取消；补充 UI 回归
    - _Requirements: 13.1, 13.3, 13.4, 13.6, 13.7_

- [x] 13. 人物识别与人物管理工具
  - [x] 13.1 在 `persons/person_api.py` 新增 `POST /api/persons/identify`（图片归一化、人物服务身份校验与特征抽取、参考图特征缓存比对，按角色和演员分别给出 top-k，统计未覆盖的参考图）
    - _Requirements: 16.1, 16.2_
  - [x] 13.2 在 `agent/tools/persons.py` 实现 `get_person`、`save_person`、`delete_person`、`assign_source_work`、`add_person_reference`、`remove_person_reference`、`identify_person`
    - _Requirements: 16.3, 16.4, 16.5_
  - [x]* 13.3 编写测试（`tests/test_person_identify.py`、`tests/test_agent_persons.py`）：用确定性人物服务桩验证识别排名；人物服务不可用时返回 503；参考图质检失败的转述；作品不可删除；关联镜头示例的转述
    - _Requirements: 16.1, 16.2, 16.4_

- [x] 14. 设置领域扩展工具
  - [x] 14.1 在 `agent/tools/settings_tools.py` 实现 `delete_model_profile`、`reset_business_settings`、`list_voice_catalog`、`register_voice`、`delete_voice`、`clone_voice`（通过 ask_question 表单取得授权，按后端选择附件或 URL，没有提问通道时拒绝）
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_
  - [x]* 14.2 编写性质测试：授权只来自表单
    - **Property 13: 授权只来自表单**
    - **Validates: Requirements 18.3**
  - [x]* 14.3 编写测试（`tests/test_agent_settings_ext.py`）：自身档案与激活中档案受保护、按后端分派、删除默认音色时联动清除、MCP 调用 clone_voice 被拒
    - _Requirements: 18.1, 18.4, 18.5_

- [x] 15. 模型视觉输入
  - [x] 15.1 新增配置 `AgentLlmConfig.vision`；在 `runtime_settings.PROFILE_FIELDS`、`settings_api._effective`、设置页 agent 档案表单中增加复选框；文案写入 `messages.json` 后运行 `build-messages.py`；同步 `config.example.yaml`
    - _Requirements: 5.1_
  - [x] 15.2 修改 `agent/llm/types.py`：增加 image 块与 `tool_message(images=)`；`registry.ToolOutput.images`；新建 `agent/vision.py`，实现 `materialize`（解析、缩放、上限、按协议放置、失败时替换）
    - _Requirements: 5.2, 5.4, 5.5, 5.7_
  - [x] 15.3 修改 `agent/llm/openai_compat.py` 与 `anthropic.py` 的图片块转换；修改 `agent/runtime.py`：每步发请求前调用 `materialize`，vision 关闭时在结果文本中注明，`estimate_tokens` 对图片按固定值计数；用户附件图片生成 image 块；`sample_frames`、`search_by_media`、`identify_person` 填充 `images`
    - _Requirements: 5.2, 5.3, 5.4, 5.6_
  - [x]* 15.4 编写性质测试：日志无图片数据、图片上限（`tests/test_agent_vision.py`）
    - **Property 6: 日志无图片数据**
    - **Property 7: 图片上限**
    - **Validates: Requirements 5.3, 5.5, 5.6**
  - [x]* 15.5 编写测试：两种协议的 payload 结构、ScriptedLlm 收到的图片块、图片引用解析失败时的替换、设置往返
    - _Requirements: 5.1, 5.4, 5.7_

- [x] 16. 创作写工具扩展
  - [x] 16.1 修改 `agent/tools/studio_write.py`：`ShotFields` 增加 recall/granularity/recall_limit，`ShotOperation.clear`，LABELS 与级联提示；实现 `rename_job`、`attach_query_media`（先上传查询文件，再改召回设置，只需一次确认）
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_
  - [x]* 16.2 编写性质测试：召回设置往返
    - **Property 12: 召回设置往返**
    - **Validates: Requirements 14.1**
  - [x]* 16.3 编写集成测试：人物、素材范围、查询文件错误的转述；attach_query_media 完整链路；clear 语义
    - _Requirements: 14.2, 14.3, 14.4_

- [x] 17. 人物审核、发布与演员表导入工具
  - [x] 17.1 在 `agent/tools/persons_review.py` 实现 `list_review_shots`、`review_shot`、`publish_persons`、`list_search_releases`、`change_search_release`、`import_cast`、`confirm_cast_import`
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5_
  - [x]* 17.2 编写测试：审核规则（必须带角色、演员与角色关系校验）、发布前置条件的转述、当前生效组合受保护、多附件导入
    - _Requirements: 17.2, 17.3, 17.4, 17.5_

- [x] 18. 创作诊断与人工选片
  - [x] 18.1 修改 `studio/studio_api.py`，新增 `GET /plans/{pid}/diagnostics`：分镜 hard/soft 检查、字幕滤镜与字体检查、节点状态与 conflicts 原因、`inputs_by_stage`
    - _Requirements: 15.1_
  - [x] 18.2 在 `agent/tools/studio_detail.py` 实现 `diagnose_plan`、`get_version_lineage`、`suggest_shot_query`、`get_material_digest`、`preview_version`、`run_column`（danger，跟踪构建）
    - _Requirements: 15.2, 15.3_
  - [x] 18.3 在 `agent/tools/studio_detail.py` 实现 `get_selection_candidates`、`preview_selection`（窗口与区间两种方式，必要时设置人工锚点，把请求体和 token 存入 ctx.extra）、`confirm_selection`（write）
    - _Requirements: 15.4, 15.5, 15.6, 15.7_
  - [x]* 18.4 编写测试（`tests/test_studio_diagnostics.py`、`tests/test_agent_studio_detail.py`）：缺字体与修改旁白时的诊断原因、run_column 输入变化时被拒、两条选片路径、revision 变化后要求重新预览、未配音时的提示
    - _Requirements: 15.1, 15.3, 15.5, 15.6, 15.7_

- [x] 19. Checkpoint - 全部领域工具测试通过
  - Ensure all tests pass, ask the user if questions arise.

- [x] 20. 风险策略、场景与文档
  - [x]* 20.1 编写性质测试：不开放物理删除（用记录请求的假客户端执行全部工具的最小参数集）；并断言每个 write/danger 工具都有 preview
    - **Property 14: 不开放物理删除**
    - **Validates: Requirements 19.1, 19.2, 19.3, 19.4**
  - [x] 20.2 在 `tests/agent_scenarios.py` 新增场景：跨领域动态加载、附件检索、找相似、索引回退、取消构建、取消素材任务、人物审核与发布、视觉输入；同步更新 `scripts/agent_eval.py`
    - _Requirements: 1.7, 3.7, 5.2, 7.6, 9.6, 12.10, 13.8, 17.3_
  - [x] 20.3 更新 `docs/agent.md`（领域与工具表、动态加载、附件、视觉、取消、API、配置、MCP 导出与 domains）、`docs/studio.md`、`docs/admin.md`、`docs/cleanup.md`、`config.example.yaml`，以及 `CLAUDE.md` 的智能助手段落（“约 30 以内”改为动态加载约束）
    - _Requirements: 20.1, 20.2_

- [x] 21. Final checkpoint - 统一检查
  - 运行 `uv run ruff check backend/src tests`、`python scripts/check-doc-links.py`、`python scripts/build-messages.py --check`、`for f in frontend/src/*/*.js; do node --check "$f"; done`、`uv run mindex --help`、`uv run pytest -q`、`git diff --check`
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选测试任务，可以为了更快完成 MVP 而跳过；Property 测试使用 `@settings(max_examples=100, deadline=None)`，并加注释 `# Feature: agent-capability-expansion, Property <n>: <标题>`
- `messages.json`、`shared/config.py`、`agent/runtime.py`、`agent/api.py` 是共享文件，依赖图保证修改这些文件的任务不在同一波次
- 任务 1.7 一次性创建全部工具模块并完成注册，之后各领域任务只改自己的模块
- 交付时说明实际运行的验证，以及未验证的范围（真实模型的视觉效果、真实人物服务的识别质量、真实 GPU 任务的取消时延）

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.4"] },
    { "id": 1, "tasks": ["1.5", "1.3"] },
    { "id": 2, "tasks": ["1.6", "1.7"] },
    { "id": 3, "tasks": ["1.9", "1.8"] },
    { "id": 4, "tasks": ["1.10", "2.1", "3.1", "4.1", "5.1"] },
    { "id": 5, "tasks": ["2.2", "3.2", "4.2", "5.2"] },
    { "id": 6, "tasks": ["2.3", "3.3", "5.3", "7.1"] },
    { "id": 7, "tasks": ["2.4", "3.4", "6.1", "7.2"] },
    { "id": 8, "tasks": ["2.5", "6.2", "7.3"] },
    { "id": 9, "tasks": ["6.3", "7.4"] },
    { "id": 10, "tasks": ["6.4", "6.5"] },
    { "id": 11, "tasks": ["6.6"] },
    { "id": 12, "tasks": ["9.1", "10.1", "11.1", "12.1", "13.1", "14.1"] },
    { "id": 13, "tasks": ["9.2", "10.2", "11.2", "12.2", "13.2", "14.2"] },
    { "id": 14, "tasks": ["10.3", "11.3", "12.3", "13.3", "14.3"] },
    { "id": 15, "tasks": ["11.4", "12.4", "15.1", "16.1", "17.1"] },
    { "id": 16, "tasks": ["12.5", "15.2", "16.2", "17.2", "18.1"] },
    { "id": 17, "tasks": ["12.6", "15.3", "16.3", "18.2"] },
    { "id": 18, "tasks": ["15.4", "18.3"] },
    { "id": 19, "tasks": ["15.5", "18.4"] },
    { "id": 20, "tasks": ["20.1", "20.2"] },
    { "id": 21, "tasks": ["20.3"] }
  ]
}
```
