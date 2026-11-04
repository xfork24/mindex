# 水平扩展需求—调用点—证据矩阵

更新：2026-11-05（北京时间）。需求以[增强交付方案](horizontal-scaling-enhancement-plan.md)为准。
当前结果和原始产物见[实施验证记录](../docs/scaling-enhancement-verification.md)。

状态为三个独立维度：实现 / 接入 / 验证。`部分` 表示仍有必交付调用点未闭合；
`待实测` 表示没有执行证据，不能据脚本或组件测试标记完成。
此表保留未完成需求，不表示缩小交付范围。

| ID | 需求与主要调用点 | 实现 / 接入 / 验证 | 证据或下一项关闭条件 |
|---|---|---|---|
| A01 | 后端入口与拒绝 SQLite 回落：`shared/backends/context.py`、业务 `Store.open` | 已实现 / 已接入 / 已验证 | `test_scaling_foundation.py`；构造边界静态检查；白名单分支仍须保持运行期拒绝越界 |
| A02 | 显式部署组合、期望库身份：`shared/config.py`、`open_context` | 已实现 / 已接入 / 已验证 | 无效组合在创建节点目录/SQLite 前拒绝；期望 UUID 不匹配拒绝 |
| A03 | PG/S3/Chroma namespace：`shared/backends/s3.py`、`vectors.py`、`processing/indexing.py` | 已实现 / 已接入 / 已验证 | `test_backend_identity.py`：相同资源键跨库隔离；旧 namespace 显式导出；无自动回落 |
| A04 | 全部缓存身份与上下文生命周期 | 部分 / 部分 / 部分 | 下载、查询向量、人物版本和 PG 快照已绑定库身份；其余节点文件与上下文退出逐项核查 |
| A05 | 查询/控制池与总连接预算：`shared/db/pg_backend.py` | 已实现 / 已接入 / 已验证 | 查询池耗尽时控制池仍可用；控制连接拒绝普通写入；全节点连接预算需部署核算 |
| A06 | 追加迁移、协议与启动能力检查：`shared/db/migrate_pg.py`、`doctor.py` | 部分 / 部分 / 部分 | v15→19、v18→19 数据/历史校验值通过；混合版本、分词器/模型/后端能力的统一启动准入待闭合 |
| A07 | 源码、wheel、冻结产物 | 已实现 / 已接入 / 部分 | 当前 wheel 309 模块逐字节核对；当前 macOS arm64 sidecar 已重建，schema 19、无系统时区数据的资源探针通过；其他平台和混合版本仍待验收 |
| B01 | source/dialogue/person/topic 账本与入队同事务：`shared/queue_dispatch.py` | 已实现 / 已接入 / 已验证 | `test_scaling_correctness.py`、`test_scaling_foundation.py`；触发器已被显式事务替代。范围为账本路径，CLI 前台直接处理入口仍须单独审计持久化覆盖 |
| B02 | attempt 绑定、取消、回收与终态原子提交：`queue_pg.py`、`task_progress.py` | 已实现 / 已接入 / 已验证 | TTL 回收、失败上限、跨目录取消、人工重试、账本/队列终态用例 |
| B03 | 显式凭证、线程和提交 fencing：`db/fencing.py`、`pg_backend.py` | 已实现 / 已接入 / 部分 | 显式 commit、处理线程池和过期写入拒绝已测；双机 SIGSTOP/控制失败仍待演练 |
| B04 | Studio 持久执行链：`studio/executor.py`、`studio_api.py` | 待实现 / 待接入 / 待验证 | 所有步骤、节点、列、构建进入持久队列；文件发布受同一 attempt 保护 |
| B05 | index 持久执行链：`cli.py`、`admin/source_tasks.py::IndexTasks`、`shared/queue_dispatch.py`、`processing/indexing.py` | 已实现 / 已接入 / 已验证 | `test_scaling_execution.py`：真实视频构建、词法回填、人物/组合发布、取消重试、终态事务与指针原子发布；发布锁持续至提交，双 worker 跨目录串行继承；冻结模型恢复与提交锁竞争；单机隔离后端 180 项通过 |
| B06 | 稳定周期去重：`topics/scheduler.py`、`topics/runner.py` | 已实现 / 已接入 / 已验证 | 库时区、周期/账本/入队同事务；leader 切换/重复提交只生成一个周期任务，失败上限三次；跨目录重放及崩溃回滚已测，双机故障仍见 G06 |
| B07 | 外部副作用幂等与未知结果恢复：模型 submit/poll、Studio 处理器 | 待实现 / 待接入 / 待验证 | 稳定幂等键、request_id、恢复状态；未知结果禁止盲目重试 |
| B08 | GPU 物理资源组与真实模型服务并发 | 部分 / 部分 / 待实测 | 现有槽位不足以证明物理资源组隔离；需真实模型限流与并发验收 |
| C01 | 小检查点不可变对象与 PG 指针：`shared/checkpoints.py`、`files.py` | 已实现 / 部分 / 部分 | 跨节点修订读取已测；全部权威文件登记尚未完成 |
| C02 | Studio 不可变目录 manifest + 当前 revision 指针 | 待实现 / 待接入 / 待验证 | `workspace/versions.py` 仍逐文件递归发布，不能作为目录原子提交；必须消除旧节点文件覆盖 |
| C03 | 所有人为修改携带修订：Studio 写接口与前端 | 部分 / 部分 / 部分 | 部分 If-Match/CAS 已有；后台标题遵守 Studio 写锁，避免旧步骤状态写回；缺前置条件 428、全写接口覆盖、刷新保留编辑内容仍待实现 |
| C04 | 权威文件闭包：字幕/归档、转写/分析、人物、音乐、声音、选题、Studio | 部分 / 部分 / 部分 | `processing/subtitle_archive.py` 等调用点仍须登记；清空任意节点缓存后全业务续跑验收 |
| C05 | 全使用期媒体 pin：`Store.materialize_media`、`ShotMedia.materialized`、HTTP 响应 | 已实现 / 部分 / 部分 | 管理端响应、镜头合成与 PG 指针摘要已测；其他裸 `resolve_media` 消费仍待改造 |
| C06 | 跨进程 staging 预算/活动 pin：`shared/backends/staging.py` | 已实现 / 部分 / 部分 | 多进程 pin、预约、崩溃 owner 回收通过；输出和临时工作空间预约尚未全覆盖 |
| C07 | attempt/发布/备份/下载统一远端 pin 与 GC 协调 | 部分 / 部分 / 部分 | 下载 pin 和宽限基础用例通过；统一标记、引用复验、条件删除与下载竞争仍待闭合 |
| D01 | pending/multipart/complete/abort 上传会话 | 待实现 / 待接入 / 待验证 | `shared/uploads.py` 仍是流式临时落盘 helper；必须有持久会话与 pending key 归属 |
| D02 | 上传摘要、配额、幂等完成、过期回收 | 部分 / 部分 / 部分 | S3 不可变上传复算 SHA256/条件创建已测；客户端会话配额与恢复协议尚未实现 |
| D03 | 浏览器独立 endpoint、CORS、Content-Type 与访问协议 | 部分 / 部分 / 待实测 | 跨 API 媒体读取通过；HEAD/Range/ETag/过期 URL/长视频 seek 需完整协议矩阵 |
| E01 | 可见性、annotation、素材范围的 eligible 统计快照：`search/lexical_query.py` | 已实现 / 已接入 / 已验证 | SQL BM25 与 Python 参考分数/排序、人物投影五类查询逐项一致 |
| E02 | 人物证据投影与候选水合：`persons/persons.py`、`SearchEngine._hydrate` | 已实现 / 已接入 / 已验证 | PG 按素材/身份取证据；命中只水合所需片段/父镜头/整理信息 |
| E03 | 去掉集群预热全快照和 representation map：`search/warmup.py` | 已实现 / 已接入 / 已验证 | 禁止全快照/人物大缓存/map 的预热用例通过；缺派生表的查询回退仍未去除 |
| E04 | eligible/候选/缓存字节/请求体/查询时间预算 | 部分 / 部分 / 待验证 | 当前仍将 eligible 全列表带入内存；不能据小样本查询通过宣称满足 RSS/尾延迟闸门 |
| E05 | 10万/100万 segments、100万/300万 vectors、20并发与质量 | 待实测 / 待实测 / 待实测 | 原门槛 p95≤2s、p99≤5s、每 API RSS≤2GiB、Hit@5≥85% 保持不变 |
| F01 | 统一 CLI 校验与激活记录：`maintenance/migration.py` 的 preflight/export/load/verify/activate | 已实现 / 已接入 / 已验证 | 拒绝非空/异值目标；CLI 拒绝激活缺必要媒体或向量的诊断 bundle |
| F02 | migration_id、源清单身份、元数据/日志原子提交：`db/transfer.py` | 已实现 / 已接入 / 已验证 | 真子进程 `os._exit` 崩溃与提交后失败续跑；消除本地 JSON 日志窗口 |
| F03 | 分阶段/分对象/分集合续跑与恢复账本 | 部分 / 部分 / 待验证 | 当前只能对已提交元数据续跑；部分对象写入、旧 running attempt 的完整重建策略待闭合 |
| F04 | 媒体/向量/Studio 历史闭包与秘密裁剪 | 部分 / 部分 / 部分 | 样本媒体/向量/逻辑视图往返通过；所有业务引用闭包和所有秘密载体尚未验证 |
| F05 | 身份切换、激活 gate、目标写入后的逆向迁移 | 部分 / 部分 / 部分 | 资源自首份写入绑定源 UUID、小样本双向通过；业务节点加载/激活准入、运行中身份切换与完整逆向数据待闭合 |
| F06 | PG/WAL、对象保护与实测灾备 | 待实测 / 待实测 / 待实测 | 记录停写边界与吞吐；RPO≤24h、RTO≤8h 必须实测，不以组件恢复代替 |
| G01 | 两 API/两 worker、独立目录的真实素材处理 | 已实现 / 已接入 / 已验证 | `test_cluster_process_flow.py`：四个进程上传/执行/跨节点读媒体、摘要与 worker owner 对账 |
| G02 | 单实例网关 SSE/附件/取消/重连/MCP 完整代理 | 部分 / 部分 / 部分 | 流式代理与循环检测已接入；全入口、断流关闭、认证与恢复仍需完整故障矩阵 |
| G03 | outbox 先落事件后 ack、持久去重与生命周期 | 已实现 / 已接入 / 部分 | 持久回执与重放组件测试通过；真实网关 kill/restart/SSE 重连待验收 |
| G04 | 业务 API/远程 stdio MCP 不创建本机 agent 状态 | 已实现 / 已接入 / 已验证 | `test_scaling_execution.py`：远程 CLI 禁止 Store.open/覆盖层读取，未创建节点目录；PG 业务 API 在 SessionStore 构造前拒绝本机会话模式 |
| G05 | live/ready、监控、部署和故障 runbook | 部分 / 部分 / 部分 | `/api/live`、`/api/ready` 与池/队列/staging 指标已接入；共享服务故障及运维演练待验收 |
| G06 | 双机故障与真实 CPU 吞吐 ≥1.5 倍 | 待实测 / 待实测 / 待实测 | 两主机重复 kill/SIGSTOP/断连/取消/发布/备份-purge/网关恢复；不能用并发睡眠任务代替 |
| H01 | 系统加密凭据、明文迁移、错误配置不静默回落 | 已实现 / 已接入 / 部分 | 桌面外壳 32 项通过；三平台实际密钥环尚未实测 |
| H02 | 远程检索/上传/SSE/创作/重启/断网/清除 | 部分 / 部分 / 待实测 | 真实安装应用中逐项执行并保存日志，不能用外壳 mock 代替 |
| H03 | 三平台安装包与本地 sidecar 回归 | 部分 / 部分 / 部分 | 本批 macOS arm64 冻结 sidecar、命名时区资源、32 项外壳测试通过；三平台安装包、安装与远程完整流程待执行 |
| H04 | 部署设置校验、热更新/重启边界与双语文案 | 部分 / 部分 / 部分 | 现有设置读展示与桌面双语错误已接入；全部字段的编辑/校验与凭据引用待闭合 |

本批实现提交：`c0c213c`、`eedcd31`、`400f598`。最终本地完整回归 2823 通过、144 个环境跳过；
真实后端 180 通过且无跳过，可选运行时 34 通过且无跳过。合并后 2965 个不同测试节点通过，
仅两个 Windows 进程分支未在本机执行。其余性能、双机及安装包需求仍以表中状态为准。

共同验收约束：默认本地测试离线；真实后端专项缺依赖或任何 skip 均失败；
所有证据保留源码摘要、配置/版本、测试选择和原始日志。失败、环境不足、中断的记录均保留，
不算作通过。agent 多实例、NFS、存储 HA/分片不纳入必交付。
