# 水平扩展部署指南（阶段状态与 runbook）

本页描述 sqlite+本地 chroma+单机文件系统缺省形态之外的可配置后端与部署形态。设计依据与
逐阶段交付状态见 [水平扩展方案](../plans/horizontal-scaling-plan.md) 与
[盘点文档](../plans/horizontal-scaling-inventory.md)。**缺省 local 形态零依赖、行为不变。**

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
3. `mindex doctor` —— 组合校验与逐后端连通检查（退出码 1 = 需修复；不执行 DDL、只读）。
4. 以 `database.backend: postgres` 启动 `mindex serve`。可选 `vector.backend: chroma_server`
   （独立 Chroma 服务，镜像 digest 与 HNSW 参数由部署固定）、`storage.backend: s3`
   （媒体出口与检查点迁移随集群装配交付，当前单节点验证）。
5. 队列形态（`queue.backend: postgres`）：后台任务入队，`mindex worker --queues source,dialogue --concurrency 1`
   消费；补偿扫描在启动时补入「账本已落、入队前崩溃」的任务。
6. 租约锁（`locks.backend: postgres`）：fencing token 单调不复用，TTL 90s/心跳 20s；
   失去租约的执行不得提交（DB 级校验）。选题调度器在该形态以 leader 租约单实例触发。

## 已知边界（诚实清单）

- 检索：文字检索的 eligible/BM25 已下推 SQL（派生词法索引，`mindex indexes lexical` 回填）；
  persons 证据路径的数值 json 查询尚未 PG 化（会在该形态明确报错）。
- 媒体：`storage.backend=s3` 为单节点验证形态；媒体 HTTP 出口的 presign/代理回退、
  uploads/检查点/Studio 工作文件远端化与 GC 随集群装配交付——在此之前**不发布为多机支持**。
- 备份：业务 bundle 的 PG 一致性导出/导入随集群装配交付；该形态下备份/切片迁移/镜头修复
  显式拒绝（`UnsupportedOperation`）。
- 队列：账本先落、入队后补（崩溃窗口由补偿扫描兜底）；外部付费调用的未知结果一律
  人工核对，不自动重试。

## 集群 runbook（装配中；正式版随双机验收记录补全）

1. **准入**：`mindex doctor` 全绿；所有节点同一 schema 版本、同一 library_id；
   API/worker 使用不同本地缓存目录，禁止共享 data_dir。
2. **agent**：单实例 `server.role: agent-gateway`（agent.sqlite3/附件留在网关持久卷）；
   其余 API 的 `/api/agent` 指向网关。内部回环令牌不跨集群共享。
3. **维护**：破坏性维护（clean/purge/备份）经 maintenance gate 串行（装配中）。
4. **cutover**：迁移 CLI（preflight/export/load/verify/activate）与 runbook 随
   PG 一致性导出交付；在此之前 local↔cluster 只承诺同版本程序间的备份导入导出。
5. **回退**：写入新后端前可直接切回旧配置；一旦在目标产生写入，必须停写后按
   备份恢复或逆向装载，禁止直接改回 sqlite/local/file/inline。
