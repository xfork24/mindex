# mindex clean 磁盘清理功能 + 数据目录评估分类

## Context

data/ 已 33G：无淘汰机制的缓存、换配置后永远不再命中的检查点、历史留档、孤儿文件持续堆积。本任务：
1. 评估每个子目录的数据性质与可删性（第一节，含用户要的分类）
2. 新增 `mindex clean`：**A 级无条件安全 → 每次都删；B 级可能要用 → 按预定义策略（类别+生命期/保留数）过期删；C 级 → 只记录不删**。默认预览，`--apply` 才执行

用户决策（2026-10-19）：默认预览+`--apply`；索引版本保留最近 2 个非活动+活动；persons/frames、model-cache、一次性验证产物、VACUUM **只进待调整清单**，调整后再清理。

## 一、目录评估与三级分类（实测 2026-10-19，data/ 共 33G）

### 分类标准
- **A 每次删**：删了无影响或秒级按需再生成，且不被任何状态引用
- **B 策略删**：可能要用，按「类别 + mtime 生命期 / 保留数」过期删
- **C 不清理**：事实来源或需人工决策（部分进「待调整清单」）

| 路径 | 大小 | 内容 | 等级 | 依据 |
|---|---|---|---|---|
| `sources/<hash>/` | 3.0G | 原片 | C | 事实来源，purge 也保留 |
| `sources/` 根未注册文件 | 12.8M | 孤儿 mp4 | B | 宽限 24h 后删（防上传中） |
| `sources/.uploads/`、`subtitles/.uploads/` | ~0 | 上传暂存 | A | finally 正常清，残留即崩 |
| `clips/` | 8.6G | 切片（segments.path 引用） | C；孤儿→A | 布局不可变，仅随 purge 整源删 |
| `thumbnails/` | 87M | 缩略图（segments.thumbnail） | C；孤儿→A | 删了前端 404，无懒重建 |
| `context/*.mp4` | 8.6M | 播放转码缓存 | B | 秒级再生成，mtime 过期 |
| `transcripts/<sid>/<asr_key>/` | ~0 | ASR 块检查点 | B | 旧指纹永不命中（重生成贵） |
| `analysis/<sid>/<profile>/` | 828K | LLM 分组检查点 | B | 同上（重生成最贵） |
| `dialogue/<sid>/<profile>/` | 3.2M | 对白检查点 | B | profile 随素材走，按 channel_data 保留集 |
| `subtitles/<sid>/*.json` | 56K | 字幕解析缓存 | B | `uploads/` 原件除外（→C） |
| `chroma/` | 1.2G | 18 索引版本，active 1 个 | B | 保留 active+最近 2 ready+全部 building |
| `studio/<job>/` | 3.7G | 任务目录 | 分层 B | previews/旧 attempt/旧 zip 过期删；任务本体 C（已有 DELETE 接口）；一任务 315 attempt 目录 1.4G 是重灾区 |
| `persons/frames/` | **12G** | 人物中间帧 | **C 待调整** | 删则证据图 404，先调整生成策略 |
| `persons/references|imports/` | 20M | 用户上传图 | C；孤儿→A | 引用计数范式 persons/persons.py:530 |
| `voices/` | 小 | 克隆样本 | B | 无行孤儿+`pending-*`+试听残留删 |
| `model-cache/` | 1.7G | 对齐模型缓存 | **C 待调整** | 删需重下 1.7G |
| `capabilities/`、`deployments/`、`plans/` | <10M | 基线/清单/计划 | C | 事实来源或极小 |
| `reports/` | 128K | 导出产物 | B | 可再导出，过期删 |
| `studio-verification/`、`e2e/`、`verification/` | 333M | 一次性验证 | **C 待调整**/B（verification 覆盖写快照） |
| `metadata.sqlite3` | 1.8G | 事实来源库 | C | 回收需 VACUUM（待调整） |
| `metadata.sqlite3.bak-*` | 10M | 人工备份 | **C 待调整** | 用户确认后手删 |
| 锁文件（pipeline.lock 等） | 0B | 持久锁 | C | 不占空间不删 |
| `.DS_Store`×58、崩溃残留 `asr-*` 等前缀目录、`*.partial` | ~小 | 垃圾 | A | 每次删 |

仓库根其它（clean 不涉及）：`models/` 17G 权重勿动；四个 `.venv*` 隔离环境勿动；`artifacts/` 人工决定；`.pytest_cache`/`.ruff_cache` 可随手删。

### 待调整清单（写入 docs/cleanup.md，clean 永不自动碰）
1. **persons/frames 12G**——先在人物处理侧调整（处理完成后仅留 evidence 引用帧），再纳入
2. **model-cache 1.7G**——与对齐环境预下载策略一起决策
3. **studio-verification/ 332M、e2e/、bak-voicefix 10M**——归档/删除人工决定
4. **VACUUM**——大量删除后停服执行；后续可做 `mindex clean --vacuum` 可选项（1.8G 库耗时、须独占）

## 二、实现设计

