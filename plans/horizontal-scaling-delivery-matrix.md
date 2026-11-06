# 水平扩展需求—调用点—证据矩阵

更新：2026-11-06（北京时间）。需求以[增强交付方案](horizontal-scaling-enhancement-plan.md)为准。
当前结果和原始产物见[实施验证记录](../docs/scaling-enhancement-verification.md)。

状态为三个独立维度：实现 / 接入 / 验证。`部分` 表示仍有必交付调用点未闭合；
`待实测` 表示没有执行证据，不能据脚本或组件测试标记完成。
此表保留未完成需求，不表示缩小交付范围。

后续执行以[剩余能力闭合计划](horizontal-scaling-completion-plan.md)为准；基线 `e54b645`。
下表中的既有验证不等于本轮复验。全部剩余调用点按以下批次推进，状态只有在对应证据落盘后更新：

| 批次 | 矩阵条目（含仍需补充验收的条目） | 本轮证据 |
|---|---|---|
| 一：上下文、准入、任务与 GPU | A04、A05、A06、B01、B02、B03、B08 | `tests/test_backend_lifecycle.py`；`artifacts/scaling-completion-batch1/`（含失败及重跑记录；尚非整批完成） |
| 二：资源闭包、staging、pins 与 GC | C01、C02、C04、C05、C06、C07 | `resource_registry.py`、`object_coordination.py`、`work_budget.py`；`test_object_coordination.py`；`artifacts/scaling-completion-batch2/coordination-fifth/`（232 通过、零跳过）与 `media-directed.xml`（102 通过）；保留失败重跑，统一故障矩阵待执行 |
| 三：上传与媒体访问 | D01、D02、D03 | `artifacts/scaling-completion-batch3/uploads-fifth/`：239 通过、零跳过；真实 Chromium 独立 endpoint/CORS、断流续传与 75 秒视频 seek |
| 四：查询预算与容量 | E01、E02、E03、E04、E05 | `artifacts/scaling-completion-batch4/query-seventh/`：254 通过、零跳过；原规模容量/质量仍独立待实测 |
| 五：迁移、激活与灾备 | F01、F02、F03、F04、F05、F06 | `artifacts/scaling-completion-batch5/migration-third/`：271 通过、零跳过；`recovery-second/` 持久卷 8 MiB/WAL 恢复通过，正式 RPO/RTO 待代表性规模实测 |
| 六：网关、设置、桌面与发布 | A07、B04、B05、B06、B07、C03、G01、G02、G03、G04、G05、G06、H01、H02、H03、H04 | `artifacts/scaling-completion-batch6/`：`backends-third/` 285 通过零跳过；`faults/` 断连演练通过；`cpu/` 1.8713 倍；macOS 重建应用与凭据清除演练通过；`local-final/` 冻结回归 |

双机、规定大容量、真实模型、Windows/Linux 安装和实际系统密钥环仍按原门槛独立验收；
脚本交付、小样本和组件验证均不能代替这些实测。

