# 水平扩展增强实施与验证记录

更新：2026-11-05（北京时间）。需求来源：[增强交付方案](../plans/horizontal-scaling-enhancement-plan.md)。
本页区分实现、接入与实测；没有执行证据的条目不标记完成。
逐项需求与调用点见[交付矩阵](../plans/horizontal-scaling-delivery-matrix.md)。

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

验收入口会核对测试文件覆盖，新增外部后端标记文件却未登记时直接失败。
[集群 CI](../.github/workflows/scaling.yml) 已配置在 push/PR/手动触发时执行同一入口，
从固定 MinIO 源码提交构建服务，并保存原始证据。已检查工作流 YAML；云端尚未实际执行。

`artifacts/scaling/20261104T153551Z-90c16ea71455/` 是一次失败记录：Docker 虚拟磁盘
8 GiB 用满，PG 报 `DiskFull`，结果为 1 个失败、58 个错误；按环境不足保留，未算作通过。
只清理本轮创建的服务并修复临时卷管理后，使用上述 tmpfs 配置重跑通过。

`artifacts/scaling/20261104T171921Z-a5f9498afc59/` 保留 139 通过、1 失败的记录：
新增预热测试在模块首次导入期间替换了 VectorIndex，检索模块捕获替身后影响后续测试。
已改为在真实空集合上禁止全 map 读取，恢复测试隔离；后续 141 项复跑通过。
`artifacts/local/20261104T172231Z-bda67c001c69/` 是主动中止的回归：复核发现 PG 快照缓存
还缺库身份、媒体未复验 PG 指针摘要后停止，补齐检查再冻结；该轮不计作完整通过。

桌面外壳 `node --test desktop/test/*.test.js`：**32 通过、0 跳过**。
这些是工程集成和外壳逻辑证据，不代表容量、真实模型质量或三平台安装验收。
此前完整分组回归 `artifacts/scaling-local-frozen/` 为 2800 通过、120 跳过、1 失败；
唯一失败是窗口保存查询次数，已修复并通过完整窗口模块 22 项回归
（`artifacts/scaling-studio-window-final.xml`）。这不是最终全量全绿证据。
冻结源码全量回归 `artifacts/local/20261104T165019Z-e8f26c6f959a/` 已完成：
**2801 通过、120 跳过、0 失败/错误**，2921 个节点恰好执行一次，源码摘要前后一致。
之后新增 namespace 与人物投影等实现，正在重新冻结执行最终回归；上述结果只代表当时源码。
该脚本记录每个测试节点与源码摘要，确保全部节点恰好运行一次，并在结束时复核源码未改变。

回归已定位并修复：旧 SQLite 快照携带新增列时的重复迁移、clean 上下文锁的异常捕获位置、
相似检索误访问 embedder、质量评估误用查询缓存。查询向量缓存已增加素材库身份。
浏览器旧路径断言按现有创作主页 `/` 与检索页 `/search` 修正，设置页回归等待异步载入后再断言。
Studio 性能测试的 Store 替身改为统一 `Store.open(config)` 接口，仍保留构造次数约束。
窗口保存把镜头与隐藏区间改为复用同一批源内行，并向内部函数传入已有 Store，
保留 `overlays≤40`、`ffprobe=0`、`rows≤4` 的原约束；对应范围与可见性测试通过。

源码与 wheel 构建成功，已核对新增集群模块实际进入 wheel 且内容与构建时源码一致。
最新 wheel 的 308 个 Python 模块逐字节与冻结源码一致，清单与摘要见
`artifacts/scaling-wheel-final.json`。
macOS arm64 冻结 sidecar 已在独立构建环境生成并通过
`check-desktop-sidecar.py`：冻结自检、原生 dsh/rg、package-data、真实视频 prepare、
健康/页面/模板与退出检查全部通过。产物与日志位于
`artifacts/sidecar-enhancement-macos/`，没有覆盖原有 `desktop/build/backend`。
最新源码已重建并通过同一检查，证据及归档在 `artifacts/sidecar-enhancement-macos-final/`；
`backend-macos-arm64.zip` 与构建记录均带 SHA256，源码摘要与本轮 142 项后端验收一致。
此结果不等同于三平台安装包或远程完整工作流验收。
ruff、文档链接、文案镜像、资源版本参数、前端/桌面 JS 语法与 `git diff --check` 通过。
mypy 83 个模块通过。初次检查发现的 17 项错误已按真实协议、生成器返回类型与后端类型修复，
没有新增类型豁免或缩小检查范围。

## 尚未交付或验收

| 批次 | 尚未关闭的项目 |
|---|---|
| A | 全上下文生命周期、全部节点缓存的身份隔离、混合版本准入与升级演练 |
| B | Studio/index 持久执行链、周期去重键、外部调用幂等与未知结果恢复、真实 GPU 资源分组 |
| C | Studio 目录 manifest/CAS、全部权威文件、全部媒体使用期 pin、完整输出/临时空间预约、统一远端 pins/GC |
| D | 上传会话族、浏览器 endpoint/CORS、断点与配额、媒体完整协议矩阵 |
| E | 仍有 eligible 全列表和缺派生表时的全快照回退；候选/缓存字节/请求体/查询预算、原定容量/尾延迟与 Hit@5 门槛 |
| F | 分阶段/分对象续跑、完整媒体/向量闭包与秘密审计、业务激活 gate、运行中身份切换、目标写入后的全量逆向迁移与灾备 RPO/RTO 实测 |
| G | 四进程素材链路已验证；其他真实业务全链路、网关所有入口/断流/恢复、共享服务故障与双机演练仍待完成 |
| H | 三平台安全存储、远程检索/上传/SSE/创作/断网恢复、安装包与本地 sidecar 回归 |

本次没有双机、规定容量、真实模型或三平台安装环境的执行证据，整体交付保持待完成。
