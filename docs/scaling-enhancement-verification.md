# 水平扩展增强实施与验证记录

更新：2026-11-05（北京时间）。需求来源：[增强交付方案](../plans/horizontal-scaling-enhancement-plan.md)。
本页区分实现、接入与实测；没有执行证据的条目不标记完成。
逐项需求与调用点见[交付矩阵](../plans/horizontal-scaling-delivery-matrix.md)。

> 本页是分阶段的历史档案，其中"仍待完成"等表述反映当时的结论。剩余能力的实现与单机验收
> 已在[剩余能力闭合执行记录](scaling-completion-verification.md) 中完成；双机、容量、真实模型、
> 灾备 RPO/RTO 与三平台安装仍按原门槛独立验收。

## 持久创作与上传核心批次（2026-11-05）

本批交付 B04、B07、C02、D01，并补齐 D02 以及修订、产物、日志、字幕归档和媒体使用期接入。
运行与恢复协议见[持久创作、目录发布与上传会话](scaling-durable-delivery.md)。schema 追加到 21。

实现提交：`bef02b8`（共享底座）与 `e47e706`（业务接入与验收入口）。报告的 code_version
为起测时 HEAD `d6b44db`；下列源码摘要覆盖测试时工作区，与这两次实现提交一致。

冻结源码摘要：`118c24909fe529f9a28ca82bafdf4cd53ec357ce5c22df3e7e857fcfe17868c0`。

| 验证 | 本次结果 | 原始证据 |
| --- | --- | --- |
| 隔离 PG / MinIO / Chroma 专项 | 205 通过，0 失败、0 错误、0 跳过；源码未变化 | `artifacts/scaling-core-118c24909fe5/backends/` |
| 真实 CPU source 吞吐 | 单 worker 53.43 秒、双 worker 28.49 秒，8 个任务分别生成 88 个 segments；吞吐 1.8757 倍，门槛 1.5 倍通过 | `artifacts/scaling-core-118c24909fe5/cpu/`：环境、镜像、JUnit、逐 worker 日志、配置、原始结果、ffmpeg 版本 |
| wheel 模块及资源 | 316 个模块、69 个前端资源逐字节匹配，无遗漏或多余模块；时区依赖进入 wheel | `artifacts/scaling-core-118c24909fe5/wheel.json` |
| 可选 SDK / 动画运行时 | Python 3.13.11 隔离 wheel 环境 34 通过，0 跳过；环境已清理 | `artifacts/scaling-core-118c24909fe5/extras/` |
| 默认依赖负向检查 | Python 3.11.16 无 PG/S3/person 可选驱动的隔离 wheel 环境，381 通过、25 个后端用例预期跳过；环境已清理 | `artifacts/scaling-core-118c24909fe5/lean/` |
| macOS arm64 冻结 sidecar | 构建、自检、资源、真实视频 prepare、API 启动和 SIGTERM 退出通过 | `artifacts/scaling-core-118c24909fe5/sidecar/` |
| Electron 外壳 | 32 通过，0 跳过 | `artifacts/scaling-core-118c24909fe5/desktop-shell.log` |
| 完整本地冻结回归 | 3009 节点全部执行：2853 通过、156 环境跳过，0 失败/错误；源码未变化 | `artifacts/scaling-core-118c24909fe5/local/` |
| 浏览器完整素材工作流 | 上传失败/重复、四类处理、人物、发布、检索、重载、桌面/窄屏布局通过；确定性模型 | `artifacts/scaling-core-118c24909fe5/browser/` |
| 实际 macOS 应用远程工作流 | 带认证上传/处理/检索、SSE 与重连、创作工作区编辑、409 后保留输入、断网恢复、菜单重启通过；未生成本机元数据库，退出不停止服务端 | `artifacts/scaling-core-118c24909fe5/macos/remote-report.json`、`studio-report.json`、日志与截图 |
| DMG / ZIP 与隔离安装副本 | macOS arm64 ad-hoc 产物、DMG 挂载、复制、签名校验及安装副本远程工作流通过；临时副本已清理 | 同目录 `distribution.json`、`install-report.json`、`installed/` |

