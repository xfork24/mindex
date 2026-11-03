# 素材容量水平扩展：评审结论与实施方案

状态：分阶段实施中（2026-11 起；进度见 [盘点文档](horizontal-scaling-inventory.md) 的各阶段变更清单）。不代表当前程序已支持全部新配置、新命令或集群部署。各阶段通过验收后才能进入下一部署形态，不能直接照示例修改生产配置。

## 0. 执行约定（2026-11 补充，随实施立项确认）

1. **本地实现保留为单机简单实现**：现有 sqlite + 本地嵌入 chroma + 单机文件系统
   存储的零依赖缺省形态长期保留，不因分布式实现而移除或降级。每个后端能力
   （关系数据库、向量库、媒体存储、锁、队列）都抽象为协议（阶段 0 的
   `shared/backends/`），当前实现即该协议的 local 实现；分布式实现（postgres、
   chroma_server、s3、pg-lease/queue）按本方案分阶段加入。后端选择经 config
   文件配置（阶段 1 起；`database`/`vector` 节），设置页「部署后端」分区提供
   形态/身份/健康诊断；写回式配置界面随阶段 5 集群装配交付。
2. **分阶段实施、逐阶段验证、逐阶段提交**：按 §11 的阶段（0→1→2→3→4A/4B→5→6）
   推进；每个阶段先补语义/特征测试，接入新后端后通过该阶段的验收闸门（含完整
   回归）才进入下一阶段，每完成一个阶段向 `scaling-refactor` 分支提交一次，
   提交信息记录该阶段的交付物与验证结果。

## 1. 结论与交付边界

方向可行，但原方案不能直接实施。主要障碍不是更换数据库驱动，而是单机事务、文件和进程状态隐含的正确性约束。采用以下路线：保留零依赖本地模式，先完成共享元数据与检索改造，再接入持久队列、共享媒体和集群发布协议，最后开放多个 API 副本。禁止只换 PG 或把 data_dir 挂到 NFS 就启动多个实例。

首期集群交付：一个逻辑素材库、一个可信协作空间，PostgreSQL + S3 兼容对象存储 + 独立 Chroma 服务 + 无状态业务 API + 多 worker。agent 使用独立单实例网关，直到会话执行与事件恢复完成；不以负载均衡粘性代替恢复协议。保留 index/studio/topics 的必要串行临界区，按素材/任务并行重处理。多用户并发不等于租户权限隔离，细粒度 RBAC 与多租户计费不属于首期。

这是应用和处理能力的水平扩展。单 PG 主库、单 Chroma 服务仍是容量上限和故障点，不宣称数据库、向量索引自动分片或高可用。达到后文容量闸门后另立存储分片/HA 方案。

必须保持：本地缺省配置与工作流；strict config；嵌入空间身份；已发布索引与切分布局不可变；锁顺序 pipeline → index-write → library → register → studio → topics；检查点与内容摘要；业务备份的秘密裁剪；中文错误与中英 UI。源码不变量由“SQLite 是事实来源”演进为“配置选定的元数据库是事实来源”，迁移阶段同步更新 AGENT.md/CLAUDE.md。Studio 仍保留任务目录逻辑结构和不可变版本，远端通过目录清单与修订指针实现。

## 2. 对原方案的关键修正

| 原假设/遗漏 | 实际问题 | 本版处理 |
| --- | --- | --- |
| Store.connect 插适配器即可使业务 SQL 零改动 | sqlite3.Row 同时支持名称/位置访问；直连 SQLite、异常捕获、事务控制分散；REPLACE 会删旧行再插入，UPSERT 不等价 | 保留薄适配层，但逐条登记 SQL；复杂/语义相关语句显式双方言，逐调用点迁移 |
| 只从 PK 推导 conflict target | 还存在 UNIQUE(role,name)、name UNIQUE 等约束，且省略列默认值、rowid 更新顺序会变化 | 为每种写入声明冲突键、保留/重置列和排序语义，不能按表名统一猜测 |
| simple tsvector 与 jieba tokens 严格相同 | PG parser 仍会重新分词；特殊 token/长度限制可能不同；channel 缺文档时当前代码仍把空文档计入 N | 首版用精确 token 倒排表；明确空文档、过滤、分词版本和统计口径，验证后才讨论 FTS 优化 |
| 取完全部 FTS 命中回 Python 就消除了线性成本 | 高频词仍可能命中全库；按过滤动态算 N/df 仍需扫描；人物和 annotation 过滤必须先于统计 | 默认在 SQL 内统计和评分，流式精确重排作为诊断；不保证任意查询为次线性 |
| 最终 top-limit 才读快照 | 时长重排、IoU 去重、人物证据和对白展示需要更早读取候选字段 | 分层投影，融合候选批量读取，完成去重后补齐结果快照 |
| lease + TTL 就能替代文件锁 | 进程暂停/网络分区后旧 worker 仍可能运行，双写共享文件和完成状态 | 单调 fencing token、条件提交、不可变 attempt 对象、GC 延迟；失去租约即停止提交 |
| 路径 hash 作为 tenant_key | 同库不同机器的路径不同，导致锁互不相见 | 持久化 library_id UUID；锁和缓存以 library_id 隔离，worker_id 为进程启动 UUID |
| PG advisory lock 不支持共享模式 | PG 实际同时提供共享与独占 advisory lock | 因长任务连接占用、失联后写入风险选择租约；短事务用行锁或事务 advisory lock |
| queue claim 唯一就保证任务唯一执行 | 崩溃、重试和回收会重复执行，外部调用无法随 PG 事务回滚 | 至少一次交付，幂等键、attempt、超时回收、可见失败队列和外部副作用策略 |
| PG 单事务导出即可不停写备份 | 默认 READ COMMITTED 非全事务快照；媒体/向量/Studio 不在 PG MVCC 内 | 一致性快照 + 发布清单 + 资源 pin；首版短时暂停写入/清理，验收后才开放在线备份 |
| config 切回旧后端即可回退 | 新库、新对象和服务端向量的后续写入不会自动回到旧端 | 写入前可切回；开放写入后必须停写、逆向迁移或恢复备份，并核对全部介质 |
| Chroma HttpClient 即向量水平扩展 | 只把索引内存移出 API；全量 eligible ID 与 representation map 仍会膨胀 | 服务端过滤与候选映射按需读取，单节点性能实测；Qdrant 后续评估 |
| 20 GiB 上传/20 GiB staging 足够 | staging 还需输出/转码/临时文件；单次 PUT 的后端限制与大文件不匹配 | 大文件 multipart；按工作集预约磁盘，超限拒绝或排队，不因淘汰破坏运行任务 |
| agent.sqlite3 PG 化即可多 API | 内存 runtime、附件、回环令牌、执行归属仍在单进程；该库含秘密且原本不进业务备份 | 首期独立单实例 agent 网关，单独迁移/灾备策略；通用 API 扩容前代理路由与失败恢复必须就绪 |

