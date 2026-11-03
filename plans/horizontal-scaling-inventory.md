# 水平扩展改造盘点（阶段 0 产出）

状态：随阶段 0 维护；每完成一处改造把分类从「需双后端」移到「已改造」并注明提交。
依据：2026-11-03 对源码的全量扫描（三个并行探索报告 + 人工核对），配合
[horizontal-scaling-plan.md](horizontal-scaling-plan.md) §3。文件行号会漂移，以符号名为准。

分类口径：

- **本地专用**：只服务本地单机形态（本地工具、测试、暂存 SQLite、桌面）；集群调用链不得触达。
- **需双后端**：业务/处理路径上的调用点，必须走统一抽象并在 SQLite 与 PostgreSQL（或
  local/S3、file/pg-lease、chroma_local/chroma_server、inline/pg-queue）下语义一致。
- **已改造**：已经过 BackendContext 或协议封装，并有特征测试锚定语义。

## 1. 构造链扫描（Store / SessionStore / VectorIndex）

集群路径不能静默落回本地 SQLite（plans §3）。当前构造链（阶段 0 后）：

| 入口 | 现状 | 分类 |
| --- | --- | --- |
| `api.py create_app` | `open_context(config)` → `store = backends.store`；`app.state.backends` 挂上下文 | **已改造**（阶段 0；阶段 1 起按形态返回 PG Store） |
| `Store.open(config)` | 新增规范入口（阶段 0 恒等于本地 Store） | **已改造** |
| `Store(root)` 直构 | 约 90 处（CLI、studio handlers、processing、search/warmup、persons worker 子进程、maintenance） | **需双后端**（阶段 1 注入与全面处理；本地工具可保留直构） |
| `Pipeline.__init__` / `IndexBuilder.__init__` / `dialogue.py` / `shot_repair.py` | 各自 `Store(config.data_dir)` | **需双后端**（阶段 1：接受注入的 ctx/store） |
| `SearchEngine(store, ...)`（search.py:173） | 接受 Store 实例（API 传入的是 ctx.store） | **需双后端**（阶段 2 改造检索数据源） |
| `search/warmup.py` | watcher 线程自建长驻 Store（warmup.py:66/180/210） | **需双后端**（阶段 2 与失效改造一起处理） |
| `VectorIndex(root, index_id)` | search.py:262 每请求构造；indexing/channel_index/clean_index 内多处 | **需双后端**（阶段 2 经 `ctx.vectors.open()`） |
| `agent/`：`SessionStore(root)`、`ChangeFeed`、`AttachmentStore` | agent/api.py:94-108、mcp_server.py:151-162 | **本地专用**（首期 agent 单实例网关，plans §9；agent PG 化是后续独立工单） |
| `maintenance/backup_db.py store_at()`、`clips.py` | `Store.__new__` 跳过迁移/连接，直接开暂存副本 | **本地专用**（备份暂存格式仍是 SQLite，plans §10） |
| `persons/person_worker.py:17` | 独立子进程 `Store(Path(sys.argv[1]))` | **需双后端**（阶段 3 由 worker CLI 取代） |
| `settings/local_services.py`、`RuntimeConfig` | `Store(config.data_dir)` | **需双后端**（阶段 1 随 API 链注入） |

## 2. 元数据库直连（sqlite3 API 面）

### 2.1 Store 自身（storage.py）

| 事实 | 说明 | 分类 |
| --- | --- | --- |
| 构造即迁移 | `Store.__init__` 开连接设 WAL + `migrate(METADATA_STEPS)`；幂等 | 需双后端（PG：migrate 独立 CLI，启动只检查版本，plans §4） |
| `connect()` 每调用新连接 | `sqlite3.connect(timeout=30)`、`Row` 工厂、`PRAGMA foreign_keys=ON`、退出提交/异常回滚 | 已改造（协议 RelationalDatabase 的本地实现即 Store） |
| `rows/one/execute` | 薄封装，业务大量使用 | 需双后端（PG 侧同 API；SQL 逐条登记） |
| `run()` 审计 | INSERT 用 `lastrowid`；插入与终态更新是**两个事务**（崩溃留 running 行） | 需双后端（阶段 1；语义保留：允许残留 running） |
| 进程内缓存 | `_SNAPSHOT_CACHE`（按 db 路径）、`_hot_cache`（active_index 1s TTL） | 需双后端（阶段 2 改按修订号失效；多副本各自缓存） |

