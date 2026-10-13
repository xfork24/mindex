# CLI 完全参考

安装后有两个入口：`mindex`（本机客户端）和 `mindex-model-server`（GPU 侧服务，见文末）。所有命令的中文帮助可用 `--help` 查看。

## 全局规则

- `--config` / `-c` **必须放在子命令之前**：`uv run mindex --config profiles/int8.yaml index`。也可设环境变量 `MINDEX_CONFIG`（默认读 `config.local.yaml`）。
- 配置模型 strict（`extra="forbid"`），未知键直接校验失败。
- `data_dir` 相对路径按**配置文件所在目录**解析；`deployment_manifest` 同理。
- 离线阶段命令之间以 `data/pipeline.lock` 互斥，并行第二个实例立即失败。

## 离线处理

### `mindex prepare VIDEOS...`

导入原视频、检测镜头并实际切片（= `detect` + `split`，纯本地 CPU，不需要模型服务）。可一次传多个文件。每个素材返回内容 SHA256 作为 SOURCE_ID。

```bash
uv run mindex prepare '/路径/电影.mkv' /路径/素材2.mp4
```

### `mindex detect VIDEO`

只扫描并保存分镜计划到 `plans/SOURCE_ID.json`，不切片、不调用嵌入模型。最后一行输出 SOURCE_ID。计划原子写入，后续编码中断不需要重扫。

### `mindex split SOURCE_ID`

按已保存计划切片；重跑可恢复（已存在的媒体跳过），也会修复个别缺失的切片/缩略图。

### `mindex inspect SOURCE_ID`

输出 JSON：计划镜头数、预计精细片段数、镜头时长 min/median/p95/max、短镜头数量、已落盘数量、缺失文件、最近处理记录。

### `mindex export SOURCE_ID [--output DIR]`

导出 `manifest.json`、`shots.csv`、`segments.csv`、`review.html`（浏览器直接打开的分页检查页，48 单元/页）。默认 `data/reports/SOURCE_ID/`。CSV 路径相对 `data_dir`；review.html 使用相对媒体路径，移动报告需保留相对布局。

### `mindex transcribe [--source SOURCE_ID]`

批量转写（30 秒块检查点）。省略 `--source` 处理全部素材。无音轨素材记录 `no_audio`；`asr.enabled: false` 记录 `disabled`。完成后可停止 ASR 服务。

### `mindex align PYTHON [--source SOURCE_ID]`

用独立 CPU ForcedAligner 环境对齐已缓存的原始 ASR 文本（仅 `asr.backend: llama_cpp` 路径需要）。**`PYTHON` 是位置参数**，指向 `.venv-align/bin/python`：

```bash
uv run mindex align .venv-align/bin/python
```

要求配置中 `asr.aligner_revision` 已固定为实际 commit。

### `mindex analyze [--source SOURCE_ID]`

分析镜头和精细片段，组织连续场景；失败后可重跑，已完成片段的检查点复用。

### `mindex index [--activate] [--mode visual|enriched] [--source SOURCE_ID] [--kind KIND...]`

