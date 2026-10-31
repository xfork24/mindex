# 索引版本与空间身份

索引是**不可变的版本化产物**：任何影响向量含义的变化都产生新索引，从不原地覆盖。这套机制的目标是——任何时候活动索引里的向量都能追溯到唯一确定的模型身份、预处理与分析配置。

新版 `auto` / `visual` 使用 format 2，独立记录实际渠道表示清单，增量保留其他素材和渠道，并按模型空间与输入匹配复制兼容向量。详见 [三渠道说明](recall-enhancement.md)。以下 format 1 结构仍支持查询与回退。

## 旧版索引版本怎么生成

`IndexBuilder.build()` 计算索引规格 spec：

```python
spec = {
    "space": 嵌入服务身份,          # model/revision/quantization/dims/normalized/fps/max_frames/max_pixels/implementation...
    "preprocessing": video 配置,     # 切分窗口与采样参数
    "analysis_key": 分析指纹,        # enriched 模式；visual 为 None
    "mode": "visual" | "enriched",
    "manifest": 全部片段快照的指纹,  # 成员与描述的不可变清单
    "format": 1,
}
index_id = fingerprint(spec)        # spec 的 SHA256 前 24 位
```

同一 spec 重跑得到同一 index_id：`INSERT OR IGNORE` 幂等，已写入的向量（Chroma 里的表示 ID）直接复用，逐表示检查点避免重复嵌入。**换模型、量化、维度、预处理或分析配置 → 不同 spec → 新索引**。

## 表示（representations）

每个片段在索引中可有多类表示：

- **video**：物理媒体嵌入。≤6 秒整段一次；更长镜头/场景在嵌入时拆 4s/3s 窗口，每窗一个向量（同一片段多窗口，检索时同路只计最佳）。
- **description / style / dialogue**：三个文本视图分别嵌入；空文本不生成向量。visual 模式没有文本视图。

Chroma collection 名为 `idx-<index_id>`，`embedding_function=None`（显式向量，禁用自动嵌入），cosine 空间。SQLite `representations` 表是向量 ID 的登记簿；**Chroma 先写、SQLite 后记**，崩溃后靠对账修复。

## 发布、检查与激活

1. `build` 完成时先跑 `check_index`：向量 ID 与 representations 完全对账、每个成员 complete、快照里的媒体文件真实存在、每片段表示齐全（video + 其视图）。
2. 检查通过并再次核验模型空间与部署门槛 → 状态 `ready`。
3. `--activate` 或 `mindex indexes activate ID` → 再查一遍 → SQLite 事务内把 `settings.active` 指向该索引。
4. `mindex check [--index-id]` 可随时复查，问题列出且退出码 1。

Web 与智能助手使用同样约束的 `/api/indexes`（`admin/indexes.py`）：`GET /api/indexes` 列出全部版本（新到旧）、当前活动版本与检索组合；`GET /api/indexes/{id}/check` 返回 `{index_id, ok, issues}`；`POST /api/indexes/{id}/activate` 持处理锁与索引写锁，依次检查一致性、嵌入空间与能力门槛、视频预处理，全部通过才激活并在后台预热检索（检查有问题 422，空间或预处理不符 503，锁超时 409，失败时活动索引不变）。

**回退 = 先切回旧模型服务/配置，再 `indexes activate 旧索引ID`。** 只激活旧索引而查询端还用新模型会被空间身份校验拒绝（503）。

## chroma HNSW 落盘阈值与向量孤儿自愈

chroma 的元数据段每笔提交，HNSW 向量段仅在累计 `sync_threshold`（默认 1000）个元素时才把 id_map 与索引写入磁盘。构建收尾不冲阈值时，尾部向量只存在于内存与 WAL；进程重启或跨进程访问后可能变成「元数据有、HNSW 无」的孤儿，向量复用时 chroma 报 `Error executing plan: Internal error: Error finding id`（上游 chroma-core/chroma#6975）。两层防护：

- `VectorIndex.flush()`：每次构建收尾幂等重写既有向量冲过阈值，保证新版本完整落盘；
- `VectorIndex.vector()`：复用读取遇孤儿 id 时降级为 None，由上层重新嵌入写入新版本（不吞其他错误）。

`check_index` 只对账 chroma 元数据段与 representations 表，看不见 HNSW 段内部状态，孤儿不会阻断发布，只能靠上述机制兜底。

## 空间身份与跨空间禁令

- 自定义 `mindex` 嵌入服务在 `/health` 返回 space，客户端逐字段核对（model/dims/quantization/normalized/fps/max_frames/max_pixels/revision/implementation），每次向量响应带 `space_id`（= `fingerprint(space)`），不一致即拒绝。
- `llama_cpp` 后端没有服务端 space：身份来自**部署清单**（`e2e pin` 固定权重/mmproj 哈希 + 量化 + 池化 + 可观察运行指纹），每次请求前后重核运行身份；适配器把这套身份组装成等价 space。
- `tokenhub` 后端同样没有服务端 space：身份纯由配置构造（model/revision/quantization/dimensions + video 预处理 + `implementation="tokenhub-multimodal-1"`），直接进入索引指纹；换模型名或维度即新空间。
- 在线查询时活动索引的 `spec.space` 必须等于当前嵌入服务身份，`spec.preprocessing` 必须等于当前 video 配置，否则 503。**禁止跨空间比较向量**；同维度不等于同空间。

