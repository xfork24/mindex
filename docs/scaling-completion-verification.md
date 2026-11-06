# 剩余能力闭合执行记录

计划：[水平扩展剩余能力闭合与单机故障验收](../plans/horizontal-scaling-completion-plan.md)。
执行基线：`e54b645`，2026-11-05，macOS 单机。保留原工作区 `config.example.yaml` 改动。
本记录不替代原验证记录，也不把部分能力标为整批交付。

## 第一批：生命周期

调用链：`open_context` → `BackendContext` → `Store.close`；CLI 的命令级
`resource_scope`、API 最外层 lifespan、worker `run` 的 finally 与 doctor 退出。
staging 不再共享进程全局 owner；PG 池按活跃 backend 持有者计数；S3 客户端随上下文关闭。
锁提供者持有 Store，确保临时 Config 构造的锁在心跳和释放期间仍持有上下文。

定向新增测试：`tests/test_backend_lifecycle.py`，覆盖 Store/context 幂等关闭、同目录独立
staging owner、调用级异常清理、启动身份不匹配清理、共享池不被其他 owner 提前关闭、
API lifespan、CLI 调用退出、worker 异常退出，以及借出组件的生命周期、账本锁内 GC、
API 构造失败。预热入口及后台监听线程也使用调用级资源边界，serve 退出时停止预热线程。

自动回收由资源自身的 finalizer 承担。staging 关闭只释放 owner 锁，下一次账本操作按
已有崩溃回收协议清理预约和 pins；回收期间不重入其他实例已持有的 ledger 锁。

原始证据在 `artifacts/scaling-completion-batch1/`：

- `lifecycle/initial.xml` / `initial.log`：24 通过、12 环境跳过；这是离线探索，不能算后端专项通过。
- `lifecycle/lifecycle.xml` / `lifecycle.log`：新增 8 项通过。
- `backends/`：首次真实后端运行失败；68 失败、26 错误、119 通过，零跳过。
  首个生命周期缺陷是锁提供者没有持有 Store，导致上下文被回收后仍尝试释放 PG 租约，
  随后失效执行凭证引发连锁失败。保留原日志及 JUnit，不删除失败证据。
- `backends-retry/`：212 通过、1 失败，零跳过。借出的 media 没有保留上下文，导致并发写入期间
  staging 提前关闭；同时捕获 GC 回收重入 ledger 的警告。两者均按真实失败记录。
- `lifecycle/ownership.xml`：预热、Web 认证及生命周期回归 39 项通过。
- `lifecycle/final.xml`：新增生命周期用例共 11 项通过。
- `backends-final/`：修复借出组件所有权及 GC 重入后重跑；线程未处理异常和 finalizer 异常
  提升为失败，不能以 pytest warning 掩盖。216 通过、零失败/错误/跳过，源码摘要未变化。
- `backends-typed/`：补齐媒体字段类型及 PG backend 类型收窄后复验，216 通过、零失败/错误/跳过，
  源码摘要未变化。之后仅把 `setattr` 改为 `cast(Any, component)` 属性赋值，满足 Ruff B010；
  该最终源码另跑 `lifecycle/regression.xml` 所列本地回归，源码摘要及完整命令见 `execution.json`。
- `lifecycle/mypy-final.log`：上下文与 PG 后端类型检查通过。Ruff、数据库构造边界及文档链接检查通过。
- `lifecycle/regression.xml`：受影响本地 API/CLI/Studio 等 484 项通过，但并发首次建库出现
  `PytestUnhandledThreadExceptionWarning`，不能作为干净通过。已为本地 WAL/迁移初始化加跨进程锁，
  并加强 `test_library_id_concurrent_first_open_wins`，要求四个线程全部返回。
- `lifecycle/regression-final.xml` 与 `backends-verified/`：最终源码顺序复验，均将线程和回收异常
  升为错误；本地回归 484 通过，只有依赖的 Starlette 弃用提示，无线程/回收异常。
  真实后端 216 通过、零失败/错误/跳过，源码摘要校验一致。历史失败和带异常警告的结果均继续保留。

本段结束时未完成：全部缓存身份审计、统一启动准入、前台任务账本覆盖、GPU 物理资源组及未知执行核对。
第二至六批及完整冻结回归尚未完成；外部验收保持原状态。

## 第一批续作：准入、缓存与前台任务

