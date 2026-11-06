# 水平扩展部署指南（阶段状态与 runbook）

本页描述 sqlite+本地 chroma+单机文件系统缺省形态之外的可配置后端与部署形态。设计依据与
逐阶段交付状态见 [水平扩展方案](../plans/horizontal-scaling-plan.md) 与
[盘点文档](../plans/horizontal-scaling-inventory.md)。剩余能力的实现与单机验收以
[剩余能力闭合计划](../plans/horizontal-scaling-completion-plan.md) 与
[实施验证记录](scaling-completion-verification.md) 为准。**双机、规定大容量、真实模型与
跨平台安装实测仍为独立验收状态；未完成前不应把这些门槛当作已通过。**

## 运行形态与后端组合

| 形态 | database | vector | storage | queue | locks | 进程 | 状态 |
|---|---|---|---|---|---|---|---|
| local（缺省） | sqlite | chroma_local | local | inline | file | 单 API/CLI/桌面 | 稳定 |
| pg-single | postgres | chroma_local \| chroma_server | local \| s3（单节点验证） | inline \| postgres | file \| postgres | 单 API + 可选 worker | 可用（过渡测试） |
| cluster | postgres | chroma_server | s3 | postgres | postgres | 多 API + 多 worker + agent-gateway | 单机验收完成（本页下半部）；双机独立验收 |

配置见 [config-reference.md](config-reference.md) 的 `database`/`vector`/`storage`/`queue`/`locks` 节。
凭据只从环境变量读取（`MINDEX_DATABASE_DSN`、`MINDEX_CHROMA_TOKEN`、`MINDEX_S3_ACCESS_KEY/SECRET_KEY`）。

## 部署步骤（pg-single 起步）

1. 准备 PostgreSQL 16（校验证书的 TLS；本地开发可 `docker run postgres:16-alpine`）。DSN 放入环境变量。
2. `mindex migrate` —— 唯一 DDL 入口（API/worker 启动只校验版本，不做迁移）。
   PG v15 为冻结基线，后续 v16–25 按序追加（已发布 DDL 不改写）；已有库升级不会重跑基线 DDL。
3. `mindex doctor` —— 组合校验与逐后端连通检查（退出码 1 = 需修复；不执行 DDL、只读）。
4. 以 `database.backend: postgres` 启动 `mindex serve`。可选 `vector.backend: chroma_server`
   （独立 Chroma 服务，镜像 digest 与 HNSW 参数由部署固定）、`storage.backend: s3`
   （媒体出口、检查点与双路径上传均已接入，见下文与[双路径上传](scaling-upload.md)）。
5. 队列形态（`queue.backend: postgres`）：后台任务入队，`mindex worker --queues source,dialogue --concurrency 1`
   消费；source/dialogue/person/topic/index/studio 六类账本的创建/重试与入队在应用层同事务，补偿扫描只修复历史滞留。
   租约缺省 120 秒、心跳 20 秒；`--task-ttl/--renew-interval`（故障验收演练用）可缩短租约观察回收与 fencing。
6. 租约锁（`locks.backend: postgres`）：fencing token 单调不复用，TTL 90s/心跳 20s；
   失去租约的执行不得提交（DB 级校验）。选题调度器在该形态以 leader 租约单实例触发。

升级 v19 时先停止旧版 API/worker 与调度器，再执行 `mindex migrate`，随后用同一版本启动所有节点。
当前不支持新旧版本节点滚动混跑；缺少系统 IANA 数据的环境由基础依赖 `tzdata` 提供命名时区数据。

## 已知边界（诚实清单）

