# CLI 完全参考

安装后有两个入口：`mindex`（本机客户端）和 `mindex-model-server`（GPU 侧服务，见文末）。所有命令的中文帮助可用 `--help` 查看。

## 全局规则

- `--config` / `-c` **必须放在子命令之前**：`uv run mindex --config profiles/int8.yaml index`。也可设环境变量 `MINDEX_CONFIG`（默认读 `config.local.yaml`）。
- 配置模型 strict（`extra="forbid"`），未知键直接校验失败。
- `data_dir` 已有元数据库时，CLI 命令会合并设置页（`/settings`）保存的覆盖层——激活的模型档案与检索调参对 CLI 同样生效，与 Web 行为一致；`mindex e2e` 命令组例外，始终使用 yaml 原值（验证框架保持可复现）。
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

### `mindex enrich-dialogue [SOURCE] [--subtitle-stream INDEX] [--no-publish] [--workers 2]`

字幕优先补充对白；省略 SOURCE 则批量处理。默认断点续跑并自动发布；`--no-publish` 仅生成对白数据。MiniMax 保存服务词级时间戳，不调用本地 ForcedAligner。默认不触发 LLM 分析。Web 与 CLI 共用生效配置和管线锁。详见 [三渠道改造说明](recall-enhancement.md)。

### `mindex transcribe [--source SOURCE_ID]`

批量转写（30 秒块检查点）。省略 `--source` 处理全部素材。无音轨素材记录 `no_audio`；`asr.enabled: false` 记录 `disabled`。完成后可停止 ASR 服务。

### `mindex align PYTHON [--source SOURCE_ID]`

用独立 CPU ForcedAligner 环境对齐已缓存的原始 ASR 文本（仅 `asr.backend: llama_cpp` / `openai` 路径需要，二者都只出原始文本）。**`PYTHON` 是位置参数**，指向 `.venv-align/bin/python`：

```bash
uv run mindex align .venv-align/bin/python
```

要求配置中 `asr.aligner_revision` 已固定为实际 commit。

### `mindex analyze [--source SOURCE_ID]`

分析镜头和精细片段，组织连续场景；失败后可重跑，已完成片段的检查点复用。

### `mindex index [--activate] [--mode auto|visual|enriched] [--channel CHANNEL...] [--source SOURCE_ID] [--kind KIND...]`

