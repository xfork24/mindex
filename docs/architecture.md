# 架构与模块详解

Mindex 是中文视频片段检索系统：文字找视频优先，支持视频找相似片段。本机 CPU 只运行切片、SQLite、FastAPI 和页面；大模型以独立 HTTP 服务运行在 GPU 机器上。单 GPU 分阶段互斥执行，**不能假设多个模型服务同时可用**。

## 总体数据流

```
离线（分阶段、可断点续跑；pipeline.lock 与 locks/ 下的处理锁互斥）

  原视频 ──prepare(=detect+split，纯本地 CPU)──┐
                                              ├─→ clips/ + thumbnails/ + plans/
  原视频 ──transcribe / enrich-dialogue──→ transcripts/（30 秒块检查点）、字幕轨道
                └─(llama_cpp / openai 后端)→ 原始文本 ──align(独立 CPU venv)→ 时间戳对白
  片段+对白 ──analyze(Qwen)──→ annotations + scene_plans + scene 片段
  片段 ──persons process(人物服务)──→ persons/ 特征与证据 ──人工审核──→ 发布人物证据
  画面 / 对白 / 分析 ──index(WeMM)──→ chroma/ 向量 + index_members 快照（按渠道增量、可复用兼容向量）
                                        │
                                   check 通过 → activate（SQLite 事务）

在线（只调用嵌入服务，无 Qwen 实时重排）

  文字查询 ──WeMM 文字向量──┐
    新版索引（format 2）：画面 / 对白 / 分析三渠道，对白与分析各自融合向量与 BM25
                            ├─→ 渠道权重 RRF → 同源 IoU 去重 → 结果（可叠加人物筛选）
    旧版索引（format 1）：四路向量召回 + jieba BM25 → 五路加权 RRF
  图片 / 视频查询 ──（视频按 4s/3s 窗口）──→ 仅画面表示召回 ────────────┘

创作（/studio）：文案 ─拆分镜→ 逐镜召回（上面的检索）与 TTS 配音 ─选片对齐→ FFmpeg 渲染 → 成片与产物
```

三渠道的细节见 [recall-enhancement.md](recall-enhancement.md)，旧版五路召回见 [search.md](search.md)。

## 模块职责

前后端分别放在仓库根目录的 `frontend/` 与 `backend/`，两侧按页面功能划分模块：

```text
mindex/
├── frontend/src/
│   ├── search/       # 视频检索 HTML/JS
│   ├── admin/        # 素材管理 HTML/JS/CSS
│   ├── persons/      # 人物库 HTML/JS/CSS
│   ├── studio/       # 创作 HTML/JS/CSS
│   ├── settings/     # 设置 HTML/JS/CSS
│   └── shared/       # 公共样式、国际化、播放器、侧栏、多选组件
└── backend/src/
    ├── api.py / cli.py / cli_*.py / __init__.py  # 应用装配、命令入口及 e2e/persons/clips 命令组、包信息
    ├── search/       # 视频检索、检索/媒体 API
    ├── admin/        # 素材管理、素材/对白任务、详情
    ├── persons/      # 人物库、扫描 worker、人物服务
    ├── studio/       # 创作状态机、工作台与各步骤
    ├── settings/     # 运行时设置、音色库、声音克隆
    ├── processing/   # 管线、索引、对白、字幕、镜头修复、对齐 worker
    ├── models/       # 模型适配、部署核验、模型服务
    ├── shared/       # 配置、类型、存储、媒体、锁、进度、国际化
    ├── maintenance/  # 清理、备份、切片迁移与恢复
    └── diagnostics/  # e2e、评测、benchmark、报告与检查页
```

源码直接位于 `backend/src/`，没有额外的 `mindex/` 目录；Python 的 `mindex` 命名空间由 `pyproject.toml` 的目录映射提供。各包保留原模块文件名，例如创作 API 从 `mindex.studio_api` 迁移到
`mindex.studio.studio_api`。仓库统一使用新导入路径，不提供旧模块兼容层。
`__init__.py` 不自动加载业务模块，保持 CLI 延迟加载与服务环境隔离。

页面路径 `/`、`/admin`、`/persons`、`/studio`、`/settings` 与 `/api/*` 接口不变。
静态资源由 `/assets/<功能模块>/<文件名>` 提供，源码位于 `frontend/src/<功能模块>/`。
`shared/web_assets.py` 统一定位资源：源码运行时读取仓库的 `frontend/src/`；
wheel 安装后读取随包的 `mindex/web/`。Setuptools 通过包目录映射将后端源码安装为 `mindex`，将前端资源打入 wheel 的 `mindex/web/`，
源码发布包也保留 `frontend/src/`。导出检查页和后端国际化使用同一份共享资源。
前端保持原生 HTML/JS/CSS，无需额外 Node 构建或单独启动前端服务器。

