# 磁盘清理（mindex clean）

`mindex clean` 扫描 data 目录中可安全回收的数据：**A 级无条件安全项每次都删；B 级可能要用
的按预定义策略（类别 + 文件生命期 / 保留数）过期删除；C 级只报告、永不自动删除**。
默认预览（只列出将删除的内容与字节数），`--apply` 才执行。过期阈值在配置 `clean` 节调整，
见 [config-reference.md](config-reference.md)。设计决策与评估记录见
[../plans/disk-cleanup-plan.md](../plans/disk-cleanup-plan.md)。

```bash
uv run mindex clean             # 预览
uv run mindex clean --apply     # 实际删除
uv run mindex clean --apply --vacuum  # 删除后收缩 metadata/chroma 库文件
uv run mindex clean --apply --days 0  # 清空全部按龄过期项（阈值临时替换为 0 天）
```

## 阈值临时覆盖（--days）

`--days N` 把本次运行的全部时间阈值临时替换为 N 天，不改配置文件：clean 节所有
`*_max_age_days`（含超期 building 版本）与助手附件 TTL（换算 N×24 小时）。N 比配置
小更激进（`--days 0` 即清空全部按龄过期项），比配置大更保守。**硬保护不受影响**：
现行 profile 检查点、活动与保留索引版本、引用中/当前 attempt、上传宽限期与 `uploads/`
用户原件。注意旧转写/分析检查点被删后重生成需 ASR 重听/LLM 重算。

输出为统一 JSON：`categories`（每类 `category/level/description/policy/item_count/bytes/items
（前 20 条）/kept/result/error/skipped`）、`notes`（C 级待调整项与建议）、`total`。索引版本与
runs 行的 bytes 无法精确统计，诚实输出 0 并在 policy 说明。

CLI 在 JSON 之前向 stdout 输出带时间戳的中文进度日志：入口与媒体引用集、逐规则扫描
（`i/N` 与耗时）、取锁/逐类别执行（含删除数）、VACUUM 开始/完成与心跳（`仍在 VACUUM …`，
默认每 5 秒一条，因 VACUUM 不触发 sqlite 进度回调故由后台线程计时）、总耗时汇总。长任务
可从最后一行判断当前阶段；单条规则扫描或删除大量文件时也可能暂时没有新输出。

## A 级：每次都删（无条件安全）

| 类别 | 范围 | 判定 |
|---|---|---|
| `temp_crash` | 已知前缀临时目录、根级及素材处理树内的 `*.partial` / `.partial.*` | 正常退出及时回收；持锁后清理崩溃残留；在线检索 `query-*` 另遵守 `temp_grace_hours` 并在删除前复核；保护源文件和媒体引用，不扫描 Studio 归档 |
| `ds_store` | 全目录树 `.DS_Store` | 纯 Finder 产物；C 级子树内不触碰 |
| `uploads_residue` | `sources/.uploads`、`subtitles/.uploads` | 上传中断残留；宽限 `temp_grace_hours` 内视为进行中保留 |
| `orphan_clips` / `orphan_thumbnails` | clips、thumbnails 下无引用文件 | 引用集 = segments 行 + **保留索引版本**快照（与 check_index 同口径）；孤儿判定不会误删旧版本快照引用的文件 |
| `orphan_context` | `context/*.mp4` 无对应 segment 行 | 文件名即 segment id；下次访问秒级重转码 |
| `person_frames` | 人物冗余展示帧 | 已确认镜头每个演员/角色保留真实分最高的 accepted 帧；未确认镜头保留证据帧与覆盖缓存中的帧；全部现存人物发布快照引用的帧保留；持锁后重算引用；旧库先执行 `persons migrate-frames` 压缩 |
| `orphan_person_png` | references/imports 下无引用 PNG | `person_references.digest` ∪ `person_imports.images[].digest` 引用计数；宽限 `temp_grace_hours` 内保留，执行时重读引用 |
| `orphan_person_features` | `person_features` 中源已删的缓存行 | `source_id` 非空且不在 sources 表（源删除接口的防御性兜底）；`source_id` 为空的 reference 行保留；换模型产生的旧空间缓存不自动清理（C 级 notes 报告总量，clean 离线拿不到人物空间身份） |
| `orphan_voices` | voices 下无 `voices.sample_path` 引用的文件 | 含 `pending-*` 失败残留与试听文件；宽限 `temp_grace_hours` 内保留，执行时重读引用 |
| `orphan_sources` | sources 根下未注册文件 | 不在 sources 表且超宽限期；已注册原片永不触碰 |
| `chroma_orphan_segments` | chroma 顶层不在 segments 表中的 UUID 目录 | chroma 的 delete_collection 只删 sqlite 行不回收 HNSW 目录；集合已不存在的目录即残留，字节数精确统计；apply 段持 index-write 锁重读引用后删除（正在写入的集合其 segments 行先于目录落盘，不会被误判）。版本删除（`delete_index_version`）自身也连带回收该版本的 segment 目录，本规则兜底历史残留 |