在中断后的工作区上继续，追加 v22，保留 v15–v21 DDL 与校验值。新增运行协议 2、
物理资源组与占用表；旧协议节点必须停机统一升级。业务上下文核对库身份，集群 API 启动与检索
拒绝缺失/分词器不匹配的派生数据及旧格式全快照路径，提示回填或重建。

`gpu.groups` 声明物理容量及任务类别，共驻模型服务使用同一组；集群任务必须声明组或显式
列入 `gpu.cpu_queues`。占用持久化，TTL/回收不会释放未知远端执行；`mindex gpu list/resolve`
提供核对入口，释放必须附依据。模型服务真实限流与 GPU 吞吐仍独立待实测。

CLI 的 prepare/detect/split/transcribe/align/analyze/enrich-dialogue/ingest 在 PG 队列模式中
经 source/dialogue 账本提交并等待；API、CLI 和 worker 共用冻结配置及执行入口。
真实 PG + FFmpeg 测试验证 CLI prepare 的账本、prepared 和队列终态一致。

缓存审计已覆盖快照、查询向量、人物版本/图像、向量映射、管理工作摘要和分析译文。
库数据缓存绑定库 UUID 和后端身份；本地直接 Store 的测试通过元数据取同一身份。
分词、静态文案和模板解析等纯函数缓存与库无关。Chroma 业务客户端由上下文/借出集合持有，
关闭时释放；工作摘要线程池按上下文关闭，worker 的临时 Store 按 attempt 退出释放。

本轮证据：`artifacts/scaling-completion-batch1/admission-first/`（23 失败，198 通过）、
`admission-second/`（221 通过）、`admission-third/`（测试错误使用控制连接，1 失败、222 通过）、
`admission-fourth/`（223 通过，零跳过，源码摘要一致）。
单独定向：391 项准入/CLI 帮助/缓存测试通过；54 项工作摘要、生命周期、预热、素材与对白回归通过。
类型检查、Ruff 和数据库构造边界通过。后续仍须在全部批次闭合后冻结源码执行完整回归。

## 第二批进行中：资源登记、工作集及删除协调

追加 v23 登记对象状态与远端 pins；v22 和以前的已发布迁移保持不变。
`resource_registry.referenced_objects` 供 GC 与媒体导出共用，保留全部目录历史，
覆盖检查点、人物、音乐、声音样本、选题配置、外部回执和上传资产。
PG 引用发布、pin 与 GC 使用同一库级协调锁；GC 独立提交删除意图，删除前再次读取
当前引用和 pins，并核对对象身份。删除意图崩溃后可恢复，删除中的对象拒绝新签名/发布。

S3 安全删除通过独立探针核验。当前隔离 MinIO 忽略 DELETE If-Match；未启用可靠条件删除
或版本化时自动 GC 明确停止。已启用版本化的 bucket 可验证指定 VersionId 删除能力后使用；
程序不自动更改生产 bucket 版本化。验收仅为本轮隔离测试 bucket 显式启用版本化。

节点工作集预算涵盖输入缓存、输出和临时文件；JSON/上传/物化复制与 FFmpeg 输出在写入前
预约，未知输出逐块增长，拒绝时停止编码器并清理半成品。活动输入由工作集/attempt 或
HTTP 响应的物化上下文保留；HTML 导出复制引用媒体到导出目录。

原始证据：`artifacts/scaling-completion-batch2/`。
`coordination-first/` 因 PG 池 finalizer 重入非重入锁而中断，保留采样及日志；已改为重入锁并增加回归。
`coordination-second/` 为 228 通过、1 失败、零跳过，暴露 MinIO 条件删除不安全并停止 GC；
`coordination-third/` 为 228 通过、1 失败、零跳过，暴露声音表分页使用错误 rowid；已改用整数 id。
本节为进行中记录，尚不能将整批或全部计划标为完成。

`coordination-fourth/`：230 通过、1 失败、零跳过；旧测试仍使用已释放的裸 Path，
改为显式物化上下文后复验。`coordination-fifth/`：232 通过、零失败/错误/跳过，源码摘要一致；
包含真实 PG 删除期间的下载签名与引用发布竞争，以及既有跨进程 staging/恢复、冷节点、bundle/purge 用例。
`media-directed.xml`/`.log`：Studio 产物、工具、音乐、声音、渲染、窗口、媒体和管线 102 项通过。
`stage2-pin-local2.log` 为离线定向 32 通过、6 环境跳过；外部节点随后由真实后端专项覆盖，
不把跳过作为通过。核心新增模块 Mypy、全树 Ruff、构造边界、文档链接与 diff 检查通过。
单机统一故障矩阵和最终冻结回归仍需在后续批次完成后执行。

