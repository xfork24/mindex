# 仓库结构与架构审查（2026-10）

范围：2026-10-25 的工作区。上次审查见 `project-review-2026-10-22`（已删除）。本文是带日期的审查记录。

## 结论

核心不变量依然成立：SQLite 是事实来源，嵌入空间有身份，索引与切分布局不可变，产物版本不可变。底层分层也守住了：`shared`、`models`、`processing` 不依赖上层，并有测试约束。

问题集中在三处，都出在增长最快的上层：

1. **执行机制重复**：三套几乎相同的后台任务 runner，四种取消方式，两种文件锁实现，加锁顺序只写在注释里。
2. **创作工作台集中**：`studio_workspace.py` 有 2,068 行，阶段逻辑按阶段名写成 if/elif，分散在六个函数里；`studio.js` 有 3,003 行，全部是全局作用域。新增阶段或画面来源要同时改很多处。
3. **约束不完整**：上层包之间仍有函数内延迟导入形成的环，导入边界测试只覆盖三个包，且不递归；迁移是零散的 PRAGMA 加 ALTER；没有 CI。

## 规模

| 项目 | 2026-10-22 | 2026-10-25 |
|---|---|---|
| 后端 Python | 26,164 行 | 37,498 行 |
| `studio_workspace.py` | 1,661 | 2,068 |
| `persons/persons.py` | 1,287 | 1,497 |
| `frontend/src/studio/studio.js` | 2,559 | 3,003 |
| 测试函数 | 953 项 | 约 1,029 个 `def test_` |

各包规模：studio 6,575、persons 4,033、maintenance 3,996、agent（含 tools、llm）7,139、models 3,019、processing 2,713、shared 2,697、admin 2,005、settings 1,556。

## 发现

### 高

**A1 后台任务 runner 三份复制。**
`admin/source_tasks.py` 的 `SourceTasks`、`admin/dialogue_tasks.py` 的 `DialogueTasks`、`persons/person_processing.py` 的 `PersonTasks`，生命周期完全相同：`lock → get → start → retry → cancel → launch(daemon thread) → run(begin/save_progress/TaskCancelled/终态/释放锁)`。对外的行序列化也各写一份（`public_task`、`task_public`、`_task_value`），`agent/watcher.py` 还要分别知道三者的 URL 和状态词汇。
建议：抽出 `BackgroundTaskRunner`。（任务 8）

**A2 创作工作台是上帝模块，阶段逻辑分散。**
`studio_workspace.py` 一个文件里混着这些职责：job.json 持久化、方案与版本模型、文案与分镜编辑、签名与复用、预检、执行器、渲染与合成、取消、恢复、详情。`STAGES`（:36）是固定元组，`_settings`、`specification`、`preflight`、`execute`、`conflicts`、`node_dependencies` 各自按阶段名写分支。新增一种画面来源，要改全部这些位置，再加上 `studio_models.py` 里的 Literal。
建议：拆成 `studio/workspace/` 包，并引入 `StageHandler` 注册表。（任务 13、14）

**A3 取消与锁机制并存。**
- 取消有四种：任务表状态加 `CancelRegistry`，`studio_api.py` 闭包里的 `threading.Event`，旧执行器的 `steps.cancel` 哨兵，构建的 `builds/<id>.cancel` 哨兵。
- 锁有两种实现：fcntl 的 `SharedFileLock` 和 `filelock.FileLock`。
- 加锁顺序（pipeline → index-write → studio）只写在 `maintenance/clean.py:4-5` 的注释里，又在 `maintenance/backup_files.py` 中手工重写了一遍。

建议：统一为 `shared/locks.py`，用 `acquire_ordered` 按序加锁；创作执行交给 `StudioExecutor`。（任务 10、12）

### 中

**A4 上层包静态成环。**
只看模块级导入，依赖图是无环的；算上函数内导入，有以下环：`admin↔persons`、`admin↔search`、`persons↔maintenance`、`persons↔settings`、`settings↔agent`。另外 `maintenance` 反向依赖 `agent`、`studio` 和 `cli_common`（`maintenance/backup_cli.py:14`）。`tests/test_import_boundaries.py` 只约束三个包，而且用的是 `glob("*.py")`，不会递归。
建议：递归扫描，把现存的边冻结为白名单，新增的违规边让测试失败。（任务 7）

**A5 `shared/agent_domains.py` 分层错位。**
这个文件里的领域说明文字和页面领域映射是 agent 的策略内容，放进 shared 只是为了让 `shared/config.py` 能校验领域名。
建议：shared 只保留领域名元组，其余移入 `agent/`。（任务 7.4）

