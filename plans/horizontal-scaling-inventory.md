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

## 9. 阶段 1 变更清单（pg-single）

- 新增 `shared/db/`：统一异常（挂 sqlite3 异常体系）、统一行对象、受控方言翻译器
  （占位符/保留字 end+trigger/INSERT OR REPLACE→ON CONFLICT 忠实改写含 rowid=DEFAULT
  与默认列重置/无列名 INSERT 补列/json_extract 文本等值+json_type/CURRENT_TIMESTAMP
  同形化/CASE-END 深度识别）、SQLite/PG 后端（进程内按 DSN 共享池）、PG 基线 DDL
  （43+1 表，显式 rowid identity 列）、基线迁移器（schema_migrations+迁移锁+library.id
  种子）、SQLite↔PG 往返装载内核（transfer.py）。
- Store 拆出 `backend` 注入（SqliteBackend 行为等价搬移）；新增 `PgStore`（启动仅
  check_schema，不执行 DDL）；`open_context` 支持 pg-single 形态。
- 方言化：curation 的 SOURCE_SQL/SEGMENT_SQL/隐藏过滤（OR 真值/SUM 布尔聚合 →
  CASE）、indexing check_index 的 json_extract 子查询；维护路径（备份导出/导入、切片
  迁移/清理、镜头边界修复）在 PG 下显式拒绝（UnsupportedOperation）。
- 配置：`database` 节（postgres dsn_env/池参数，exclude 序列化）；`mindex migrate`
  命令；设置页「部署后端」只读分区；config.example.yaml/docs 同步。
- 依赖：psycopg[binary]+psycopg-pool 放可选 `cluster` extra；markers
  requires_postgres/requires_s3/requires_chroma_server。
- 验收：tests/test_pg_backend.py（真实 PG16：迁移幂等/SQL 特征对照/rowid 序列/
  租约互斥/往返/create_app 冒烟）；SQLite 全量回归无行为变化。
- **未支持（阶段 2/3 交付）**：persons 证据路径的数值 json 比较/json_each（PG 上
  响亮失败）；检索热路径仍全快照装载（阶段 2 投影改造）；多 API/多机（阶段 3+）。

## 10. 阶段 2 变更清单（检索与服务端向量）

- 派生词法索引（plans §6.1）：`search_segments`/`lexical_docs`/`lexical_terms`/
  `lexical_index_meta`（迁移 v12，SQLite 与 PG 基线同步）；`shared/tokenizer.py`
  下沉唯一分词实现并定义分词器版本指纹；`shared/lexical_store.py` 发布时构建
  （channel_index check 后、ready 前，幂等）+ `mindex indexes lexical` 回填/重建；
  purge/shot_repair/clean_index/备份裁剪同步失效派生行。
- SQL 检索路径（`search/lexical_query.py`）：eligible 投影（隐藏/时长/粒度/素材
  下推 SQL，分析字段 Python casefold；OR 真值按方言 CASE）+ 请求级临时表 +
  SQL 内 Okapi BM25（N/df/mean 同口径；空文档计入分母）+ 边界候选按参考公式精确
  重排（容差 1e-10，测试锚定与 `lexical_search` 逐分一致）。
- 检索路径改造：无人物条件的文字检索与 video/image/similar 走投影 + 融合后
  `_hydrate` 批量水合命中快照（不再全量快照常驻）；人物/旧索引/分词器版本不一致
  自动回退全快照参考路径；warmup 在派生索引可用时跳过全量分词预热。
- chroma_server（plans §6.2）：`VectorConfig`（url/token_env，凭据只走 env）；
  `chroma_http_client` 进程内共享 HttpClient；`VectorIndex` 支持注入 client；
  构建器/check_index/warmup/检索全部按配置选择客户端；open_context 校验
  chroma_server 需 postgres。
- 方言补齐：`IS ?`→`IS NOT DISTINCT FROM`、INSERT 列清单保留字加引号；
  pipeline segments 命名参数改位置参数；processing/search 构造链切 `Store.open`。
- 验收：`tests/test_lexical_query.py`（eligible/BM25 与参考实现逐分一致 21 用例）、
  `tests/test_chroma_server.py`（独立服务门控）、`tests/test_pg_chroma_e2e.py`
  （PG+chroma_server 全链路：管线→发布→检索→一致性校验）。
- **未验证/后续**：百万段容量与 p95/p99 闸门（需容量基准工装）；检索过滤策略的
  服务端下推深化（当前 HttpClient 复用本地查询策略）；persons 数值 json 路径。

## 11. 阶段 3 变更清单（租约/队列/worker）

