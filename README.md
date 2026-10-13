# Mindex：中文视频片段检索

以 **WeMM-Embedding-9B / 4096 维**为默认主模型，优先做好文字找视频，同时支持视频找相似片段。视频画面、中文描述、情绪镜头描述和对白分别索引，再融合排名。没有音频查询或 OmniRetriever 依赖。

已增加 llama.cpp/GGUF 适配、六片段能力门槛、分阶段实验、原始 ASR 与独立 CPU 对齐、人工标注冻结及消融评测。当前用户部署及真实 trailer 验证请按 [端到端操作手册](E2E_RUNBOOK.md) 执行；测试设计见 [端到端计划](E2E_TEST_PLAN.md)。llama.cpp 后端正式建库前须完成部署清单和 `e2e phase smoke`。

系统性参考文档（架构、管线、CLI、配置、检索、索引、e2e 框架、后台与工具）见 [docs/](docs/README.md)。

## 本机启动

需要 Python 3.11、uv、FFmpeg 和 ffprobe。本机只运行切片、数据库、API 和页面；模型在 GPU 机器上运行。

```bash
uv sync --extra dev
cp config.example.yaml config.local.yaml
uv run mindex serve
```

打开 <http://127.0.0.1:8000>。没有素材和模型服务时，页面会显示明确的未就绪状态；不会用模拟向量冒充真实检索。配置中的服务地址和模型名可随时修改，API key 通过环境变量读取。

## 素材后台管理

打开 <http://127.0.0.1:8000/admin>，按素材显示名称或原文件名搜索，查看镜头和精细片段，播放、下载，并编辑显示名称、备注、收藏及隐藏状态。列表每页 48 条；点击镜头可查看其子片段，片段可返回并定位父镜头。

后台直接读取已导入素材，无需活动索引或 GPU 服务。素材仍通过 `mindex prepare` 命令导入；刷新后台即可查看。名称和备注不修改文件、稳定 ID 或已有嵌入。隐藏立即排除检索：素材隐藏影响全部子片段，镜头隐藏影响其精细片段；恢复上级时保留片段自身的隐藏状态。选择“已隐藏”或“全部”可恢复内容。收藏不影响相似度排名。

整理信息保存到 SQLite 独立表，旧库启动时自动补齐表结构，旧素材默认未隐藏、未收藏。首版不包含网页上传、物理删除或时间边界编辑，沿用本地单用户访问方式。

## 先测试整部电影：分镜、切片与画面索引

支持 FFmpeg 可解码的 MP4、MKV 等容器。默认配置已指向 WeMM `http://192.168.1.86:4010`、ASR `:4020`；TTS `:4030` 不参与本工具链。当前步骤只需要在 GPU 上运行 WeMM。完整操作见 [电影处理指南](FILM_WORKFLOW.md)。

```bash
# 分镜和切片在本地 CPU 执行，不需要启动 GPU 模型。
uv run mindex prepare '/路径/电影.mkv'
# 上一条命令最后输出电影 SOURCE_ID。
uv run mindex inspect SOURCE_ID
uv run mindex export SOURCE_ID

# 此时只启动 WeMM，直接嵌入精细视频片段，不依赖 ASR 或 Qwen 分析。
uv run mindex index --mode visual --source SOURCE_ID --activate
uv run mindex check
uv run mindex search '一个人在雨夜街道上奔跑'
uv run mindex serve
```

也可分开执行 `detect '/路径/电影.mkv'`、`split SOURCE_ID`，在扫描完成后先检查切点。检测计划先写入 `plans/`；切片或建索引中断后重跑对应命令，复用已有结果。`export` 输出 JSON/CSV 清单和可本地打开的分页视频检查页。

`visual` 默认只索引 `moment`，不会把尚未分析的占位说明嵌入或添加虚构标签；文字直接匹配 WeMM 视频向量，页面标记该模式。需要镜头索引可加 `--kind shot`，两种都要则重复指定 `--kind moment --kind shot`。`--source` 建立只包含该电影的独立索引，发布后活动库只显示这份索引；省略它会包含所有已完成切片的来源。

## 单 GPU 完整语义索引

先按 [模型部署说明](DEPLOYMENT.md) 部署服务。模型互斥执行时，**分阶段运行**，不要使用要求各服务均可访问的 `ingest` 快捷命令。

