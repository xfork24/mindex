# Implementation Plan: topic-discovery

## Overview

按能力纵向切分：每个任务交付时，该能力在 REST、Web 页面、CLI、智能助手工具（MCP 同名导出）四个入口同时可用，并在 Web 端测试、桌面冒烟中各验证一次。

全程约束：

- 业务逻辑只写在 `backend/src/topics/`；REST 薄封装，CLI 直接调用服务层，智能助手工具经回环调用 REST。
- 每新增一项能力，同步登记 `topics/capabilities.py`，`tests/test_topics_parity.py` 必须通过。
- 界面文字以 `frontend/src/shared/messages.json` 中文为唯一源，改完运行 `python scripts/build-messages.py` 与 `python scripts/build-assets.py`。
- 签名快照（`tests/test_studio_signature_snapshot.py`）不变；`WRITER_PROMPT_VERSION` 不变。
- 不新增 Python 依赖；桌面端依赖锁定精确版本。
- 前端只经 `MindexApi`、只用相对地址；桌面专属能力只经 `window.MindexDesktop` 特性检测。
- 全部完成后在当前分支提交。

## Tasks

- [x] 1. 基础：数据表、锁、配置、档案能力与四个入口骨架
  - [x] 1.1 `_metadata_v3` 建 6 张 `topic_*` 表；`locks.py` 登记 `topics`；`task_progress.TASK_TABLES` 加 `topic_runs`；`BackgroundTaskRunner` 恢复改为 `recover()` 钩子；`TopicsConfig` 与配置样例（`config.example.yaml`、`desktop/config.template.yaml`）
    - _Requirements: 1.1, 3.4, 11.2, 11.3_
  - [x] 1.2 `topics/models.py`、`topics/profiles.py`、`topics/api.py`（档案 CRUD），`create_app` 接入并增加 `topic_llm`、`scheduler` 参数
    - _Requirements: 1.1, 1.2, 1.3, 10.2_
  - [x] 1.3 `cli_topics.py`：`mindex topics profiles ...`，在 `cli.py` 注册
    - _Requirements: 10.1, 10.2_
  - [x] 1.4 新领域 `topics`（`AGENT_DOMAINS`、`DOMAIN_INFO`、`PAGE_DOMAIN`）；`agent/tools/topics.py`：`list_topic_profiles`、`save_topic_profile`、`delete_topic_profile`；`build_registry` 注册；工具数量断言与 `docs/agent.md` 工具表
    - _Requirements: 10.1, 10.4_
  - [x] 1.5 页面骨架 `studio/topics.html/js/css`：三个页面标签栏插入「选题挖掘」、`register_page`、档案切换与编辑、`MindexAgentContext`/`MindexAgentFocus`、`agent-panel.js` 的 `topics` 页面地址；中英文案
    - _Requirements: 1.4, 13.1, 13.3, 13.4, 11.1_
  - [x] 1.6 `topics/capabilities.py` 与 `tests/test_topics_parity.py`；`tests/test_topics_profiles.py`（迁移、CRUD、权重校验、CLI、工具）；`tests/topics_ui.cjs` + `tests/test_topics_ui.py`；`desktop_smoke.cjs` 增加 `/studio/topics` 检查
    - _Requirements: 10.3, 11.4_

- [x] 2. 数据源框架、RSS、本地来源与立即抓取
  - [x] 2.1 `topics/sources/base.py`（协议、注册表、`http_get`、掩码）、`rss.py`、`local.py`；`topics/items.py`（去重、入库、过期清理）
    - _Requirements: 2.1, 2.2, 2.3, 2.7, 2.8, 2.9_
  - [x] 2.2 REST：数据源 CRUD、试抓、立即抓取、条目列表；CLI `sources list/add/update/remove/test`、`fetch`；工具 `list_topic_sources`、`save_topic_source`、`delete_topic_source`、`test_topic_source`、`fetch_topic_items`
    - _Requirements: 10.1_
  - [x] 2.3 页面数据源区（增删改、启停、试抓、最后错误）
    - _Requirements: 13.2_
  - [x] 2.4 测试：解析、XXE、超时、超大响应、协议、去重、掩码；一致性；UI
    - _Requirements: 2.2, 2.3, 2.8, 2.9_