| ID | 需求与主要调用点 | 实现 / 接入 / 验证 | 证据或下一项关闭条件 |
|---|---|---|---|
| A01 | 后端入口与拒绝 SQLite 回落：`shared/backends/context.py`、业务 `Store.open` | 已实现 / 已接入 / 已验证 | `test_scaling_foundation.py`；构造边界静态检查；白名单分支仍须保持运行期拒绝越界 |
| A02 | 显式部署组合、期望库身份：`shared/config.py`、`open_context` | 已实现 / 已接入 / 已验证 | 无效组合在创建节点目录/SQLite 前拒绝；期望 UUID 不匹配拒绝 |
| A03 | PG/S3/Chroma namespace：`shared/backends/s3.py`、`vectors.py`、`processing/indexing.py` | 已实现 / 已接入 / 已验证 | `test_backend_identity.py`：相同资源键跨库隔离；旧 namespace 显式导出；无自动回落 |
| A04 | 全部缓存身份与上下文生命周期 | 部分 / 部分 / 部分 | 上下文/Store 显式关闭、命令级退出、API 生命周期、worker/doctor 和预热线程清理已接入；借出组件保留上下文，PG 共享池持有者计数，staging owner 独立。`test_backend_lifecycle.py` 11 项通过；[本轮记录](../docs/scaling-completion-verification.md)。快照/人物/查询/工作摘要/译文绑定库与后端，Chroma 业务客户端关闭已接入；续作真实后端 223 项通过。依赖探测的总期限在第六批闭合 |
| A05 | 查询/控制池与总连接预算：`shared/db/pg_backend.py` | 已实现 / 已接入 / 已验证 | 查询池耗尽时控制池仍可用；控制连接拒绝普通写入；全节点连接预算需部署核算 |
| A06 | 追加迁移、协议与启动能力检查：`shared/db/migrate_pg.py`、`doctor.py` | 部分 / 部分 / 部分 | v15→21、v18→21 数据/历史校验值通过；追加 v22 与运行协议 2；混合协议拒绝、库身份和派生数据准入已接入；依赖能力有界探测在第六批补齐 |
| A07 | 源码、wheel、冻结产物 | 已实现 / 已接入 / 部分 | 源码包、wheel 与前端资源逐字节核对；本批按当前源码重建 macOS arm64 冻结后端（`check-desktop-sidecar.py` 通过）并重新打包 `.app/.dmg/.zip`，`codesign --verify --deep --strict` 通过。其他平台和混合版本仍待验收 |
| B01 | source/dialogue/person/topic 账本与入队同事务：`shared/queue_dispatch.py` | 已实现 / 已接入 / 已验证 | `test_scaling_correctness.py`、`test_scaling_foundation.py`；触发器已被显式事务替代。范围为账本路径，CLI 前台八类处理入口已接入 source/dialogue 账本，真实 PG+FFmpeg prepare 一致性通过（test_scaling_completion.py） |
| B02 | attempt 绑定、取消、回收与终态原子提交：`queue_pg.py`、`task_progress.py` | 已实现 / 已接入 / 已验证 | TTL 回收、失败上限、跨目录取消、人工重试、账本/队列终态用例 |
| B03 | 显式凭证、线程和提交 fencing：`db/fencing.py`、`pg_backend.py` | 已实现 / 已接入 / 部分 | 显式 commit、处理线程池和过期写入拒绝已测；双机 SIGSTOP/控制失败仍待演练 |
| B04 | Studio 持久执行链：`studio/tasks.py`、`studio_api.py`、`studio_logs.py` | 已实现 / 已接入 / 已验证（单机） | 步骤/节点/整列/构建共用 `studio_tasks`；提交与入队、终态与 manifest 同事务；PG 事件日志、空缓存产物读取、跨节点取消、过期拒绝与回执重放通过。双机故障仍见 G06 |
| B05 | index 持久执行链：`cli.py`、`admin/source_tasks.py::IndexTasks`、`shared/queue_dispatch.py`、`processing/indexing.py` | 已实现 / 已接入 / 已验证 | `test_scaling_execution.py`：真实视频构建、词法回填、人物/组合发布、取消重试、终态事务与指针原子发布；发布锁持续至提交，双 worker 跨目录串行继承；冻结模型恢复与提交锁竞争；本批隔离后端 205 项通过 |
| B06 | 稳定周期去重：`topics/scheduler.py`、`topics/runner.py` | 已实现 / 已接入 / 已验证 | 库时区、周期/账本/入队同事务；leader 切换/重复提交只生成一个周期任务，失败上限三次；跨目录重放及崩溃回滚已测，双机故障仍见 G06 |
| B07 | 持久创作外部副作用幂等与未知结果恢复：`shared/external_calls.py`、模型工厂、工具 submit | 已实现 / 已接入 / 已验证（单机） | 稳定调用 ID、独立回执、request_id 续查、二进制对象与类型恢复；未知结果阻断、核对依据、原账本重试及双语核对入口；提供商真实幂等/限流仍待实测 |
| B08 | GPU 物理资源组与真实模型服务并发 | 部分 / 部分 / 待实测 | 已接入 gpu.groups/cpu_queues、持久占用与附依据核对；PG TTL/回收不释放未知占用已测。真实模型限流与并发仍独立待实测 |
| C01 | 小检查点不可变对象与 PG 指针：`shared/checkpoints.py`、`files.py` | 已实现 / 部分 / 部分 | 跨节点修订读取已测；全部权威文件登记尚未完成 |
| C02 | Studio 不可变目录 manifest + 当前 revision 指针：`shared/directory_files.py` | 已实现 / 已接入 / 已验证（单机） | 显式依赖先上传、目录 CAS 原子切换；读取绑定 snapshot；旧缓存不能回写 job.json；历史清单及产物 bundle 往返通过 |
| C03 | 所有人为修改携带修订：`studio/revisions.py` 与共享 Web/CLI/MCP 客户端 | 已实现 / 已接入 / 已验证（单机） | 共享 Studio 人工写入缺 If-Match 返回 428、冲突 409，单次请求原子发布；客户端传递目录修订且不自动重试 409。故障矩阵在真实 PG/S3 上以两个 API 并发同修订提交验证恰一个 200、另一个 409（真实 CAS，非响应注入），冲突方刷新后可保存且新修订对两 API 可见；浏览器显式刷新与输入保留已覆盖 |
| C04 | 权威文件闭包：字幕/归档、转写/分析、人物、音乐、声音、选题、Studio | 已实现 / 已接入 / 部分 | 统一资源引用登记与分页遍历供备份/GC 共用；补齐人物图像、音乐导入与声音样本发布，目录历史保守保留；本轮真实后端 232 通过，媒体定向 102 通过，完整故障矩阵待执行 |
| C05 | 全使用期媒体 pin：`Store.materialize_media`、attempt 保留、HTTP 响应 | 已实现 / 部分 / 部分 | 人物提取、分析、分镜检测、Studio 预览/下载/归档改用上下文；持久 worker 兼容 Path 入口保留到 attempt 结束。诊断/修复/Studio 兼容 Path 入口纳入工作集物化保留；远端无作用域的裸 Path 读取明确拒绝；HTTP 响应持有独立物化上下文至结束 |
| C06 | 跨进程 staging 预算/活动 pin：`shared/backends/staging.py` | 已实现 / 部分 / 部分 | 多进程 pin/预约/崩溃回收已有验证；新增上传分片输入与 S3 上传/合并暂存预算。JSON、复制/物化、产物下载及 FFmpeg 输出写前预约并逐块增长；本轮真实后端 232 通过，媒体定向 102 通过，完整故障矩阵待执行 |
| C07 | attempt/发布/备份/下载统一远端 pin 与 GC 协调 | 部分 / 部分 / 部分 | v23 统一远端 pin 与持久删除意图；引用/签名/删除共用协调锁，删除前重读引用及 pins；条件删除不可靠且未启用版本化时停止自动 GC；真实 PG 删除/签名/发布竞争通过，完整故障矩阵待执行 |
| D01 | pending/multipart/complete/abort 上传会话：`shared/upload_sessions.py`、`upload_api.py` | 已实现 / 已接入 / 已验证（单机） | PG/SQLite 持久会话、独立凭据、pending 分片、跨 API 续传、完成登记、abort；Web/旧上传/agent 与 CLI 共用状态机。v24 增加 S3 原生 multipart、预签名 Content-Length、真实分片核对与未登记 UploadId 恢复；两路径真实后端复验通过 |
| D02 | 上传摘要、配额、幂等完成、过期回收 | 已实现 / 已接入 / 已验证（单机） | 分片及完整 SHA256 服务端复算；字节/会话/分片数预算；重复完成、错误归属、摘要不符、过期回收；API 生命周期回收及显式 CLI 接入 |
| D03 | 浏览器独立 endpoint、CORS、Content-Type 与访问协议 | 部分 / 部分 / 部分 | 已完成上传素材与 Studio 产物的跨 API 读取、HEAD/Range/ETag 基础用例；独立浏览器 endpoint/CORS、错误签名/大小、过期 URL 和 75 秒视频实际 seek 已通过；原始截图与日志见第三批记录 |
| E01 | 可见性、annotation、素材范围的 eligible 统计快照：`search/lexical_query.py` | 已实现 / 已接入 / 已验证 | SQL BM25 与 Python 参考分数/排序、人物投影五类查询逐项一致 |
| E02 | 人物证据投影与候选水合：`persons/persons.py`、`SearchEngine._hydrate` | 已实现 / 已接入 / 已验证 | PG 按素材/身份取证据；命中只水合所需片段/父镜头/整理信息 |
| E03 | 去掉集群预热全快照和 representation map：`search/warmup.py` | 已实现 / 已接入 / 已验证 | 禁止全快照/人物大缓存/map 的预热用例通过；集群缺派生表/旧分词器拒绝全快照回退，提示回填；本地与过渡模式兼容路径保留 |
| E04 | eligible/候选/缓存字节/请求体/查询时间预算 | 已实现 / 已接入 / 已验证（单机） | SQL 请求级关系、512 ID 分页、候选水合、64 MiB/128 项缓存、5s 总期限及可重试 503；池/SQL/慢分块/人物参考/窄范围回退通过。容量工装与小样本执行不代替原规模 RSS/尾延迟闸门 |
| E05 | 10万/100万 segments、100万/300万 vectors、20并发与质量 | 待实测 / 待实测 / 待实测 | 原门槛 p95≤2s、p99≤5s、每 API RSS≤2GiB、Hit@5≥85% 保持不变；scaling-capacity.py 工装已交付并完成独立双 API 小样本验证，原规模磁盘不足证据见第四批记录 |
| F01 | 统一 CLI 校验与激活记录：`maintenance/migration.py` 的 preflight/export/load/verify/activate | 已实现 / 已接入 / 已验证 | 拒绝非空/异值目标；CLI 拒绝激活缺必要媒体或向量的诊断 bundle |
| F02 | migration_id、源清单身份、元数据/日志原子提交：`db/transfer.py` | 已实现 / 已接入 / 已验证 | 真子进程 `os._exit` 崩溃与提交后失败续跑；消除本地 JSON 日志窗口 |
| F03 | 分阶段/分对象/分集合续跑与恢复账本 | 已实现 / 已接入 / 已验证（单机） | v25 阶段/对象/集合/批次账本；九个阶段 SIGKILL、真实 S3/Chroma 写入未落账续跑不重复写入；旧 attempt 重建/未知执行保留核对。test_migration_stages.py |
| F04 | 媒体/向量/Studio 历史闭包与秘密裁剪 | 已实现 / 已接入 / 已验证（单机） | 统一 required_objects 核对必要媒体闭包；登记配置裁剪并清除旧页，未知秘密载体不改用户内容并阻止激活；Studio 历史/上传/回执往返及字幕/人物等统一登记 |
| F05 | 身份切换、激活 gate、目标写入后的逆向迁移 | 已实现 / 已接入 / 已验证（单机） | 首份目标对象之前绑定源清单和 UUID；PG/SQLite/API/CLI/领取拒绝未激活目标，完整验证后原子开放；已激活新写入禁止旧包重放，local→PG→local 含事件序列/媒体一致 |
| F06 | PG/WAL、对象保护与实测灾备 | 已实现 / 已接入 / 部分 | verify-scaling-recovery.py：独立持久 PG 卷、basebackup 后提交经 WAL 恢复、切断源后从对象保护副本恢复；8 MiB 约 1.901s，逐表/对象摘要一致。代表性规模 RPO≤24h、RTO≤8h 仍独立待实测 |
| G01 | 两 API/两 worker、独立目录的真实素材处理 | 已实现 / 已接入 / 已验证 | `test_cluster_process_flow.py`：四个进程上传/执行/跨节点读媒体、摘要与 worker owner 对账 |
| G02 | 单实例网关 SSE/附件/取消/重连/MCP 完整代理 | 已实现 / 已接入 / 已验证（单机） | 流式代理与循环检测；SSE 事件带稳定 id 且 Last-Event-ID 续传不重放；附件经网关上传/读取摘要一致，中断后仍可完整读取并删除；MCP 初始化/工具列举经网关正常，通知流断开后立即可用；未带或错误令牌在网关与业务 API 均 401；网关回复闭包在取消时关闭上游 |
| G03 | outbox 先落事件后 ack、持久去重与生命周期 | 已实现 / 已接入 / 已验证（单机） | 网关在本地事务持久化事件/回执/游标后才确认；真实进程 SIGKILL 后重启按未确认条目重放，已见事件可见 seq 不变、停机期间改动不丢不重；重放完成后无未确认条目 |
| G04 | 业务 API/远程 stdio MCP 不创建本机 agent 状态 | 已实现 / 已接入 / 已验证 | `test_scaling_execution.py`：远程 CLI 禁止 Store.open/覆盖层读取，未创建节点目录；PG 业务 API 在 SessionStore 构造前拒绝本机会话模式 |
| G05 | live/ready、监控、部署和故障 runbook | 已实现 / 已接入 / 已验证（单机） | `/api/live` 只表示进程存活；`/api/ready` 在总期限内逐项探测数据库查询/控制、存储 bucket、向量服务、迁移状态、运行准入与网关，超限即失败并列出检查项。`--suite faults` 在两 API + 单网关真实进程上逐项停止/恢复 PG、MinIO、Chroma：各故障只使其对应检查失败、恢复后连续三次全绿；网关 SIGKILL 时助手 503 而业务与 liveness 不受影响。runbook 已更新 |
| G06 | 双机故障与真实 CPU 吞吐 ≥1.5 倍 | 已实现 / 已接入 / 部分 | 单机真实 source 处理：8 任务，单 worker 53.43s、双 worker 28.49s，吞吐 1.8757 倍通过；`verify-scaling-cpu.py`。两主机 kill/SIGSTOP/断连/取消/发布/备份-purge/网关恢复仍待实测 |
| H01 | 系统加密凭据、明文迁移、错误配置不静默回落 | 已实现 / 已接入 / 部分 | 桌面外壳 32 项通过；三平台实际密钥环尚未实测 |
| H02 | 远程检索/上传/SSE/创作/重启/断网/清除 | 部分 / 已接入 / 部分（macOS 部分通过） | 本机实际 macOS 应用和隔离安装副本已验证带认证上传/检索、SSE、工作区编辑、断网恢复与重启；本轮按当前源码重建应用并验证真实系统 `safeStorage` 加密落盘（0600、无明文）、认证远程模式、清除后回到本地模式且隔离目录零残留。完整模型创作与其他平台仍待实测 |
| H03 | 三平台安装包与本地 sidecar 回归 | 部分 / 部分 / 部分 | 本批重建 macOS arm64 冻结后端（814 MB，含字幕烧录自检）与 `.app/.dmg/.zip`，签名校验通过；隔离安装副本与远程工作流、外壳测试此前已通过。Windows/Linux 和公证分发仍待执行 |
| H04 | 部署设置校验、热更新/重启边界与双语文案 | 已实现 / 已接入 / 已验证（单机，后端契约） | 设置页部署配置返回内容、修订、字段可编辑性与重启/迁移要求；校验与保存走修订（冲突 409、只读部署 409、无可写文件明确只读）；凭据只接受已存在的环境变量引用，响应不含明文；库身份固定为当前库，切库必须走迁移；模型/检索参数继续热更新。契约由 `test_deployment_config.py` 三项覆盖，页面加载无脚本错误由既有设置页 UI 用例覆盖；部署编辑器自身交互未单独做浏览器断言 |

