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