- [x] 3. 挖掘任务：聚类与卡片归纳
  - [x] 3.1 `topics/llm.py`、`topics/cluster.py`、`topics/summarize.py`、`topics/runner.py`（`TopicRuns` 与 `discover`）；`/api/health` 的 busy 含 `topics`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_
  - [x] 3.2 REST runs/cards；CLI `discover`（本进程前台运行）、`runs ...`、`list`、`show`；工具 `discover_topics`、`get_topic_run`、`list_topics`；`TaskWatcher` 支持 `topics`
    - _Requirements: 10.1, 10.5_
  - [x] 3.3 页面：开始挖掘、进度、卡片列表与详情（基础字段）
    - _Requirements: 13.2_
  - [x] 3.4 测试：聚类、解析与重试、runner 生命周期、409、降级；一致性；UI
    - _Requirements: 3.2, 3.4, 3.5, 3.7_

- [x] 4. 打分与素材覆盖
  - [x] 4.1 `topics/coverage.py`、`topics/scoring.py`，接入流水线；单卡重新打分
    - _Requirements: 4.1–4.8_
  - [x] 4.2 REST `/score`、`/coverage`；CLI `score`、`coverage`；工具 `score_topic`、`topic_coverage`
    - _Requirements: 10.1_
  - [x] 4.3 页面：分项分数条、覆盖报告缩略图与跳转、按总分排序与状态筛选
    - _Requirements: 4.7, 13.2_
  - [x] 4.4 测试：评分函数、覆盖命中与无索引分支（真实 fixture 索引）、加权；一致性；UI
    - _Requirements: 4.1–4.8_

- [x] 5. 大纲、推荐模板与卡片编辑
  - [x] 5.1 `topics/outline.py`，流水线前 N 名写大纲；按需生成；`PATCH /cards/{id}`
    - _Requirements: 5.1, 5.2, 5.3_
  - [x] 5.2 CLI `outline`、`update`；工具 `draft_topic_outline`、`update_topic`
    - _Requirements: 10.1_
  - [x] 5.3 页面：大纲编辑（未保存提示）、模板选择、忽略/恢复、生成大纲
    - _Requirements: 13.2_
  - [x] 5.4 测试：前 N 名、模板回落、长度、状态规则；一致性；UI
    - _Requirements: 5.1, 5.2, 5.3_

- [x] 6. 从选题建创作任务
  - [x] 6.1 `topics/create_job.py`：建任务、方案参数、参考文稿、`profile.json`、`job.json.topic`、回滚、卡片状态；`studio.load_job` 默认 `topic`
    - _Requirements: 6.1, 6.2, 6.3, 6.4_
  - [x] 6.2 REST `/cards/{id}/job`；CLI `create-job`；工具 `create_job_from_topic`
    - _Requirements: 10.1_
  - [x] 6.3 页面"创建创作任务"与跳转；创作页任务详情"来自选题"回链
    - _Requirements: 6.5, 13.2_
  - [x] 6.4 测试：落盘内容、回滚、409 与 force；一致性；UI
    - _Requirements: 6.1–6.5_

- [x] 7. 档案进入文案与同步
  - [x] 7.1 `Writer.write(profile=...)`、方案级 `profile_hash`（方案字典的顶层键，不在 `PlanOptions` 里，PATCH 方案参数天然保留；新建方案沿用）、`ScriptHandler.settings/run`、详情 `script_profile_changed`
    - _Requirements: 7.1, 7.2_
  - [x] 7.2 REST `/jobs/{id}/profile`、`/sync`；CLI `job-profile show/sync`；工具 `get_job_profile`、`sync_job_profile`
    - _Requirements: 7.3, 7.4, 10.1_
  - [x] 7.3 创作页"档案已更新 / 同步"提示
    - _Requirements: 7.3, 7.4_
  - [x] 7.4 测试：签名快照不变、带档案时提示词与签名、同步后过期、档案已删除；一致性；UI
    - _Requirements: 7.1–7.4_