同一冻结源码的逐节点合并结果：**3007 个不同节点通过，仅两个 Windows 进程分支未执行**。
本地 156 个跳过项中，133 项由真实后端专项覆盖，20 项由可选运行时覆盖，1 项由独立 CPU
专项覆盖；另外两项只能在 Windows 执行。清单见 `artifacts/scaling-core-118c24909fe5/coverage.json`。
这些是功能节点覆盖，不替代本节后列出的容量、模型、双机或安装验收。

ruff、mypy（87 个模块）、数据库构造边界、文案镜像、资源哈希、JS 语法和仓库文档链接通过。
源码包的 385 个 Python/前端文件与当前代码逐字节一致，不含用户数据、`artifacts/` 或本地配置；
记录见同目录 `sdist.json`。

CPU 验收没有使用并发 sleep 或模拟媒体处理。使用真实 30 秒 480×270 / 12fps 视频，
分别预热后执行 8 个独立 source 任务；每 worker 的 OpenCV / BLAS、ffmpeg 解码及编码各
限制为一个线程，不调用 GPU 或模型，不包含索引发布；双 worker 使用独立节点目录。
核对不同 worker owner、账本终态、S3 产物大小与摘要。这只验证单机 CPU 横向处理能力。

实际应用测试使用临时库和环境变量凭据。原生 SSE 与编辑/保存经过真实服务端；目录 409
响应在浏览器侧注入以检查保留输入路径，真实 PG 双 API CAS 由后端专项验证，两项证据
分别记录。macOS 系统密钥环、Gatekeeper 公证、Windows/Linux 安装尚未实测。

资源预检见 `artifacts/scaling-core-capacity-preflight.json`：本机 8 CPU、16 GiB 内存；
默认 4096 维、300 万向量、三个版本的 float32 向量本体需要 147456000000 字节，
已超过本轮可用磁盘，尚未计入索引、PG、备份与媒体。规定容量、尾延迟及真实模型质量
保留“环境不足 / 待验收”，没有通过降低维度、规模或门槛替代验收。

保留开发期失败记录：早期分镜 fixture 镜头数断言与 TTL 设置不当、回执类型恢复错误、
模型工厂在非持久场景被无条件包装、旧浏览器脚本的折叠工作台/按钮/角色参考图假设、
新事件表的 PG AUTOINCREMENT 方言错误，以及语言切换脚本仍拦截旧上传接口。
实际应用验收另外发现通用按钮移除了自身错误容器，导致刷新后确认不可见；已修复并把
409 刷新、输入保留和确认文字纳入 `test_i18n_language_and_layout`。
`scaling-core-completion-local` 保留旧上传拦截的失败记录，`scaling-core-verified-local`
保留修复冲突提示前主动中断的记录；这些轮次不作为最终冻结通过证据。
另以隔离用例复现了人工核对后重试与旧连接迟到响应的竞争，修复前成功/错误两场景均失败；
每次实际 dispatch 现以独立回执凭证条件更新，SQLite 与 PG 四个场景通过。原始复现保留在
`artifacts/external-dispatch-generation-before.log`。此前的 `scaling-core-final-local` 因此主动
中断；`scaling-core-release-local` 保留另一项浏览器检索等待超时及中断记录（单独复跑通过），
这些轮次均不计入本次完整通过。最终完整回归在打包和其他验收结束后单独执行。
首次冻结后端离线安装因缓存缺包失败，保留日志；后续构建使用锁定版本联网安装。
工作区原有的 `config.example.yaml` 改动 不纳入本批实现提交。

以下“当前批次”保留此前增强实现的历史证据；本批状态优先以本节及交付矩阵为准。

## 当前批次