- PgLockProvider（`shared/db/locks_pg.py`，plans §7.1）：lock_resources/lock_holders
  （epoch 永不归零、counter 单调分配 fencing token；TTL 90s/心跳 20s；clock_timestamp
  短事务；持有者每次租约唯一 owner——与 flock 按 open-file 计锁语义一致）；acquire/
  acquire_ordered 与本地文件锁同形状（自动心跳 CM）；旧 token 续期/释放/提交校验拒绝。
- PgTaskQueue（`shared/db/queue_pg.py`，plans §7.2）：task_queue 表（dedupe 唯一、
  attempt_id/fencing_token、expires_at、cancel_requested）；claim = 回收 + FOR UPDATE
  SKIP LOCKED（priority→run_after→id）；complete/fail/renew 校验 owner+attempt+token
  **且未过期**（暂停过 TTL 的旧 worker 不得提交，DB 级强制）；失败有上限指数退避+
  抖动、failed 可人工重试；pending 原子取消、running 持久化取消意图。
- Worker（`worker_runtime.py` + `mindex worker` CLI）：claim 循环 + 心跳线程 + 取消
  轮询 + 槽位并发；处理器注册表映射 kind → 账本 runner（worker_handlers：冻结配置
  不含部署后端选择，worker 配置覆盖防回落 sqlite）；dispatch 分叉
  （background_tasks.launch → postgres 入队/inline 线程）+ enqueue_backlog 补偿扫描
  （账本已落未入队窗口，幂等）。
- TopicScheduler leader 租约（locks=postgres 时注入；本地维持文件锁互斥）。
- 配置：queue/locks 节（需 postgres，open_context 校验组合并接线）；设置页
  locks/queue 按配置展示；config.example/docs 同步。
- 验收：test_pg_locks 8/8（多线程争用串行化、暂停过 TTL 旧 token 拒绝）、
  test_pg_queue 8/8（claim 不相交、回收重派、退避/上限/人工重试、取消语义）、
  test_worker 6/6（引擎至少一次、失联不提交、失败重试、补偿扫描、API→worker
  端到端、调度 leader 互斥）。
- **未支持/后续**：队列与账本严格同事务（现为账本先落+补偿扫描，阶段 5 outbox）；
  GPU 声明式容量表与模型服务真实并发限制；外部模型调用幂等键（未知结果人工核对
  策略待接入真实付费调用）；多 worker 容量与吞吐基准（1.5× 闸门）。

## 12. 阶段 4A/4B 变更清单（staging 与 S3 核心）

- StagingArea（`shared/backends/staging.py`，plans §8）：工作集预约（并发预算 +
  磁盘最低余量双检查，超限 StagingFull 明确拒绝）、活动 pin（引用计数）、LRU
  淘汰（pin 不参与）、崩溃残留重扫、单文件超总预算拒绝；多线程预算安全。
- S3MediaStore（`shared/backends/s3.py`）：MediaStore 协议的 S3 兼容实现——
  不可变键 + sha256 元数据（幂等/键冲突）、boto3 传输管理器 multipart、版本
  命名空间 versions/&lt;v&gt;/&lt;key&gt;、materialize 到节点缓存（内容寻址 +
  .partial→rename + 摘要校验 + staging pin）、presign_get、iter_prefix；
  ETag 与内容 SHA256 严格区分；元数据键大小写规范化。
- 配置 storage 节（local/s3 + staging_quota_gb + s3.* 全字段）；open_context
  校验 s3 需 postgres 并装配 S3MediaStore + staging；设置页 storage 展示。
- 依赖 boto3 进 cluster extra；requires_s3 门控测试（真实 MinIO）。
- 验收：test_staging 8/8；test_s3_store 5/5（真实 MinIO：幂等/冲突、20MiB
  multipart、etag≠sha256、物化缓存命中与 pin、版本删除、presign 可取、缺失键）。
- **阶段 4B 剩余（并入阶段 5 集群装配）**：媒体 HTTP 出口的 presign/代理回退
  （Range/HEAD/ETag）与下载保留记录；uploads/检查点/Studio 工作文件的远端化与
  expected_revision 发布；GC/purge/clean/备份的 S3 适配；MinIO+ffmpeg 的
  source→index→studio 全链路闸门。S3 在此之前仅单节点验证（plans §11）。


## 13. 阶段 5 第一批变更（形态装配与诊断）

- server 配置节（role: api|agent-gateway，plans §9）；共享后端齐备
  （postgres+chroma_server+s3+postgres 队列+postgres 锁）时 open_context 形态
  推导为 cluster（缺项保持过渡形态并禁多副本）。
- `mindex doctor`（backend/src/doctor.py）：组合校验 + database/vector/storage/
  queue/locks 逐项只读检查，中文修复提示，退出码语义（0/1）；空异常消息取类型名。