### 2.2 绕过 Store 的直连

| 位置 | 行为 | 分类 |
| --- | --- | --- |
| `processing/indexing.py` `_representation_map` | `sqlite3.connect(file:...?mode=ro)` 只读 representations，模块级 LRU | 需双后端（阶段 2：服务端 metadata / 按 ID 批量查 PG） |
| `processing/shot_repair.py:129`、`maintenance/clips.py:124/300` | `sqlite3.connect(store.path)` + `Connection.backup()` 到 backups/*.sqlite3 | 需双后端（阶段 4B/5：PG 侧一致性导出替代） |
| `maintenance/clean.py` VACUUM | 对 metadata/chroma sqlite 执行 VACUUM | 本地专用（PG 交由 autovacuum，plans §10） |
| `maintenance/clean_index.py` | 直读 chroma/chroma.sqlite3 找孤儿段 | 本地专用（chroma_local 维护；server 形态另走服务 API） |
| `maintenance/backup_db.py` | snapshot/filter/rewrite/merge/validate 全套 raw sqlite（PRAGMA journal_mode/secure_delete/foreign_keys/quick_check） | 本地专用（bundle 暂存格式；PG 导出在阶段 5 重建等价物） |
| `agent/session_store.py` `Database` | 独立 agent.sqlite3，autocommit（`isolation_level=None`）+ 显式 BEGIN IMMEDIATE/COMMIT；`lastrowid`、`json_extract`、UNION ALL | 本地专用（agent 网关单实例） |

### 2.3 连接对象跨调用传递（事务加入）

| 位置 | 模式 | 分类 |
| --- | --- | --- |
| `bump_snapshot_revision(db)`（storage.py:22） | 必须在调用方事务内执行（purge.py:107、shot_repair.py:213 等） | 需双后端（阶段 1：条件更新 + 修订号语义不变） |
| `ShotMedia.register(..., db=None)`（shot_media.py:207） | 双模式：外部事务 or store.execute | 需双后端 |
| `persons/persons.py linked_shots(..., db=None)`、`purge_person_source(db,...)` | 大事务内读 | 需双后端 |
| `media_groups._validate(db, ...)` | change_members/set_groups 事务内校验 | 需双后端 |
| `maintenance/clips.py:136`、`backup_db` 长连接 | 维护事务复用 | 需双后端（阶段 4B/5 逐项） |

## 3. SQL 语义特征（→ tests/test_sql_characteristics.py）

特征测试已钉住（阶段 1 的 PG 适配器以同一组断言对照运行）：

| 特征 | 依赖点 | PG 处理方向 |
| --- | --- | --- |
| `INSERT OR REPLACE` 删旧行再插入（默认列重置、rowid 变化、NOT NULL 不豁免） | storage/curation/channel_index/dialogue/shot_repair/persons/topics/runtime_settings 等约 30 处 | 按表显式登记冲突键与「更新/重置」语义（plans §2） |
| `ON CONFLICT(id) DO UPDATE ... excluded` | media_groups.py:88 | UPSERT 等价改写（保留未提及列） |
| `INSERT OR IGNORE` 先到先得 | pipeline 注册 segments/sources、person_schema、activate、backup 合并 | `ON CONFLICT DO NOTHING` |
| `ORDER BY rowid DESC LIMIT 1` 取最新 | search_releases（storage.py:487）、channel_data（channel_index.py:49/209）、dialogue.py:161、scene_plans.py:19、curation/indexes/source_work/source_tasks、warmup.py:158、persons 多处、topics 多处 | 为依赖表加显式顺序列（seq/created），历史按原 rowid 导入（plans §5） |
| `UPDATE ... WHERE rowid=?` | shot_repair.py:201-230、backup_db.py:160-192 | 同上（显式主键） |
| 数值 OR 真值 `(a OR b) = ?`、`SUM(kind='shot')` | curation SEGMENT_SQL/过滤、clean 统计 | PG 布尔化改写（`CASE WHEN` / `::int`） |
| `json_extract` | snapshots(source_id) 过滤、agent model_events | TEXT 原文 + 受控 `jsonb` 投影列（plans §5） |
| `lastrowid` | storage.run、runtime_settings voices/profiles、agent changes/append/gaps | `RETURNING id` |
| `executemany` | channel_index 检查点、backup、clean 批删 | 批量执行 API |
| `BEGIN IMMEDIATE` | storage.activate/activate_release、persons 发布/删除、topics profiles、agent append/delete | 阶段 1：声明资源的事务接口（行锁/事务 advisory），不先 SELECT 再 BEGIN |
| CURRENT_TIMESTAMP 'YYYY-MM-DD HH:MM:SS' | 各任务表 updated_at | 显式格式化默认值（保留 TEXT） |
| TEXT/REAL 亲和、字典序排序 | segments.start/end REAL；name TEXT | 显式类型与排序对照 |
| `sqlite3.IntegrityError` | media_groups.py:93、runtime_settings.py:423/534 | 统一异常层（阶段 1） |

## 4. 文件读写盘点（→ MediaStore 化，阶段 4A/4B）

权威状态（集群必须出本地目录；plans §8「不能留节点私有权威状态」）：

| 路径 | 写入方 | 原子性 | 分类 |
| --- | --- | --- | --- |
| `sources/<sha>/<name>`、`sources/.uploads/` | pipeline.register、admin upload | 临时文件+replace，register 锁 | 需双后端（4B 上传协议/multipart） |
| `clips/<source>/shot/*.mp4` | pipeline/media.cut | `.partial`+replace | 需双后端 |
| `thumbnails/*.jpg` | pipeline | 原子 | 需双后端 |
| moment/scene 合成媒体 `cache/shot-media/` | ShotMedia.materialize（FileLock+LRU+atime） | ffmpeg+rename | 需双后端（远端物化 pin，4A/4B） |
| `plans/`、`transcripts/`、`analysis/`、`dialogue/` 检查点 | pipeline/dialogue（jsonio save） | 原子写 | 需双后端（检查点入 DB/对象清单，4B） |
| `subtitles/`、`subtitle-archives/` | subtitle_uploads/archive | replace+原子 manifest | 需双后端 |
| `persons/frames/`、`persons/<ns>/`、`cache/cast-thumbnails/` | persons 模块 | 文件直写 | 需双后端 |
| `studio/<job>/`（job.json、workspace.json、versions/、attempts/、inputs/、render/、exports/） | studio executor/workspace | 原子写 + 不可变 version | 需双后端（manifest+revision 指针发布，4B） |
| `music/<sha>` | music_library | 内容寻址 | 需双后端 |
| `voices/` | settings_api | save_upload+replace | 需双后端 |
| `agent-attachments/<token>/` | agent attachments | move 入位 | 本地专用（agent 网关持久卷，阶段 5 代理路由） |
| `reports/`、`migration-reports/`、`shot-repairs/`、`backups/*.sqlite3` | 维护/诊断 | 原子写/backup API | 本地专用→阶段 5 备份重设计 |
| `cache/query-vectors/`、`cache/admin-work/`、`context/` | 检索/管理缓存 | 原子 | 节点缓存（cluster 下仅本地，无需远端） |
| `capabilities/`、`capabilities/v2/` | 模型探测 | 原子 | 节点缓存（可重建） |
| `chroma/` | chromadb PersistentClient | 库内 | 需双后端（阶段 2 chroma_server） |
| `agent.sqlite3` | agent session store | WAL | 本地专用 |
| `logs/services/`、`locks/services/*.json` | LocalServiceManager | append | 本地专用（cluster API 不拉起本地服务，plans §7.2） |
| `locks/`（cancel 哨兵、服务 pid） | task_cancel/local_services | touch | 需双后端（3：取消走队列意图；本地保留） |

## 5. 进程状态（→ 阶段 3 队列/租约）

| 状态 | 位置 | 分类 |
| --- | --- | --- |
| BackgroundTaskRunner 守护线程 + `_RUNNERS` 弱集合 | background_tasks.py | 需双后端（阶段 3 worker CLI；本地保留 inline） |
| 任务表（source_tasks/dialogue_tasks/person_tasks/topic_runs）= 账本 | 各 admin/persons/topics 模块 | 需双后端（ledger 与入队同事务，plans §7.2） |
| CancelRegistry（进程事件+哨兵文件） | task_cancel.py | 需双后端（阶段 3：取消意图持久化+传播） |
| ProcessingLock/工作锁/index_write_lock | processing_lock.py | 需双后端（阶段 3 PgLockProvider；本地 file） |
| `recover_tasks` 启动恢复 | processing_lock.py:85 | 需双后端（阶段 3 到期回收） |
| StudioExecutor 单执行线程 + studio.lock | studio/executor.py | 需双后端（阶段 3 租约；阶段 4B revision 发布） |
| TopicScheduler（lifespan 包装、60s tick） | topics/scheduler.py | 需双后端（阶段 3 leader lease + schedule_id 去重） |
| LocalServiceManager（子进程收养、stop_on_exit） | settings/local_services.py | 本地专用 |
| 检索 warmup 线程 + 5s serving_signature 轮询 | search/warmup.py | 需双后端（阶段 2 失效改造：修订号/事件驱动） |
| agent asyncio 运行时/SSE/loopback | agent/runtime.py、loopback.py | 本地专用（阶段 5 网关） |

## 6. 进程内缓存（多副本各自持有；失效靠 DB 签名，语义可保留）

`_SNAPSHOT_CACHE`、`document_terms` lru、`_QUERY_VECTORS`（+磁盘 JSON）、
`_REPRESENTATION_MAPS`、`VectorIndex._raw_memo`、persons `_ENTITY/_EVIDENCE/_VERSION_IMAGE`、
`Store._hot_cache`、jieba 词典、`_file_hash`、`local_services._MANAGERS`、shot_media 实例缓存。
→ 阶段 2 逐项核对失效键（按 library_id/index/release/curation revision），淘汰 API（retain_*）仅本进程。

## 7. 测试映射

| 抽象/语义 | 测试 |
| --- | --- |
| BackendContext/本地协议 | tests/test_backends_local.py |
| SQL 特征基线 | tests/test_sql_characteristics.py（阶段 1 参数化到 PG） |
| 现有回归 | 全量 pytest（阶段 0 基线：2012 通过 / 15 跳过；`test_topics_ui.py::test_topics_discovery_page[cards]` 在改动前后均失败，属预存在的浏览器用例问题，与本改造无关） |
| 阶段 1 起 | `MINDEX_TEST_POSTGRES_DSN` 门控矩阵（plans §12） |

## 8. 阶段 0 变更清单

- 新增 `backend/src/shared/backends/`（context/db/media/locks/vectors/queue 协议 + 本地实现）。
- `Store.open(config)` 规范入口；`api.py create_app` 经 `open_context` 构造并挂 `app.state.backends`。
- settings 新键 `library.id`（建库生成 UUID，锁/缓存租户键；plans §2）。
- pyproject packages 增加 `mindex.shared.backends`。
- 无行为变化：本地路径全部等价旧实现；新抽象不触发网络。
