# e2e 实验框架

`mindex e2e`（`e2e.py`）是**分阶段真实验证框架**：单 GPU 串行推进，不启动、不切换、不卸载任何远程模型——模型由部署者手动切换。它的目的：在宣称检索质量之前，把「真实模型、真实素材、人工标注」的证据链固化下来。

e2e 命令组**不应用设置页（`/settings`）保存的覆盖层**，始终使用 `config.local.yaml` 原值——验证实验必须可复现，不受运行时热切换影响（其余 CLI 命令都会合并覆盖层）。

当前实验：`data/e2e/trailer-20261013/`（117 秒 trailer，61 shot / 61 moment）。**`run.json` 是实际阶段状态**，任何时候以它为准，不以文档描述为准。

## 信任模型：probe / pin

`e2e probe` 只读探测 `/health`、`/props`、`/v1/models`，返回原始观测、规范化七项模型元数据、诊断摘要及能力。七项字段仅用于结构校验，不是内容身份；缺失或类型无效拒绝。

`e2e pin` 生成 schema 2 清单，本地使用互斥的 `--weights-file`/`--mmproj-file`（并提供 `--server-cwd`），或远程操作员声明 `--weights-sha256`/`--mmproj-sha256`。旧清单拒绝覆盖。文件内容身份、稳定向量契约与部署观测分层保存；schema 1 保持原字段校验，不自动转换。推理使用实时模型 ID，前后核对部署及本地文件状态。

v2 空间使用显式协议版本 `llama-native-wemm-2`，不包含地址、路径、别名、构建或整文件源码哈希。部署变化须重新 pin/smoke；构建、后端或数值配置改变后，使用固定基线逐条验证余弦相似度 ≥0.999。标准 HTTP API 仍无法证明实际加载的投影及全部启动参数，这些由部署者声明；本地 SHA256 只证明本地文件内容。

迁移全部命令显式指定 `--config config.macos-v2.yaml --no-runtime-overrides`，该开关贯穿验收 Web 配置读取。完整步骤和回滚见 [macOS 迁移](macos-model-migration.md)。

## init：冻结实验输入

`e2e init --source ID --run DIR` 校验素材切片完整、配置一致、文件哈希匹配后，生成：

| 文件 | 内容 |
|---|---|
| `run.json` | 实验状态：source_id、data_dir、layout_key、segments_hash、curation_hash、11 个 phase 状态 |
| `source.json` / `segments.json` / `config.initial.json` | 冻结的输入快照 |
| `LABELING.md` | 标注说明 + 全部 moment 的时间清单 |
| `queries.draft.jsonl` | 20 条文字草稿（前 8 dev 后 12 test），五类均匀分布 |
| `no-answer.draft.jsonl` | 5 条无答案诊断查询 |
| `video-queries/` + `video-queries.draft.jsonl` | 12 个查询视频：self(4)/crop(2)/crosscut(2)/codec(4) 变体 |

每次 `phase` 执行前重新校验：数据目录与切分参数未变、整理状态未变（curation_hash）、原素材与全部切片哈希未变。任何漂移 → 要求新建实验。**质量比较需要冻结的相同候选集。**

## 11 个 phase

`run.json` 的 phase 状态机：`pending → running → complete | failed`；重跑把上一次结果移入 `history`，每次尝试的输入输出存 `attempts/`。

| phase | 需要的服务 | 作用 | 关键产物 |
|---|---|---|---|
| `smoke` | WeMM | 六片段能力门槛：维度/范数/有限性/内容敏感性/重复稳定性/身份稳定性 | `smoke.json`；v2 固定基线与逐部署记录在 `data/capabilities/v2/<space_id>/`（建库、检索与普通推理前置） |
| `visual` | WeMM | moment 画面索引；仅加 `--activate` 才发布 | `visual-latest.json` 记录 index_id |
| `preview` | WeMM | 跑草稿查询只出排名供人工检查，**不计质量分数** | `preview-<index>-<hash>.json` |
| `asr` | ASR(:4020) | 原始转写（30 秒块），llama_cpp 路径状态 `pending_alignment` | `transcripts/.../raw/` |
| `align` | 无（CPU） | 独立 venv 强制对齐；`--python .venv-align/bin/python` | `aligned-N.json`，状态 `complete` |
| `analyze-smoke` | Qwen(:4040) | 只分析 3 个抽样片段，核对 JSON 与画面依据 | 样本分析结果 |
| `analyze` | Qwen | 全量分析 + 场景组织 | annotations、scene_plans |
| `enriched` | WeMM | 完整索引；仅加 `--activate` 才发布（仍仅 moment） | 新 index_id |
| `evaluate` | WeMM | 冻结文字标注评测（`--queries frozen-queries.jsonl --split dev|test`） | `evaluation-<index>-<split>.json` |
| `video-evaluate` | WeMM | 冻结视频查询评测 | 报告含 index_id/split/phase 文件名 |
| `check` | 无 | 复查指定/实验索引一致性 | 问题列表 |