## 第三批进行中：双路径上传

追加 v24（上传 transport、原生 multipart 身份、完成阶段/错误与分片 ETag），
同步 PG 方言的追加列映射。默认 API 路径兼容，S3 直传对会话 pending key 签名；
浏览器使用独立 endpoint，内部数据客户端不变。Content-Length 纳入分片签名，
完整 SHA256 在服务器复算，不能使用 multipart ETag 替代。

原生 multipart 创建后 UploadId 未落账、未登记分片、远端完成后未记账与正式对象
写入后未登记资产均有恢复入口。会话处于 completing 时仍占配额，可再次 complete；
abort/reap 清理应用分片和原生 multipart。浏览器保留会话，提供续传和显式重新上传。
部署说明见 [双路径上传配置](scaling-upload.md)。doctor 核验实际 endpoint/签名/CORS，
仅操作独立随机探针；readiness 不执行这些写入探针。

`artifacts/scaling-completion-batch3/uploads-first/`：228 通过、10 失败、零跳过；
新增列未被 PG SELECT * 展开，导致所有上传入口一致失败，已修复方言映射。
`local.xml`：SQL、上传兼容、摘要和界面文案 40 项通过，11 个外部节点由真实后端专项执行。
本节仍为进行中，不代表第三批或全部计划完成。

`uploads-second/`：237 通过、1 失败、零跳过；新增测试把 `Store.one()` 的字典当作位置行访问，
已修正测试。`uploads-third/`：239 通过、零失败/错误/跳过，源码摘要一致。
真实 Chromium 通过独立 endpoint/CORS，PUT 被 S3 接收后代理切断响应，显式续传恢复分片且
未重新 PUT；75 秒 H.264/AAC 视频实际 seek 到第 60 秒，截图为 `s3-browser-upload.png`。

补查隐藏临时文件崩溃后计费缺口：staging 预约按文件路径登记，预约与可见文件在同一账本锁内
原子交接；owner 失效后的隐藏残留重新计入缓存并可淘汰。`staging-final.xml` 24 项通过。
浏览器“重新上传”先取消旧会话释放配额；兼容、摘要、直传凭据不发往应用以外地址及双语文案
回归 10 项通过（`compatibility-final.log`）。核心新增模块 Mypy 与全树 Ruff 通过。

`uploads-fourth/`：238 通过、1 失败、零跳过；负向签名测试向代理发送大请求体时，隔离代理在
S3 提前拒绝后发生 BrokenPipe，不能作为凭据拒绝证据。改以同一会话末尾小分片检验后，
`uploads-fifth/` 最终 239 通过、零失败/错误/跳过，源码摘要一致；错误签名及超声明 Content-Length
均得到真实 S3 403，完成边界恢复、摘要错误、到期清理、过期 URL、HEAD/Range/If-Range/ETag
与两条上传路径通过。既有失败证据继续保留，统一故障矩阵及最终冻结回归仍待执行。

## 第四批进行中：分页关系、总期限和容量工装

集群查询在同一 PG repeatable-read 事务内建立临时 eligible 关系；SQL 聚合统计、
BM25、人物范围与排序共用它，ID 默认 512 条分页。annotation 保持 Unicode casefold，
人物证据只取当前页重叠区间，候选水合不读取整版快照；集群禁止全快照回退。
可表达的素材/粒度条件下推 Chroma，窄范围回退逐页查询后合并全局排序。
查询默认 5 秒，覆盖池等待、SQL、嵌入/Chroma HTTP、媒体命令及水合；503 明确可重试。
Chroma 使用上下文所有的 SDK ServerAPI/集合与有界传输，避免 SDK 构造期无限网络等待。
查询向量内存/磁盘缓存均限制条目和字节；默认 64 MiB、128 项。词法回填也已按页构建。

原始证据 `artifacts/scaling-completion-batch4/`：

- `query-first/`：239 通过、零跳过；现有集成回归。
- `query-second/`：248 通过、2 失败、零跳过；新增参考对照发现人物时长混排浮点顺序、
  空提及查询返回字段差异，按参考行为修复。
