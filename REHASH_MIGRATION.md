# REHASH_MIGRATION：macOS 模型身份迁移实际执行记录

日期：2026-10-14。执行依据：[REHASH_REFINEMENT_PLAN.md](REHASH_REFINEMENT_PLAN.md)；操作手册：[docs/macos-model-migration.md](docs/macos-model-migration.md)。

## 结论

- 代码重构（三层身份、schema 2 清单、部署兼容门槛、`--no-runtime-overrides`）已完成并通过全量测试；本记录落盘时全部改动已提交。
- trailer v2 空间验收完成：visual/moment 索引 61 个成员构建成功，文本/图片/视频真实检索验收通过（见下文）。
- 正片（2336 moment）按计划须以同一 v2 空间重建，尚未执行；命令见文末，由操作员安排时间运行。
- 旧 schema 1 清单、旧索引与活动索引全部保留；当前活动索引仍为正片旧空间 `8ebf8b8455e2e94d277cf8fa`，未受迁移影响。

## 身份与信任

| 项 | 值 |
|---|---|
| weights SHA-256 | `4bb0e97b279404883207001fea15f6369d7c43930d1adc73a406ba1d9b66806c`（本地流式计算） |
| mmproj SHA-256 | `62d8c77bbfd8c8cdf4a2f99b401dda5d978d1413d5bea29b15fc7c109a9c73b7`（本地流式计算） |
| content_id | `4458eb7ee1f77c52feeb4f51` |
| space_id | `460b7e65df356420bca9d6a5`（implementation `llama-native-wemm-2`、numerical `llama-cpp-wemm-1`、4096 维） |
| 部署观测 | `http://127.0.0.1:8888`，alias `wemm-macos`，build `b11579-8f574b0ad`，ctx 32768 单 slot，f16 K/V cache，Q8_0 |
| 当前清单 | `data/deployments/wemm-macos-v2-32768-alias.json`（schema 2，trust `local-sha256`，deployment_id `500bff8c17ce0450060d226b`） |

清单纯迹：18:33 初版 `wemm-macos-v2-32768.json`（e1995dd6）→ 18:35 `…-verified.json`（62fac95a，建立基线的部署）→ 18:41 `…-alias.json`（500bff8c，alias 启动参数版本，`config.macos-v2.yaml` 引用）。三份均保留，未覆盖。

## 冻结与备份

- 18:20 停止写入后创建 `data.backup-20261014-182043/`（APFS `cp -cRp` 写时复制）及 `.meta` 清单；数据库完整性、索引成员与媒体已核对。
- 迁移前代码基线：commit `5b02310`（其后重构改动见 git 历史）。

## 容量验收（ctx=32768）

`data/e2e/wemm-macos-capacity-32768.json`：**passed**。最长输入（6.0s、24 帧、696×376）video 嵌入 56.1s、text 7.07s；进程峰值 RSS ≈ 9.84GB；系统 free 9%、swap 已用 ≈ 6.6GB（压力高但未失败，未降级 ctx）。首次观测（pid 36167）被兼容检查拒绝，正常重启后（pid 36299）复验通过——拒绝记录保留于 `wemm-macos-capacity-32768-observation-rejected.json`。

## 基线与跨部署兼容

- 基线：`data/capabilities/v2/460b7e65df356420bca9d6a5/`（6 段固定视频样本 + 6 条固定中文文本，inputs_id `d881007b4e462c6bec31d817`，baseline_sha256 `04849844…`，由部署 62fac95a 于 18:39 建立，`baseline.sha256` 标记齐全）。
- smoke（119–120s）：8 项结构检查全部通过（有限性/维度/单位范数/内容敏感性/重复稳定性/空间稳定性），结果存 `data/e2e/trailer-macos-v2/smoke.json`。
- 跨部署验证：alias 部署 500bff8c 对基线逐条余弦——text `[1.0, 1.0, ≈1.0×4]`、video 同样全部 ≥0.999（阈值 0.999），记录存 `capabilities/v2/…/deployments/500bff8c….json`；每次重验先 revoke 旧记录，历史记录保留。
- 中断恢复实测：首次 visual 构建中断于 3/61（服务停止，无错误日志）；重启服务后重跑同一命令，已建向量全部跳过（Chroma 既有 ID 不重复计算），元数据自愈，最终 61/61 完成；恢复段耗时 1110s（约 20s/成员）。

## Trailer v2 索引与检索验收

- 索引：`506568882ba6c1a8189c93f5`（visual/moment，61 成员，schema 2 空间，未激活）。`mindex check` 成员完整性通过。
- 验收执行（全部 `--config config.macos-v2.yaml --no-runtime-overrides`，真实 WeMM 服务，2026-10-14 晚）：

| 验收 | 方式 | 结果 |
|---|---|---|
| 中文文本（12 条 test 冻结标注，已人工确认） | `e2e phase evaluate --split test`（40.6s） | hit@1=0.750、**hit@5=0.917**（11/12，超 0.85 目标）、mrr@10=0.819、ndcg@10=0.844；warm p50≈550ms、p95≈612ms；未进前 5 的仅 text-11。各路由 profile（video_only/video_asr/fusion/no_dialogue/no_style）结果一致；description_only 无对应向量为预期 |
| 视频（12 条 self 变体 draft 标注） | `e2e phase preview` + draft 标注对照（217.5s） | 12/12 相关片段排名=1；单查询 p50≈12.5s、max 73.9s（视频查询需切窗多次嵌入） |
| 图片（12 条 derived_frame_format draft 标注，jpg/png/webp 三格式） | `e2e phase preview` + draft 标注对照 | 12/12 相关片段排名=1；单查询 p50≈2.4s |
| Web 层（`serve --port 18302 --index-id 50656888…`，固定配置贯穿） | HTTP API 真实调用 | `/api/status` 正常返回 v2 索引与空间；文本查询期望片段 rank-2；图片上传查询期望片段 rank-1、往返 2.8s。上一轮因服务未就绪 503 的图片 Web 验收就此闭环 |