- `server.form: cluster` 显式要求 PG、S3、Chroma 服务、PG 队列及 PG 锁全部配置，缺失时拒绝启动。
  可设置 `server.library_id` 校验期望库。共享集合名使用 `lib-<库UUID无连字符>-idx-<索引ID>`，
  S3 实际前缀为 `<storage.s3.prefix>/libraries/<库UUID无连字符>/`，下载缓存也按库隔离。
  普通业务不会读取旧版未隔离集合/前缀；旧库迁移须显式使用 `migration export --legacy-namespace`
  导出完整 bundle，再装载到新的空目标。步骤见[备份与迁移](backup.md#旧共享命名空间升级)。
  查询/控制池分离，按 `database.postgres.connection_budget` 校验当前进程总容量。
- 检索：文字检索的 eligible/BM25 已下推 SQL（派生词法索引，`mindex indexes lexical` 回填）；
  人物条件复用同一 eligible 分页关系；条件可下推 Chroma 时直接下推，窄范围分页读取后合并全局排序。
  查询默认 5 秒总期限（池等待/SQL/向量/水合），查询向量缓存默认 64 MiB；集群禁止全快照回退。
  规定规模（10万/100万 segments、100万/300万 vectors、20 并发）的容量与质量闸门保持独立实测，
  工装见 [容量验收](scaling-capacity.md)。
- 媒体：`storage.backend=s3` 覆盖媒体出口（预签名 + Range 代理 + 下载 pin）、双路径上传
  （API 分片中转与浏览器直连 S3 预签名 multipart，见[双路径上传](scaling-upload.md)）、
  统一权威资源登记与 GC 协调删除（删除前重读引用与 pins、条件删除不可靠时停止自动 GC）。
  Studio 工作文件的目录修订条件发布（If-Match → 409/428）与跨 API 真实 CAS 已验证。
- 备份：业务 bundle 的 PG 一致性导出/导入已交付（`mindex migration export|load|verify`，
  含 `--with-media`/`--with-vectors`）；切片迁移/镜头修复等 SQLite 专属维护仍显式拒绝。
- 队列：source/dialogue/person/topic/index/studio 创建、重试、领取、回收、取消与终态同步；
  事务及处理子线程执行 fencing。索引构建、词法回填、人物发布与检索组合激活复用
  index 账本；发布指针在队列/账本终态事务中切换，发布锁保持到提交结束。
  Studio 持久执行链、外部调用幂等登记与未知结果人工核对已交付；提供商真实幂等/限流独立实测。
- staging：同一节点跨进程共享预约/pin，活动进程持 OS owner 锁；崩溃后回收，不按 TTL
  淘汰暂停进程的文件。输出与临时空间写前预约，未知大小输出按块增长且不突破预算。
- 迁移：`preflight/export/load/verify/activate` 加九阶段持久账本（预检→身份→媒体→向量→
  元数据→任务恢复→验证→激活）。首次写入前绑定源清单摘要与库 UUID；已有目标内容先核对
  再补账，异值拒绝覆盖；未激活目标拒绝业务写入与任务领取。local→cluster→local 往返一致。
  代表性规模 RPO≤24h/RTO≤8h 保持独立实测，演练脚本见 [迁移与恢复](scaling-migration.md)。
- 智能助手：cluster 业务 API 的变更上报写入共享 outbox（POST /api/agent/changes），
  由单实例网关幂等转入 SSE。网关先在本地事务持久化事件、回执与游标再确认；崩溃重放
  保留同一可见事件身份（Last-Event-ID 续传不重放）。网关故障时助手明确不可用（503），
  业务 API 与检索不受影响。
- persons：审核/发布路径的 JSON 查询已完成 SQLite/PostgreSQL 双方言适配（含双后端
  一致性测试）；`json_each/json_group_array` 等不再在 PG 上失败。

## 索引任务与调度时区

`queue.backend: postgres` 时，`mindex index`、`indexes lexical/activate`、`persons publish/activate`
先持久入队，再等待 worker 终态；必须启动消费 `index` 队列的 worker。调用端等待期间不持有处理锁。
`POST /api/index-tasks` 接受 `build`、`lexical`、`activate`、`person-publish`、`activate-release`，
返回 202 与任务 ID；`GET /api/index-tasks` 及 `GET /api/index-tasks/{id}` 查询进度和结果，
`POST /api/index-tasks/{id}/cancel` 或 `/retry` 操作任务。
原同步激活和人物发布接口保留原入口，经同一账本执行。
CLI 使用 `mindex indexes tasks list/show/retry/cancel`；助手的任务查询、重试、取消使用 `kind=index`。

`topics.scheduler.timezone` 使用 IANA 名称，例如 `Asia/Shanghai`。本地缺省沿用系统时区；
PG 缺省为 UTC，首次初始化调度器时绑定到共享 settings。后续节点显式时区与库不一致时拒绝启动调度器；
省略配置则继承库时区。leader 租约 `scheduler:topics` 与选题工作锁分离。
周期键由档案、库时区中的日期及当天尝试序号确定；重复提交返回已有任务，失败后最多三个自动尝试。
时区属于部署配置，修改需重启；已有库的时区变更应先停调度并核对当日周期，不能仅修改一个节点。

## 集群 runbook（正式版随双机验收记录补全）

1. **准入**：`mindex doctor` 全绿；所有节点同一 schema 版本、同一 library_id；
   API/worker 使用不同本地缓存目录，禁止共享 data_dir。
2. **健康**：`/api/live` 仅表示进程存活；`/api/ready` 在有界期限内逐项探测数据库
   （查询/控制通道）、存储 bucket、向量服务、迁移状态、运行准入与网关，任一失败返回
   503 并列出具体检查项。依赖断连与恢复的演练经 `scripts/verify-scaling.py --suite faults` 执行。
3. **agent**：单实例 `server.role: agent-gateway`（agent.sqlite3/附件留在网关持久卷）；
   其余 API 的 `/api/agent` 指向网关，共享 `MINDEX_WEB_TOKEN`。内部回环令牌不跨集群共享。
   网关重启后按持久回执重放 outbox，SSE 客户端按 Last-Event-ID 续传。
4. **部署配置**：设置页「部署配置」读取/校验/保存当前节点的配置文件，返回修订与
   字段边界；凭据只接受环境变量引用，保存需修订校验，身份/后端/连接池/资源组/存储
   变更需重启，库切换必须走迁移流程。无可写配置文件的部署返回只读状态。
5. **维护**：破坏性维护（clean/purge/备份）经 maintenance gate 串行。
6. **cutover**：`mindex migration export --target BUNDLE --with-media --with-vectors`
   → `migration preflight --source BUNDLE` → `migration load --source BUNDLE --with-vectors`
   → `migration verify --source BUNDLE` → `migration activate --source BUNDLE`。
   使用空目标并先停写；各阶段可中断续跑（先核对已有内容再补账）。
   local↔cluster 只承诺同版本程序间的导入导出。
7. **回退**：写入新后端前可直接切回旧配置；一旦在目标产生写入，必须停写后按
   备份恢复或逆向装载，禁止直接改回 sqlite/local/file/inline。