建立或继续新索引，输出索引 ID。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--activate` | 关 | 全部检查通过后设为活动索引 |
| `--mode` | `enriched` | `visual` 只索引画面向量（默认 moment），`enriched` 完整四类表示 |
| `--source` | 全部 | 只选该素材，建立只含它的独立索引 |
| `--kind` | visual→moment；enriched→全部 | 可重复指定 `shot`/`moment`/`scene`；visual 不支持 scene |

### `mindex ingest VIDEOS... [--activate]`

执行全部阶段（prepare→transcribe→analyze→index）。**仅适用于服务可同时访问的环境**，单 GPU 互斥场景禁用。

## 检索与状态

### `mindex search TEXT [--granularity moment|shot|scene] [--limit 20]`

文字检索，输出 JSON 结果（limit 1–50）。机制见 [search.md](search.md)。

```bash
uv run mindex search '雨夜，一个人强忍眼泪，镜头慢慢推近' --granularity scene
```

### `mindex status [--health]`

索引、素材、处理记录与模型配置；`--health` 额外探测嵌入服务并校验空间身份是否匹配活动索引。

### `mindex serve [--host 127.0.0.1] [--port 8000] [--index-id INDEX_ID]`

启动页面与检索 API。`/` 检索页、`/admin` 素材后台、`/docs` OpenAPI。

`--index-id` 将查询、片段媒体和上下文绑定到指定 ready 索引，用于独立版本验证；不会修改活动索引。

## 索引版本管理

### `mindex check [--index-id ID]`

发布前一致性检查（向量与表示对账、片段完成、文件存在、表示齐全）。默认检查活动索引；发现问题列出并以退出码 1 结束。

### `mindex indexes list`

列出全部索引版本与当前活动索引。

### `mindex indexes activate INDEX_ID`

检查通过后在 SQLite 事务内发布。回退 = 先切回旧模型服务/配置，再激活旧索引。

## 评测与联调

### `mindex evaluate QUERIES [--split test] [--split dev|video-test] [--output FILE] [--index-id INDEX_ID]`

评测已冻结标注的查询集。未确认标注（`label_status != confirmed`）会被拒绝。split 对应查询文件中的 `split` 字段：`dev` 调参、`test` 报告、`video-test` 视频查询。

```bash
uv run mindex evaluate eval/queries.jsonl --split dev --output data/eval-dev.json
```

### `mindex model-check VIDEO [--text '描述'] [--repeats 3] [--output FILE]`

验证真实文字/视频嵌入：维度、范数、余弦、耗时重复稳定性，抓取服务 `/metrics` 显存（如有）。要求视频 ≤6 秒。

```bash
uv run mindex model-check data/clips/ID/moment/SEG.mp4 --text '画面描述' --output data/model-check.json
```

## e2e 实验框架

详见 [e2e-framework.md](e2e-framework.md)。所有命令不会启动或切换远程模型。

| 命令 | 作用 |
|---|---|
| `mindex e2e init --source ID --run DIR` | 冻结已有分镜、生成标注草稿与 12 个查询视频；不调用模型 |
| `mindex e2e probe --output FILE [--role embedding\|asr\|analyzer]` | 只探测已启动服务，保存兼容性报告；无推理 |
| `mindex e2e pin --weights-sha256 H --mmproj-sha256 H --quantization Q --output FILE [--role ...]` | 固定文件哈希与运行身份，生成部署清单；已存在则拒绝覆盖 |
| `mindex e2e phase PHASE --run DIR [--python PATH] [--queries FILE] [--split test] [--index-id ID] [--activate]` | 执行一个阶段；构建默认不发布，查询默认固定实验版本 |
| `mindex e2e freeze QUERIES --run DIR --confirmed-by '姓名'` | 人工确认后冻结标注并记录 SHA256 |
| `mindex e2e review QUERIES --run DIR` | 生成可播放全部候选、选择目标并导出确认标注的本地页面 |
| `mindex e2e status --run DIR` | 查看阶段状态、当前配置分析完成数量和活动索引，无模型请求 |

phase 取值：`smoke → visual → preview → asr → align → analyze(-smoke) → enriched → evaluate → video-evaluate → check`。其中 `align` 需 `--python`，`preview`/`evaluate`/`video-evaluate` 需 `--queries`。

## mindex-model-server（GPU 侧）

在角色独立 venv 中运行本项目自定义模型服务（部署见 [DEPLOYMENT.md](../DEPLOYMENT.md)）：

```bash
.venv-embedding/bin/mindex-model-server --role embedding --config config.local.yaml --host 0.0.0.0
```

| 参数 | 说明 |
|---|---|
| `--role` | `embedding`（默认端口 4010）/ `asr`（4020）/ `detector`（18203），必填 |
| `--config` | 配置文件路径 |
| `--host` / `--port` | 监听地址；端口缺省按角色 |

三个角色之间用 GPU 文件锁互斥（`MINDEX_GPU_LOCK` 可改路径，默认 `~/.cache/mindex/gpu.lock`），单 worker。鉴权用 `MINDEX_MODEL_API_KEY` 环境变量。