依据：SQLite [冲突处理语义](https://www.sqlite.org/lang_conflict.html)、PG [文本函数](https://www.postgresql.org/docs/16/functions-textsearch.html)、[事务隔离](https://www.postgresql.org/docs/16/transaction-iso.html)、[锁函数](https://www.postgresql.org/docs/16/functions-admin.html)。这些是实现约束，不是本项目已经完成的测试结果。

## 3. 当前代码核对与先行盘点

本次读取源码确认的入口如下；文件行号会随开发漂移，不再以未经重算的 SQL 数量作为工作量依据。

| 范围 | 当前事实与必须覆盖的调用方 |
| --- | --- |
| 元数据库 | `shared/storage.py` 的 Store(root) 在构造时迁移，connect 每次开 SQLite；rows/one/execute 外还有业务持连接执行、executemany 和位置取值；`shared/migrations.py` 用 PRAGMA 与 SQLite 脚本解析 |
| 方言和排序 | storage.activate 在 search_releases 使用 rowid；pipeline、dialogue、channel_index、scene_plans 也按 rowid 选最新；shot_repair 按 rowid 更新。原清单遗漏 search_releases 与若干别名 rowid |
| 方言之外 | `admin/curation.py` 的 OR 数值真值与 PG 类型规则不同；`shared/media_groups.py` 捕获 sqlite3.IntegrityError；person/topic 表及 BLOB 向量、运行时配置均需盘点 |
| 词法检索 | `search/search.py` 的 tokens/document_terms/lexical_search：jieba、Counter、按 eligible 算 N/df/mean；format 2 对每个 eligible segment 构造 channel 文档，缺 channel 时为空 |
| 结果与过滤 | `_snapshots` 全量 decorate/filter；effective_hidden 包含 source、segment、parent；人物 onscreen/mentioned 会进一步缩小 eligible；`_result` 有时长排序及同源时间 IoU 去重 |
| 向量 | `processing/indexing.py` 的 PersistentClient、全索引 representation map、先过采样再过滤和 where 兜底；`_representation_map` 直连 metadata.sqlite3 |
| 文件状态 | ShotMedia、uploads、studio/workspace、persons、topics、transcripts/plans 检查点、reports 与 agent attachments；不只是六个媒体 HTTP 出口需要适配 |
| 进程状态 | background_tasks、processing_lock、task_cancel、StudioExecutor、TopicScheduler、LocalServiceManager、agent runtime/loopback/SSE |
| 备份/维护 | backup_db 的 backup/merge/filter/rewrite、backup 的目录切换与 recover 日志；clean/purge/clips/shot_repair 的文件操作不能由连接适配器自动支持 |
| agent | session_store.Database 独立 SQLite、自动提交语义与显式事务；agent/schema.py 独立迁移序列。原业务 bundle 有意排除 agent 会话与秘密 |

阶段 0 产出 `plans/horizontal-scaling-inventory.md`（待创建）：每处直连、SQL、异常、连接 API、文件读写、锁与缓存按“本地专用/需双后端/已改造”归类，附测试映射。尤其扫描 Store(config.data_dir)、Store(root)、SessionStore、VectorIndex(root,...) 的构造链；集群路径不能静默落回本地 SQLite。

## 4. 运行形态与配置契约

先定义有效组合，随后在 shared/config.py 实现，不通过 mode 暗改后端缺省值。

| 形态 | database / vector / storage / queue / locks | 允许的进程 |
| --- | --- | --- |
| local（现状） | sqlite / chroma_local / local / inline / file | 单 API，现有 CLI/桌面行为 |
| pg-single（过渡测试） | postgres / chroma_local / local / inline / file | 单主机单 API，禁止多机写入 |
| cluster（最终） | postgres / chroma_server / s3 / postgres / postgres | 多业务 API、多 worker；agent 单实例网关 |

NFS 为后续可选共享媒体后端，不能存放 SQLite、嵌入式 Chroma 或作为分布式锁依据。若增加 NFS，要验证实际服务器/协议/挂载的 rename、同步与错误恢复语义；不是一句 flock 告警就支持生产。S3 兼容产品也要逐项验证 multipart、checksum、version/CAS 能力，不能默认等同 AWS。

配置草案（以下为未来字段和值示例，暂不可使用）：

```yaml
server:
  mode: cluster
  role: api                     # api / agent-gateway；worker 是独立 CLI
library_id: "<建库生成并在数据库中校验的 UUID>"
database:
  backend: postgres
  postgres:
    dsn_env: MINDEX_DATABASE_DSN
    pool_min: 0
    pool_max: 8
    control_pool_max: 2
vector:
  backend: chroma_server
  chroma_server:
    url: "https://vectors.internal"
    token_env: MINDEX_CHROMA_TOKEN
storage:
  backend: s3
  s3:
    endpoint_url: "https://objects.example"
    bucket: mindex
    prefix: "libraries/<library_id>/"
    region: "<region>"
    access_key_id_env: MINDEX_S3_ACCESS_KEY
    secret_access_key_env: MINDEX_S3_SECRET_KEY
    staging_quota_gb: 100         # 示例，按任务工作集计算
    presign_expire_seconds: 900
queue:
  backend: postgres
locks:
  backend: postgres
```

缺省仍全部 local；cluster 缺任何共享后端即启动失败，列出中文修复提示。数据库、存储、向量 namespace 必须匹配 library_id。远程 PG 使用校验证书的 TLS；本地 compose 可以明确使用开发例外。凭据只从 env/密钥服务读取；配置导出、任务冻结快照、日志与错误都不能展开秘密。服务能力和分词版本进入可诊断的版本记录。

数据目录在 local 为事实存储，在 cluster 仅为节点缓存/临时目录；集群权威检查点必须进入数据库或对象清单。API/worker 不可在启动时自动执行集群 DDL：独立 migrate 命令执行迁移，启动仅检查 schema 版本与能力，避免多个版本同时改表。

## 5. 数据库适配：显式语义优先

保留 Store.rows/one/execute，但引入 Store.open(config)/运行上下文注入。Store(root) 仅明确服务本地工具、测试与暂存 SQLite；集群调用链必须传同一个 BackendContext（library_id、db、media、locks、vectors、queue）。禁止全局 monkey patch 或根据路径偷偷发现远端配置。

- `shared/db/` 提供 SQLite/Pg adapter、事务上下文、游标迭代/分页、批量写入、统一行对象及数据库异常。行对象兼容名称与位置访问；覆盖 fetchone/fetchall/迭代、rowcount、executemany、提交/回滚。业务改捕获统一的完整性异常，不能漏掉 sqlite3.Error 分支。
- 不做通用 SQL 转译。占位符转换只覆盖受控语句，扫描字符串、双引号标识符、行/块注释并测试；PG 原生 SQL 直接走专用方法。REPLACE、JSON 函数、数值 OR、SUM(kind=...) 等布尔聚合、PRAGMA、无列名 INSERT 均显式改写并指定列；SQL 数值/文本排序与 SQLite 宽松类型也纳入对照。
- JSON 首版保留 TEXT 原文，保留摘要/快照字节、键顺序和数值表示；需要过滤的字段生成独立投影列/表，必要时使用受控 JSON cast。不要一开始全库改 JSONB 再伪装返回字符串。队列 payload 等新增结构可使用 JSONB。
- 已有时间戳首版保留 TEXT 与 UTC 固定格式，PG 默认值显式格式化；不全库重写历史类型。新增 lease/queue 的时间列用 TIMESTAMPTZ。BLOB 映射 BYTEA，返回 bytes。
- 所有 REPLACE 调用单独决定“更新现有行”还是“重置为新行”。含默认列、关联键、最新顺序的行为先写特征测试。为依赖 rowid 的表增加明确的顺序字段：历史按原 rowid 导入，后续按原语义维护；不能给全部旧行随机分配 identity。search_releases 的选择规则也必须覆盖。
- `BEGIN IMMEDIATE` 改为声明资源的事务接口：短期保守使用同库事务互斥，再逐步收窄到资源行锁；不能先 SELECT 再 BEGIN。读改写和去重依赖唯一约束/SELECT FOR UPDATE，涉及所有写入方。死锁/序列化失败仅重试完整且可重入的事务，不重试外部模型调用。
- 双方言迁移只追加，保留已发布 SQLite 步骤。PG 基线对应当前逻辑 schema，独立跟踪 metadata/agent 版本；以后共用逻辑版本，允许后端空操作。schema_migrations 记录版本/校验值，受迁移锁保护。不同后端物理表/索引可以不同，不要求 PG 专属 tsvector 在 SQLite 存在。
- PG 查询池与心跳/控制小池分开，后者只跑短事务，不为长任务 pin 连接。总连接上限按“API 副本 × 池 + worker 进程 × 池 + agent + CLI + 维护余量”计算；并发预算超过 PG 预留前拒绝扩容。配置 statement/lock/idle-in-transaction 超时并暴露池等待指标。

新增 package 时同步 pyproject.toml 的显式 packages；PG/S3 依赖放可选 cluster extra，更新 uv.lock。基础桌面包不强制包含这些二进制依赖。

## 6. 检索：先保持统计语义，再消除全快照装载

### 6.1 精确词法倒排

新增派生数据（均绑定 index_id，可重建，不能改变已发布内容快照）：

- `search_segments(index_id, segment_id, source_id, parent_id, kind, start, end, annotation_projection, indexed_views, ...)`，以冻结快照生成；curation 的展示名/隐藏状态实时 JOIN。字段索引以 EXPLAIN 与数据分布决定。
- `lexical_docs(index_id, segment_id, channel, doc_length)`，每个 segment 为 dialogue/analysis 都建行，缺文档长度为 0。`lexical_terms(index_id, channel, term, segment_id, tf)`，主键覆盖这些键，增 token 查询/文档外键索引。term 使用确定性逐字节相等语义，不采用模糊或不区分大小写 collation。
- 复用同一 `tokens()`，持久化 tokenizer 版本（jieba 版本、词典摘要、tokens 实现版本）；不同节点版本不一致禁止构建/查询新派生索引。迁移回填不能只依赖当前安装的词典且不留记录。

查询先得到完整 eligible 关系：索引/粒度/素材组交集、source/segment/parent 隐藏、时长、annotation、人物 onscreen/mentioned。annotation 首版可以只读取小投影在 Python casefold 后把 ID 放入请求级临时关系，再供 SQL 统计；这仍可能线性，但不再拉全部 snapshot。空交集直接返回空结果，不扩大范围。

N、mean、df 必须在该 eligible 上计算，channel 缺失的空文档计入分母；mean 为 0 时按现算法用 1。先按查询 token JOIN 倒排取命中，再在 SQL 内累计相同 BM25 公式，按 score DESC、segment_id 的确定性顺序取当前 candidates。term 按当前 Python 顺序累计的参考实现保留为流式诊断路径；SQL 聚合顺序和浮点误差不能承诺逐位相同。验收要求分数在固定容差（初始 1e-10，记录绝对/相对误差）内，排序一致；SQL 先保留 candidates 边界误差范围内的完整候选集合，再以参考公式重算裁切，不能只对已截断 top-K 重排（漏出的边界项无法补救）。容差需以实际语料和公式误差验证，并检查其对 RRF 的影响。N/df/mean 与 eligible 使用同一查询快照，避免并发 curation 改动混合统计。

高频词和复杂人物筛选仍有最坏 O(N) 成本；设置查询超时与诊断，不可静默截断候选或改用全库统计。若百万规模闸门失败，再评估 WAND/专用搜索服务，不能直接宣布这一阶段已达标。PG FTS 可作为后续候选加速，但必须证明 token 适配（例如 array_to_tsvector 而非重新 parser）、限制兜底与 eligible 语义，不列为首期依赖。

### 6.2 向量与结果组装

Chroma HttpClient 是首选最小改动；服务版本、镜像 digest、HNSW 参数和客户端兼容性需固定。VectorStore 协议包含显式向量批量写入/读取、分页导出、查询、校验和删除，保留 embedding_function=None 与空间身份。

集群不载入全量 representation map，优先使用服务端 metadata 返回 segment_id/view，再对候选批量查 PG。过滤先服务端缩小 index/view/source/kind；任意 eligible 可采用有上限的 ID 过滤或自适应过采样。超出请求长度或过采样预算必须走经过验证的后端过滤路径或报查询预算不足，禁止退回无过滤查询、漏掉人物/素材限制。高选择性过滤（1%、0.1%）是强制基准。

融合全部 route candidates 后读取必要 start/end/source/证据投影，执行时长排序与 IoU 去重，再补齐返回快照；不能先取 limit 再去重而导致少返回。warmup 和缓存按 library_id/index/release/curation revision 命名，不再每 API 全量分词/装载 HNSW；活动发布与可见性用数据库修订号失效。

百万条 4096 维 float32 的裸向量约 15.3 GiB，尚未含 HNSW、metadata、多个索引版本和构建临时空间，不能据记录数直接承诺容量。若 Chroma 未通过性能闸门，转入 Qdrant 评估并补独立迁移/召回实验。pgvector 的普通 vector/halfvec ANN 索引维度限制影响 4096 维直接接入，但其有二值量化检索再重排等路线，不能断言技术上绝对不可行；首期因引入量化评估而不选。参考 [pgvector 官方说明](https://github.com/pgvector/pgvector)、[Chroma 架构](https://docs.trychroma.com/docs/overview/architecture)。

## 7. 锁、队列和故障恢复

### 7.1 租约与 fencing

建立 `lock_resources(library_id, name, epoch, ...)` 和 `lock_holders(library_id, name, owner_id, mode, fencing_token, expires_at, ...)`。同资源获取/续期/释放/回收时以资源行 FOR UPDATE 串行化；共享持有者为多行，独占需无有效共享/独占。加入等待独占者优先策略或有界等待，避免共享请求饿死维护。保留现锁顺序，禁止隐式共享→独占升级，并用当前文件锁测试明确重入及超时行为。

epoch 永不因删除持有者而归零；独占新授予递增，token 不复用。续期/释放必须匹配 owner+token 且未过期，不能复活已失效租约。时间由 PG statement_timestamp()/clock_timestamp() 的短事务提供；now() 固定于事务开始，不用长事务做心跳。初始 TTL 90s、心跳 20s，可压测调整；过期后再回收，kill -9 的恢复应为 TTL + 回收周期 + 调度等待，而非“TTL 内完成”。见 [PG 时间函数](https://www.postgresql.org/docs/16/functions-datetime.html)。

所有可变业务状态的提交在同一短事务里锁定并校验 holder/token/有效期，然后条件更新；旧 token 的进度、完成、发布、取消确认都拒绝。外部产物按 attempt_id 写新 key，只有有效 holder 能更新权威指针。S3 不会检查 PG fencing token，故不得覆盖共享 workspace.json 或旧 attempt 文件；失败产物延迟 GC。失联 worker 停止接单、取消/终止可终止子进程，恢复连接后仍必须复验 token。不能仅依靠“心跳线程还活着”。

### 7.2 至少一次任务队列

队列字段至少包含：library_id、id、kind、ledger_id、dedupe_key、priority、status、run_after、attempts/max_attempts、owner、attempt_id、fencing_token、expires_at、cancel_requested、error、created/updated。状态为 pending/running/succeeded/failed/cancelled；超过重试上限的 failed 保留并可人工重试。建立 pending claim 局部索引、running 到期索引和业务唯一 dedupe 约束。

- claim 在一个短事务用 FOR UPDATE SKIP LOCKED，筛选 worker 能力/queue，按 priority、run_after、id 取任务，attempts 加一并分配新 attempt/token；到期 running 先条件回收再重排。只有原 owner/token 能续期和完成。
- 账本（source/dialogue/person/topic，另含 studio/index 登记）与 enqueue 同事务；重复提交只产生一个有效工作。事务外入队用 durable outbox，不使用“先写账本后发队列”的双写窗口。
- ledger 是用户可见状态来源，队列只存引用及调度状态；ledger 必须绑定当前 attempt，队列和 ledger 完成同事务。payload 不序列化函数、局部路径、数据库连接或凭据。
- 超时、重试采用有上限指数退避与抖动；heartbeat 线程故障使任务失败而非无限续跑。取消 pending 原子转 cancelled；取消 running 持久化意图并传播到 ffmpeg/模型调用，返回状态不能早于账本一致。
- 模型计费、远程生成等副作用：服务支持时传幂等键；不支持时保存外部 request_id 并查询恢复。未知执行结果不能盲目重试付费调用，应标记人工核对。不能承诺 exactly-once。
- GPU slots 按实际物理资源/共驻模型组声明，不只是每 base_url 一个计数器；lease 控制准入，模型服务也要限制真实执行并发。旧请求在失联后可能仍执行，slot 不得立即释放后超卖 GPU。
- TopicScheduler 仅由单个调度角色运行或持 leader lease，周期触发带唯一 schedule_id+scheduled_at；cluster API 不自动拉起 LocalServiceManager 或人物子 worker。

建议 CLI（待实现）：`mindex worker --queues source,dialogue,person,studio,index --concurrency N`。起步用 2 worker，每个进程并发 1；完成容量与 GPU 准入测试后增加。索引构建、Studio 与备份保留协调者，pipeline 粗锁仅保护布局/发布或维护，不可让每个 source 全程独占全局锁导致 worker 数增加却无吞吐收益。

## 8. 媒体、检查点与 Studio 发布

MediaStore 的 key 沿用相对路径，但 protocol 不能只返回裸 Path：

```python
class MediaStore(Protocol):
    def stat(self, key: str, *, version: str | None = None) -> MediaObject: ...
    def open_read(self, key: str, *, version: str | None = None): ...
    def materialize(self, key: str, *, version: str | None = None): ...  # ContextManager[Path]
    def put_immutable(self, key: str, source, *, sha256: str) -> MediaObject: ...
    def delete_version(self, key: str, version: str) -> None: ...
    def iter_prefix(self, prefix: str): ...
    def presign_get(self, key: str, *, version: str, expires: int) -> str | None: ...
```

MediaObject 区分 key/size/sha256/backend_version/etag；ETag 不是通用内容 SHA256。远端物化返回上下文，整个 ffmpeg/PyAV/响应使用期持本地缓存 pin；内容锁按 key+version 防重复下载。先写临时文件、验证摘要、fsync/rename 后可见；不能边下载边作为完整文件使用。大媒体用流，不 read bytes。put 完成并验 hash 后再提交元数据，PG 指针发布前对象不可被正式使用。

staging 用显式 LRU 与活动 pin，运行前预约输入+输出+临时空间，并设节点级并发磁盘预算/最低余量；支持单文件大于缓存上限时明确拒绝。20 GiB 原片可能需要远超 20 GiB 工作集。缓存 pin 与远端删除 lease 是两种保护，后者与 GC 协调。

上传协议：鉴权/限流/配额预约 → 随机 pending key → 小文件 PUT 或大文件 multipart（按实际 S3 兼容服务限制选择）→ complete 验证 upload_id/所有者/长度/服务端可信 checksum → 计算或验证内容 SHA256 → 写不可变最终 key → 注册素材。客户端声明的哈希与 multipart ETag 均不能作为唯一可信校验。complete 幂等；定期 abort 孤立 multipart、清理过期 pending 并归还配额。禁止对正式内容 key 发可重用的写 URL。参考 [S3 预签名 URL](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)。

媒体响应先做现有 web_auth 与库访问检查，再签发短期读 URL；URL 是可转交能力，过期前无法靠登出撤销。签发时建立覆盖 URL 生命周期和下载宽限的保留记录，避免 presign 后立即被 purge。代理回退必须支持 Range/HEAD/ETag/断点请求；浏览器直连要求独立可达 endpoint、HTTPS、CORS 和 Content-Type。测试过期重签与长视频 seek。

Studio 与其它工作文件采用“不可变 manifest + PG 当前 revision 指针”：attempt 在本地生成目录树，上传所有文件及摘要清单，再以 expected_revision+token 条件发布。人工 PATCH 同样走修订检查，不能仅保护 worker 而让 API 写旧目录覆盖新状态。保留 job.json/workspace.json 的导出逻辑视图；本地模式保留现有原子文件路径。transcripts、plans、人物帧、topics profile、reports、agent attachments 都要纳入文件盘点，不能留节点私有权威状态。

PG、向量、对象之间不用伪分布式事务：索引版本依次 building → vectors/media verified → ready → active，任一步失败均不可激活；发布事务记录不可变 manifest/revision。崩溃重试按稳定 ID 补齐，资源引用只随提交可见。GC 使用标记→宽限→复验引用→条件删除，覆盖运行 attempt、发布版本、备份 pin、下载 pin；破坏性维护统一走分布式 maintenance gate。

## 9. agent 与桌面

首期将 /api/agent、agent changes/SSE 与相关附件路由固定代理至单实例 agent-gateway，网关持久卷保存 agent.sqlite3/附件，内部回环仍调用自身 ASGI 或受控业务 API；内部进程 token 不跨集群共享。其他业务 API 不创建独立 SessionStore/运行循环。独立 mindex mcp 的集群模式同样调用网关/API，不在所在节点写 agent.sqlite3；跨 API/worker 的变更通知先写 PG outbox，再由网关幂等转入 ChangeFeed，避免本地 SSE 事件分裂。网关故障时助手明确不可用，重启后从事件恢复或标记中断，不能假装继续未落盘的工具调用。入口完整性通过路由测试验证，不只配置负载均衡 cookie。

agent PG 化作为后续独立阶段：包含事件 seq 原子分配、session 执行 lease/fencing、Last-Event-ID 重放与断线恢复、附件共享、secret vault 密钥管理、未知工具结果恢复。业务 bundle 继续不含 agent 库/秘密；基础设施备份单独加密覆盖并演练恢复。

桌面首选远程 API 客户端模式：连接服务 URL 和用户凭据，远程数据处理/存储留在服务端，避免向每台终端分发 PG/S3 管理凭据。本地桌面模式保持原 sidecar。实现前验证页面导航、WebSocket/SSE、上传、外部 URL 和跨源策略。原“sidecar 直连 PG/S3 胖客户端”降为可选后续方案，仅可信受管终端、最小权限凭据，依赖全部集群安全机制与共享向量/队列；不是只完成 PG+S3 就开放。

## 10. 备份、迁移与正式切换

业务 bundle 外层格式优先保持，暂存 SQLite 的 schema 随追加迁移演进；只承诺同版本程序支持 local↔cluster 导入导出，不承诺旧二进制读取新 schema。派生检索表可按清单重建；schema 校验、表白名单、显式列写入、裁剪与重定位都要更新，不能仅增加 snapshot_to_file。PG VACUUM 不是 SQLite 压缩的等价替换，交由 autovacuum/运维，不在业务事务里运行。

首版一致性备份流程：阻止新业务写入/接单及 GC → 排空正在提交的任务（只读服务可继续）→ 固定 active release、Studio revision、媒体 version 与向量集合清单 → PG `REPEATABLE READ READ ONLY` 流式导出到暂存 SQLite → pin 所有引用版本 → 验证清单不再变化并解除写 gate → 复制不可变对象和向量 → 校验并封存 bundle → 释放 pins。长事务需监控 PG vacuum/WAL 压力。首版如果清单尚未覆盖某类可变文件，保持停写直到其复制结束，不声称在线一致性。

后续在线备份以全部可变发布纳入 revision/manifest 为前提。PG 物理备份/WAL、对象版本保护和 Chroma 运维备份是集群灾备的另一层；业务 bundle 不替代 PITR。定期在空白目标恢复，查元数据引用、向量身份/计数/抽样值、媒体 hash 与 Studio 版本。

迁移 CLI（均待实现）分 preflight/export/load/verify/activate；manifest 记录 migration_id、源 library_id/schema/tokenizer、行数与确定性摘要、对象版本、collection 身份和断点。初次只支持装载空目标或同 migration_id 续跑，禁止对任意已有库 COPY 后“冲突跳过”。续跑冲突要比较内容，异值即失败；装载后修复 identity/sequence 到 max(id)，校验约束/孤儿/NULL/排序字段/BLOB/JSON 原文，不只抽样计数。服务端向量按原 ID/embedding/metadata 原样导入并验证，不重嵌入；物理 ANN 图可能变化，召回结果按 ANN 容差验收。

正式切换 runbook：

1. 在隔离环境用真实备份完成一次全流程演练；测得导出/上传/验证吞吐，估算停写窗口、暂存盘与恢复时间。先试非关键小库，数十 TB 必须预复制不可变媒体/ready 向量，再停写做最终差量和校验；首期不做 CDC 或双写。
2. 归档源配置（脱敏）、代码版本、local 业务备份和单独 agent 灾备；记录源数据只读边界。暂停调度、worker、用户编辑与所有 CLI 写入；旧实例仍可访问时必须只读。
3. 完成最终 SQLite 一致性快照、PG 装载、对象差量、向量导入和全量清单验证。中断只按 migration_id 续跑，不手工删日志绕过检查。
4. 以只读目标配置启动一个 API，检查 library_id/schema、发布身份、素材、检索与 Studio 历史产物；通过后启动 1 worker 进行受控新任务冒烟。此受控写入也视为越过回退边界。
5. 记录 cutover_id/时间并仅在目标开放写入，再增加 worker/API。确认旧端被停止或强制只读，避免双主。观察错误率、池、租约和资源预算。
6. 未在目标写入时可停止目标并切回源；一旦目标产生写入，必须停写后从目标导出回本地/恢复到新目录，验证对象、向量、Studio 与 ledger 后再切流。禁止直接改回 SQLite/local/file/inline。

初始恢复目标：已完成业务写入迁移 RPO=0（靠停写校验，不是后台复制承诺）；日常灾备 RPO≤24h、RTO≤8h 为试点目标，必须以容量演练记录通过后才可承诺。队列恢复只恢复账本可重试任务，旧 running attempt 不直接认定成功。

## 11. 分阶段工单与准入条件

阶段按可独立审查的提交拆分；每阶段先补语义测试，再接入新后端。计划中出现的新文件/命令均为交付物，不是已存在能力。

```text
0 盘点/基线/本地抽象
  ├─ 1 PG 单机过渡 ─┬─ 2 检索与服务端向量
  │                 └─ 3 租约/队列/worker 基础
  └─ 4A MediaStore 本地/staging 原型
1 + 3 + 4A → 4B S3/共享检查点/Studio 条件发布/GC
2 + 3 + 4B → 5 集群装配/备份迁移/双 API 准入
5 → 6 桌面远程 API 与可选 agent 集群化
```

阶段 4A 可并行开发；在分布式发布/GC 就绪前，S3 只用于单 worker 验证，不能发布为多机支持。新节点随时加入必须受 schema、身份、能力和资源预算检查。

| 阶段 | 具体交付与关键路径 | 验收闸门 |
| --- | --- | --- |
| 0 | 完成 inventory；记录本地基线；BackendContext、DB/Media/Lock/Vector/Queue 本地协议；构造入口迁移；SQL 特征测试；更新 package 清单 | 现有相关与完整测试通过；无行为变化；新抽象不触发网络；依赖/备份/桌面路径未退化 |
| 1 | PG DDL/adapter/异常/事务/连接池；metadata 迁移器；Store 注入、直连与 rowid 全面处理；维护能力未支持时显式拒绝 | 真实 PG16 的 schema/JSON/BLOB/排序/约束/并发语义；SQLite→PG→SQLite 往返校验；仅 pg-single，不许多实例 |
| 2 | projection/精确倒排/过滤/SQL BM25；按候选读快照；Chroma server 与 vectors upload；失效/warmup 改造 | eligible 一致；BM25 分数/排名与端到端评测通过；1%/0.1% 过滤不越界；无全快照/全 map 常驻；性能闸门通过 |
| 3 | PgLockProvider、queue/ledger 原子入队、worker CLI、取消/重试/心跳/fencing；scheduler leader 与 GPU 准入 | 多进程互斥/共享/超时；claim/重试幂等；暂停过 TTL 后旧 worker 不得提交；取消与模型未知结果策略；单机本地文件过渡环境验证 |
| 4A/4B | LocalMediaStore/staging → S3 multipart/媒体响应/manifest；全部权威文件出本地；Studio expected_revision 发布；purge/clean/备份适配与 GC pins | 真实 MinIO + ffmpeg 的 source→index→studio；大文件与断点、空间不足、旧 revision 竞争、过期下载、防删；所有 inventory 远端阻塞项关闭 |
| 5 | 集群角色装配、agent-gateway 路由；maintenance gate、完整 bundle 导入/导出；cutover/reverse runbook；doctor/health/metrics、部署文档 | 2 API + 2 worker + PG + MinIO + Chroma + agent-gateway；API/worker 使用不同本地目录，禁止共享 data_dir；跨主机重复全链路与恢复演练；通过后才允许 cluster 多副本 |
| 6 | 桌面远程 API 模式、用户凭据管理与三平台冒烟；agent PG/可选胖客户端另拆工单 | 桌面检索/上传/SSE/创作/断网恢复；默认本地打包不膨胀；agent 多实例需额外会话恢复闸门 |

依赖与回退：0 可按提交 revert；1–4 在隔离/试点数据验证，通过备份恢复或完整逆向装载回退，不盲切生产 backend。阶段 5 前不以任何组合对外宣称集群就绪。各阶段维护“已实现/未支持/已验证”能力表，doctor 在启动前拒绝不完整组合。

## 12. 验证、容量和运维闸门

默认测试仍离线；新增后端通过 `MINDEX_TEST_POSTGRES_DSN`、`MINDEX_TEST_S3_ENDPOINT`、`MINDEX_TEST_CHROMA_SERVER` 门控并在缺失时 skip。提交的集群实现必须在 CI 专项启用真实后端矩阵，不能因全 skip 而验收。compose 固定镜像版本/digest，自动创建隔离库/bucket/collection 并清理，不能读取用户 data/ 或本地密钥。新增 requires_postgres/requires_s3/requires_chroma_server marker；原测试参数化与新后端专属测试并用。

功能测试至少覆盖：空文档/空查询/重复词、中英数字及特殊 token、所有 channel、人物 onscreen/mentioned、父镜头隐藏、组空交集、annotations casefold、短长时长、重复时间区间、全部旧索引格式。format 1 保留兼容与冒烟；容量验收用 format 2，并有显式升级到新版本指引，不在只读旧索引上偷偷重写内容。当前 40 条评测若仍为权威基线则 Hit@5≥85%，实际执行前确认评测集版本；40 条不足以替代上述对照和随机语料特征测试。

故障注入：kill -9、SIGSTOP 超 TTL 再恢复、PG/S3/Chroma 断连、心跳池耗尽、上传/发布前后崩溃、两个 API 同时编辑、重复 complete/claim、备份同时 purge、agent 网关重启与 SSE 重连。核对状态、引用及真实产物，不只检查 HTTP 200。强制跨两台机器验证，单机 compose 无法揭露本地路径依赖。

容量基准先记录硬件/版本/数据生成方法，分 10万与100万 segments、100万与300万 vectors、每库 3 个索引版本；媒体容量与数据库/向量数分开报告。用合法确定性向量验证吞吐时明确不代表模型质量，质量仍用真实模型评测。记录冷/热 p50/p95/p99、CPU/RSS、DB/向量内存与盘、网络、池等待、GC、队列等待和各阶段吞吐。

首轮工程目标（待阶段 0 基线确认、上线前不得临时放宽）：

- 20 并发客户端、固定 2 API 资源预算下，热检索服务端 p95≤2s、p99≤5s（不含外部嵌入耗时，另报端到端）；1%/0.1% eligible 过滤单独达标。冷启动时间单独记录。
- 同一 API 资源下扩大到百万 segments，RSS 不随全量 snapshot 线性复制；每 API 目标≤2 GiB，记录候选/缓存上限。高频词尾延迟超标则阶段 2 不通过，执行搜索后端优化工单。
- 无 GPU 瓶颈的独立 CPU source 任务，2 worker 相对 1 worker 吞吐≥1.5倍；GPU 任务按实际 capacity 排队，不要求线性加速；不许因旧 pipeline 全程独占而退化成串行。
- 恢复测试后无重复可见版本、无旧 token 完成、无悬空媒体引用；重派延迟≤TTL+回收周期+有界排队时间（无积压测试场景）。上线前按实测确认恢复目标和磁盘余量。

监控至少提供：API 就绪/存活分离；PG 连接/事务/池等待；pending 最老任务/重试/failed；租约到期/拒绝旧 token；GPU slot 与真实在途；S3 错误/孤立上传；staging 预约/水位/命中；检索候选数和耗时；索引版本与 GC pin；备份最近成功/恢复演练。共享依赖故障时 readiness 失败、API 返回明确未就绪，不假数据降级；词法降级沿用现有明确告警策略。

## 13. 文档与完成定义

实现时同步 AGENT.md、CLAUDE.md、docs/architecture.md、config-reference.md、backup.md、web-auth.md、deployment.md、desktop.md 和 config.example.yaml；新增 docs/scaling.md 与部署/故障恢复 runbook；plans/roadmap.md 对齐依赖和实际交付状态。涉及 UI 则修改 messages.json 后生成资源；涉及新包/可选依赖则核对 wheel 与 sidecar 打包。

本方案完成设计评审后可以从阶段 0 执行；完成开发的定义是阶段 5 的双机链路、故障注入、容量与恢复演练均有可复现记录。阶段 6 单独验收，不阻塞服务端首期交付。未经这些验收，不把“有 PostgreSQL 连接和两个 worker”视为水平扩展完成。