| 内容 | 实现 | 接入 | 验证依据 |
|---|---|---|---|
| 显式部署组合与期望库身份 | 已实现 | `open_context` | `test_scaling_foundation.py` 的组合/身份拒绝用例 |
| 共享资源 namespace 与缓存身份 | 已实现 | Chroma/S3 工厂、查询/人物/PG 快照缓存 | 相同索引/对象键跨库隔离、同节点路径换库仍不复用旧快照 |
| 查询/控制池独立容量、超时、总预算和退出清理 | 已实现 | `PgStore` / worker / 租约 | 真实 PG 查询池耗尽仍能使用控制池，控制连接拒绝业务写入 |
| PG 基线与追加迁移、校验值 | 已实现 | migrate 与启动检查 | 真实 v15 带数据升级、重跑、异值校验及未来版本拒绝 |
| 四类账本原子入队、attempt 绑定与状态同步 | 已实现 | source/dialogue/person/topic、CLI 人物入口 | 事务回滚、TTL 回收、跨目录取消、真实 worker 并发 |
| 事务与处理线程 fencing | 已实现 | 写事务、显式 commit、对白/镜头处理线程池 | 过期事务显式 commit 拒绝、跨线程显式凭证与线程池写入拒绝 |
| 跨进程 staging 预约与 pin | 已实现 | S3 物化 | 活动进程 pin 防淘汰、跨实例预算、强制结束进程后的 owner 回收 |
| 共享媒体指针与响应期 pin | 已实现 | `Store.materialize_media`、管理端媒体、镜头合成 | 跨 API 返回媒体、镜头字节摘要与共享对象一致、消费方异常后释放 pin、墓碑优先于缓存 |
| 两 API/两 worker 真实 CPU 素材链路 | 已实现 | 四个独立进程与节点目录 | `test_cluster_process_flow.py`：真实上传/分镜处理/另一 API 读媒体、不同 worker owner、无 SQLite 文件 |
| 本地迁移元数据/日志原子提交 | 已实现 | `migration load` | 子进程 `os._exit` 崩溃回滚、提交后失败续跑、异值目标先于媒体复制被拒绝 |
| 元数据 bundle 激活限制 | 已实现 | `migration activate` | 有素材缺媒体时拒绝激活；缺必要向量时拒绝 |
| 样本全量往返与旧 namespace 导出 | 已实现 | `migration export --legacy-namespace`、load/verify/activate | 样本 local→PG/S3/Chroma→local；核对库 UUID、媒体、向量和权威文件逻辑视图 |
| 人物与整理信息投影 | 已实现 | format 2 且词法派生索引可用的查询 | 五类人物查询与全快照参考结果一致；PG 证据的素材/身份边界；水合拒绝全库整理 map |
| PG 预热内存边界 | 已实现 | `search.warmup` | 不调用全量快照、人物证据或 representation map；向量查询从服务端元数据解释命中 |
| 桌面系统加密凭据与拒绝静默回落 | 已实现 | 远程读取/保存/迁移/清除、错误恢复菜单 | 外壳单测；三平台系统密钥环及安装包待实测 |

## 独立验收入口

```bash
uv sync --extra dev --extra cluster
uv run python scripts/check-scaling-boundaries.py
uv run python scripts/verify-scaling.py --minio-binary /path/to/minio
uv run python scripts/verify-local.py --workers 4
# 在没有其他验收负载时单独执行；--output 指向尚不存在的目录
uv run python scripts/verify-scaling-cpu.py --minio-binary /path/to/minio --output artifacts/cpu-YYYYMMDD
```

脚本创建独立 PostgreSQL 和 Chroma 容器、临时 MinIO 数据目录及随机凭据/端口，
不会使用 `config.local.yaml`、用户数据或已有后端；结束时清理自己创建的服务。
PG/Chroma 按镜像 digest 固定，MinIO 记录可执行文件摘要与版本。
PG 数据使用上限 768 MiB 的 tmpfs；容器清理显式移除本轮临时卷。
该存储配置只用于本页组件测试，不能推导容量、断电持久性或灾备结论。
`artifacts/scaling/<UTC时间与随机ID>/` 保存代码版本、源码摘要、平台、服务版本、
JUnit、pytest 原始日志和执行结果；任何测试跳过均令验收失败。

2026-11-04（UTC）隔离后端选集逐步扩展：首轮 **100 通过、0 跳过**；加入本地迁移崩溃
与账本回收验证后 **107 通过、0 跳过**；加入四进程真实链路及媒体 pin 后
**109 通过、0 跳过**。原始产物分别位于：