建立或继续新索引，输出索引 ID。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--activate` | 关 | 全部检查通过后设为活动索引 |
| `--mode` | `auto` | `auto` 使用已完成渠道；`visual` 更新画面；`enriched` 保留旧版完整索引入口 |
| `--source` | 全部 | 更新该素材；新版保留活动索引中的其他素材和渠道 |
| `--channel` | 全部可用渠道 | 可重复指定 `visual` / `dialogue` / `analysis`；未选渠道保留 |
| `--kind` | 新版沿用活动粒度，首次 moment；enriched→全部 | 可重复指定 `shot`/`moment`/`scene`；visual 不支持 scene |

### `mindex ingest VIDEOS... [--activate]`

执行默认阶段（prepare→enrich-dialogue→auto index），不触发 LLM 分析。**仅适用于服务可同时访问的环境**，单 GPU 互斥场景禁用。

## 检索与状态

### `mindex search TEXT [--granularity moment|shot|scene] [--limit 20] [--channel CHANNEL...]`

文字检索，输出 JSON 结果（limit 1–50）。机制见 [search.md](search.md)。

```bash
uv run mindex search '雨夜，一个人强忍眼泪，镜头慢慢推近' --granularity scene
```

### `mindex status [--health]`

索引、素材、处理记录与模型配置；`--health` 额外探测嵌入服务并校验空间身份是否匹配活动索引。

### `mindex serve [--host 127.0.0.1] [--port 8000] [--index-id INDEX_ID]`

启动页面与检索 API。`/` 检索页、`/admin` 素材后台、`/studio` 创作页、`/settings` 设置页、`/docs` OpenAPI。

`--index-id` 将查询、片段媒体和上下文绑定到指定 ready 索引，用于独立版本验证；不会修改活动索引。

## 索引版本管理

### `mindex check [--index-id ID]`

发布前一致性检查（向量与表示对账、片段完成、文件存在、表示齐全）。默认检查活动索引；发现问题列出并以退出码 1 结束。

### `mindex indexes list`

列出全部索引版本与当前活动索引。

### `mindex indexes activate INDEX_ID`

检查通过后在 SQLite 事务内发布。回退 = 先切回旧模型服务/配置，再激活旧索引。

## 磁盘清理

### `mindex clean [--apply]`

扫描可安全回收的磁盘数据；**默认预览**，加 `--apply` 才真正删除。分级判定、保留策略与 C 级待调整清单详见 [cleanup.md](cleanup.md)；过期阈值在配置 `clean` 节调整（[config-reference.md](config-reference.md)）。

- **A 级（每次都删）**：崩溃残留临时目录、`.DS_Store`、上传暂存残留、孤儿文件（切片/缩略图/转码缓存/人物图/声音样本/未注册原片）
- **B 级（策略过期）**：非活动索引版本（保留活动 + 最近 `clean.index_keep_versions` 个 ready + 全部 building）、旧指纹检查点、context/studio/reports/字幕缓存、runs 旧记录
- **C 级（只报告不删）**：persons/frames、model-cache、一次性验证产物、数据库备份、VACUUM 建议——出现在输出 `notes` 中

apply 分两段持锁：先独占 `pipeline.lock` + `index-write.lock`（管线与索引类，与素材删除同序），释放后再取 `studio.lock`（创作类，拿不到则该三类标记 skipped 而非失败）。管线/索引锁被占时以中文提示退出码 1。大规模清理前建议停 serve。

```bash
uv run mindex clean                       # 预览各类别可回收项与字节数
uv run mindex clean --apply               # 实际删除
```

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
| `mindex e2e pin --quantization Q --output FILE [--role ...]` | 生成 schema 2；本地文件参数与远程 SHA 声明互斥，不覆盖旧清单 |
| `mindex e2e phase PHASE --run DIR [--python PATH] [--queries FILE] [--split test] [--index-id ID] [--activate]` | 执行一个阶段；构建默认不发布，查询默认固定实验版本 |
| `mindex e2e freeze QUERIES --run DIR --confirmed-by '姓名'` | 人工确认后冻结标注并记录 SHA256 |
| `mindex e2e review QUERIES --run DIR` | 生成可播放全部候选、选择目标并导出确认标注的本地页面 |
| `mindex e2e status --run DIR` | 查看阶段状态、当前配置分析完成数量和活动索引，无模型请求 |

phase 取值：`smoke → visual → preview → asr → align → analyze(-smoke) → enriched → evaluate → video-evaluate → check`。其中 `align` 需 `--python`，`preview`/`evaluate`/`video-evaluate` 需 `--queries`。

## mindex-model-server（GPU 侧）

在角色独立 venv 中运行本项目自定义模型服务（部署见 [DEPLOYMENT.md](DEPLOYMENT.md)）：

```bash
.venv-embedding/bin/mindex-model-server --role embedding --config config.local.yaml --host 0.0.0.0
```

| 参数 | 说明 |
|---|---|
| `--role` | `embedding`（默认端口 4010）/ `asr`（4020）/ `detector`（18203），必填 |
| `--config` | 配置文件路径 |
| `--host` / `--port` | 监听地址；端口缺省按角色 |

三个角色之间用 GPU 文件锁互斥（`MINDEX_GPU_LOCK` 可改路径，默认 `~/.cache/mindex/gpu.lock`），单 worker。鉴权用 `MINDEX_MODEL_API_KEY` 环境变量。

## schema 2 pin 与迁移

本地使用 `--weights-file FILE --mmproj-file FILE --server-cwd DIR`，流式计算 SHA256 并核对 `/props.model_path`；远程使用 `--weights-sha256 SHA --mmproj-sha256 SHA`，明确标记操作员声明。`--launch-parameters FILE` 记录部署者确认的启动参数 JSON，并参与部署摘要。HTTP API 不能证明实际加载的投影或全部数值配置。未经当前部署 smoke 基线对照不能建索引或检索；详见 [macOS 迁移](macos-model-migration.md)。

## 原片只登记与对白方式

`mindex register 视频...` 验证媒体、按内容去重并原子登记到当前素材库，不启动分镜检测或模型。返回 `source_id` 与 `duplicate`。

`mindex enrich-dialogue SOURCE --method auto|subtitle|asr [--subtitle-stream INDEX] [--no-publish]` 分别指定自动字幕优先、严格文本字幕轨道或直接 ASR。严格字幕需要选轨，失败不回退；默认 auto 兼容已有调用。处理成功而发布失败时返回独立 `publication` 错误，处理结果保留。
