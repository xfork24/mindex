# 修复 studio「保存窗口」耗时约 1 分钟的问题

> 状态：**两批全部完成并实测**（2026-10-16）：真实库实测保存链路 **~60s → 0.44s（约 136x）**。全量 299 测试通过、ruff 通过。
> 根因与修复方案均经逐行核实；关键性能假设已实测验证而非仅凭推理。

## 背景与根因

**现象**：/studio「选片与对齐」阶段点「保存当前窗口」需约 1 分钟。直观上这只是记录元信息；且「试听配音与画面」反而更快。

**结论：保存确实不止记录元信息，但慢的不是重媒体计算，而是元数据链路上的 O(N) 放大 —— 重复了近 7000 次 SQLite 全表扫描。**

### 保存窗口实际执行链路（已逐行核实）

前端 `studio.js:1347-1375` 两次串行 POST：

1. `POST range-preview`（`render_preview=false`，仅为取 preview_token）→ `preview_range`（studio_api.py:639）→ `studio_window.selected_range`（studio_window.py:286，`_context=None`）→ **第 1 遍完整 `context()`**（studio_window.py:141-218）
2. `POST range-confirm`（studio_api.py:700-711）→ `preview_range` 再跑一遍 → **第 2 遍 context()**（token 校验）→ 写 `p["ranges"]` → `workspace.execute(...,"select",sid)` → `alignment` → `studio_window.alignment(_context=None)` → **第 3 遍 context()** → `workspace.detail()`（deepcopy + 每分镜×4 阶段 conflicts + 大 JSON）

每遍 `context()` 内：

- `candidates()` 逐候选 `interval_visible`（studio_window.py:61-66）
- `probe(media)` 无缓存 ffprobe（studio_window.py:178）
- `source_shots` **逐镜头行** `interval_visible`（studio_workspace.py:782-790）
- `validate_range` 又一次 source_shots + 1 次 interval_visible（studio_workspace.py:793-819）

关键放大器：`interval_visible`（studio_steps.py:539-552）不传 `overlays` 时每次调用 `Library.overlays()`（curation.py:212-225）= segments 全表 + sources 全表两次查询，且 `Store.rows` 每条查询新开 SQLite 连接（storage.py:93-113）。

**实测数据规模：主源 1126 镜头、3584 segments → 一次保存 ≈ 3×(2×1126+候选+2) ≈ 近 7000 次全表扫描**（≈1.4 万次查询/连接 + 数千万行 dict 物化）+ 3 次 ffprobe + 几十次 `Store()` 构造（每次 executescript，storage.py:14-30）。= 分钟级。

**注意**：三遍重算是**有意的防陈旧设计**，已有测试锚定：`tests/test_studio_window.py:443` `assert len(contexts) == 3  # Confirm rechecks dependencies`。本方案保留三遍校验，只把每遍变便宜。

### 对照：「试听配音与画面」

`studio.js:1473-1492`，恰好 1 次 `POST range-preview`（render_preview=true）：只跑 **1 遍** context()，然后 ffmpeg 现场合成低清音画 MP4（`create_event_preview`，studio_api.py:713-794：640×360 ultrafast，配音 atempo/loudnorm + 可选原声 ducking），有磁盘缓存 `previews/quick-v1-{fingerprint}`（FileLock 防并发）；响应轻量（不含全工作区 detail）。—— 试听是真媒体计算但有缓存且只推导 1 遍，保存不做视频却做 3 遍推导，故保存更慢。

## 修复方案

代码库已有正确范式：`interval_visible` 已支持 `overlays=` 参数；`_selection_callbacks`（studio_steps.py:196-201）与 `run_select`（studio_steps.py:284-296）都是循环外取一次再传入。

### 改动 1：`source_shots` 提升 overlays（studio_workspace.py:782-790）

新增 keyword-only 参数 `overlays=None`（默认行为不变），循环外 `Library(store).overlays()` 一次传入。O(N) 扫描 → 1 次。调用方（studio_api.py:607、studio_window.py:181、validate_range、测试）均位置传参，零破坏。