- `artifacts/scaling/20261104T150712Z-671d65a26ba3/`
- `artifacts/scaling/20261104T151205Z-0ad6d1c333e5/`
- `artifacts/scaling/20261104T153041Z-a666765c293b/`
- `artifacts/scaling/20261104T153917Z-8d200f50045d/`（109 通过，0 跳过）
- `artifacts/scaling/20261104T154847Z-6dff1ab46cb5/`（扩展至所有已标记外部后端文件，132 通过，0 跳过）
- `artifacts/scaling/20261104T165119Z-6806da9b4222/`（恢复会话后复跑，132 通过，0 跳过）
- `artifacts/scaling/20261104T171440Z-823ff0eb1a12/`（加入库隔离和资源身份往返，135 通过，0 跳过）
- `artifacts/scaling/20261104T172150Z-54a9ece65385/`（人物投影、权威视图恢复及旧 namespace 导出，141 通过，0 跳过；源码摘要前后一致）
- `artifacts/scaling/20261104T172734Z-cc7e8de11eda/`（加入同节点换库缓存与 PG 媒体指针摘要校验，142 通过，0 跳过；源码摘要前后一致）
- `artifacts/scaling/20261104T175252Z-51ce9ec26a26/`（补齐未标记维护/网关用例与 SQL 语义基线，23 文件、161 通过、0 跳过；源码摘要前后一致）
- `artifacts/scaling/20261104T180238Z-1463088a8cf3/`（默认依赖门控修正后复跑，161 通过、0 跳过；源码摘要前后一致）

验收入口同时核对后端标记与后端环境变量，新增相关文件却未登记时直接失败。
[集群 CI](../.github/workflows/ci.yml) 的 `cluster` job 执行同一入口，
从固定 MinIO 源码提交构建服务，并保存原始证据。本段记录时仅检查了工作流 YAML，云端尚未实际执行。

`artifacts/scaling/20261104T153551Z-90c16ea71455/` 是一次失败记录：Docker 虚拟磁盘
8 GiB 用满，PG 报 `DiskFull`，结果为 1 个失败、58 个错误；按环境不足保留，未算作通过。
只清理本轮创建的服务并修复临时卷管理后，使用上述 tmpfs 配置重跑通过。

`artifacts/scaling/20261104T171921Z-a5f9498afc59/` 保留 139 通过、1 失败的记录：
新增预热测试在模块首次导入期间替换了 VectorIndex，检索模块捕获替身后影响后续测试。
已改为在真实空集合上禁止全 map 读取，恢复测试隔离；后续 141 项复跑通过。
`artifacts/local/20261104T172231Z-bda67c001c69/` 是主动中止的回归：复核发现 PG 快照缓存
还缺库身份、媒体未复验 PG 指针摘要后停止，补齐检查再冻结；该轮不计作完整通过。
覆盖合并时还发现两个未标记的 PG 用例被遗漏，已扩展文件覆盖检查并纳入上述 161 项。
`artifacts/scaling/20261104T174958Z-ff0b924bf108/` 保留 160 通过、1 失败的记录：
旧网关断言改为实际流式转发后，mock 响应被 httpx 预先消费；改为 AsyncByteStream 后重跑通过。
`artifacts/local/20261104T175041Z-8955a1f41f0f/` 在该测试替身修正前主动中止，不算完整通过。
`artifacts/scaling-default-deps/` 保留默认依赖负向验收：初次为 4 失败、41 错误、54 通过、
62 跳过，原因是部分 fixture 在环境门控前导入 cluster 驱动。修正 8 个测试文件的导入顺序后，
移除 boto3/psycopg 驱动的隔离副本为 **54 通过、107 个后端用例预期跳过、0 失败/错误**。
真实后端专项仍保持零跳过，未修改业务断言或放宽外部后端验收条件。
`artifacts/local/20261104T175435Z-d219d7fe2504/` 在补齐该默认依赖门控时主动中止，
之后重新冻结执行；该轮不算作完整通过。