## B 级：策略过期（可能要用，超期才删）

| 类别 | 范围 | 保留规则 |
|---|---|---|
| `index_versions` | 非活动索引版本 | 保留活动 + 最近 `index_keep_versions`（默认 2）个 ready + **`index_building_max_age_days`（默认 7 天）内的 building**（超期视为中断残留并入删除，另失去同 spec 跨运行续跑）；删除 = chroma 集合（含 segment HNSW 目录）+ representations/index_members/index_versions 行 + `index_report.` 键 + 关联检索组合（其人物快照无其他引用则连带删）。删除后失去该版本 `indexes activate` 回退能力与增量发布向量复用 |
| `transcript_checkpoints` | `transcripts/<源>/<asr_key>/` | 当前 asr_key 永不删（即使过期）；旧指纹超 `transcripts_max_age_days` 删（重生成 = ASR 重听） |
| `analysis_checkpoints` | `analysis/<源>/<profile>/` | 当前 annotation_profile（分析配置 + 对白内容版本）永不删；旧 profile 超期删（重生成 = LLM 重算，最贵） |
| `dialogue_checkpoints` | `dialogue/<源>/<profile>/` | 保留集 = `channel_data` 现行 dialogue profile（processing 行天然保护进行中任务） |
| `context_cache` | `context/*.mp4` | 最近访问或修改时间超 `context_max_age_days`（默认 14 天）；执行时持文件锁再检查，保留锁文件 |
| `shot_media_cache` | `cache/shot-media/**`（含 probes/） | 最近访问或修改时间超 `shot_media_max_age_days`（默认 7 天）；可按内容指纹再生；执行时持租约锁再检查，播放/渲染中跳过，保留锁文件以避免并发进程锁住不同 inode |
| `studio_previews` | `studio/<任务>/previews/` | mtime 超期；预览按请求指纹命名可重生成 |
| `studio_attempts` | `studio/<任务>/attempts/` | **checkpoints.json 引用的与各步骤当前 attempt 一律保留**，其余树 mtime 超 `studio_attempt_max_age_days` 删（含 edit-* 编辑归档与同名结果 json）；execution/step running 的任务整体跳过 |
| `studio_exports` | `studio/<任务>/exports/*.zip` | mtime 超 `studio_export_max_age_days`；页面可重新打包 |
| `reports_exports` | `reports/` | 树 mtime 超 `reports_max_age_days`；可重新导出 |
| `subtitle_cache` | `subtitles/<源>/*.json(.lock/.partial)` | mtime 超期；**`uploads/` 用户原件永不触碰**；缓存键含原片字节与 mtime，按需重提取 |
| `agent_attachments` | `agent-attachments/<token>/`、`agent-attachments/.uploads/` | 过期助手附件：记录超过 `agent.attachment_ttl_hours`（默认 168 小时）即删除记录与目录；`agent.sqlite3` 中无记录的 token 目录与 `.uploads/` 上传暂存残留超过 `temp_grace_hours` 才删；`agent.sqlite3` 不存在时不会创建，其余表不触碰 |
| `runs_history` | runs 表 complete/failed 行 | `finished_at` 超 `runs_max_age_days` 且非 running；行删除不缩小库文件（见 VACUUM） |