- [x] 8. 素材采集清单
  - [x] 8.1 `topics/gaps.py`：合并、列表、状态、CSV 导出（UTF-8 BOM）
    - _Requirements: 8.1, 8.2_
  - [x] 8.2 REST、CLI `gaps list/done/reopen/export`、工具 `topic_gaps`、`update_topic_gap`；页面采集清单区
    - _Requirements: 8.2, 10.1, 13.2_
  - [x] 8.3 测试：合并规则、导出编码、状态；一致性；UI
    - _Requirements: 8.1, 8.2_

- [x] 9. 热榜接口与 Brave 搜索
  - [x] 9.1 `topics/sources/hotlist.py`（字段映射、DailyHotApi 预设）、`brave.py`（web/news、查询上限、401/429 中文提示）
    - _Requirements: 2.4, 2.5_
  - [x] 9.2 四个入口支持新类型（schema 联合类型）；页面第三方提示
    - _Requirements: 2.10, 10.1_
  - [x] 9.3 测试：MockTransport 解析与错误码、字段映射缺失；一致性；UI
    - _Requirements: 2.4, 2.5_

- [x] 10. 日历导入
  - [x] 10.1 `topics/sources/calendar.py`（ICS/CSV、YEARLY、窗口、时区；导入的事件存 `topic_calendar_events`）与 `topics/samples/{finance,tech,history}.csv`；package-data、MANIFEST、`desktop_check`/`check-desktop-sidecar.py` 自检
    - _Requirements: 2.6, 11.2_
  - [x] 10.2 REST 上传/样例；CLI `sources import-calendar`；工具 `import_topic_calendar`；页面上传与载入样例
    - _Requirements: 10.1_
  - [x] 10.3 测试：解析、重复、窗口、时区；打包断言；一致性；UI
    - _Requirements: 2.6, 11.2_

- [x] 11. 每日定时与启动补跑
  - [x] 11.1 `topics/scheduler.py`（`due`、`tick`、线程、lifespan 包装、`status`）
    - _Requirements: 9.1, 9.2, 9.3, 9.5_
  - [x] 11.2 REST `/schedule`；CLI `schedule status`；工具 `get_topic_schedule`；页面定时状态
    - _Requirements: 9.4, 10.1_
  - [x] 11.3 测试：注入时钟的到点、不重复、补跑、关闭、锁冲突；一致性；UI
    - _Requirements: 9.1–9.5_

- [x] 12. 桌面托盘常驻
  - [x] 12.1 `desktop/lib/tray.js`、`main.js`（关闭隐藏、托盘菜单、首次通知、偏好）、`preload.js`（`background`）、图标与 `build.files`
    - _Requirements: 12.1, 12.2, 12.3, 12.4_
  - [x] 12.2 选题页"后台运行中"提示（特性检测）
    - _Requirements: 12.4, 11.1_
  - [x] 12.3 测试：`desktop/test/tray.test.js`；`desktop_smoke.cjs` 关窗后 health 可达、退出后停止
    - _Requirements: 12.1, 12.2_

- [x] 13. 文档与整体验收
  - [x] 13.1 `docs/topics.md`；更新 `docs/studio.md`、`docs/agent.md`、`docs/README.md`、`docs/desktop.md`、`CLAUDE.md`、`plans/ROADMAP.md`
    - _Requirements: 14.1_
  - [x] 13.2 `tests/test_topics_e2e.py`（slow）：页面路径与 CLI + 智能助手路径
    - _Requirements: 14.2_
  - [x] 13.3 全量验证：ruff、文档链接、messages/assets `--check`、`node --check`、`pytest -m "not slow"`、`desktop` 的 `node --test`；提交
    - _Requirements: 14.3_
    - 验收中补齐：备份 `TABLES` 登记 `topic_*` 表（`source_id` 不指向素材库，部分备份整体保留，导入时挖掘任务标记中断，`tests/test_topics_backup.py`）；`pyproject.toml` 声明 `mindex.topics`/`mindex.topics.sources`；嵌套空命令组不加载配置、各命令帮助带示例；写作约束快照移到 `shared/writing_profile.py`，studio 不依赖 topics；依赖边界白名单登记 topics