桌面外壳 `node --test desktop/test/*.test.js`：**32 通过、0 跳过**。
这些是工程集成和外壳逻辑证据，不代表容量、真实模型质量或三平台安装验收。
此前完整分组回归 `artifacts/scaling-local-frozen/` 为 2800 通过、120 跳过、1 失败；
唯一失败是窗口保存查询次数，已修复并通过完整窗口模块 22 项回归
（`artifacts/scaling-studio-window-final.xml`）。这不是最终全量全绿证据。
冻结源码全量回归 `artifacts/local/20261104T165019Z-e8f26c6f959a/` 已完成：
**2801 通过、120 跳过、0 失败/错误**，2921 个节点恰好执行一次，源码摘要前后一致。
之后新增 namespace、人物投影、PG 缓存身份和媒体指针校验，最终冻结回归位于
`artifacts/local/20261104T172748Z-c6b25d9b9799/`：**2807 通过、129 跳过、0 失败/错误**，
2936 个节点恰好执行一次，源码摘要前后一致。
129 个跳过项中，107 个需要隔离外部后端，对应当前用例由上述 161 项零跳过专项覆盖；
其余为 DSH/动画可选依赖及两个 Windows 专属分支。DSH/动画已在隔离 Python 3.13.11
环境补跑：**34 通过、0 跳过**，含实际 SDK 工具调用/恢复/图像边界/浏览器与动画沙箱，
证据为 `artifacts/scaling-extra-final/`。
默认依赖门控修正后，在包含 DSH/动画依赖的 Python 3.13.11 隔离环境再次完整回归：
`artifacts/local/20261104T180434Z-e531ff87e18f/`，**2827 通过、109 跳过、0 失败/错误**。
2936 个节点全部恰好执行一次，源码摘要与最终 161 项后端专项一致且前后未变。
合并本轮完整回归与后端专项后，**2934 个不同节点已通过，剩余两个仅在 Windows 执行**；
逐节点合并清单在 `artifacts/scaling-enhancement-test-coverage.json`。这里的覆盖不替代双机、
规定容量、真实模型、完整业务故障及三平台安装验收。
该脚本记录每个测试节点与源码摘要，确保全部节点恰好运行一次，并在结束时复核源码未改变。

回归已定位并修复：旧 SQLite 快照携带新增列时的重复迁移、clean 上下文锁的异常捕获位置、
相似检索误访问 embedder、质量评估误用查询缓存。查询向量缓存已增加素材库身份。
浏览器旧路径断言按现有创作主页 `/` 与检索页 `/search` 修正，设置页回归等待异步载入后再断言。
Studio 性能测试的 Store 替身改为统一 `Store.open(config)` 接口，仍保留构造次数约束。
窗口保存把镜头与隐藏区间改为复用同一批源内行，并向内部函数传入已有 Store，
保留 `overlays≤40`、`ffprobe=0`、`rows≤4` 的原约束；对应范围与可见性测试通过。

源码与 wheel 构建成功，已核对新增集群模块实际进入 wheel 且内容与构建时源码一致。
上一批（`6c0e12a`）wheel 的 308 个 Python 模块逐字节与该批冻结源码一致，清单与摘要见
`artifacts/scaling-wheel-final.json`。
macOS arm64 冻结 sidecar 已在独立构建环境生成并通过
`check-desktop-sidecar.py`：冻结自检、原生 dsh/rg、package-data、真实视频 prepare、
健康/页面/模板与退出检查全部通过。产物与日志位于
`artifacts/sidecar-enhancement-macos/`，没有覆盖原有 `desktop/build/backend`。
最新源码已重建并通过同一检查，证据及归档在 `artifacts/sidecar-enhancement-macos-final/`；
`backend-macos-arm64.zip` 与构建记录均带 SHA256，构建时的全树摘要保留在记录中。
在 `6c0e12a` 之前后续只调整了测试覆盖/fixture 门控，构建记录另外核对应用源码树与该版本一致，
`backend/src`、`frontend/src`、`desktop` 的 Git 树均未变化。
此结果不等同于三平台安装包或远程完整工作流验收。
ruff、文档链接、文案镜像、资源版本参数、前端/桌面 JS 语法与 `git diff --check` 通过。
mypy 83 个模块通过。初次检查发现的 17 项错误已按真实协议、生成器返回类型与后端类型修复，
没有新增类型豁免或缩小检查范围。

## 2026-11-05 续批：持久索引发布、周期去重与远程 MCP

本批实现提交为 `c0c213c`、`eedcd31`、`400f598`。原始报告的 code_version 是起测时的
`6c0e12a`；以冻结源码摘要绑定最终实现树，打包规则另有独立摘要。