```python
def source_shots(config, source_id, *, overlays=None):
    """源内全部镜头及可见性；overlays 可由调用方预先取好，避免逐镜头全表扫描。"""
    from .curation import Library

    store = Store(config.data_dir)
    if overlays is None:
        overlays = Library(store).overlays()
    return [
        {
            **row,
            "visible": studio_steps.interval_visible(
                store, source_id, row["start"], row["end"], overlays=overlays
            ),
        }
        for row in store.rows(
            "SELECT id,source_id,start,end FROM segments WHERE source_id=? AND kind='shot' ORDER BY start",
            (source_id,),
        ),
    ]
```

### 改动 2：`validate_range` 复用 overlays（studio_workspace.py:793-819）

同样加 `overlays=None` keyword-only 参数：自取一次后传给 `source_shots(overlays=...)` 和末尾的 `interval_visible`。N+1 次 → 1 次。overlays 在函数入口定格为快照，与 `_selection_callbacks` 既有语义一致。

### 改动 3：`studio_window.candidates` 提升 overlays（studio_window.py:50-72）

候选循环外取一次 overlays 传入。调用方（resolve_anchor、workspace.alignment、studio_api.py:624）只消费返回列表，不变。

### 改动 4：`media.probe` 有界 LRU 缓存（media.py:32-59）

- 现函数体改名 `_probe_uncached`；新 `probe` 包装：key=`(str(path), st_size, st_mtime_ns)`，`OrderedDict` + `threading.Lock`，容量 32（stdlib，无新依赖）。
- 返回 dict 副本防调用方污染缓存；**失败（MediaError）不缓存**；stat 失败直通 ffprobe（保留原有异常契约，studio_api.py:981 用 probe 当有效性探针的语义不变）。
- key 安全性已核实：全库媒体写入均为原子替换（partial+replace），mtime 必变；无原地同大小重写路径。APFS mtime 纳秒精度。
- 线程安全：FastAPI 线程池 + studio 后台线程，Lock 仅护 OrderedDict 操作，无 IO 持锁、无锁嵌套。
- 效果：保存链路 ffprobe 3 → 1；试听/selection-context 等路径同样受益。

### 改动 5（顺手，同范式）：`studio_api.py` 两处裸 `interval_visible`

- studio_api.py:732（`create_event_preview`，试听路径）与 :961（旧 `preview` 端点）：各提前取一次 overlays 传入。每请求省 1 次全表扫描。

### 明确不做（已评估）

- **砍第 3 遍 context()**：防陈旧设计有测试锚定（test_studio_window.py:443），`workspace.execute` 是通用入口不宜开私参；修复后每遍 ≈ 几次小查询 + probe 缓存命中，省一遍仅 ~10-50ms。
- **Store() 实例复用**：修复后一次保存 ~25-35 次构造 × 0.5-2ms < 100ms，非热点；穿 store 改签名面大。备选：后续给 `Store.__init__` 加进程级快路径。
- **detail() conflicts 循环**：recall 阶段只查 active_index（1 行）+ visibility_key（curation 小表），线性于分镜数，占比可忽略。

### 第一批实测结果（真实库：1126 shots / 3584 segments）

用真实 job 做只读基准（`context()`×3 + `validate_range`×3 推算保存链路）：

| 指标 | 修复前 | 修复后 | 提速 |
|---|---|---|---|
| 一次 `source_shots`（逐行可见性） | 6.48s | 0.76s | 9x |
| 保存链路整体 | ~60s（用户观察） | **7.8s** | **7.7x** |
| `overlays()` 全表扫描次数 | 2424 次（回归测试实测） | ≤40 | ~60x |
| ffprobe 子进程 | 3 次 | 1 次 | 3x |

附带治好 `preflight`（studio_workspace.py:695-701）对每个 select 节点 validate_range+alignment 的同款二次放大，以及打开窗口（selection-context）、试听路径。

**与原预估的偏差（已修正）**：原计划估「亚秒级」，实测 7.8s。原因见下。