质量口径：12 条查询低于正式 40 条下限，evaluate 自标 `acceptance: smoke_only`；视频/图片为 draft 标注（未经 `e2e freeze` 确认），self/derived 变体验证的是向量空间一致性与链路正确性，不构成正式检索质量结论。结果文件：`data/e2e/trailer-macos-v2/attempts/evaluate-e6d4d9bb2b91-result.json`、`preview-50656888…-827b3720f7d6.json`（视频）、`preview-50656888…-ad11c77c6e15.json`（图片）。

## 自动化回归

| 检查 | 结果 |
|---|---|
| `uv run pytest -q`（全量） | 224 passed、1 skipped |
| `uv run pytest tests/test_deployment_v2.py -q` | 61 passed |
| `uv run ruff check src tests` | 通过 |
| `node --check src/mindex/static/app.js` | 通过 |

## 正片重建（待执行）

trailer 验收通过后，使用同一 v2 契约重建正片（约 2336 个 moment，按实测速度需十余小时连续占用，注意内存压力）：

```bash
bash scripts/run-local-wemm.sh > data/e2e/wemm-macos-v2-feature.log 2>&1
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides index \
  --mode visual --source 074e8016c9b494027b0f3dfc875b68a735698bdc08170b83e65d616c26b17034 --kind moment
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides check --index-id 新索引ID
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides indexes activate 新索引ID
```

中断后重跑同一 `index` 命令即续建；构建/检查/最终核验失败不会切换活动索引。完成代表性检索验收后才 activate；不要把 trailer 测试索引设为默认素材库。

## 追加：ASR 本地化重 pin（2026-10-14 晚）

前端报「llama.cpp 运行身份发生变化」：`asr.json`（schema 1，10-13 从 `models/` 目录用 `./Qwen3-ASR…` 启动时固定）与旧脚本 `run-local-qwen3-asr.sh` 的相对路径启动（`/props.model_path` 报 `../models/…`）观测不一致。ASR 属计划明确不迁移范围，本次补齐：

- `scripts/run-local-qwen3-asr.sh` 按 `run-local-wemm.sh` 模式重写（按脚本位置解析、绝对路径、`--alias qwen3-asr-macos`，任意目录可启动）。
- 重新 pin 为 schema 2：`data/deployments/asr-macos-v2.json`（trust `local-sha256`，content_id `582c81555505044dcad93b3c`，deployment_id `aa58fd4b23f0853b8fa347d1`）；旧 `asr.json` 保留。
- `config.local.yaml` 与 `config.macos-v2.yaml` 的 asr 清单指向新文件；已重启 `mindex serve`。
- 验证：真实音频经 `LlamaTranscriber.raw` 转写成功，`checked_runtime` schema-2 全链路（deployment_id + 角色校验 + 本地文件哈希）通过。

## 追加：Analyzer 迁移准备（2026-10-14 晚，待远端在线执行）

远端（192.168.1.86:4040）当前不可达，pin 需要活服务探测，故材料就绪、命令待执行：

- 文件（本地 `/Volumes/Storage512G/model/`，exFAT）已流式计算 SHA-256，**与旧 schema-1 analyzer.json 所 pin 完全一致**——10-13 的 Ollama 部署（blob `sha256-9aecf1cd…`）即同一套权重，内容身份连续，仅服务封装变化。
  - weights `Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp.gguf`：`9aecf1cd41b2cb2f32a74e0d889e33855ebef43b26f43b43feb5720239e677e5`
  - mmproj `mmproj-Qwen3.8-27B-BF16.gguf`：`13cb7bebccbd04afc8f4090cb949ecf8937cdf7377c5799b1a0c594e7c0d3e16`
- 启动参数声明：`data/e2e/analyzer-swift15-launch.json`（ctx 262144）。
- 信任模式：远端部署 → `operator-attested-sha256`（哈希来自本地同一文件的实测）。

远端在线后依次执行（仓库根目录）：

```bash
curl -s http://192.168.1.86:4040/health    # 确认在线
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides e2e probe --role analyzer \
  --output data/e2e/analyzer-swift15-probe.json
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides e2e pin --role analyzer \
  --weights-sha256 9aecf1cd41b2cb2f32a74e0d889e33855ebef43b26f43b43feb5720239e677e5 \
  --mmproj-sha256 13cb7bebccbd04afc8f4090cb949ecf8937cdf7377c5799b1a0c594e7c0d3e16 \
  --quantization IQ3_S \
  --launch-parameters data/e2e/analyzer-swift15-launch.json \
  --output data/deployments/analyzer-swift15-256k.json
# 然后把 config.macos-v2.yaml 与 config.local.yaml 的 analyzer.deployment_manifest
# 指向 ./data/deployments/analyzer-swift15-256k.json（旧 analyzer.json 保留）
```

注意事项：远端必须是原生 llama-server（暴露 `/props`、七字段 meta 完整、mmproj 实际加载使 `modalities.vision` 为真），Ollama 式封装会被新代码拒绝；`--quantization` 以服务报告的 GGUF ftype 为准（文件名指示 IQ3_S）。

## 回滚

按 [docs/macos-model-migration.md](docs/macos-model-migration.md) 「回滚」节执行：停止写入 → 恢复备份对应代码/YAML/清单/活动索引 → 旧索引必须配套旧身份实现与旧部署使用。失败目录先重命名保留，不直接删除。
