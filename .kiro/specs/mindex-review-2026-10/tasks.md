# Implementation Plan: Mindex 全面审查（2026-10）、架构改造与创作能力扩展

## Overview

- 阶段 A（任务 1–2）：只新增文件，不修改任何已跟踪文件。
- Gate（任务 3）：等待 agent-capability-expansion 完成，通过统一检查后单独提交它的改动。
- 阶段 B（任务 4–27）：代码与文档改动，全部保持未提交。

标记 `*` 的子任务为测试任务。所有用户可见文案写入 `messages.json` 后运行 `scripts/build-messages.py`。

## Tasks

- [x] 1. 本 spec 与审查报告（只新增文件）
  - [x] 1.1 编写 `requirements.md`、`design.md`、`tasks.md`
  - [x] 1.2 `docs/reviews/2026-10/features-roadmap.md`：功能缺口（选题、文案、生成、后期、分发、桌面端）与"当前可做"清单
    - _Requirements: 1.1, 1.2_
  - [x] 1.3 `plans/ROADMAP.md`：M1–M4，含垂直领域选题挖掘设计、桌面端方案对比与性能基线指标
    - _Requirements: 1.3, 1.4_
  - [x] 1.4 `docs/reviews/2026-10/architecture.md`：规模、依赖图、架构异味、重构候选
    - _Requirements: 1.1, 1.2_
  - [x] 1.5 `docs/reviews/2026-10/code-quality.md`：按严重度列发现
    - _Requirements: 1.1, 1.2_
  - [x] 1.6 `docs/reviews/2026-10/docs-consistency.md` 初稿：偏差清单（修正状态在任务 26 回填）
    - _Requirements: 13.2_

- [x] 2. 等待期间的只读准备
  - [x] 2.1 记录当前 recall 签名快照与 fixture 构建产物的生成脚本（并入 13.1：工作台代码在任务 13 前不变，届时在重构前生成快照）
    - _Requirements: 6.5, 6.6_

- [x] 3. Gate：提交 agent-capability-expansion（e891e20，2026-10-25 02:33，该 spec 已全部完成）
  - [x] 3.1 轮询直到 tasks.md 无未完成/进行中任务，或累计等待 2 小时（不做统一检查）
    - _Requirements: 2.2_
  - [x] 3.2 暂存它当时的全部改动路径（排除本功能新增文件），中文提交说明，不推送
    - _Requirements: 2.3, 2.4_

- [ ] 4. 安全与鉴权
  - [ ] 4.1 `shared/web_auth.py`：`next` 校验、`/logout` 仅 POST；前端登出改 POST
  - [ ] 4.2 `mindex serve --allow-insecure`；非回环无令牌拒绝启动
  - [ ]* 4.3 `tests/test_web_auth.py` 补充用例
    - _Requirements: 3.1, 3.2, 3.3_

- [ ] 5. IO 与资源
  - [ ] 5.1 `shared/jsonio` 唯一实现；`studio.save`/`save_bytes` 改用；全仓库补 `encoding="utf-8"`
  - [ ] 5.2 `clone_voice`：线程池执行、`save_upload` 限额、失败清理、唯一 voice_id
  - [ ] 5.3 `save_upload` 失败自清理；附件 `put` 线程池；备份子进程超时；`shot_repair` 正则判空
  - [ ]* 5.4 并发原子写、UTF-8、克隆 413 与不阻塞事件循环
    - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 6. 日志与前端轮询
  - [ ] 6.1 后台线程顶层 `log.exception`；标题生成 warning；连接测试错误脱敏
  - [ ] 6.2 `persons.js` 链式 setTimeout + 隐藏暂停 + 退避；`agent-panel.js` SSE 退避；`settings.js` 静默 catch 改提示
  - [ ]* 6.3 caplog 断言 traceback；node 单测验证退避调度
    - _Requirements: 4.5, 4.6, 4.7_