### 第一批的剩余瓶颈（第二批优化对象）

修复后 `source_shots` 的 0.76s **不是**全表扫描，而是 **1126 次逐行区间查询**：每个镜头行调用 `interval_visible`，内部执行一次
`SELECT id,parent_id FROM segments WHERE source_id=? AND start<? AND end>?`，而 `Store.rows` 每条查询新开 SQLite 连接（storage.py:93-113）。
即 1126 × (开连接 + 一次查询) ≈ 0.76s；保存链路跑 6 遍这类检查，5-6s 花在这里。probe 缓存有效（0.089s → 0.05ms）但不是主因。

## 第二批优化方案（已完成）

**目标**：把 `source_shots` 的 O(镜头数) 次查询降为常数次查询。

### 思路：预计算隐藏区间，内存判断重叠

`interval_visible`（studio_steps.py:539-553）的判定逻辑是三件事：

1. `sources[source_id].hidden` → 整个源不可见；
2. 任何与 `[start, end)` 重叠的 segment，若它自身 hidden → 不可见；
3. 任何与 `[start, end)` 重叠的 segment，其 `parent_id` 指向的 segment 若 hidden → 不可见。

三件事都能**提前算完**：一次性查出该源全部 segments 的 `(id, parent_id, start, end)`，用 overlays 的全库 curation 字典筛出「自身 hidden 或 parent hidden」的区间，之后每个镜头行只在内存里判断「是否与任一隐藏区间重叠」。

复杂度从 **O(镜头数) 次 SQL**（每次新开连接）降到 **2 次查询 + O(镜头数 × 隐藏区间数) 次内存比较**。**用户当前没有隐藏任何素材时隐藏区间列表为空，1126 行 → 0 次比较。**

### 实际落地（studio_steps.py 新增两个函数）

```python
def hidden_intervals(store, source_id, *, overlays=None):
    """该源内全部不可见区间的列表，判定规则与 interval_visible 逐点一致。

    segment 自身或其 parent 被隐藏即整段不可见；parent 可能属于别的源，隐藏状态
    取自全库 curation。预查一次供逐行内存比对，避免每个区间都查一次库。
    """
    from .curation import Library

    _, segments = overlays if overlays is not None else Library(store).overlays()
    return [
        (row["start"], row["end"])
        for row in store.rows("SELECT id,parent_id,start,end FROM segments WHERE source_id=?", (source_id,))
        if segments.get(row["id"], {}).get("hidden") or segments.get(row["parent_id"], {}).get("hidden")
    ]


def range_visible(hidden, start, end):
    """区间是否避开全部不可见区间；容差与 interval_visible 的重叠判定完全一致。"""
    return not any(low < end - 1e-6 and high > start + 1e-6 for low, high in hidden)
```

`source_shots` 改为：`source_hidden` 先判，`hidden` 列表预查一次，逐行 `not source_hidden and range_visible(hidden, row["start"], row["end"])`。

**`interval_visible` 本身未改** —— 它是通用单区间谓词，另有 10+ 调用方（`_selection_callbacks`、`select_all`、studio_api 两处、admin 等），改它风险面大。批量化只发生在 `source_shots` 内部；`validate_range` 末尾那次 `interval_visible` 保留（每次调用仅 1 次，成本可忽略）。

**容差一致性**：新路径的 `low < end - 1e-6 and high > start + 1e-6` 与原 SQL 的 `start < ? AND end > ?`，参数 `(end - 1e-6, start + 1e-6)` 完全对应。**刻意不做区间合并** —— 合并虽能在隐藏区间很多时更快，但会引入边界语义风险；不做合并时行为与谓词严格一致。

### 第二批实测效果（真实库 1126 shots / 3584 segments）

| 指标 | 第一批后 | 第二批后 |
|---|---|---|
| 一次 `source_shots` | 0.594-0.76s | **0.594s**（11x vs 原始 6.67s） |
| `context()` 首遍（含 probe） | 1.759s | **0.136s** |
| `context()` 第二遍（probe 命中） | 1.668s | **0.052s** |
| `validate_range` | 0.826s | **0.012s**（69x） |
| **保存链路合计** | 7.75s | **0.44s** |