旧检出目录迁移后，主环境运行 `uv sync --extra dev` 刷新可编辑安装；
已有独立服务环境可运行 `uv pip install --python .venv-person/bin/python --no-deps -e .`
（检测环境对应 `.venv-detector/bin/python`）。这只更新项目路径，不要求重装模型依赖。

`cli.py` 保留 Typer 命令入口，重模块在命令函数内延迟导入；`--config` 必须放在子命令之前。
模型独立服务入口为 `mindex.models.model_server`，人物服务入口为
`mindex.persons.person_server`；`processing/align_worker.py` 保持独立子进程运行。

下表 Python 模块路径相对于 `backend/src/`。

| 模块 | 职责 |
|---|---|
| `cli.py` | Typer CLI 入口（`mindex`）：全局选项、核心命令、`indexes` 组；重模块在命令函数内延迟导入，`--config` 必须放在子命令之前 |
| `cli_e2e.py` / `cli_persons.py` / `cli_clips.py` / `cli_common.py` | `e2e`、`persons`、`clips` 命令组，以及共用的 JSON 输出函数 |
| `processing/pipeline.py` | 各阶段编排：导入去重、分镜、切片、转写、对齐、分析；`pipeline.lock` 文件锁 |
| `processing/indexing.py` | `IndexBuilder`（建索引、发布前检查）与 `VectorIndex`（Chroma 封装） |
| `search/search.py` | `SearchEngine`：新版三渠道召回与旧版五路召回、RRF 融合、去重、BM25 降级；`status()` |
| `api.py` + `search/api.py` | FastAPI 应用装配、页面注册；检索 API、媒体/上下文播放 |
| `admin/admin.py` + `admin/curation.py` | 素材后台 API 与整理数据（显示名/备注/收藏/隐藏），独立于索引 |
| `settings/runtime_settings.py` | 设置页数据层：模型配置档案（model_profiles）、音色库（voices）、业务覆盖键（settings KV）与 `apply_overrides` 热生效覆盖层 |
| `settings/settings_api.py` | `/api/settings/*` 路由：档案 CRUD/激活/测试连接、业务调参、音色与克隆 |
| `settings/voice_clone.py` | 厂商声音克隆客户端：MiniMax 上传克隆、阿里 DashScope URL 复刻 |
| `studio/studio.py` | 创作任务状态机与 job.json 持久化（五步：split/recall/tts/select/render） |
| `studio/studio_api.py` | `/api/studio/*` 路由：任务创建、轮询、后台线程执行 |
| `studio/studio_models.py` / `studio/studio_files.py` | 创作 API 的请求体模型与召回引用校验；产物下载、分镜预览、打包与任务删除路由 |
| `studio/studio_steps.py` / `studio/studio_select.py` / `studio/studio_render.py` | 创作各步骤编排、选片纯算法、ffmpeg 渲染合成 |
| `studio/studio_artifacts.py` | 创作检查点/产物/诊断记录（attempt 不可变留档） |
| `models/adapters.py` | 模型适配层公共部分：`ModelError` 等错误类型、带重试的 `request`、七个 Protocol 与 `make_*` 工厂（见下）；工厂调用时才导入后端 |
| `models/embedders.py` / `models/qwen.py` / `models/speech.py` / `models/storyboard.py` / `models/transcribers.py` / `models/detectors.py` | 各角色的后端实现：嵌入、Qwen 分析、TTS、拆分镜（含原文覆盖校验）、转写（含 MiniMax 词级时间戳解析）、分镜检测 |
| `models/llama_cpp.py` | 对用户已有 llama-server 的原生协议适配（`/v1/embeddings` 的 `input_video`、`input_audio`）；部署清单核验 |
| `shared/images.py` / `models/image_embedding.py` | 静态图片解码、方向/透明度/像素处理；扩展原生图片嵌入，保持既有文字/视频空间指纹 |
| `models/model_server.py` | GPU 侧自定义服务（`mindex-model-server`，角色独立 venv）：WeMMEngine / ASREngine / TransNetEngine |
| `diagnostics/e2e.py` | `mindex e2e` 分阶段真实验证框架（init/probe/pin/phase/freeze） |
| `diagnostics/evaluation.py` + `diagnostics/benchmark.py` | 评测（Hit@1/5、MRR@10、nDCG@10）与 `model-check` 单模型联调 |
| `diagnostics/film.py` | `inspect` 进度报告与 `export`（JSON/CSV/分页检查页） |
| `shared/media.py` | FFmpeg/ffprobe 封装：探测、精确切片、缩略图、抽帧、帧时间戳 |
| `shared/storage.py` | SQLite `Store`：建表、JSON 检查点、runs 记录、活动索引切换 |
| `processing/align_worker.py` | 强制对齐独立子进程 worker（不导入 mindex/chromadb/GPU 运行时） |
| `models/analyzers.py` | `PinnedQwenAnalyzer`：把分析检查点绑定到实际 GGUF 部署 |
| `shared/types.py` | Pydantic 模型：Granularity、Filters、TextSearch、Annotation、Utterance |
| `admin/source_tasks.py` / `admin/dialogue_tasks.py` / `admin/source_work.py` | 素材分步后台任务与对白任务；读取分镜、对白、分析、人物四项工作的真实结果 |
| `admin/segment_details.py` | 镜头详情：只读汇集对白、字幕、分析与人物覆盖，不触发推理 |
| `processing/channel_index.py` / `processing/embedding_coverage.py` | 三渠道增量索引构建、嵌入覆盖率统计 |
| `processing/dialogue.py` / `processing/subtitle_uploads.py` / `processing/subtitle_archive.py` | 对白处理（字幕优先、ASR）、外部字幕上传登记、字幕归档与 SHA256 校验 |
| `processing/shot_repair.py` | 显式、先备份的镜头边界修复，不删除已有媒体 |
| `persons/persons.py` / `persons/person_api.py` | 人物库与发布模型；`/api/persons/*` 路由与页面 |
| `persons/person_models.py` / `persons/person_names.py` | 人物请求/实体模型与小型校验；名称解析、文档提及识别与出镜区间查询辅助 |
| `persons/person_processing.py` / `persons/person_worker.py` / `persons/person_frames.py` / `persons/person_thumbnails.py` | 人物扫描管线与后台 worker、展示帧布局、缩略图 |
| `persons/person_server.py` | GPU 侧人物检测/特征服务（`mindex-person-server`） |
| `studio/studio_workspace.py` / `studio/studio_window.py` / `studio/studio_logs.py` | 方案、构建与版本工作台，定长选片窗口，只追加的执行事件日志 |
| `settings/voices_catalog.py` | TTS 音色目录：按当前合成后端聚合系统音色、本地音色库与克隆能力 |
| `shared/shot_media.py` | 统一镜头媒体服务：时间基准转换、截图/抽帧、跨镜重建；业务读取不直读历史切片路径 |
| `shared/processing_lock.py` / `shared/task_progress.py` | 处理锁（含 GPU 互斥）、任务进度持久化 |
| `admin/work_cache.py` | 素材工作台摘要缓存：先返回上次结果，后台刷新昂贵的覆盖统计 |
| `shared/trace.py` | 调用诊断记录槽：模型适配与 ffmpeg 调用写入，Studio 执行步骤时接管；无会话时不记录 |
| `shared/person_schema.py` | 人物库 SQLite 表结构、迁移与实体规范化，供 `Store` 初始化使用 |
| `models/compatibility.py` | 按部署绑定的嵌入兼容性门槛（六文本六视频基线） |
| `shared/uploads.py` / `shared/review_style.py` / `shared/web_assets.py` / `shared/jsonio.py` | 流式上传落盘与限额、导出复核页内嵌样式、静态资源定位、JSON 原子写入 |
| `shared/web_auth.py` | 可选访问令牌认证（`MINDEX_WEB_TOKEN`）：`/login`、`/logout`、Cookie 与 Bearer；未设置时不安装 |
| `shared/i18n.py` | 把中文消息映射为稳定 `message_key`，供前端展示译文 |
| `maintenance/clean.py` / `clean_index.py` / `clean_studio.py` / `purge.py` | 磁盘清理（分级规则）、索引与 Studio 清理规则、素材物理删除（保留源视频） |
| `maintenance/backup.py` / `backup_files.py` / `backup_cli.py` / `backup_db.py` / `backup_vectors.py` | 离线备份与迁移：导出导入编排、文件与校验辅助、CLI 注册、SQLite 与向量处理 |
| `maintenance/clips.py` | 旧切片迁移、物化、归档校验与边界修复 |
| `diagnostics/label_review.py` | 离线相关性复核页；确认仍由人工完成 |
| `frontend/src/` | 原生 HTML/JS/CSS，按五个页面划分模块；公共组件位于 `frontend/src/shared/` |