- `query-third/`：250 通过、零跳过；分页/参考评分、PG 快照、SQL/池/慢速分块总期限和缓存预算通过。
- `query-fourth/`：251 通过、1 失败、零跳过；容量工装误依赖未声明 psutil，改为系统 RSS 及 API OS 峰值指标。
- `query-fifth/`：静态检查发现缺失 sys 导入，主动中断本轮 pytest 并清理本轮服务；记录为 interrupted。
- `query-sixth/`：252 通过、零失败/错误/跳过，源码摘要一致。小样本容量工装完整创建三个集合、
  工程嵌入服务与两个独立 API 进程，44 请求零失败，两个 API 峰值 RSS 约 288/287 MB；
  此结果明确标为 smoke_passed_not_acceptance，capacity_passed=false，不代替原规模或真实模型质量。

`scaling-capacity.py` 固定 10万/100万 segments、每版本 100万/300万向量、三个版本、4096维、
20 并发与 1%/0.1% 范围，错误请求计入失败和延迟分布。使用独立空库生成并验证实际集合规模、
两个 API 真实 PID、RSS、指标及源码摘要；说明见 [容量工装](scaling-capacity.md)。
`capacity-100k-preflight.json`、`capacity-1m-preflight.json` 保留本机磁盘不足的原始证据。
原规模裸向量分别为 49,152,000,000 / 147,456,000,000 字节，尚未包含 HNSW、PG、WAL 和备份；
本机 16 GiB 内存、约 32 GiB 可用盘不能容纳，正式容量、尾延迟及真实模型 Hit@5 仍待外部实测。

`query-seventh/` 最终源码复验：254 通过、零失败/错误/跳过，源码摘要一致。
API 查询池耗尽在期限内返回带重试标记的 503，liveness 继续 200；窄范围不能在过采样预算后
扩大到全库，逐页 ID 回退得到全局结果。纯人物排序按全 eligible 计算排名，并逐候选页做 IoU
去重，候选页为 1 也不能截断 eligible 或改变参考结果。类型、Ruff、构造边界与链接检查通过。
后续第五、六批与最终冻结回归继续执行。

## 第五批进行中：持久阶段与激活门禁

追加 v25，迁移首份目标对象写入前持久绑定完整清单摘要、源数据库摘要与 UUID；
预检、身份、媒体、向量、元数据、任务恢复、验证、激活按阶段记录。
媒体/集合/向量批次先记 pending，已有目标先逐字节/逐向量核对再补账，拒绝异值覆盖。
元数据复制与阶段提交同事务，恢复后运行表清单另存，不把旧 running attempt 当作成功。
PG/SQLite 业务写入与领取统一拒绝未激活目标；API 可启动诊断，默认设置初始化跳过迁移库。
未知 GPU/远端请求保留占用并进入核对，已激活目标禁止旧 bundle 重放。
秘密按登记配置字段裁剪，未知结构化载体保留用户原文并列出位置阻止正式激活。
统一媒体闭包、Studio 历史与目标新写入后的反向导出同步校验，操作说明见 [迁移与恢复](scaling-migration.md)。

`artifacts/scaling-completion-batch5/` 原始证据：

- `migration-first/`：262 通过、6 失败、零跳过。修复迁移中 API 默认档案写入、PG 导出暂存库 WAL/连接未关闭造成摘要变化，以及旧测试未遵守激活门禁和升级结构还原不完整。
- `migration-second/`：267 通过、1 失败、零跳过。最后一项为测试误用控制连接领取需更新业务账本的任务，改为普通事务，控制通道仍禁止业务写入。
- `stages-local-second.xml`：12 通过、2 个 PG 节点由真实后端专项覆盖。九个阶段边界用子进程 SIGKILL；向量成功写入而未记账的续跑禁止重复 upsert。
- `recovery-first/`：脚本启动轮询异常范围错误导致启动失败；已修复，不计通过。
- `recovery-second/`：**实际 Docker 持久命名卷**，PG 16.15 basebackup+WAL、独立 MinIO 保护与恢复；8 个对象 8,388,608 字节，保护 LSN 后切断源 PG/MinIO，新实例约 1.901 秒恢复，零观测提交丢失，逐表/逐对象摘要一致，basebackup 后写入经 WAL 恢复。源码不变。此工程样本明确不是代表性规模 RPO/RTO 验收，两项正式门槛均保留 false。