默认实验构建不改变素材库活动索引。`preview/evaluate/video-evaluate/check` 优先使用 `--index-id`；未指定时使用本实验最近完成的 enriched 或 visual 索引。这样新增电影并发布新索引后，trailer 对比仍使用冻结的 61 个候选。

可用 `mindex serve --port 8001 --index-id INDEX_ID` 打开独立版本进行浏览器验证，媒体、上下文与查询均绑定同一版本，主库的活动指针保持独立。

## freeze：人工标注冻结

先生成可播放全部候选的本地复核页：

```bash
uv run mindex e2e review data/e2e/trailer-20261013/queries.draft.jsonl --run data/e2e/trailer-20261013
```

复核页只在用户逐条勾选确认并填写确认人后导出标注；程序不会自动确认。

```bash
uv run mindex e2e freeze data/e2e/trailer-20261013/queries.draft.jsonl \
  --run data/e2e/trailer-20261013 --confirmed-by '实际复核人'
```

要求：每条 `label_status: confirmed`（程序不自动批准）、查询 ID 唯一、text/video 二选一非空、普通查询有相关目标（`no_answer: true` 除外）、视频文件哈希未变。产出 `frozen-queries.jsonl` + 带 SHA256 与确认人的 manifest；**冻结集已存在时修订标注必须新建实验**。`evaluate`/`video-evaluate` 只接受带匹配 manifest 的冻结集。

相关目标支持 `{"segment_id": ..., "grade": 1-3}` 或 `{"source_id", "start", "end", "grade", "min_overlap"(默认0.5)}`；grade 1/2/3 = 部分/主要/完全相关。区间命中要求交集覆盖较短者的 `min_overlap` 比例。

## 评测 profile 与指标

同一冻结候选集上比较六个 profile：`video_only`、`video_asr`、`description_only`、`fusion`、`no_dialogue`、`no_style`。`video_asr` 同时排除描述/风格的向量和 BM25 字段，后两个消融同时从 BM25 移除对应字段。视频与图片查询只进 `video_only`；图片集用 `image` 路径、媒体 `sha256` 和 `split: image`，冻结后可运行 `phase evaluate --split image`。报告保留候选时间布局与整理状态哈希。指标：

- **Hit@1 / Hit@5**：前 1/5 条是否有相关结果。
- **MRR@10**：首个相关结果排名倒数。
- **nDCG@10**：**按相关组计**——同一相关区间的重复窗口最多奖励一次（与逐条候选标注的标准数据集分数不可直接横比）。
- 延迟：首请求 + 后续 P50/P95。
- 无答案查询只记录返回结果（误召回诊断），不混入相关性分数，也不要求返回空集。

报告同时记录模型空间、查询 SHA256、索引与整理状态指纹；评测过程中索引或整理状态变化 → 拒绝比较。**只用 dev 调参**；trailer 规模属于冒烟，正式验收需多真实来源 + 20 dev + 40 test 人工标注，Hit@5 ≥85% 是目标而非现状。

## 当前实验进度（截至 2026-10-13）

`run.json` 是实时状态。已完成真实 WeMM、ASR/CPU 对齐及 Qwen 三片段验证；Qwen 批次分析已启动。新增完整电影已有独立 visual 索引（2,336 个 moment），trailer 实验继续使用独立版本。步骤见 [E2E_RUNBOOK.md](../E2E_RUNBOOK.md)。

## 工程测试 vs 真实验收

`uv run pytest -q`（真实 FFmpeg/SQLite/Chroma/HTTP，确定性模型 fixture）验证**工程链路**；不证明真实模型中文检索质量、INT8 兼容性或显存峰值。真实验收走本框架并记录到 [VERIFICATION.md](../VERIFICATION.md)。真实模型结果、人工标注、浏览器证据、GPU 数据缺一时，保持待验收状态。
