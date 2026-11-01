# 共享基础设施（shared/）

`backend/src/shared/` 里跨功能复用的底层机制：文件锁、SQLite 迁移、后台任务三件套。各功能文档只讲用法，本页讲清这些机制的契约。

## 数据目录文件锁（shared/locks.py）

数据目录的并发用**具名文件锁**（位于 `data_dir/locks/`）互斥，并规定规范加锁顺序：

```
pipeline → index-write → library → register → studio → topics
```

- 一次需要多把锁时用 `acquire_ordered`，名称必须按 `ORDER` **不递减**排列；逆序直接 `ValueError` 拒绝，避免死锁。
- 平台差异收口在此：POSIX 用 flock，Windows 用 LockFileEx。
- 业务里的锁实例：`data/pipeline.lock`（离线管线互斥）、studio 全局执行锁、素材库锁、topics 锁、取消哨兵目录 `locks/cancel/`（见下）。磁盘清理与锁的关系见[磁盘清理](cleanup.md)。

## SQLite 版本化迁移（shared/migrations.py）

库结构变更走 `PRAGMA user_version` 加有序步骤列表，**不散写 ALTER**：

- `steps[i]` 把库从版本 `i` 升到 `i + 1`；每一步与对应 `user_version` 递增在**同一个 `BEGIN IMMEDIATE` 事务**内提交，中断后重跑从失败步骤继续。
- 版本 1 是引入机制时的完整 schema，必须对 `user_version=0` 的旧库幂等（`IF NOT EXISTS`、补列先查 `PRAGMA table_info`）。
- **只追加步骤，不修改已发布步骤**；步骤内不用 `executescript`（会先提交当前事务），多语句用 `execute_script`。
- 两个库各自维护步骤表：metadata 库在 `shared/storage.py` 的 `METADATA_STEPS`，智能助手库 `agent.sqlite3` 在 `agent/schema.py` 的 `STEPS`。

## 后台任务三件套

素材、对白、人物处理与选题运行共用同一套后台任务机制（`/admin` 工作台与页面轮询都建立在它上面）：

- **`background_tasks.py`（runner 生命周期）** — 子类声明任务表与中文提示，实现 `lock`、`restore`、`execute` 钩子；基类负责查询、重试、取消、启动线程，以及终态写入、取消处理、异常日志、锁释放与线程表清理。GPU 互斥经 `shared/processing_lock.py`。
- **`task_progress.py`（持久化进度）** — 进度与处理阶段写入任务表，重启后可恢复显示；`published`/`indexed` 为提交后阶段（此后取消无回退意义）；`queued`/`running` 可取消，`failed`/`interrupted`/`cancelled` 可重试并复用已有检查点。
- **`task_cancel.py`（取消标记）** — 进程内 Event 与 `data_dir/locks/cancel/<key>` 哨兵文件**双写**，跨进程生效；`TaskCancelled` 异常不继承 `ValueError`，避免被通用参数错误处理吞掉。创作构建的取消哨兵（`builds/<build_id>.cancel`）同一思路，见[创作工作流](studio.md#构建与人工控制)。

## 其他常驻模块

- `shared/shot_media.py` — 统一 shot 媒体服务（时间基准转换、截图/抽帧、PCM 提取、跨镜重建），业务读取一律经它。
- `shared/web_assets.py` / `shared/media.py` — 前端资源路由与 ffmpeg/ffprobe 定位（`MINDEX_FFMPEG`/`MINDEX_FFPROBE`）。
- `shared/web_auth.py` — 访问鉴权，见 [Web/API 访问鉴权](web-auth.md)。
- `shared/i18n.py` — 后端消息反向映射，见 [界面文案与多语言](i18n.md)。