`migration-third/`：最终真实 PG/MinIO/Chroma **271 通过、零失败/错误/跳过**，源码摘要一致。
新增真实 S3 对象、远端 Chroma 批次写入后 SIGKILL，恢复先核对已有内容，不重复写入；
未知 GPU/外部请求的占用/核对状态、激活组声明门禁通过。追加迁移保持已发布 DDL 校验值，
修复 PG Studio 事件 id/物理 rowid 的规范序与恢复后的序列推进。Ruff、构造边界及链接通过。
新迁移模块 Mypy 使用 follow-imports=skip 定向通过；普通传递检查仍报告旧依赖模块 11 项类型错误，未据此宣称全树类型检查通过。
第五批定向实现/接入验收完成；统一故障矩阵与最终冻结复验随第六批继续。

## 第六批：网关、设置、桌面与统一故障验收

计划要求的实现与接入在本批闭合：网关 SSE 事件身份与 outbox 先落盘后确认、readiness 有界逐项
探测、设置页部署配置读写、桌面凭据清除、统一单机故障矩阵与依赖断连演练；文档同步更新。

统一故障矩阵 `tests/test_scaling_fault_matrix.py` 在真实两 API、两 worker、单网关、PG、MinIO、
Chroma 上执行八项演练：SSE 事件 id 与 Last-Event-ID 续传、网关 SIGKILL 后按持久回执重放
（可见事件身份不变、无重复）、附件与 MCP 断流后恢复、worker `kill -9` 回收重派、`SIGSTOP`
超租约后旧进程恢复禁止提交、执行中取消竞争、发布事务崩溃后指针原子性、双 API 真实目录修订 CAS。
worker 增加 `--task-ttl/--renew-interval`（仅故障演练用）以缩短租约观察窗口。

依赖断连演练由 `scripts/verify-scaling.py --suite faults` 创建两 API、单网关与一次性 PG/MinIO/Chroma，
逐项停止并恢复：PG 停止只使数据库（查询/控制/迁移/准入）检查失败；MinIO 停止只使 `storage.bucket`
失败；Chroma 停止只使 `vectors.server` 失败；每项恢复后连续三次全绿才记为恢复；网关 SIGKILL 时
助手返回 503 而业务 API 与 liveness 不受影响，重启后恢复。liveness 始终只表示进程存活。

原始证据目录 `artifacts/scaling-completion-batch6/`：

- `local-first/`：完整离线回归 3096 项，33 失败（保留）。失败暴露以下实现缺陷，均已修复：
  设置页部署脚本缺少 `defer` 导致全站 `MindexI18n is not defined`；`shared/admission` 反向依赖
  `models.adapters`（`ModelError` 已下沉 `shared/errors.py`，`models` 侧重导出）；API 接受 CLI
  专属 `force`（改为 422，强制重算仅存在于显式 CLI）；SQLite v24/v25 迁移对 `user_version=0`
  快照非幂等（按列存在性跳过 `ADD COLUMN`）；`Persons.confirm_import` 的 `TransactionStore`
  缺少 `pg/backend/cache_identity`，导致迁移门闩与权威登记取不到后端；集群 `check_index` 只查
  本地缩略图缓存（改为同时接受权威登记）；多个测试在应用 lifespan 之外经回环调用工具/MCP
  （改为同一应用上下文内断言）；召回签名纳入了查询期限/分页/缓存预算等不改变语义的旋钮
  （改为语义字段投影，四个 `recall` 快照条目随之更新，其余阶段签名逐字节不变）。
- `backends-first/`：真实后端 285 项，8 失败，全部为新故障矩阵的测试缺陷（账本引用误当队列主键、
  附件 id 取裸 token、桥接轮询竞态、`changes/log` 数据已解析、激活演练使用空索引）。
- `backends-second/`：285 项，2 失败（保留）：签名 URL 用 1 秒有效期做可用性断言在满载下临界；
  `SIGSTOP` 演练回收窗口不足。均为测试边距问题，断言语义未放宽。
