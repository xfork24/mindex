# 磁盘清理（mindex clean）

`mindex clean` 扫描 data 目录中可安全回收的数据：**A 级无条件安全项每次都删；B 级可能要用
的按预定义策略（类别 + mtime 生命期 / 保留数）过期删除；C 级只报告、永不自动删除**。
默认预览（只列出将删除的内容与字节数），`--apply` 才执行。过期阈值在配置 `clean` 节调整，
见 [config-reference.md](config-reference.md)。设计决策与评估记录见
[../plans/MINDEX_CLEAN_PLAN.md](../plans/MINDEX_CLEAN_PLAN.md)。

```bash
uv run mindex clean             # 预览
uv run mindex clean --apply     # 实际删除
```

输出为统一 JSON：`categories`（每类 `category/level/description/policy/item_count/bytes/items
（前 20 条）/kept/result/error/skipped`）、`notes`（C 级待调整项与建议）、`total`。索引版本与
runs 行的 bytes 无法精确统计，诚实输出 0 并在 policy 说明。

## A 级：每次都删（无条件安全）

| 类别 | 范围 | 判定 |
|---|---|---|
| `temp_crash` | `asr-*`/`align-*`/`embed-*`/`dialogue-*`/`query-*` 等前缀临时目录、根级 `*.partial` | TemporaryDirectory 正常退出即消失，存在即崩溃残留；无 mtime 门槛 |
| `ds_store` | 全目录树 `.DS_Store` | 纯 Finder 产物；C 级子树内不触碰 |
| `uploads_residue` | `sources/.uploads`、`subtitles/.uploads` | 上传中断残留；宽限 `temp_grace_hours` 内视为进行中保留 |
| `orphan_clips` / `orphan_thumbnails` | clips、thumbnails 下无引用文件 | 引用集 = segments 行 + **保留索引版本**快照（与 check_index 同口径）；孤儿判定不会误删旧版本快照引用的文件 |
| `orphan_context` | `context/*.mp4` 无对应 segment 行 | 文件名即 segment id；下次访问秒级重转码 |
| `orphan_person_png` | references/imports 下无引用 PNG | `person_references.digest` ∪ `person_imports.images[].digest` 引用计数（与删除接口同口径） |
| `orphan_voices` | voices 下无 `voices.sample_path` 引用的文件 | 含 `pending-*` 失败残留与试听文件（可再生） |
| `orphan_sources` | sources 根下未注册文件 | 不在 sources 表且超宽限期；已注册原片永不触碰 |

## B 级：策略过期（可能要用，超期才删）

| 类别 | 范围 | 保留规则 |
|---|---|---|
| `index_versions` | 非活动索引版本 | 保留活动 + 最近 `index_keep_versions`（默认 2）个 ready + **全部 building**；删除 = chroma 集合 + representations/index_members/index_versions 行 + `index_report.` 键 + 关联检索组合（其人物快照无其他引用则连带删）。删除后失去该版本 `indexes activate` 回退能力与增量发布向量复用 |
| `transcript_checkpoints` | `transcripts/<源>/<asr_key>/` | 当前 asr_key 永不删（即使过期）；旧指纹超 `transcripts_max_age_days` 删（重生成 = ASR 重听） |
| `analysis_checkpoints` | `analysis/<源>/<profile>/` | 当前 annotation_profile（分析配置 + 对白内容版本）永不删；旧 profile 超期删（重生成 = LLM 重算，最贵） |
| `dialogue_checkpoints` | `dialogue/<源>/<profile>/` | 保留集 = `channel_data` 现行 dialogue profile（processing 行天然保护进行中任务） |
| `context_cache` | `context/*.mp4` | mtime 超 `context_max_age_days`（默认 14 天）；写入中（锁被持）跳过 |
| `studio_previews` | `studio/<任务>/previews/` | mtime 超期；预览按请求指纹命名可重生成 |
| `studio_attempts` | `studio/<任务>/attempts/` | **checkpoints.json 引用的与各步骤当前 attempt 一律保留**，其余树 mtime 超 `studio_attempt_max_age_days` 删（含 edit-* 编辑归档与同名结果 json）；execution/step running 的任务整体跳过 |
| `studio_exports` | `studio/<任务>/exports/*.zip` | mtime 超 `studio_export_max_age_days`；页面可重新打包 |
| `reports_exports` | `reports/` | 树 mtime 超 `reports_max_age_days`；可重新导出 |
| `subtitle_cache` | `subtitles/<源>/*.json(.lock/.partial)` | mtime 超期；**`uploads/` 用户原件永不触碰**；缓存键含原片字节与 mtime，按需重提取 |
| `runs_history` | runs 表 complete/failed 行 | `finished_at` 超 `runs_max_age_days` 且非 running；行删除不缩小库文件（见 VACUUM） |

## C 级：只报告不删（notes 字段）

以下出现在每次输出的 `notes` 中（含大小与建议），clean 永不自动删除：

| 路径 | 说明 |
|---|---|
| `persons/frames/` | 人物处理中间帧（实测单源可达 12G）。证据图 API 仍引用，删除即 404；应先调整人物处理侧的帧保留策略，再纳入清理 |
| `model-cache/` | 强制对齐 HF 模型缓存（1.7G），删除后下次 align 重新下载 |
| `studio-verification/`、`e2e/`、`verification/` | 一次性人工验证产物，归档或删除请人工决定 |
| `metadata.sqlite3.bak-*` | 人工数据库备份，确认无需回退后手动删除 |
| VACUUM 建议 | 大量删除后可停服执行 `sqlite3 metadata.sqlite3 'VACUUM'` 回收库文件空间（需独占库，1.8G 库耗时较长）；`person_features` 等大表的回收依赖人物侧调整 |

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
锁文件（0 字节）。素材级整源删除走 `/admin` 的删除接口（含 studio 引用保护）。