本批追加 v19，保留 v15–18 的 DDL 与历史校验值。新增 index_tasks 账本，
CLI、REST、内部 IndexBuilder 及人物/检索组合发布均接入持久调度。
冻结配置与提交参数放在 `shared/task_config.py`，`shared/queue_dispatch.py` 统一入队；
执行器为 `admin/source_tasks.py::IndexTasks`。处理层不依赖管理/设置层，依赖白名单保持原约束。
活动指针在队列和账本终态事务中切换；失败、取消、任务过期及资源租约丢失均禁止发布。
索引串行区保持至终态提交，子线程获取的发布租约也由显式凭证在终态事务复验。
本地 inline 行为继续保留，增加 REST `/api/index-tasks` 与 CLI `indexes tasks`。

定时调度以独立 leader 锁和库绑定时区运行；周期占位、挖掘账本和入队同事务。
跨节点重放返回同一任务，失败后的自动尝试以序号递增，每天最多三次。
远程 stdio CLI 跳过本地覆盖层读取；PG 业务 API 在构造会话前拒绝本机 MCP 会话模式。

本批真实后端选集扩至 24 文件，新增用例在 `tests/test_scaling_execution.py`：

- 首轮：`artifacts/scaling/20261104T215527Z-dfb7b58b3180/`，172 通过、0 跳过。
- 中间检查：`artifacts/scaling/20261104T215931Z-c3be9559be9b/`，177 通过、0 跳过；之后继续补强账本绑定与冻结模型恢复。
- 本批最终后端专项：`artifacts/scaling/20261104T232126Z-d4c310ceaa62/`，180 通过、0 跳过；
  源码摘要 `dfcd97d21c5ce3808d567cea82a6d6fded262a5a8632c4541fb3336ac01a004f` 前后一致。
- 覆盖真实视频入队构建、跨目录 worker 词法回填、取消/重试、发布登记后失败/取消/超 TTL、
  子线程、账本重新绑定与资源租约丢失、两个 worker 的增量发布串行继承、人物与检索组合发布、
  调度 leader 切换/重放/事务内崩溃、v18→19 历史校验值及非空新表导出。
  排队后模型配置变化仍使用账本冻结模型；worker 等待 API 提交锁，不把释放竞争误记为失败。
- 新 wheel 位于 `/tmp/mindex-scaling-execution-wheel/`，309 个 Python 模块与本批源码逐字节一致，
  无多余模块；摘要与清单见 `artifacts/scaling-execution-wheel.json`。
- 该 wheel 在不含 PG/S3/人物扩展的临时环境中验证：366 通过、25 个明确的外部后端跳过，
  `artifacts/scaling-execution-delivery-lean/` 保存安装清单、依赖探测、JUnit 与原始日志。
  环境结束后清理；基础依赖含 `tzdata`，无系统 IANA 数据时的命名时区测试通过。
- Python 3.13.11 的动画录制及真实 SDK/runtime 补充检查 34 通过、0 跳过，
  `artifacts/scaling-execution-delivery-extras/` 保存固定依赖、版本、JUnit 与源码摘要。
  与最终后端专项和 wheel 为同一源码摘要，临时环境已清理。
- 最终完整离线回归：`artifacts/local/20261104T232125Z-8371fcf5b1e4/`，
  2967 个节点全部恰好执行一次，2823 通过、144 个环境跳过、0 失败、0 错误，应用/测试源码摘要前后一致。
  合并真实后端与可选运行时后，2965 个不同节点通过；仅两个 Windows 进程分支未在本机执行。
  汇总与剩余节点见 `artifacts/scaling-execution-delivery.json`。