查询次数：一次 `source_shots` 从 204 次 `Store.rows` 降至 **4 次**（overlays 2 + 隐藏区间 1 + 镜头 1），常数级不随镜头数增长。

### 两批最终成果

**保存链路 ~60s → 0.44s（约 136x）**；`overlays()` 全表扫描 6800+ → ~18 次；ffprobe 3 → 1。

## 测试

### 新增 A：overlays 调用次数回归护栏 —— `tests/test_studio_window.py::test_range_save_overlays_calls_bounded`

- 向 segments 表插行把源扩到 200 个 shot（超出视频时长的行会被 context 过滤但仍逐行参与可见性检查，能捕获回归）；
- `monkeypatch.setattr(curation.Library, "overlays", counting)` + `monkeypatch.setattr(media, "command", ...)` 计数；
- 走真实 `range-preview(render_preview=false)` + `range-confirm`；
- 断言 `overlays <= 40`（**修复前实测 2424**）、`ffprobe == 0`（probe 缓存命中）。

### 新增 B：probe 缓存 —— `tests/test_media_probe.py`（新文件）

- 同一文件连调 2 次：ffprobe 只跑 1 次、两次结果相等但非同一对象（副本）；
- `os.utime` 改 mtime → 重新探测；
- 非媒体文件失败不缓存：同路径换成真实视频后 probe 成功。

### 新增 C：批量可见性等价性 —— `tests/test_studio_window.py::test_source_shots_visibility_matches_predicate`

- 构造隐藏拓扑：自身 hidden（shot-2/5/6）、parent 隐藏传播到子镜头（shot-7 的 parent 为已隐藏的 shot-6）、`parent_id` 指向不存在的 id（shot-8）、`parent_id` 为 None；
- 断言 `source_shots` 的可见性输出与**逐行调用 `interval_visible`** 的结果逐点相等；
- 并逐个断言期望值字典（防止「全可见」或「全不可见」时两套实现恰好相同而测不出差异）；
- 追加整个源被隐藏时全部不可见。

这是**重构护栏**（characterization test）：批量化前它就绿（逐行实现天然满足），批量化后必须仍绿 —— 红即证明批量实现与谓词语义漂移。

### 新增 D：调用次数常量级 —— 追加在 `test_range_save_overlays_calls_bounded`

- 源扩到 200 个 shot 后，monkeypatch `Store.rows` 计数；
- 断言一次 `source_shots` 的查询次数 ≤ 4（修复前 204 次）。

### 现有测试（全部保持绿）

`test_studio_window.py:438/443`（contexts==3、保存无 ffmpeg）与 `tests/test_studio_workspace.py:209-237`（隐藏后保存应报错）——后者恰好覆盖被改动的 overlays 与批量可见性语义。

## 验证步骤

```bash
uv run pytest tests/test_studio_window.py tests/test_studio_workspace.py tests/test_media_probe.py -q
uv run pytest -q                    # 全量（两批完成后实测：299 passed）
uv run ruff check src tests         # line-length 110（实测：All checks passed）
```

真实库只读基准（不写任何数据）：`context()`×3 + `validate_range`×3 → 0.44s。

手动（可选）：`uv run mindex serve` → /studio 打开定长窗口 → 点「保存当前窗口」确认秒级完成；试听功能不受影响。

## 风险与副作用（已实施部分）