上一批实现提交：`c0c213c`、`eedcd31`、`400f598`。该批本地完整回归 2823 通过、144 个环境跳过；
真实后端 180 通过且无跳过，可选运行时 34 通过且无跳过。合并后 2965 个不同测试节点通过，
仅两个 Windows 进程分支未在本机执行。其余性能、双机及安装包需求仍以表中状态为准。

共同验收约束：默认本地测试离线；真实后端专项缺依赖或任何 skip 均失败；
所有证据保留源码摘要、配置/版本、测试选择和原始日志。失败、环境不足、中断的记录均保留，
不算作通过。agent 多实例、NFS、存储 HA/分片不纳入必交付。

本批核心能力的运行说明见[持久创作、目录发布与上传会话](../docs/scaling-durable-delivery.md)。

剩余能力闭合第六批（基线 `e54b645`）证据见 `artifacts/scaling-completion-batch6/`：
真实后端专项 **285 通过、0 失败/错误/跳过**（`backends-third/`，含新增八项故障矩阵）；
依赖断连演练 **通过**（`faults/`：PG/MinIO/Chroma 各自只影响对应检查、恢复连续三次全绿、
网关 SIGKILL 后助手 503 而业务与 liveness 不受影响）；真实 CPU source 吞吐
**1.8713 倍**（`cpu/`，1 worker 77.67 s、2 worker 41.51 s，各 88 镜头与 8 对象摘要一致）；
macOS 按当前源码重建冻结后端与 `.app/.dmg/.zip`，真实应用凭据清除演练通过
（`artifacts/scaling-credential-clear-macos-app/`：Keychain 加密落盘、清除回本地、零残留）。
失败与中间轮次（`local-first/` 33 失败、`local-second/` 3 失败、`backends-first/` 8 失败、
`backends-second/` 2 失败）连同修复说明一并保留，见
[实施验证记录](../docs/scaling-completion-verification.md)。

第六批同时修复的运行时缺陷：设置页部署脚本缺 `defer`（全站 UI 报错）；`shared→models`
包边界违规；API 接受 CLI 专属 `force`；SQLite v24/v25 迁移对 `user_version=0` 快照非幂等；
`Persons.confirm_import` 的临时 Store 缺后端身份；集群 `check_index` 不认权威缩略图登记；
PG 连接池未在检出时探测失效连接；召回签名混入预算旋钮。完整清单与证据见验证记录。
