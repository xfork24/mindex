# 水平扩展部署指南（阶段状态与 runbook）

本页描述 sqlite+本地 chroma+单机文件系统缺省形态之外的可配置后端与部署形态。设计依据与
逐阶段交付状态见 [水平扩展方案](../plans/horizontal-scaling-plan.md) 与
[盘点文档](../plans/horizontal-scaling-inventory.md)。后续交付以
[增强方案](../plans/horizontal-scaling-enhancement-plan.md) 与
[当前验证记录](scaling-enhancement-verification.md) 为准。**集群尚未完成正式验收。**

## 运行形态与后端组合

| 形态 | database | vector | storage | queue | locks | 进程 | 状态 |
|---|---|---|---|---|---|---|---|
| local（缺省） | sqlite | chroma_local | local | inline | file | 单 API/CLI/桌面 | 稳定 |
| pg-single | postgres | chroma_local \| chroma_server | local \| s3（单节点验证） | inline \| postgres | file \| postgres | 单 API + 可选 worker | 可用（过渡测试） |
| cluster | postgres | chroma_server | s3 | postgres | postgres | 多 API + 多 worker + agent-gateway | 装配中（本页下半部） |

配置见 [config-reference.md](config-reference.md) 的 `database`/`vector`/`storage`/`queue`/`locks` 节。
凭据只从环境变量读取（`MINDEX_DATABASE_DSN`、`MINDEX_CHROMA_TOKEN`、`MINDEX_S3_ACCESS_KEY/SECRET_KEY`）。

## 部署步骤（pg-single 起步）

1. 准备 PostgreSQL 16（校验证书的 TLS；本地开发可 `docker run postgres:16-alpine`）。DSN 放入环境变量。
2. `mindex migrate` —— 唯一 DDL 入口（API/worker 启动只校验版本，不做迁移）。
   PG v15 为冻结基线，后续 v16–18 按序追加；已有库升级不会重跑基线 DDL。
3. `mindex doctor` —— 组合校验与逐后端连通检查（退出码 1 = 需修复；不执行 DDL、只读）。
4. 以 `database.backend: postgres` 启动 `mindex serve`。可选 `vector.backend: chroma_server`
   （独立 Chroma 服务，镜像 digest 与 HNSW 参数由部署固定）、`storage.backend: s3`
   （媒体出口与检查点迁移随集群装配交付，当前单节点验证）。
5. 队列形态（`queue.backend: postgres`）：后台任务入队，`mindex worker --queues source,dialogue --concurrency 1`
   消费；四类现有账本的创建/重试与入队在应用层同事务，补偿扫描只修复历史滞留。
6. 租约锁（`locks.backend: postgres`）：fencing token 单调不复用，TTL 90s/心跳 20s；
   失去租约的执行不得提交（DB 级校验）。选题调度器在该形态以 leader 租约单实例触发。

## 已知边界（诚实清单）

- `server.form: cluster` 显式要求 PG、S3、Chroma 服务、PG 队列及 PG 锁全部配置，缺失时拒绝启动。
  可设置 `server.library_id` 校验期望库。共享集合名使用 `lib-<库UUID无连字符>-idx-<索引ID>`，
  S3 实际前缀为 `<storage.s3.prefix>/libraries/<库UUID无连字符>/`，下载缓存也按库隔离。
  普通业务不会读取旧版未隔离集合/前缀；旧库迁移须显式使用 `migration export --legacy-namespace`
  导出完整 bundle，再装载到新的空目标。步骤见[备份与迁移](backup.md#旧共享命名空间升级)。
  查询/控制池分离，按 `database.postgres.connection_budget` 校验当前进程总容量。
- 检索：文字检索的 eligible/BM25 已下推 SQL（派生词法索引，`mindex indexes lexical` 回填）；
  人物相关 SQL 已做双方言适配，人物全快照与集群预热的容量问题仍未关闭。
- 媒体：`storage.backend=s3` 已覆盖媒体出口（预签名 + Range 代理 + 下载 pin）、上传
  直写不可变对象、检查点写穿透与跨节点回读、S3 孤对象回收（引用闭包 + 宽限 + pin）；
  Studio 工作文件的 expected_revision 条件发布已交付（If-Match → 409）。仍为单节点
  验证形态：媒体出口的 CORS/独立 endpoint、Studio attempt 产物远端化与完整 GC pin
  生命周期未完成，暂不发布为多机支持。
- 备份：业务 bundle 的 PG 一致性导出/导入已交付（`mindex migration export|load|verify`，
  含 `--with-media`/`--with-vectors`）；切片迁移/镜头修复等 SQLite 专属维护仍显式拒绝。
- 队列：source/dialogue/person/topic 创建、重试、领取、回收、取消与终态同步；
  事务及处理子线程执行 fencing。Studio/index 持久调度、稳定周期去重键、外部调用的
  幂等登记与未知结果人工核对状态仍未实现，不据组件测试声明完整执行链完成。
- staging：同一节点跨进程共享预约/pin，活动进程持 OS owner 锁；崩溃后回收，不按 TTL
  淘汰暂停进程的文件。业务输出/临时空间的全部预约调用点仍需覆盖。
- 迁移：`preflight/export/load/verify/activate` 可用。本地与 PG 的元数据和恢复日志同事务；
  续跑先拒绝异值目标再复制媒体，缺少必要媒体/向量的元数据 bundle 不允许激活。
  分对象断点、完整媒体闭包、切换/逆向迁移与 RPO/RTO 实测仍未完成。
- 智能助手：cluster 业务 API 的变更上报写入共享 outbox（POST /api/agent/changes），
  由单实例网关幂等转入 SSE；会话/SSE 仍需网关，网关故障时助手明确不可用。
- persons：审核/发布路径的 JSON 查询已完成 SQLite/PostgreSQL 双方言适配（含双后端
  一致性测试）；`json_each/json_group_array` 等不再在 PG 上失败。

## 集群 runbook（装配中；正式版随双机验收记录补全）

1. **准入**：`mindex doctor` 全绿；所有节点同一 schema 版本、同一 library_id；
   API/worker 使用不同本地缓存目录，禁止共享 data_dir。
2. **agent**：单实例 `server.role: agent-gateway`（agent.sqlite3/附件留在网关持久卷）；
   其余 API 的 `/api/agent` 指向网关。内部回环令牌不跨集群共享。
3. **维护**：破坏性维护（clean/purge/备份）经 maintenance gate 串行（装配中）。
4. **cutover**：`mindex migration export --target BUNDLE --with-media --with-vectors`
   → `migration preflight --source BUNDLE` → `migration load --source BUNDLE --with-vectors`
   → `migration verify --source BUNDLE` → `migration activate --source BUNDLE`。
   使用空目标并先停写；当前入口仍需完整闭包与故障实测，不应直接用于正式切换。
   local↔cluster 只承诺同版本程序间的导入导出。
5. **回退**：写入新后端前可直接切回旧配置；一旦在目标产生写入，必须停写后按
   备份恢复或逆向装载，禁止直接改回 sqlite/local/file/inline。