### 新模块 `backend/src/maintenance/clean.py`（≤750 行，超限先拆 `maintenance/clean_studio.py` 再拆 `maintenance/clean_index.py`）

```python
@dataclass(frozen=True) class CleanItem: path: str; bytes: int; detail: dict
@dataclass class CleanAction:
    category: str; level: str; description: str; policy: str
    items: list[CleanItem]; bytes: int; kept: dict
    execute: Callable[[], dict] | None   # 闭包捕获扫描结果，锁内调用

RuleFunc = Callable[[Config, Store, datetime], CleanAction | None]
RULES: list[RuleFunc] = [...]           # 数据驱动的类别列表
def clean_library(config, *, apply: bool, now=None) -> dict
```

- **扫描段（无锁）**：逐规则扫描，绝不删除；SQLite 走 WAL 快照
- **执行段（仅 apply）**：两段持锁（见锁方案），逐类调 `execute`；单类异常不断全局，记入该类 `error`
- 整体包 `with store.run("clean")` 留 runs 审计（shared/storage.py:149-166）
- 输出 JSON：`{applied, scanned_at, categories:[{category,level,description,policy,item_count,bytes,items(截20),kept,error,skipped}], notes:[C级待调整+字节数], total}`；bytes 无法精确的类别（索引版本、runs）诚实输出 0 并在 policy 注明

### A 级（每次删）
- **A1 temp_crash**：data 根一层前缀 `asr-`/`align-`/`embed-`/`dialogue-`/`query-`/`query-window-`/`query-image-` 目录 + `*.partial`。apply 段独占管线锁保证无在跑管线，无 mtime 门槛
- **A2 ds_store**：rglob `.DS_Store`，跳过 C 级子树与 chroma
- **A3 uploads_residue**：两个 `.uploads` 空目录删；文件 mtime < now-24h（temp_grace_hours）删，宽限内记 kept
- **A4 孤儿三件套**（clips/thumbnails/context，共用引用集）：
  ```
  keep_ids = {active} ∪ 最近 index_keep_versions 个 ready ∪ 全部非 ready(building)
  referenced = segments(path,thumbnail) ∪ keep_ids 各版本 index_members.snapshot 的 path/thumbnail
  ```
  `store.snapshots(vid)` 与 check_index（processing/indexing.py:254-259）同口径只取 complete=1。孤儿 = 盘上存在 ∉ referenced；context 按 stem ∉ segments.id。删后自底向上 rmdir 空父目录
- **A5 orphan_person_png**：references/imports 下 stem ∉ (person_references.digest ∪ person_imports.images[].digest)，复刻 persons/persons.py:530 引用计数
- **A6 orphan_voices**：voices/ 下文件 ∉ voices.sample_path（preview/pending 残留自然命中）
- **A7 orphan_sources**：sources 根常规文件 ∉ sources.path 且 mtime 超宽限