- `backends-third/`：285 通过、零失败/错误/跳过，源码摘要一致；含新增故障矩阵八项。`backends-final/` 为提交后同摘要复验，同样 **285 通过、零失败/错误/跳过**，以下表为准。
- `faults/`：依赖断连演练通过；六项组件测试通过、零跳过（池耗尽时 readiness 区分查询/控制池、SSE Last-Event-ID 续传、部署配置修订冲突/只读/凭据引用三项）；各步耗时与逐项检查结果写入 `report.json`（PG 停止 7.2 s / 恢复 2.5 s，MinIO 恢复 1.3 s，Chroma 恢复 1.3 s，网关 kill→restart 2.3 s）。
- `cpu/`：真实 CPU 吞吐 1 worker 85.72 s、2 worker 44.13 s，**1.9426 倍**（门槛 1.5），
  两个阶段各 88 个镜头、8 个对象摘要逐一核对一致。
- 桌面：冻结后端按当前源码重建并通过 `check-desktop-sidecar.py`（含中文字幕烧录、隔离 PATH）；
  `.app/.dmg/.zip` 重新打包且 `codesign --verify --deep --strict` 通过；
  凭据清除演练见 `artifacts/scaling-credential-clear-macos-app/`：系统 `safeStorage` 加密
  落盘（0600、无明文）、带认证的远程模式、清除后回到本地模式、隔离目录无令牌/密文残留。
- `local-second/`：3 失败（保留）；均为工具调用越过应用 lifespan 的用例，改为在同一上下文内断言后通过。
- `local-final/`：3096 项、2 失败（保留）：`delivery_templates` 页面脚本在满载下晚到、
  `test_agent_shot_details` 的 hypothesis `data_too_large` 健康检查。前者改为等待按钮状态稳定，
  后者按生成器上限抑制该健康检查；两处单独重跑通过。
- `local-final2/`：3096 项、1 失败（保留）：搜索页在四路并行满载下等待结果卡片超时。
  该脚本改为等待 `/api/search/text` 响应后再断言，并为主要 UI 脚本设置满载默认等待（显式
  timeout 的断言不受影响）。上述三处均为测试稳定性问题，未放宽任何断言语义。
- `local-final3/`：**3096 项、零失败/错误**，189 个环境跳过（可选运行时、外部后端与平台分支），
  源码摘要 `4d6dfbcc0006090b18cf53878ca7b6af30f0c5d26972b29fff364a6bcc19d61d` 与结束时一致。

本批修复的运行时缺陷（非测试问题）：PG 重启后连接池残留失效连接导致 readiness 持续 503，
池现在在检出时执行探针并丢弃失效连接；集群 `check_index` 的缩略图存在性判断不认权威登记，
冷节点激活会误报文件缺失；`faulthandler_timeout=60` 在长专项中与 `cancel_dump_traceback_later`
争锁导致挂起，验收入口改为 600 秒。演练环境同时修正：PG 使用命名卷（`--tmpfs` 在容器
`stop/start` 后会重新 initdb）、容器使用固定宿主端口（`stop/start` 会重新分配随机端口）。

未在本轮通过的部分保持原状态：双机、规定大容量、真实模型、Windows/Linux 安装与系统密钥环实测
仍为独立验收；代表性规模 RPO/RTO、提供商真实幂等/限流同理。

### 提交后复验（源码摘要 `4d6dfbcc0006090b18cf53878ca7b6af30f0c5d26972b29fff364a6bcc19d61d`）

代码提交 `e667ab2` 后在**同一源码摘要**上重跑全部真实后端验收与桌面演练，避免"证据来自提交前工作区"：

| 专项 | 结果 | 证据 |
|---|---|---|
| 完整离线回归 | 3096 项，零失败/错误，189 环境跳过 | `local-final3/` |
| 真实后端 integration | 285 项，零失败/错误/跳过 | `backends-final/` |
| 依赖断连演练 | 通过，六项组件测试零跳过 | `faults/` |
| CPU 吞吐 | 1.9426 倍（85.72 s → 44.13 s） | `cpu/`、`artifacts/scaling-cpu-throughput-result.json` |
| macOS 凭据清除 | 通过（真实应用、真实 Keychain） | `artifacts/scaling-credential-clear-macos-app/` |

期间两次 integration 运行因**环境**失败并保留：Docker 虚拟机磁盘写满（overlay 7.8 GiB 用尽，
PG 报 `No space left on device`）后清理未使用镜像/卷恢复；另一次为 chromadb 本地 Rust 绑定偶发挂起，
清理环境后复跑通过。二者都不是代码失败，也不计入"通过"。

macOS `.app` 构建自 `backend/src`、`frontend/src`、`desktop` 的当前内容；此后这些目录未再修改
（提交只涉及测试、脚本与文档），因此应用内后端与提交源码一致。