## 模型适配层

七个 `Protocol` 与 `make_*` 工厂在 `models/adapters.py`，后端实现分在同目录的 `embedders`、`qwen`、`speech`、`storyboard`、`transcribers`、`detectors` 模块：`MultimodalEmbedder`、`VisualAnalyzer`、`Transcriber`、`Aligner`、`ShotDetector`、`Speaker`（TTS）、`Storyboarder`（拆分镜 LLM）。替换模型 = 实现协议接入新 backend，不改检索管线。

| 工厂 | backend 值 | 实现 |
|---|---|---|
| `make_embedder` | `mindex`（默认） | `HTTPEmbedder`：本项目 `/health`、`/embed/text`、`/embed/video` 协议 |
| | `llama_cpp` | `LlamaEmbedder`：llama-server `/v1/embeddings` 原生 `input_video`，WeMM `<embedding>` 模板 |
| | `tokenhub` | `TokenHubEmbedder`：腾讯 TokenHub `/v1/embeddings/multimodal`（WeMM 系线上版），客户端抽帧、身份纯配置构造 |
| `make_transcriber` | `mindex` | `HTTPTranscriber`：`/transcribe` 直接返回带时间戳对白 |
| | `llama_cpp` | `LlamaTranscriber`：`input_audio` 只出原始文本，须再跑 CPU 对齐 |
| | `openai` | `OpenAIChatTranscriber`：OpenAI 兼容 chat + `input_audio`（如百炼 qwen3-asr-flash），同样只出原始文本 |
| `make_analyzer` | `openai`（默认） | `QwenAnalyzer`：OpenAI 兼容 `/v1/chat/completions` |
| | `llama_cpp` | `PinnedQwenAnalyzer`：额外核验部署清单与视觉投影 |
| `make_speaker` | `openai`（默认） | `OpenAISpeaker`：`/audio/speech`（本地 llama-server） |
| | `minimax` | `MiniMaxSpeaker`：t2a_v2（hex 音频解码） |
| | `dashscope` | `DashScopeSpeaker`：阿里 DashScope 原生协议 |
| `make_storyboarder` | `openai` | `QwenStoryboarder`：OpenAI 兼容 chat 拆分镜 |
| `make_detector` | `adaptive`（默认） | `AdaptiveShotDetector`：本地 PySceneDetect 逐帧检测 |
| | `transnet` | `HTTPShotDetector`：远程 TransNetV2 服务 `/detect/video` |