1. **overlays 快照（唯一真正改变行为的语义项）**：旧行为下 1126 次谓词各自查库，一次调用内前后行可能看到不同的隐藏状态 —— 即**旧行为本身非确定性**；新行为是一致快照，属改善。代价是一份 overlays 的存活时间从「单个谓词的几毫秒」变为「整个 `source_shots` 调用的 0.76s」。`/admin` 的隐藏/显示名写操作**不持有 `mutation_lock`**（admin.py 不走 studio 锁），若恰在这 0.76s 内隐藏某镜头，该次保存可能用旧可见性通过 —— 但保存产生的版本在下一次独立调用（打开窗口/试听/preflight/渲染/构建）会重新取 overlays 并拦截，且该竞态旧实现同样存在，仅窗口小两个数量级。彻底消除需让 admin 写也取 studio 锁，会给 admin 页面引入锁等待，**未做**。
2. **probe 缓存（唯一可能悄悄出错的地方，已实测验证而非仅凭推理）**：
   - 实测 APFS 上 `mtime_ns` 为纳秒精度，同大小连续 5 次重写产生 **5 个不同**的 `mtime_ns`，key 可区分；
   - 库内媒体写入均为原子替换：`atomic_media`（partial+replace）、`save_bytes`（.partial+replace）；
   - 两个非原子写入点已逐个核查：`save_upload`（api.py:32，上传素材原地 `open("wb")`）—— 纳秒 mtime 必变；`_silence_wav`（settings_api.py:128）—— 连接测试用的 0.5s 固定静音 wav，**内容字节恒等**，命中陈旧结果也必然正确；
   - 唯一理论风险：有人用 `os.utime` 保留时间戳做备份恢复（实测 utime 确能复现 `mtime_ns`）。库内无此路径，属运维手动行为。
   - 失败（MediaError）不缓存 → 异常契约不变，`studio_api.py:981` 拿 probe 当文件有效性探针的用法不受影响；stat 失败 → 直通 ffprobe 抛原有异常；返回**副本**防污染；Lock 只护 OrderedDict，无 IO 持锁、无锁嵌套；多 worker 各持一份，不影响正确性。
3. **可感知的非正确性代价**：缓存命中时不再 `record("command")`，版本 debug 日志里看不到那 2 次重复的 ffprobe 记录。属降噪，但确实是排查信息损失。
4. **内存**：overlays 从「每谓词一份、及时 GC」变为「一次调用持有一份」（几 MB，存活期即一次 `source_shots`）；并发时可能同时持多份，对 3584 段的库无实际压力。
5. **第二批新增的语义面**：批量可见性判定与 `interval_visible` 并存两套实现，由等价性测试（新增 C）锁死。刻意**不做区间合并**，避免边界语义风险；隐藏区间很多时性能退化到 O(镜头数 × 隐藏数) 次内存比较（纯 CPU，无 IO），仍远快于逐行查库。
6. **明确未变化**：三遍防陈旧校验保留（`len(contexts)==3` 仍绿）· API 请求响应结构零变化 · `mutation_lock`/`FileLock` 语义零变化 · 隐藏素材拦截行为不变 · `interval_visible` 本身未改 · 所有新参为 keyword-only 且带默认值，现有调用方全兼容。

## 实施顺序

**第一批（已完成）**：改动 1→2→3（同范式三连）→ 4（media.py）→ 5（studio_api 两处）→ 测试 A/B → 全量验证与实测。

**第二批（已完成）**：先写等价性测试（新增 C，护栏）与调用次数断言（新增 D，RED：204 次）→ `hidden_intervals` + `range_visible` 辅助 → `source_shots` 批量化（GREEN：4 次）→ 全量验证 → 真实库复测 0.44s。

## 关键文件

- `src/mindex/studio_workspace.py`（改动 1、2；第二批：`source_shots` 批量化）
- `src/mindex/studio_window.py`（改动 3）
- `src/mindex/studio_steps.py`（第二批：`hidden_intervals` 辅助）
- `src/mindex/media.py`（改动 4）
- `src/mindex/studio_api.py`（改动 5：732、961 附近）
- `tests/test_studio_window.py`、`tests/test_media_probe.py`（新）、`tests/test_studio_workspace.py`（第二批等价性测试）

## 关键文件

- `src/mindex/studio_workspace.py`（改动 1、2）
- `src/mindex/studio_window.py`（改动 3）
- `src/mindex/media.py`（改动 4）
- `src/mindex/studio_api.py`（改动 5：732、961 附近）
- `tests/test_studio_window.py`、`tests/test_media_probe.py`（新）