索引保留集另包含 Studio 工作区/任务引用的内容索引与检索组合，以及运行中/排队中素材任务的
`progress.index_id`。失败、中断或取消任务在最近更新仍处于 `index_building_max_age_days` 保留期内
时也保护其索引，避免历史失败记录永久阻止回收。引用文件无法解析时保守保留全部版本；执行删除前重新核对。上述保留
版本的媒体同样受到保护。人物帧迁移保留旧发布快照所引用的原图，只更新当前展示引用。

播放媒体缓存同时有 5 GiB 容量上限：素材化时最多每分钟按最近访问顺序回收一次，跳过
持租约文件；命中已有缓存也更新访问时间。请求正常结束或客户端断开都会释放播放租约。

## C 级：只报告不删（notes 字段）

以下出现在每次输出的 `notes` 中（含大小与建议），clean 永不自动删除：

| 路径 | 说明 |
|---|---|
| `model-cache/` | 强制对齐 HF 模型缓存（1.7G），删除后下次 align 重新下载 |
| `studio-verification/`、`e2e/`、`verification/` | 一次性人工验证产物，归档或删除请人工决定 |
| `metadata.sqlite3.bak-*` | 人工数据库备份，确认无需回退后手动删除 |
| `person_features` | 人物特征缓存总量（按 kind 报行数）：换模型产生的旧空间缓存 clean 离线拿不到人物空间身份，无法安全判定，删除请人工决定；删除代价是人物重扫全量 GPU 推理 |
| VACUUM 建议 | 大量删除后可用 `mindex clean --apply --vacuum` 回收库文件空间（见下节）；此前为手动执行 `sqlite3 … 'VACUUM'` |

## 库文件收缩（--vacuum）

删除行不会缩小库文件。`mindex clean --apply --vacuum` 在全部删除执行完、锁释放**之后**，
对 `metadata.sqlite3` 与 `chroma/chroma.sqlite3` 依次执行 `VACUUM`（结果在顶层 `vacuum`
字段，含每库前后字节数）。VACUUM 需要无其他连接：库被占用（如 serve 运行中）时单库记
`error` 跳过并提示停服重试，整体不失败退出。只传 `--vacuum` 不传 `--apply` 时仅报告库
大小与建议。4G 级库收缩耗时数分钟（期间每 5 秒输出一条心跳日志），故不默认执行。

## 锁与并发

- **两段持锁**：先独占 `pipeline.lock`（与全部 ProcessingLock 用户互斥；`enforce_gpu_lock=false`
  时管线锁本为共享，唯有直接独占才安全）+ `index-write.lock`（短超时），执行管线与索引类；
  完全释放后再取 `studio.lock`（timeout=0，拿不到则 studio 三类标记 skipped），执行创作类。
  与素材删除（admin DELETE）、`mindex index` 同序，无持有-等待环。
- **锁冲突**：管线锁被占 → `有离线处理任务正在运行…` 退出码 1；索引锁被占 → 中文提示退出码 1。
- **serve 不持这些锁**：检索在线时 context/预览/字幕缓存靠 mtime 宽限（默认 ≥14 天，远大于
  秒级生成窗口）避免误删正在写入的文件；非活动 chroma 集合删除不影响活动集合检索。
  大规模清理前建议停 serve。
- 每次 apply 在 runs 表留一条 `clean` 审计记录。

## 永不清理（事实来源）

`sources/<hash>/` 原片、`metadata.sqlite3`、`deployments/`、`persons/references|imports`
（有引用时）、`subtitles/*/uploads/` 字幕原件、在用 voices 样本、`capabilities/`、`plans/`、
锁文件（0 字节）、创作任务的 `studio/<任务>/versions/` 版本产物与 `inputs/`（查询媒体、画面来源文件、参考文稿；删除方案后不再被引用的版本也保留）、`agent.sqlite3`（只按 `agent_attachments` 规则删附件记录）。素材级整源删除走 `/admin` 的删除接口（含 studio 引用保护）。