```bash
# 1. 本地 CPU 分镜和切片，不需要大模型。
# 如果选择 TransNetV2，先启动 detector 服务，并设置 detector.backend: transnet。
uv run mindex prepare /绝对路径/素材1.mp4 /绝对路径/素材2.mp4

# 2. 在 GPU 机器停止其他模型，启动 ASR 服务。
uv run mindex transcribe

# 3. 停止 ASR 服务，启动已有 qwen3.8:27b。
uv run mindex analyze

# 4. 停止 Qwen，启动 WeMM 服务；建索引并发布。
uv run mindex index --activate
uv run mindex check

# 5. 保持 WeMM 常驻，启动网页。
uv run mindex serve
```

默认 `index --mode enriched` 建立包含描述、对白及场景的完整索引；省略 `--mode` 等效。阶段失败后重跑相同命令即可。素材内容哈希、片段 ID、分析配置和索引版本用于断点恢复。30 秒转写块、逐片段分析和逐表示索引均有检查点。`analyze --source ID` / `transcribe --source ID` 可只处理一个素材。完整模式下，无音轨视频仍须执行 `transcribe`，记录无音轨状态。

若暂时不部署 ASR，可以显式设置 `asr.enabled: false`，再运行 `transcribe`。该索引会记录 `disabled`，没有对白召回；开启 ASR 后需重跑转写、分析和建索引。此模式不能用于宣称对白检索效果已经达标。

分镜默认是本地 `AdaptiveDetector`，逐帧检测、最短镜头 1 帧。它是可运行的启动配置，**不是 TransNetV2 已验证的替代品**。正式素材可选择 TransNetV2；两者都需要人工抽检。切分参数变化请使用新的 `data_dir`，保留原素材布局和旧索引。

## 处理与查询

- 镜头保留视觉切点和短镜头；不把音频停顿当成镜头切点。
- 默认片段：不超过 6 秒的镜头整体保留；长镜头采用 4 秒窗口、3 秒步长，并补齐结尾。
- Qwen 分析有序采样帧和对白，组织最长 45 秒的连续场景。仅镜头与窗口自动切分；首版没有额外的动作起止定位模型。
- 每个镜头、片段和场景均实际保存 H.264/AAC MP4 与缩略图。长镜头、场景在嵌入时继续拆成短窗口。
- 文字查询分别召回画面、动作场景描述、情绪镜头描述、对白与 BM25，使用加权 RRF 排名。
- 视频查询最长 30 秒、100MB，按窗口做画面检索，临时文件自动清理，查询视频不入库。
- 在线查询只调用 WeMM，无 Qwen 实时描述或在线大模型重排。分数不是匹配概率。

```bash
uv run mindex search '雨夜，一个人强忍眼泪，镜头慢慢推近'
uv run mindex search '两个人争吵后转身离开' --granularity scene
uv run mindex status --health
```

## 模型与索引切换

嵌入服务须提供固定的空间身份：模型、实际权重 revision、量化、维度、预处理和实现指纹。身份既出现在健康检查，也出现在每次向量响应中。查询身份与活动索引不符时返回 503，禁止跨空间比较。

```bash
# 修改配置或模型服务后，新建独立索引；不会覆盖旧索引。
uv run mindex index
uv run mindex indexes list
uv run mindex check --index-id 新索引ID
uv run mindex indexes activate 新索引ID

# 回退：先切回旧模型服务/配置，再发布旧索引。
uv run mindex indexes activate 旧索引ID
```