- /api/health 携带部署形态与后端概览（plans §12 就绪检查）。
- docs/scaling.md：形态/组合表、pg-single 部署步骤、诚实边界清单、集群 runbook 骨架。
- 测试 test_doctor 5/5（缺省通过、非法组合、chroma 连不通 fail-closed、缺 DSN、
  pg-single 实测）。
- **阶段 5 第二批**：maintenance gate（PG advisory 全局互斥，clean/purge 接入）；
  cluster 业务 API 的 /api/agent 与 /mcp 改为网关指引（503+gateway_url）。
- **阶段 5 第三/四批**：PG 一致性导出内核（REPEATABLE READ + 逐表 manifest）与
  `mindex migration export|load|verify`（空库装载、rowid 保序、identity 序列先行、
  对账闭环）；/api/health 队列指标；migration 命令组帮助约定。
- **阶段 5 第五批（媒体出口）**：`shared/media_delivery.py` —— S3 形态 GET 无
  Range → 307 预签名（浏览器直连），Range/HEAD → 代理物化（206 断点/seek）；
  下载保留 pin（PG download_pins，URL 生命周期 + 300s 宽限，防签发后即删）；
  /api/media、/api/thumbnails、/api/shots/*/media 接入；MinIO 实测 5/5。
- **阶段 5 剩余**：agent 变更 outbox、PG bundle 的媒体/向量清单复制与打包、迁移
  CLI 的 preflight/activate 与断点、cutover/reverse runbook 实测记录、监控指标
  面板化、双机故障注入与容量闸门；4B 剩余（检查点远端化、Studio revision 发布、
  S3 purge/clean GC 适配、uploads 远端化）。


## 14. 阶段 5 第六批 / 阶段 6 首块（本次提交）

- `migration export --with-media`：按既有可达性口径（referenced_files，与本地备份
  同一口径）复制正文/缩略图/分镜/人物帧/Studio 输入；S3 逐 key 物化、本地直接复制；
  生成 media_manifest.json（size/sha256）供装载后校验。测试含本地媒体闭包 3 对象。
- 向量集合原样导出与装载已交付（`maintenance/vector_bundle.py` +
  `migration export|load --with-vectors`）：float32 原字节 base64、id/文档/元数据
  逐项保真，装载不重新嵌入；测试 3/3。
- 阶段 6 首块（桌面远程模式）见 §13 提交说明；三平台打包冒烟与用户凭据管理 UI
  为剩余项（当前为环境变量/remote.json 配置）。

## 15. 完成度与剩余工单（截至本次提交）

已达到（有可复现验证）：
- 本地形态零行为变化（完整快速回归通过；预存在失败仅 topics_ui cards，main 基线复现）。
- pg-single：PG16 实测 schema/JSON/BLOB/排序/约束/并发语义；SQLite→PG→SQLite 往返；
  create_app 冒烟；doctor；migration 闭环。
- 检索扩容：派生词法索引 + SQL BM25 与参考实现逐分一致；投影 + 候选水合；旧索引回退；
  chroma_server 实测；PG+chroma_server 全链路 e2e。
- 租约/队列/worker：多线程争用串行化、暂停过 TTL 旧 token 拒绝、claim 不相交、
  回收重派、退避/上限/人工重试、取消语义、API→worker 真实任务端到端、调度 leader 互斥。
- staging：预约/pin/LRU/磁盘余量/单文件超限/多线程预算。
- S3：MinIO 实测 put 幂等/冲突、20MiB multipart、物化缓存、版本删除、presign 可取、
  媒体出口 307/206/HEAD、purge 前缀删除与 pin 跳过。
- 集群装配与诊断：cluster 形态推导、agent-gateway 路由 503、maintenance gate 互斥、
  health 指标。

剩余（未交付或未验证，按计划闸门尚不能宣称集群就绪）：
1. 媒体版本清单与 cutover pin 的完整生命周期（现为导出快照 + 媒体/向量闭包，
   缺 pin/GC 协调与在线差量）。
2. Studio expected_revision 条件发布、检查点/transcripts/plans 远端化、uploads 远端化
   与 GC pin 全链路（当前 S3 为单节点验证：媒体出口与 purge 已接，其余按 plans §8 待办）。
3. maintenance gate 与处理任务的全面互斥：现为 PG 维护互斥 + 既有文件锁；locks=postgres
   尚未替换 ProcessingLock/index_write_lock 的全部路径（多机互斥依赖阶段 3 租约的接线面）。
4. 双机跨主机故障注入（kill -9/SIGSTOP 超 TTL/断连/双 API 同时编辑）、容量基准
   （10万/100万 segments、1%/0.1% 过滤、p95/p99、2 worker 吞吐 1.5×）、
   备份恢复演练与 RTO/RPO 记录。
5. 桌面三平台打包冒烟与用户凭据管理 UI（现为环境变量/remote.json）；agent PG 化与
   会话恢复（plans §9 明确为后续独立工单）。
6. persons 证据/发布路径的 SQLite 专有 JSON 查询（`json_each`、`json_group_array`、
   `json_object`、数值 json 比较，约 20 条）在 PG 形态尚未适配，会显式报错。适配需要
   逐条双方言改写 + 双后端 persons 测试矩阵，作为独立工单；NFS 后端未评估。


## 16. 缺口修复（单机可验证项，1/3/4/5/6/7/8/9/10）

逐项按 `tests/test_scaling_remaining_verification.py` 验证（每项先断言当前行为，
能力交付后相应 xfail 分支已改为通过断言）：

1. **锁接线**：新增 `shared/lock_backend.py`；`ProcessingLock`/`PublicationLock`/
   `index_write_lock`/`recover_tasks`/register/studio/topics/person 审核路径按
   `locks.backend` 选择文件锁或 PG 租约（资源锁 key 与文件形态同源）；实测同一 PG
   库不同 data_dir 的独占 pipeline、同素材资源锁、index-write 互斥，以及无活跃
   租约时任务恢复标记中断。
3. **Studio 条件发布**：`workspace.persist(expected_revision=)` 在资源锁内读-比对-
   写（CAS），`RevisionConflict` → 409；`GET /workspace` 返回 `workspace_revision`；
   `POST /plans`、`activate`、`delete` 支持 `If-Match`；并发两写者恰好一个成功。
4. **检查点远端化**：`shared/checkpoints.py` 写穿透（plans/transcripts/analysis）+
   本地优先回读；新节点（不同 data_dir）从 S3 读回分镜计划，本地无副本亦可续跑。
5. **uploads 远端化**：`Pipeline.register` 在 S3 形态直写内容寻址不可变对象并跳过
   本地副本；`Store.resolve_media` 优先本地产物、缺失时按需物化远端；重复上传幂等。
6. **S3 孤对象回收**：`clean` 增加 S3 规则——引用闭包（与本地备份同口径）+ 宽限
   （`clean.s3_orphan_grace_hours`）+ 下载 pin 复验后条件删除；被引用/pin/未过宽限
   对象保留。
7. **persons PG 双方言**：JSON 方言扩展（通用 json_extract 文本/数值/布尔、嵌套路径、
   json_each→jsonb_array_elements、json_type 归一 number→real、value 文本比较、
   `length(CAST(x AS BLOB))`→octet_length）；审核临时表、确认人聚合、紧凑证据、
   发布快照与版本过滤显式 PG 变体；`SELECT *`/`alias.*` 展开为显式列（排除 rowid）
   保持两后端结果形状一致；numeric→float/memoryview→bytes 行值归一。双后端一致性
   测试覆盖审核摘要/列表、发布快照、版本图像索引。
8. **agent 变更 outbox**：PG `agent_outbox` 表（运行态），cluster 业务 API 的
   `POST /api/agent/changes` 写入 outbox（201），单实例网关 `OutboxBridge` 轮询并
   幂等转入 ChangeFeed（drain 单事务标记消费，重复 drain 为空）。
9. **指标**：`/api/health` 与新增 `/api/metrics` 暴露连接池等待、租约到期/持有数、
   队列深度、staging 水位；修复 psycopg_pool 3.3 指标 API（`get_stats`）。
10. **GPU 容量**：`gpu.capacity`/`gpu.queues` 声明式准入，槽位为资源租约（跨节点、
    TTL 到期回收）；实测 capacity=1 时两个 worker 实际并发不超过 1。

**仍不在单机可验证范围**：跨两台主机的故障注入、百万级容量基准与 p95/p99、
RTO/RPO 演练记录、Windows/Linux 桌面打包冒烟、真实模型质量评测。


## 17. 阶段 6 桌面凭据管理（本次提交）

- 桌面菜单 **Mindex → 连接远程服务…**：独立设置窗口（remote-preload 暴露最小 IPC），
  填写 URL 与用户访问令牌，保存到 `remote.json`（0600、临时文件 + rename 原子替换），
  清除配置回到本地模式；保存/清除后自动重启后端生效。
- 环境变量仍优先（受管终端），窗口内明确提示；凭据不进入日志或打包产物。
- 测试：desktop node 测试 31/31（新增保存 0600/校验/清除/写失败不破坏原文件）。
- **未验证**：macOS/Windows/Linux 图形界面冒烟（本环境无 GUI），Electron 打包与
  三平台安装包未重跑；这些属于发布前检查，不影响单机功能验证结论。