## 能力门槛（llama_cpp）

`llama_cpp` 的 schema 2 在建索引、检索及普通推理前均核验当前部署已通过：先满足六文本/六视频的结构 smoke，再与同空间的固定基线逐条比较余弦相似度 ≥0.999。基线原视频、文本、输入哈希、预处理、原始向量和部署记录保存在 `data/capabilities/v2/<space_id>/baseline.json` 及样本目录，验证记录在 `deployments/<deployment_id>.json`。重验先撤销当前部署旧通过记录，基线保持不可变。缺失/损坏拒绝，兼容失败必须显式升级数值实现版本、建立新基线并重建。

v2 空间由实际 weights/mmproj SHA256、维度、池化、归一化、输入模板、视频/图片预处理及显式适配器语义版本组成。路径、别名、地址、构建和整份源码哈希均不进入稳定空间。schema 1 仍读取旧格式并执行旧运行指纹校验，旧记录不会自动改写。详见 [macOS 迁移](macos-model-migration.md)。

## 索引冻结快照

`index_members.snapshot` 保存每个片段入库时的完整状态（路径、时间、描述、对白、视图）。后续分析更新、改名、隐藏都**不改快照**：

- 显示名/隐藏等整理信息是运行时叠加层（`curation` 表），见 [admin.md](admin.md)；hidden 在召回前过滤，不需要重建索引。
- 重建相同索引（同 spec）不重复嵌入，整理操作不影响索引 ID 与向量。

## 常用操作

评测和浏览器验证可固定到一个 ready 版本，避免改变当前活动库：

```bash
uv run mindex serve --port 8001 --index-id TEST_INDEX_ID
uv run mindex evaluate eval/queries.jsonl --index-id TEST_INDEX_ID --split test --output data/test-report.json
```

固定版本仍执行相同的模型空间校验；媒体、缩略图及上下文也取该版本的快照。

```bash
uv run mindex index --mode visual --source SOURCE_ID --activate   # 建并发布
uv run mindex indexes list                                        # 查看版本与活动索引
uv run mindex check --index-id 新索引ID                            # 复查
uv run mindex indexes activate 新索引ID                            # 发布
uv run mindex indexes activate 旧索引ID                            # 回退（先切回旧模型配置）
```

## 默认发布粒度与临时推理媒体

`mindex index` 默认同步已有的 moment、shot、scene 及其有效画面、对白、描述；
旧 moment-only 索引再次发布也会补齐。`--kind` 可重复指定，显式限制本次更新粒度。
`--mode visual` 默认 moment、shot，不生成场景。单素材更新保留其他素材和未选择的渠道。
工作台分镜、对白与 LLM 分析卡片的“更新shot/moment嵌入”及任务完成后的自动发布更新 moment 与 shot（对白渠道同样适用）。
“更新scene场景嵌入”单独更新已有 scene，不重复运行 LLM 分析；三张渠道卡片均有这两个按钮，两组覆盖数与发布任务独立显示，
顶部“检索（shot/moment）”就绪行以该渠道 moment 与 shot 均已发布为准。
CLI `mindex index` 仍默认覆盖全部已有粒度。
发布不会运行 ASR、场景生成或媒体修复。

工作台嵌入覆盖率按实际向量输入的缓存键判定，包含嵌入空间、渠道及输入文本（画面使用时间范围与预处理参数）。
当前索引已包含同一片段、同一渠道的完整匹配嵌入时，即视为已更新；仅分析来源、模型记录或输入版本变化不会误报未发布。
其他兼容索引中的匹配嵌入可以复用，但尚未纳入当前索引时仍显示待发布。分析输入指纹继续用于处理溯源。

索引推理直接读取已有完整镜头；子区间、跨镜头场景与长镜头窗口按绝对时间从镜头读取，
仅在系统临时目录裁剪或拼接，成功及异常均清理。依赖检查只读取文件，不生成缩略图、
持久视频或探测缓存。缺失镜头时报素材 ID，检查通过后才允许激活，旧版本保留用于回退。

人物库允许重复发布相同证据，相同内容索引与人物快照复用同一发布记录。当前生效的发布组合必须先回退到其他组合才能删除。人物发布、回退和删除不获取内容索引写入锁，仅通过短 SQLite 事务保证一致性；GPU 处理锁仍受配置开关控制，关闭时可与其他处理任务并行。设置页最底部的“索引版本”区域显示当前生效的检索发布版、内容索引与人物快照 ID（正文取前 8 位，悬停显示完整值），列出最近 30 条检索组合，并提供发布、恢复与删除，操作进度和结果就地显示。

创作召回版本的签名分别记录“内容索引 ID”（`index_id`）与“检索发布版 ID”（`person_release`）。检索发布版 = `fingerprint([内容索引, 人物快照])`，所以内容索引或人物快照任一变化（包括只激活了另一个内容索引、人物未重新发布），检索发布版 ID 都会变化，已有召回节点随之提示“选用版本兼容性”不一致；恢复到原组合即可重新匹配。