过程中的失败和中断均保留：`artifacts/local/20261104T220008Z-7f88498c2e77/`
发现新增 CLI 帮助缺少示例；`20261104T220858Z-085407dec216/` 和
`20261104T221120Z-16108140c6b8/` 为补强归属与冻结模型恢复主动中断，均有独立 interruption.json。
`artifacts/scaling/20261104T220900Z-968357f4464e/` 虽 178 项无断言失败，源码变化导致验收失败；
`20261104T221122Z-870ec8c12d76/` 的 179 项通过为中间版本。
`20261104T221456Z-906a53b67062/` 有 1 项失败，捕获 API 提交锁释放前 worker 立即领取的竞争；
修复为可取消且受任务租约约束的等待，再通过最终 180 项专项，未放宽断言。
`artifacts/local/20261104T221914Z-cde3e763b5b4/` 发现包依赖违规后中断；
抽离共享提交协议后，包依赖、CLI、配置与设置页定向检查 385 通过、15 个环境跳过，
日志在 `artifacts/scaling-execution-boundaries.log`。mypy 检查范围增至 84 个模块并通过。
首个 wheel 校验还发现已删除模块残留于构建缓存，已归档本批残留并从干净临时目录重建；
`artifacts/scaling-execution-wheel-stale-build.json` 保留失败清单，最终 wheel 无多余模块。

完整回归 `artifacts/local/20261104T223437Z-866d35fe7f4d/` 为 2821 通过、1 失败、144 跳过，
源码摘要前后一致，不能计作全绿。失败任务的 `split` 被误标为“服务重启中断”；
后台标题线程原先只用进程内 guard，会与步骤完成写回交错，把旧状态写入 job.json。
修复后模型调用仍在锁外，标题合并等待 Studio 写锁，并在取得锁后重新读取状态。
`test_studio_title_race.py` 在修复前 wheel 上确定失败，修复后通过；原始证据为
`artifacts/scaling-execution-title-before.xml` 与 `artifacts/scaling-execution-studio-fix-final.xml`（34 通过）。
线程启动失败用例先等待独立标题任务结束再注入启动异常，仍检查启动失败后能立即获取执行锁。
首次修复检查中这两个独立写锁持有者的时序干扰也保留在 `artifacts/scaling-execution-studio-fix.xml`。

本批 macOS arm64 sidecar 已重新构建并验证，最终归档和日志在
`artifacts/sidecar-scaling-execution-macos-delivery/`。通过冻结自检、原生 dsh/rg、package-data、
真实视频 prepare、服务就绪/页面/模板与 SIGTERM 退出检查；额外验证 schema 19、新账本及命名时区。
首包在禁用系统时区数据后失败，缺失的 tzdata 资源已显式加入 PyInstaller 规则；
重建后 `PYTHONTZPATH=''` 的同一探针通过，`timezone-check.json` 保留结果。
初包日志与失败探针保留在 `artifacts/sidecar-scaling-execution-macos/`。
先完成应用回归，再补充冻结包资源规则并单独记录 SHA256；提交前清理新文件末尾空行后，
重新冻结执行上述最终完整回归、后端专项、安装环境与原生构建。
并补跑桌面配置 3 项、桌面外壳 32 项及原生冻结检查。
归档摘要与精确 spec 副本随产物保存，原有 `desktop/build` 未被覆盖。
以上仍是单机隔离组件/集成证据，双机故障、容量、模型与三平台安装的最终门槛保持不变。

## 尚未交付或验收

| 批次 | 尚未关闭的项目 |
|---|---|
| A | 全上下文生命周期、全部节点缓存的身份隔离、混合版本准入与升级演练 |
| B | Studio 持久执行链、CLI 前台直接处理入口的持久化覆盖、外部调用幂等与未知结果恢复、真实 GPU 资源分组；索引/周期的双机故障演练 |
| C | Studio 目录 manifest/CAS、全部权威文件、全部媒体使用期 pin、完整输出/临时空间预约、统一远端 pins/GC |
| D | 上传会话族、浏览器 endpoint/CORS、断点与配额、媒体完整协议矩阵 |
| E | 仍有 eligible 全列表和缺派生表时的全快照回退；候选/缓存字节/请求体/查询预算、原定容量/尾延迟与 Hit@5 门槛 |
| F | 分阶段/分对象续跑、完整媒体/向量闭包与秘密审计、业务激活 gate、运行中身份切换、目标写入后的全量逆向迁移与灾备 RPO/RTO 实测 |
| G | 四进程素材链路已验证；其他真实业务全链路、网关所有入口/断流/恢复、共享服务故障与双机演练仍待完成 |
| H | 三平台安全存储、远程检索/上传/SSE/创作/断网恢复、安装包与其余平台 sidecar 回归 |

本次没有双机、规定容量、真实模型或三平台安装环境的执行证据，整体交付保持待完成。
