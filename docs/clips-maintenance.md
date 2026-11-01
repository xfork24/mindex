# 旧切片维护（mindex clips）

历史布局里每个切片是物理文件；现行模型里 **shot 是唯一业务媒体来源**：业务读取一律经 `shared/shot_media.py`（时间基准转换、截图/抽帧、PCM 提取、跨镜 H.264/AAC 重建），不直读旧切片路径。`mindex clips` 子命令负责旧切片的物理迁移、恢复与清退，设计动机见 [shot 媒体改造计划](../plans/shot-media-refactor-plan.md)。

## 命令

所有命令**默认只读预览，`--apply` 才实际写入或删除**；配置用 YAML 原配置（不含运行时覆盖）。

| 命令 | 作用 |
|---|---|
| `mindex clips migrate [--source ID] [--apply] [--verify-media]` | 把旧切片迁移为 shot 媒体引用；`--source` 限定素材（是素材 ID，不是文件路径）；`--verify-media` 与 `--apply` 配合验证迁移媒体，仅预览时不执行 |
| `mindex clips materialize --all` | 恢复全部可恢复的旧媒体路径实体（必须显式 `--all`） |
| `mindex clips verify-archives [--source ID]` | 校验外部字幕归档（SHA256） |
| `mindex clips repair-boundaries [--source ID] [--audit JSON] [--apply]` | 修复切片边界；可读取已有审计 JSON，省略则现场诊断 |
| `mindex clips prune <迁移报告 JSON> [--apply]` | 按已通过验证的迁移报告清退旧切片文件；报告路径为位置参数 |

## 数据模型

- `media_references` 表：媒体路径 → 素材/时间区间 的引用登记，shot 媒体服务据此定位与复验。
- `clip_migration_files` 表：迁移过程中的文件依赖与摘要（`dependency_digest`），供 `prune` 判定哪些旧文件可以安全清退。
- 外部字幕先归档再迁原片：`processing/subtitle_uploads.py` 登记不可变上传，`processing/subtitle_archive.py` 做 SHA256 归档校验（`verify-archives`）。

## 与磁盘清理的分工

`mindex clips` 处理**结构迁移与显式清退**（需要迁移报告与人工确认）；`mindex clean` 处理**常规磁盘回收**（三级分类、按保留策略过期删除），见[磁盘清理](cleanup.md)。先 `clips migrate --apply` 再 `clean` 才能回收旧切片占用的空间。