## 存储布局（data_dir）

```
data/
├── sources/          # 原片副本，按内容 SHA256 命名（导入去重）
├── plans/            # 分镜计划 SOURCE_ID.json（原子写入，编码失败不重扫）
├── clips/<source>/<kind>/   # H.264/AAC MP4 切片，kind=shot/moment/scene
├── thumbnails/       # JPEG 缩略图
├── reports/SOURCE_ID/  # export 输出：manifest.json、shots.csv、segments.csv、review.html
├── transcripts/      # 30 秒块检查点；llama_cpp / openai 后端有 raw/ 与 aligned-N.json
├── chroma/           # Chroma 持久化向量（collection 名 idx-<index_id>）
├── context/          # 上下文 ±3 秒转码缓存（按需生成）
├── capabilities/     # llama_cpp 六片段能力门槛记录（按空间身份命名）
├── deployments/      # e2e pin 生成的部署清单（wemm.json、asr.json、analyzer.json）
├── e2e/              # 分阶段实验目录（见 e2e-framework.md）
├── model-cache/      # 对齐 worker 的 HF 权重缓存
├── studio/           # 创作任务目录（storyboard/candidates/audio/timeline/render + job.json）
├── persons/          # 人物参考图（references/）、导入图（imports/）、展示帧（frames/<source_id>/）
├── subtitles/        # 字幕轨道解析缓存与外部字幕上传（<source_id>/uploads/）
├── locks/            # 素材库与按素材/工作类型的处理锁
├── cache/            # 可重建缓存：admin-work/、shot-media/
├── verification/     # 运行时验证产物（如对白召回最新结果）
├── voices/           # 克隆音色样本与预览音频（设置页）
├── pipeline.lock     # 全局管线文件锁
└── metadata.sqlite3  # 事实来源
```

