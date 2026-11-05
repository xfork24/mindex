# 持久创作、目录发布与上传会话

本页说明本批实现的四项核心能力。整体交付边界和未闭合项仍见
[交付矩阵](../plans/horizontal-scaling-delivery-matrix.md)，测试记录见
[验证记录](scaling-enhancement-verification.md)。

## 部署和升级

本批将元数据 schema 从 19 追加到 21；历史 PostgreSQL 基线和 v16–19 DDL 不重写。
先停写、备份并运行 `mindex --config config.local.yaml migrate`，再启动同版本 API 和 worker。
运行进程只检查 schema。不要混用旧的逐文件 Studio 写入程序与新的目录发布程序。

本地 SQLite / inline 工作流继续使用本机文件与执行线程。Studio 持久执行要求
`database.backend=postgres`、`queue.backend=postgres` 和 `storage.backend=s3`；
正式多副本部署还必须满足完整 cluster 配置。缺少共享媒体时明确拒绝创作持久提交。

## Studio 执行和目录事务

步骤、节点、整列和整体构建写入 `studio_tasks`，经统一 worker 的 `studio` 队列执行。
API 提交时把构建记录、目录当前指针、任务账本和入队放在同一事务中。
任务配置保存模型参数和凭据引用；worker 使用自己的部署后端、目录及预算。
凭据引用丢失、归属错误或模型地址变化时拒绝恢复，不把新凭据发送给历史模型地址。

同一个 job 串行协调；素材处理不长期占用全库 Studio 独占锁。当前 attempt 显式持有目录
状态及发布租约，成功时在同一事务提交 manifest、结果、队列及账本。取消与失败收尾也在
终态事务更新可见构建状态。租约过期的处理器不能发布产物；重派可以重放已保存的外部回执。

`directory_manifests` 保存不可变的完整文件清单，`directory_heads` 保存当前 revision。
文件对象先按完整 SHA256 上传；最后用 expected revision 切换整个目录。
工作区只发布显式引用的版本、产物与参考文稿，禁止递归上传节点旧缓存。
`shared_files` 在同一事务维护兼容投影，供旧的逻辑视图、备份和本地恢复使用。
目录读操作绑定同一个 snapshot；已有 manifest 中缺失的文件不会回退到节点旧文件。

产物列表、下载、归档和执行日志可以从没有 Studio 缓存的 API 节点读取。
日志写入 PG 的追加事件表，携带 attempt 身份；读取按事件游标分页。
历史 manifest 引用参与备份和 GC 引用扫描。当前保守保留全部目录历史；这不代表已经实现
完整的历史保留策略或统一远端 pin/GC 协议。

## 人工修改与外部调用核对

共享 Studio 人工修改需要 `If-Match`。读取 job 或 workspace 响应的
`X-Studio-Revision`，原样放入修改请求的 `If-Match`：缺失返回 428，修订冲突返回 409。
一次人工修改中的文件变化统一发布。已有执行入口仍校验方案 revision、输入版本或预检 token。
浏览器以及 CLI / MCP 共用客户端传递目录修订；409 不自动重试修改。

持久执行中的模型生成及第三方工具提交先登记 `external_calls`，再调用外部系统。
调用 ID 绑定逻辑任务、节点、参数和相同调用的次序，不随 worker attempt 改变。
每次实际 dispatch 另外绑定独立回执写入凭证；人工核对后重试时，旧连接迟到的成功或
错误响应都不能改变当前 dispatch 的状态或回执。
异步提交的 request_id 与同步结果独立持久化；二进制回执放入不可变对象。
已完成回执可以重放，已知 request_id 可以继续查询。没有提供商幂等协议时，不能推断
超时表示提交失败；未知结果进入 `needs_review`，禁止自动再次提交。

在构建历史中选择“核对外部调用”，先在提供商处核实，再填写依据：

- 已知异步 submit 的任务编号：保存 request_id，恢复后继续查询原任务。
- 明确未提交，或明确失败且无可恢复产物：保存核对依据后允许重新执行。
- 尚未核实：保留待核对状态。

核对后用“恢复原构建”重试原账本，复用其调用身份。不能把一个未知调用删除后盲目重试。
相同操作也可经以下接口完成：

| 操作 | 接口 |
| --- | --- |
| 读取执行账本 | `GET /api/studio/jobs/{job}/tasks/{task}` |
| 查看外部回执状态 | `GET /api/studio/jobs/{job}/external-calls` |
| 保存核对结果 | `POST /api/studio/jobs/{job}/external-calls/{call}/resolve` |
| 恢复原账本 | `POST /api/studio/jobs/{job}/tasks/{task}/retry` |

resolve 请求包含 `expected_state`、`outcome`、`evidence`，以及需要时的 `request_id`。
outcome 为 `request_id`、`not_submitted` 或 `failed`。仍有未知回执时拒绝 retry。

## 上传会话

浏览器素材上传使用同一持久会话协议；旧的一次性素材上传和 agent 附件入口复用服务端状态机。
分片仅写入 `pending/uploads/{session}/...`；完成前不会登记为正式上传素材。
每次创建返回独立的 `upload_token`，后续请求通过 `X-Upload-Token` 传递，不放在 URL 中。

| 操作 | 接口 |
| --- | --- |
| 声明 filename / size / sha256 | `POST /api/uploads` |
| 查询已登记分片 | `GET /api/uploads/{id}` |
| 上传分片（编号从 1 开始） | `PUT /api/uploads/{id}/parts/{number}` |
| 校验并完成 | `POST /api/uploads/{id}/complete` |
| 取消未完成上传 | `DELETE /api/uploads/{id}` |
| 读取正式素材，支持 HEAD / Range / ETag | `GET /api/uploads/{id}/content` |
| 验证视频并登记到素材库 | `POST /api/admin/sources/import-upload/{asset_id}` |

这是应用层可恢复 multipart 协议，客户端通过 API 发送分片；S3 后端写大对象时另使用
其原生 multipart。它不等同于已经交付浏览器直连 S3 的预签名 multipart / CORS 部署。

服务端复算每个分片及完整文件的 SHA256，核对大小和归属。相同分片、重复完成均幂等；
不同内容占用同一分片编号会被拒绝。complete 最后原子登记正式素材和会话终态。
上传和完成中断留下的不可见对象可以回收；不能用客户端声明或 ETag 代替可信摘要。
浏览器增量计算摘要，保持有界内存；刷新页面后重新选择同一文件可继续已登记分片。

配置字段是节点配置，修改后重启：

```yaml
uploads:
  max_sessions: 256
  pending_bytes: 107374182400
  part_bytes: 8388608
  ttl_seconds: 86400
```

总分片数最多 10000。输入分片和 S3 合并暂存纳入跨进程 staging 预算。
API 生命周期每分钟回收过期 pending 会话；也可以显式运行 `mindex uploads reap`。
CLI 的 `uploads create/part/show/complete/abort/reap` 直接复用同一状态机，凭据从
`MINDEX_UPLOAD_TOKEN` 或 `--token-env` 指定的环境变量读取。

## 已验证范围与限制

隔离 PG / MinIO / Chroma 测试检查跨节点执行、取消、过期写入拒绝、外部回执重放、
人工核对后恢复、目录 CAS、空缓存产物读取、分片恢复及 bundle 恢复。
工程模型明确使用确定性适配器；这些结果不代表真实模型质量或真实提供商限流已验收。

完整检索容量闸门、全部权威文件闭包、统一远端 pin/GC、迁移逐对象续跑、双机故障、
灾备 RPO/RTO 以及各平台安装流程仍按交付矩阵分别记录，不能由本页的功能测试替代。