**A6 迁移零散。**
`shared/storage.py:135-145`、`shared/person_schema.py:46-52`、`agent/session_store.py:91-95` 各自用 PRAGMA 检查后再 ALTER。没有 `user_version`，每次构造 Store 都会重新检查一遍。
建议：`PRAGMA user_version` 加有序迁移列表。（任务 9）

**A7 两种持久化模型。**
业务数据在 SQLite，创作状态在每个任务目录的 job.json 和 workspace.json。备份、清理、物理删除都直接读 studio 的文件布局，比如 `maintenance/clean_studio.py`、`backup.py`。
这是有意的取舍，不改。但新增的输入引用（片段、图片、参考文稿、动画源码）必须由 studio 提供枚举接口，不能让维护代码再去猜文件布局。（任务 20.2）

**A8 前端无模块化。**
每个页面按固定顺序加载约 10 个全局脚本；六个页面脚本各写了一份 `api()`；`?v=` 版本参数靠手工维护。
建议：抽出 `shared/api.js`，拆分 `studio.js`，版本参数改为生成。（任务 15）

**A9 路由工厂过长。**
`studio_router` 1,005 行、`person_router` 478 行、`settings_router` 391 行，全部端点都是嵌套闭包，状态靠闭包变量共享。拆 executor 之后，`studio_router` 会明显变短。

### 低

**A10 全局 ValueError 映射为 422。**
`api.py` 把所有 `ValueError` 映射成 422，编程错误也会表现成用户错误。按领域错误类型收窄映射是更大范围的改动，记录在案，本期不做。

**A11 i18n 中间件整包重写 JSON。**
`api.py` 的 `localized_messages` 会读完每个 JSON 响应再重写。目前只有大响应受影响，本期不做。

**A12 没有 CI。**（任务 7.2）

## 当前可做

| 任务 | 内容 | 风险 | 状态 |
|---|---|---|---|
| 7 | ruff 规则、CI、递归导入边界与白名单、agent 常量归位 | 低 | 已完成：`.github/workflows/ci.yml`（推送/合并请求跑 `-m "not slow"`，每晚完整回归）；`tests/test_import_boundaries.py` 递归扫描并冻结白名单；`cli_common.py` 改为 `shared/cli_support.py`；`DOMAIN_INFO`、`PAGE_DOMAIN` 移入 `agent/domains.py`，`shared/agent_domains.py` 只留领域名 |
| 8 | `BackgroundTaskRunner` | 中 | 已完成（`shared/background_tasks.py`） |
| 9 | 两个库的版本化迁移 | 中 | 已完成（`shared/migrations.py`；`METADATA_STEPS` 现有 v1、v2，`agent/schema.py` 的 `STEPS`） |
| 10 | 统一锁模块与有序加锁 | 中高 | 已完成（`shared/locks.py`，顺序 pipeline → index-write → library → register → studio；`ProcessingLock` 的 library 非阻塞尝试是记录在案的例外） |
| 12 | `StudioExecutor` 与取消统一 | 中 | 已完成（`studio/executor.py`；后台素材/对白/人物任务的取消哨兵在 `shared/task_cancel.py`，`locks/cancel/`） |
| 13 | 拆分 `studio_workspace.py`（签名快照兜底） | 高 | 已完成（`studio/workspace/` 包，单文件 ≤700 行；`tests/test_studio_signature_snapshot.py`） |
| 14 | 阶段注册表 | 高 | 已完成（`studio/workspace/stages.py` 与 `handlers/`，`shot_stages` 按来源展开） |
| 15 | `shared/api.js`、拆分 `studio.js`、`build-assets.py` | 中（本机缺 Playwright，浏览器回归不可验证） | 已完成，与设计稿有出入：`studio.js` 按原文件顺序原样切分为 10 个经典脚本（`studio.html` 中依次为 `studio-state`、`studio-jobs`、`studio-workspace`、`studio-board`、`studio-editor`、`studio-stages`、`studio-plan`、`studio-recall`、`studio-build`、`studio-main`），按序拼接即原文件，文件名与设计稿的划分不同；任务 17 再新增 `studio-templates.js`（排在 `studio-main.js` 之前）。顶层声明仍共享全局作用域，没有改为 `window.MindexStudio`；浏览器回归仍取决于本机 Playwright |

A9 随任务 12、13 部分缓解（`studio_router` 已变短，仍是闭包式路由工厂）；A10、A11 本期未做，维持原结论。状态于 2026-10 任务 26 回填。