### SQLite 表

| 表 | 内容 |
|---|---|
| `sources` | ID（内容 SHA256）、名称、路径、媒体信息、`layout_key`、`prepared` |
| `segments` | 稳定 ID、shot/moment/scene、原片起止、文件与缩略图路径、父镜头、切分依据 |
| `source_curation` / `segment_curation` | 整理信息：显示名、备注、收藏、隐藏、更新时间（与索引快照分离） |
| `transcripts` | 按素材 + `asr_key` 存转写：utterances 与状态 complete/no_audio/disabled/pending_alignment |
| `annotations` | 按片段 + `analysis_key` 存分析：结构化描述与对应对白 |
| `scene_plans` | 按素材 + `analysis_key` 存语义场景区间计划 |
| `index_versions` | 索引版本：spec 指纹、building/ready 状态 |
| `index_members` | 索引成员：片段的不可变快照与完成状态 |
| `representations` | 表示 ID → 索引/片段/类型（video/description/style/dialogue）/文本 |
| `runs` | 各阶段处理记录（时间、状态、错误） |
| `channel_data` | 按素材 + 渠道（visual/dialogue/analysis）+ profile 保存的三渠道处理结果 |
| `source_tasks` / `dialogue_tasks` | 素材工作台的分步后台任务与对白任务：配置快照、进度、结果、发布信息 |
| `search_releases` | 已发布检索组合：内容索引 ID + 人物发布 ID |
| `media_references` / `clip_migration_files` | shot 媒体对旧切片文件的引用，以及旧切片迁移的文件登记 |
| `person_entities` / `person_references` / `person_imports` | 演员、角色、作品等实体（按 kind 区分），参考图，演员表导入草稿 |
| `person_sources` / `person_tasks` / `person_features` / `person_evidence` / `person_coverage` / `person_versions` | 人物处理：素材与作品的关联、处理任务、特征缓存、出镜证据与审核、覆盖统计、人物发布版本 |
| `settings` | KV：`active` 活动索引指针、`active.profile.<role>` 档案激活指针、`override.search` / `override.studio.voice` 业务覆盖键 |
| `model_profiles` | 设置页的模型服务配置档案（角色/名称/厂商/连接配置 JSON/密钥，UNIQUE(role,name)） |
| `voices` | 音色库：预定义与克隆音色（厂商 voice_id、样本路径、状态） |

向量不是记录主键：表示 ID 是稳定主键，业务数据通过 segment/representation ID 关联回 SQLite；Chroma 显式接收向量（`embedding_function=None`）。

## 核心不变量

1. **嵌入空间身份**：自研 `mindex` 服务在 `/health` 与每次向量响应中携带身份（model/revision/quantization/dims/预处理/implementation 指纹，由 `config.fingerprint` 计算）；`llama_cpp` 后端由部署清单 + 运行时指纹核验；`tokenhub` 后端无服务端 space，身份纯由配置构造（见 [index-versions.md](index-versions.md)）。查询身份与活动索引不符返回 503，禁止跨空间比较向量。
2. **索引不可变**：换模型、量化、维度、预处理或分析配置 → 新建独立索引版本，从不覆盖；发布前 `check`，激活在 SQLite 事务内完成。索引冻结描述与对白快照，后续分析更新不影响已发布索引。
3. **切分布局不可变**：detector/video 切分参数变化须使用新 `data_dir`，不覆盖已有布局（`layout_key` 校验）。
4. **SQLite 是事实来源**：Chroma 只存显式向量，可通过表示 ID 完全重建对账。
5. **对齐环境隔离**：强制对齐用独立 CPU venv `.venv-align`，`processing/align_worker.py` 以独立子进程运行，绝不装入应用环境。

## 在线服务

- `uv run mindex serve` 启动 <http://127.0.0.1:8000>：检索页 `/`、后台 `/admin`、创作页 `/studio`、设置页 `/settings`、OpenAPI 文档 `/docs`。
- 页面与 API 在无素材或服务未就绪时显示明确未就绪状态，**不用模拟向量冒充真实检索**。
- 检索 API 细节见 [search.md](search.md)，管理 API 见 [admin.md](admin.md)，设置页见 [settings.md](settings.md)。

## 进一步阅读

- 各阶段机制与检查点：[pipeline.md](pipeline.md)
- 索引版本与回退：[index-versions.md](index-versions.md)
- GPU 服务部署与 HTTP 协议：[DEPLOYMENT.md](DEPLOYMENT.md)