- [ ] 7. Lint、CI 与分层
  - [ ] 7.1 ruff 启用 ASYNC、BLE、RUF100，整理 noqa
  - [ ] 7.2 `slow` 标记与 `.github/workflows/ci.yml`
  - [ ] 7.3 导入边界测试递归覆盖、白名单冻结；`maintenance` 去掉对 `cli_common` 的依赖
  - [ ] 7.4 `DOMAIN_INFO`、`PAGE_DOMAIN` 移入 `agent/`
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ] 8. 统一后台任务 runner
  - [ ] 8.1 `shared/background_tasks.py`；`SourceTasks`、`DialogueTasks`、`PersonTasks` 迁移
  - [ ]* 8.2 基类单测；既有任务、取消、进度测试通过
    - _Requirements: 6.1_

- [ ] 9. 版本化迁移
  - [ ] 9.1 `shared/migrations.py`；metadata 库（`storage.py`、`person_schema.py`）
  - [ ] 9.2 agent 库（session_store、attachments、changes）
  - [ ]* 9.3 新库、旧库升级、重复打开、备份往返
    - _Requirements: 6.2_

- [ ] 10. 统一锁模块
  - [ ] 10.1 `shared/locks.py` 与 `acquire_ordered`；迁移 clean、backup_files、persons、clips 等调用点
  - [ ]* 10.2 锁顺序性质测试；clean/backup 既有测试通过
    - _Requirements: 6.3_

- [ ] 11. Checkpoint：基础设施测试通过

- [ ] 12. StudioExecutor
  - [ ] 12.1 `studio/executor.py`；`studio_api.py` 改用；取消统一；worker 异常处理修复
  - [ ]* 12.2 构建记录缺失仍记录原异常、两路互斥、既有取消测试
    - _Requirements: 4.5, 6.4_

- [ ] 13. 拆分 `studio_workspace.py`
  - [ ] 13.1 签名快照与产物一致性测试落地（基于 2.1）
  - [ ] 13.2 拆为 `studio/workspace/` 包，更新调用点与 monkeypatch 目标
  - [ ]* 13.3 结构测试：单文件 ≤700 行
    - _Requirements: 6.5_

- [ ] 14. 阶段注册表
  - [ ] 14.1 `StageHandler` 与 handlers；`studio_models` 阶段取值由注册表生成；按来源计算依赖
  - [ ]* 14.2 签名快照、preflight、conflicts 测试通过
    - _Requirements: 6.6_

- [ ] 15. 前端拆分
  - [ ] 15.1 `shared/api.js`，五个页面改用
  - [ ] 15.2 `studio.js` 拆为有序脚本；`.cjs` 测试路径更新
  - [ ] 15.3 `scripts/build-assets.py`（`--check`）
    - _Requirements: 6.7_

- [ ] 16. Checkpoint：重构后全部测试通过

- [ ] 17. 文案模板
  - [ ] 17.1 `studio/templates/*.yaml`（6 个）与 `studio/templates.py`；`script_templates` 表（迁移 v2）
  - [ ] 17.2 writer 接收模板，`WRITER_PROMPT_VERSION=3`；script 签名含模板哈希；`PlanOptions.template_id`
  - [ ] 17.3 模板 API 与创作页模板选择、管理界面
  - [ ] 17.4 智能助手：`list_script_templates`，方案选项与文案工具支持模板
  - [ ]* 17.5 模板校验、已有文案不重写、用户模板往返、引用保护
    - _Requirements: 7.1–7.6_

- [ ] 18. 参考文稿
  - [ ] 18.1 上传/粘贴/删除接口、摘要校验、上限；writer 参考段；rewrite 预检
  - [ ] 18.2 创作页参考文稿面板；智能助手通过附件添加
  - [ ]* 18.3 篡改、超限、缺失阻塞、签名区分
    - _Requirements: 8.1–8.4_