`--config 路径` 必须放在子命令之前，例如 `uv run mindex --config profiles/int8.yaml index`。也可设置 `MINDEX_CONFIG`。模型替换通过 `MultimodalEmbedder`、`VisualAnalyzer`、`Transcriber`、`Aligner` 和 `ShotDetector` 接口适配；其他嵌入服务实现 [部署文档中的协议](DEPLOYMENT.md#服务协议) 即可接入，不必改检索管线。内置 GPU 嵌入运行器针对 WeMM，不声称任意模型都能直接加载。

数据保存在配置的 `data_dir`：原片 `sources/`，分镜计划 `plans/`，切片 `clips/`，缩略图 `thumbnails/`，导出报告 `reports/`，转写检查点 `transcripts/`，元数据 `metadata.sqlite3`，向量 `chroma/`。索引冻结描述与对白快照；更新分析不会修改已发布旧索引的检索文档。

## API

FastAPI 文档：<http://127.0.0.1:8000/docs>。

| 接口 | 用途 |
|---|---|
| `GET /api/admin/sources` | 素材名称搜索、收藏/隐藏筛选、分页；不依赖索引 |
| `GET /api/admin/sources/{id}` | 素材媒体信息、切片统计、缺失文件与最近处理记录 |
| `GET /api/admin/sources/{id}/segments` | `kind=shot/moment`、`parent_id`、收藏/隐藏筛选与分页 |
| `GET /api/admin/segments/{id}` | 片段详情、父镜头与时间顺序位置 |
| `PATCH /api/admin/sources/{id}` / `segments/{id}` | 修改 `display_name`、`notes`、`favorite`、`hidden`；空名称恢复默认 |
| `GET /api/admin/segments/{id}/media` / `thumbnail` | 未嵌入片段的播放/缩略图；媒体支持 Range、HEAD，`download=true` 下载 MP4 |
| `POST /api/search/text` | JSON：`text`、`granularity`、`limit`、`filters` |
| `POST /api/search/video` | multipart：`file`、`granularity`、`limit`、JSON 字符串 `filters` |
| `GET /api/status?health=true` | 索引、处理记录、模型可用性 |
| `GET /api/segments/{id}` | 当前索引中的片段详情 |
| `GET /api/media/{id}` | 片段播放，支持 HTTP Range |
| `GET /api/thumbnails/{id}` | 缩略图 |
| `GET /api/context/{id}` | 前后 3 秒上下文，按需转码为浏览器可播放的 MP4 并缓存 |
| `GET /api/sources/{id}/media` | 原素材文件，播放能力取决于浏览器对原格式的支持 |

`granularity` 为 `moment`（默认）、`shot` 或 `scene`；`limit` 为 1–50。`filters` 支持 `source_id`、`min_duration`、`max_duration`、`scene`、`emotion` 和 `shot_size`。标签筛选为不区分大小写的子串匹配，多个字段取交集。

## 测试与真实质量评测

```bash
uv run pytest -q
uv run ruff check src tests
node --check src/mindex/static/app.js
uv run mindex evaluate eval/queries.jsonl --split dev --output data/eval-dev.json
uv run mindex evaluate eval/queries.jsonl --split test --output data/eval-test.json
uv run mindex model-check /路径/六秒以内片段.mp4 --text '画面描述' --output data/model-check.json
```

[评测标注说明](eval/README.md) 提供 60 条文字与 12 条视频查询的模板生成器。目标是固定 40 条文字测试集 Hit@5 ≥85%，融合 nDCG@10 不低于最佳单路。开发集用于采样、量化和融合权重选择，测试集不能调参。

自动化测试使用真实 FFmpeg、PySceneDetect、SQLite、Chroma 和 HTTP API，模型部分使用明确标记的确定性测试适配器。**这些测试验证工程链路，不证明真实模型中文检索质量、INT8 兼容性或 16GB 显存峰值。** 真实 WeMM、ASR 与 TransNetV2 需在用户 GPU 上完成验收，结果记录到 [验证报告](VERIFICATION.md)。

## 附带工具：哔哩哔哩缓存转 MP4

`tools/m4s-converter/`（自包含，不依赖仓库外文件）将哔哩哔哩客户端缓存的 m4s 音视频流无损合并为 MP4——MP4Box 封装、不转码、按番剧/分P自动命名、重跑幂等。唯一外部依赖是系统级 MP4Box（`brew install gpac`）。

```bash
# 转换默认缓存目录 ~/Movies/bilibili（或设 BILIBILI_CACHE 环境变量）
~/workspace/mindex/tools/m4s-converter/convert.sh

# 指定缓存目录；额外参数透传给原工具（如 -o 覆盖同名输出）
convert.sh /path/to/bilibili缓存目录
convert.sh /path/to/bilibili缓存目录 -o
```

- 输出：`<缓存目录>/output/<番剧名>-<UP主>/<分P标题>.mp4`；原始 m4s 缓存不修改。
- `convert.sh` 已自动跳过交互确认、默认关闭弹幕下载（离线可用）；需要弹幕 ass、完整参数与行为细节见 [tools/m4s-converter/README.md](tools/m4s-converter/README.md)。
- 转出的 MP4 为标准格式，可直接作为 `mindex prepare` 的输入素材。

详细设计见 [PLAN.md](PLAN.md)。