### B 级（策略过期；统一 `_tree_mtime`/`_expired` helper，阈值全来自 CleanConfig）
- **B1 index_versions**：候选 = ready ∧ 非 active ∧ created_at 排名 > keep_recent（keep_ids 计算与 A4 共用 `clean_index.retained_versions`，单一实现防口径漂移）。执行（持 index_write_lock）：①`PersistentClient(root/"chroma").delete_collection("idx-"+id)`（不存在则容错，maintenance/purge.py:96 先例）②单版本单事务：`DELETE FROM representations WHERE index_id=?` / `index_members` / `index_versions` / `settings WHERE key='index_report.'||id`（processing/channel_index.py:355）/ `search_releases WHERE content_id=?`（连带 person_versions 无引用则删，范式 persons/persons.py:276-281）。building 一律保留
- **B2/B3/B4 检查点**：transcripts 按 `profile != config.asr_key`；analysis 按 `profile != Pipeline(config).annotation_profile(sid)`（processing/pipeline.py:534-539）；dialogue 按 profile ∉ `SELECT profile FROM channel_data WHERE source_id=? AND channel='dialogue'`（processing 行天然保护进行中）。均要求树 mtime 超期
- **B5 context_cache**：mtime > context_max_age_days；顺带删同名 `.lock`（可非阻塞获取时）
- **B6 studio_previews**：`previews/**` 文件级 mtime 过期
- **B7 studio_attempts**：跳过 execution/steps running 的任务（recover_stale 语义）；keep = checkpoints.json 各项 path 的 attempt 目录 ∪ 各 step 当前 attempt 目录；其余树 mtime 超期删（checkpoints.json 损坏按空注册表处理，仅剩 step 最新保护）
- **B8 studio_exports**：exports/*.zip mtime 过期
- **B9 reports**、**B10 subtitle_cache**（严格排除 uploads/ 子目录）：mtime 过期
- **B11 runs_history**：`DELETE FROM runs WHERE status IN('complete','failed') AND finished_at IS NOT NULL AND finished_at < ?`（UTC）；running 一律不动

### 锁方案（已对照 shared/processing_lock.py / admin/admin.py:80-89 核实）
```
段一: SharedFileLock(pipeline.lock, exclusive=True) 非阻塞   ← enforce_gpu_lock=False 时管线锁本为共享，
       ↓                                                       唯有直接独占才能与所有 ProcessingLock 用户互斥
       FileLock(index-write.lock, timeout=10)
       执行 A1-A7 + B1-B5 + B9-B11 → 释放
段二（段一完全释放后）: FileLock(studio.lock, timeout=0)
       执行 B6-B8 → 释放（拿不到不失败，studio 三类标 skipped）
```
- 与 admin DELETE、CLI index 同序（先 pipeline.lock 后 index-write.lock），无持有-等待环
- studio.lock 与 index-write.lock 绝不同持（recall 步骤内部读索引，避免长期双锁）
- 锁失败中文文案：管线→`有离线处理任务正在运行，无法安全清理；请等待管线空闲后重试`（Exit 1）；index-write→`索引正在构建或发布，请稍后重试`；studio→类别 skipped
- serve 不持这些锁：context/previews/subtitles 靠 mtime 宽限（≥14 天 vs 秒级生成）；文档建议大规模清理前停 serve

### CleanConfig（shared/config.py 新增，StrictModel）
字段（默认值）：`index_keep_versions:2`、`temp_grace_hours:24`、`transcripts_max_age_days:30`、`analysis_max_age_days:30`、`dialogue_max_age_days:30`、`context_max_age_days:14`、`studio_preview_max_age_days:14`、`studio_attempt_max_age_days:30`、`studio_export_max_age_days:60`、`reports_max_age_days:60`、`subtitles_max_age_days:30`、`runs_max_age_days:90`。`Config` 加 `clean: CleanConfig = Field(default_factory=CleanConfig)`（shared/config.py:252-264 区域）。同步 `config.example.yaml`。不进 runtime 热覆盖（本地运维参数，settings/runtime_settings.py:4-7 白名单原则）

### CLI（cli.py，indexes 命令区后）
```python
@app.command()
def clean(ctx: typer.Context, apply: Annotated[bool, typer.Option("--apply", help="实际删除；缺省仅预览")] = False):
    """扫描可安全回收的磁盘数据；默认预览，加 --apply 才真正删除。"""
```
延迟导入、print_json 输出；锁冲突 `typer.echo(中文) + raise typer.Exit(1)`

## 三、测试（tests/test_clean.py，CliRunner + conftest `config` fixture，os.utime 伪造时间）

1. 预览不删任何文件（applied:false，文件全在）
2. apply 删崩溃残留+.DS_Store
3. context 过期阈值边界（旧删新留）
4. **索引版本保留**：5 版本（active+3 ready+1 building）→ 只删排名第 4 的 ready；三表行/`index_report.`键/chroma 集合/search_releases 连带全验证（真实 Chroma，建小集合）
5. **孤儿判定不误删 snapshot 引用**：索引快照引用 clip A 但 segments 行已删 → A 保留、真孤儿 C 删（硬闸门）
6. studio attempt：step 最新 + checkpoints 引用保留，超期旧 attempt 删
7. running 任务整体跳过
8. studio.lock 被持 → 三类 skipped、其余照常、exit 0
9. voices/person png 孤儿（引用计数）
10. runs 只删 complete/failed 且超期
11. pipeline.lock 冲突 → exit 1 + 中文提示
12. 未注册 source 宽限期内不删
13. subtitles 缓存删但 uploads/ 原件保留

## 四、文档

- `docs/cleanup.md`（新建）：三级分类总表、各类别判定与阈值、待调整清单（4 项）、锁与并发说明（建议停 serve）、向量复用损失说明
- `docs/cli-reference.md` 加 `mindex clean`；`docs/config-reference.md` 加 clean 节；`docs/README.md` 索引；`CLAUDE.md` 常用命令一行 + 存储布局尾注

## 五、实现顺序（5 个独立可绿的提交）

1. CleanConfig + config.example.yaml + config-reference（纯增量）
2. maintenance/clean.py 框架 + A 级 + CLI + 测试 1/2/9/11/12
3. B 级文件类（B2-B10）+ 测试 3/7/8/13
4. maintenance/clean_index.py（retained_versions/delete_index_version）+ B1/B11 + 测试 4/5/10
5. docs/cleanup.md + 其余文档

## 六、验证

- `uv run pytest tests/test_clean.py -q` 全绿；`uv run pytest -q` 全量回归；`uv run ruff check backend/src tests`
- 真实 data 预览跑一遍（不带 --apply）：核对 categories 与实际磁盘对账（预计可报收 ~1G 索引版本 + studio 旧 attempt 1G+ + 零散项），确认 persons/frames 等只出现在 notes
- 抽查预览列出的孤儿文件确实无引用后再人工 `--apply` 一次

## 风险与对策
- 孤儿误删（最高）：keep_ids/引用集单一实现，测试 5 硬闸门
- serve 在线并发：mtime 宽限 + 文档建议停 serve
- 大事务：索引版本逐版本事务，避免 1.8G WAL 库长事务
- bytes 不可精确项诚实输出 0，policy 注明