- [ ] 19. 方案级画幅
  - [ ] 19.1 `canvas`/`fit`、`studio.render.canvases`；`cut_shot` crop；签名；字幕缩放
  - [ ] 19.2 创作页与智能助手选项
  - [ ]* 19.3 分辨率、只重建 render/compose、帧数确定
    - _Requirements: 9.1–9.3_

- [ ] 20. 分镜来源抽象与 `clip`
  - [ ] 20.1 `ShotSource`、`TimelineVideo` 判别联合；来源依赖图
  - [ ] 20.2 clip 的 select/render；ducking 用片段原声；clean/backup 识别新输入
  - [ ] 20.3 创作页来源切换、片段上传与窗口预览；智能助手 `edit_shots` 支持 source
  - [ ]* 20.4 recall 签名不变、混合 compose、文件缺失、无索引纯 clip 构建
    - _Requirements: 10.1–10.4, 10.6, 10.7_

- [ ] 21. `image` 来源
  - [ ] 21.1 zoompan 推拉、图片归一化、按画幅适配、确定帧数、签名
  - [ ] 21.2 创作页与智能助手支持
  - [ ]* 21.3 帧数与分辨率、motion 签名、只重建该镜
    - _Requirements: 10.5_

- [ ] 22. 视频生成器与"视频生成"角色
  - [ ] 22.1 `models/video_generators.py`：协议、工厂、ComfyUI 后端
  - [ ] 22.2 MiniMax、通义万相后端（按官方文档，注释记录出处）
  - [ ] 22.3 配置段、`ROLES`/`PROFILE_FIELDS`、预设、测试连接（不生成）、设置页表单、ComfyUI 工作流导入与映射校验
  - [ ]* 22.4 HTTP 桩：成功、排队、失败、超时、审核拒绝；映射错误；档案往返与掩码
    - _Requirements: 11.1, 11.2, 11.3, 11.7_

- [ ] 23. `aigc` 来源
  - [ ] 23.1 `generate` 阶段：提交、轮询、取消、留档、签名、时长取整；select 冻结帧兜底；预检阻塞
  - [ ] 23.2 创作页 prompt/参考图/进度/预览；智能助手支持
  - [ ]* 23.3 签名复用、生成中取消、失败诊断、混合 compose
    - _Requirements: 11.4, 11.5, 11.6_

- [ ] 24. "动画生成"角色与沙箱录制
  - [ ] 24.1 `studio.animator` 配置（缺省复制 `agent.llm`）、角色、测试连接、设置页
  - [ ] 24.2 `models/animator.py`：提示词、静态校验
  - [ ] 24.3 `studio/animation_capture.py`：虚拟时钟、网络拦截、临时 profile、逐帧截图管道、超时；`animation` extra
  - [ ]* 24.4 静态校验拦截、两次录制帧哈希一致、网络被阻断、死循环超时、未安装时的中文指引
    - _Requirements: 12.1–12.5_

- [ ] 25. `animation` 来源
  - [ ] 25.1 `animate` 阶段、人工保存版本、render 先录制再合成、签名
  - [ ] 25.2 创作页源码编辑与沙箱预览（CSP 响应头）；智能助手生成/修改源码
  - [ ]* 25.3 手改只重建该镜、预览响应头、五种来源混合 compose
    - _Requirements: 12.6, 12.7, 10.6_

- [ ] 26. 文档一致性修正
  - [ ] 26.1 修正全部活文档（README、CLAUDE.md、AGENT.md、architecture、config-reference、settings、agent、studio、cli-reference、DEPLOYMENT 等）
  - [ ] 26.2 报告与路线图挂到 `docs/README.md`、`plans/README.md`；报告 1–4 回填完成状态
    - _Requirements: 1.5, 13.1, 13.2_

- [ ] 27. Final checkpoint：统一检查
  - ruff、`check-doc-links`、`build-messages --check`、`build-assets --check`、全部 JS `node --check`、`mindex --help`、完整 pytest、`git diff --check`；交付说明写明未验证范围
    - _Requirements: 13.3_
