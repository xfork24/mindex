# 索引版本与空间身份

索引是**不可变的版本化产物**：任何影响向量含义的变化都产生新索引，从不原地覆盖。这套机制的目标是——任何时候活动索引里的向量都能追溯到唯一确定的模型身份、预处理与分析配置。

## 索引版本怎么生成

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
2. 检查通过 → 状态 `ready`。
3. `--activate` 或 `mindex indexes activate ID` → 再查一遍 → SQLite 事务内把 `settings.active` 指向该索引。
4. `mindex check [--index-id]` 可随时复查，问题列出且退出码 1。

**回退 = 先切回旧模型服务/配置，再 `indexes activate 旧索引ID`。** 只激活旧索引而查询端还用新模型会被空间身份校验拒绝（503）。

## 空间身份与跨空间禁令

- 自定义 `mindex` 嵌入服务在 `/health` 返回 space，客户端逐字段核对（model/dims/quantization/normalized/fps/max_frames/max_pixels/revision/implementation），每次向量响应带 `space_id`（= `fingerprint(space)`），不一致即拒绝。
- `llama_cpp` 后端没有服务端 space：身份来自**部署清单**（`e2e pin` 固定权重/mmproj 哈希 + 量化 + 池化 + 可观察运行指纹），每次请求前后重核运行身份；适配器把这套身份组装成等价 space。
- `tokenhub` 后端同样没有服务端 space：身份纯由配置构造（model/revision/quantization/dimensions + video 预处理 + `implementation="tokenhub-multimodal-1"`），直接进入索引指纹；换模型名或维度即新空间。
- 在线查询时活动索引的 `spec.space` 必须等于当前嵌入服务身份，`spec.preprocessing` 必须等于当前 video 配置，否则 503。**禁止跨空间比较向量**；同维度不等于同空间。

## 能力门槛（llama_cpp）

`llama_cpp` 后端在建正式索引前强制通过**六片段能力门槛**（`e2e phase smoke`）：六个不同真实片段的文字/视频嵌入须满足有限值、正确维度、单位范数、内容改变向量、重复稳定、身份稳定。通过记录保存在 `data/capabilities/<space_id>.json`；`require_gate()` 在建库时核验，未通过或身份不符即拒绝。门槛失败重跑会显式撤销旧记录。

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
