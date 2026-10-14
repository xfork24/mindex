# macOS 模型身份迁移

执行依据为根目录 `REHASH_REFINEMENT_PLAN.md`，本次实际状态见 `REHASH_MIGRATION.md`。迁移允许重建，旧清单、旧向量与活动索引保持保留，trailer 验收通过后才能重建正片并发布。ASR、Analyzer 只适配公共接口和 schema 2，不要求同时启动；TTS 不参与本次重构。

## 身份与信任

schema 2 区分三层：weights/mmproj 的 SHA256 内容身份；决定索引兼容性的显式向量空间契约；包含地址、路径、别名、构建、ctx 与可观察参数的部署观测。七个模型字段是类型及结构校验，不能证明文件内容。缺失字段、维度错误、量化不符及能力不足均拒绝。

空间契约采用 `llama-native-wemm-2`。模板、池化、归一化、视频/图片预处理有独立语义版本；修改相关行为必须升级对应版本。修改日志、注释或 ASR 不影响 WeMM 空间。`embedding.numerical_implementation` 默认 `llama-cpp-wemm-1`，用于兼容失败后的显式新空间；不自动升级或降低阈值。

本地 pin 流式读取文件并记录绝对路径、文件状态和服务器工作目录，核对 `/props.model_path` 的解析结果。新进程首次使用重新计算 SHA256，随后按路径、设备/文件标识、大小、纳秒 mtime/ctime 缓存；状态变化重新哈希。推理期间即使文件内容没变、只有状态改变，也拒绝此次结果。服务请求使用当前完整核验报告里的模型 ID。

本地文件 SHA256 证明本地文件的内容。HTTP API 不能证明实际加载的 mmproj、后端、缓存数值配置或全部启动参数，仍由部署者确认；`--launch-parameters` JSON 记录这部分声明并参与部署摘要。远程 SHA 参数明确标记 `operator-attested-sha256`。schema 1 按原字段和运行指纹读取、校验，不自动改写；旧索引回滚需恢复旧代码与配套部署。

## 冻结、备份与配置

先盘点访问同一 `data_dir` 的 Web、CLI 和后台任务，包括 18302 验收端口，正常停止写入。保存代码版本/未提交差异、原始 YAML、SQLite 动态配置选择、部署清单、活动索引和成员清单。备份须检查 metadata/Chroma 数据库完整性、集合成员、媒体文件和原素材 SHA256，不依赖 Chroma 固定目录 UUID。macOS APFS 可用 `cp -cRp` 创建独立写时复制备份，不能用硬链接备份可变数据库。

迁移配置为 `config.macos-v2.yaml`。所有迁移命令使用相同 YAML 和全局 `--no-runtime-overrides`；`runtime_overrides: false` 也可写入 YAML，Web 的 `RuntimeConfig.current()` 持续遵守，设置页激活的档案不会改变验收配置。默认 CLI/Web 动态设置行为保持不变，e2e 仍默认使用 YAML。

## WeMM 容量验收

`scripts/run-local-wemm.sh` 按脚本位置解析绝对模型路径，可从任意目录调用。默认 ctx=32768、单 slot、f16 K/V cache，关闭额外的 prompt RAM cache（`--cache-ram 0`）。`MINDEX_LLAMA_SERVER`、`MINDEX_WEMM_PORT` 可指定本机二进制和端口。

```bash
bash scripts/run-local-wemm.sh > data/e2e/wemm-macos-32768.log 2>&1
```

记录 `/props` 与 `/v1/models`、启动日志的模型/投影/KV/计算缓冲占用、最长支持输入（默认 6 秒、24 帧、262144 像素上限）的实际推理耗时、进程峰值内存、系统内存压力与 swap。若 32768 内存不足，正常停止服务后用 `MINDEX_WEMM_CTX_SIZE=16384` 重启，使用新命名清单和配置重新做容量与兼容验证。16384 仍无法容纳最长输入则停止迁移；不能截断视频或偷偷修改预处理。

## Probe、pin 与基线

先启动 WeMM，再执行以下命令；不要覆盖现有输出清单。

```bash
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides e2e probe \
  --output data/e2e/wemm-macos-v2-probe.json
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides e2e pin \
  --weights-file models/WeMM-9B-Q8_0.gguf \
  --mmproj-file models/mmproj-WeMM-9B-BF16.gguf \
  --server-cwd "$PWD" --quantization Q8_0 \
  --launch-parameters data/e2e/wemm-macos-v2-alias-launch.json \
  --output data/deployments/wemm-macos-v2-32768-alias.json
```

启动参数 JSON 应记录实际使用的 ctx、parallel、K/V cache、GPU/backend、pooling 和 video-fps。远程部署可以互斥地改用 `--weights-sha256` 与 `--mmproj-sha256`，仍需核对真实启动参数。ASR/Analyzer 使用 `--role asr|analyzer` 并提供对应文件；不会改变其缓存身份设计。

```bash
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides e2e init \
  --source TRAILER_SOURCE_ID --run data/e2e/trailer-macos-v2
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides e2e phase smoke \
  --run data/e2e/trailer-macos-v2
```

首次通过结构 smoke 后，`data/capabilities/v2/<space_id>/` 保存六段固定原视频、六条固定文本、输入与预处理哈希、服务原始返回向量、规范化向量、契约和部署记录。`baseline.json`/`baseline.sha256` 不覆盖。后续 smoke 使用这些固定输入，每条文本、视频向量与基线的余弦相似度均须 ≥0.999，同时通过结构 smoke。结果分别保存在 `deployments/<deployment_id>.json`，每次重验先撤销当前部署旧通过记录；其他部署的历史记录保留。

建索引、检索和普通 v2 推理均检查当前部署已通过；smoke 的内部上下文可在建立基线时推理。基线缺失、损坏或固定输入变化会拒绝，不自动创建替代基线。兼容失败须显式升级数值实现版本、建立新基线并重建；这是工程策略，不能证明所有输入等价，也不证明中文检索质量。

## Trailer、正片与发布

trailer 当前为 61 个 moment，以冻结清单和数据库为准。先建立独立 visual/moment 索引，不加 `--activate`：

```bash
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides index \
  --mode visual --source TRAILER_SOURCE_ID --kind moment
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides check --index-id TRAILER_INDEX_ID
uv run mindex --config config.macos-v2.yaml --no-runtime-overrides serve \
  --port 18302 --index-id TRAILER_INDEX_ID
```

文字、图片、视频查询均在这个固定索引上验收，记录输入哈希、候选和检索耗时。可用 `e2e phase preview --index-id ... --queries ...` 保存未复核结果。人工标注未确认时不宣称质量通过，不自动执行 `e2e freeze`。六样本 smoke 不能替代质量验收。

trailer 通过后，用同一契约、相同切分/预处理重建正片。重跑相同 `index` 命令只补算缺失的 Chroma 表示，修复中断的元数据记录；构建/检查/最终模型核验失败不会切换活动索引。完成成员完整性、断点恢复和代表检索验收后才执行 `indexes activate 正片索引ID`。不要把 trailer 测试索引设为素材库默认值。

## 回滚

正常停止所有写入服务，保存失败现场。恢复备份对应的代码（包括未提交差异）、YAML、动态配置、旧部署清单及活动索引；使用旧身份实现与匹配的模型部署。若需完整恢复数据，先将失败目录重命名保留，再将备份复制回 `data/`，检查完整性与媒体后启动。不要删除失败目录，不要仅切换旧索引 ID 后继续使用新身份实现。
