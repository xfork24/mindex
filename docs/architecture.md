# 架构与模块详解

Mindex 是中文视频片段检索系统：文字找视频优先，支持视频找相似片段。本机 CPU 只运行切片、SQLite、FastAPI 和页面；大模型以独立 HTTP 服务运行在 GPU 机器上。单 GPU 分阶段互斥执行，**不能假设多个模型服务同时可用**。

## 总体数据流

```
离线（分阶段、可断点续跑，pipeline.lock 互斥）

  原视频 ──prepare(=detect+split，纯本地 CPU)──┐
                                              ├─→ clips/ + thumbnails/ + plans/
  原视频 ──transcribe──→ transcripts/（30 秒块检查点）
                └─(llama_cpp / openai 后端)→ 原始文本 ──align(独立 CPU venv)→ 时间戳对白
  片段+对白 ──analyze(Qwen)──→ annotations + scene_plans + scene 片段
  片段+描述+对白 ──index(WeMM)──→ chroma/ 向量 + index_members 快照
                                        │
                                   check 通过 → activate（SQLite 事务）

在线（只调用嵌入服务，无 Qwen 实时重排）

  文字查询 ──WeMM 文字向量──┐
                            ├─→ 四路向量召回 + jieba BM25 → 加权 RRF → IoU 去重 → 结果
  视频查询 ──4s/3s 窗口──→ 仅视频表示召回 → 窗口等权融合 ──────────────┘
```

## 模块职责

全部源码在 `src/mindex/`，约 9000 行。

| 模块 | 职责 |
|---|---|
| `cli.py` | Typer CLI 入口（`mindex`）；重模块在命令函数内延迟导入，`--config` 必须放在子命令之前 |
| `pipeline.py` | 各阶段编排：导入去重、分镜、切片、转写、对齐、分析；`pipeline.lock` 文件锁 |
| `indexing.py` | `IndexBuilder`（建索引、发布前检查）与 `VectorIndex`（Chroma 封装） |
| `search.py` | `SearchEngine`：五路召回、RRF 融合、去重；`status()` |
| `api.py` | FastAPI `create_app`：检索 API、媒体/上下文播放、静态页面 |
| `admin.py` + `curation.py` | 素材后台 API 与整理数据（显示名/备注/收藏/隐藏），独立于索引 |
| `runtime_settings.py` | 设置页数据层：模型配置档案（model_profiles）、音色库（voices）、业务覆盖键（settings KV）与 `apply_overrides` 热生效覆盖层 |
| `settings_api.py` | `/api/settings/*` 路由：档案 CRUD/激活/测试连接、业务调参、音色与克隆 |
| `voice_clone.py` | 厂商声音克隆客户端：MiniMax 上传克隆、阿里 DashScope URL 复刻 |
| `studio.py` | 创作任务状态机与 job.json 持久化（五步：split/recall/tts/select/render） |
| `studio_api.py` | `/api/studio/*` 路由：任务创建、轮询、后台线程执行、产物下载 |
| `studio_steps.py` / `studio_select.py` / `studio_render.py` | 创作各步骤编排、选片纯算法、ffmpeg 渲染合成 |
| `studio_artifacts.py` | 创作检查点/产物/诊断记录（attempt 不可变留档） |
| `adapters.py` | 模型适配层：七个 Protocol 与 `make_*` 工厂（见下） |
| `llama_cpp.py` | 对用户已有 llama-server 的原生协议适配（`/v1/embeddings` 的 `input_video`、`input_audio`）；部署清单核验 |
| `images.py` / `image_embedding.py` | 静态图片解码、方向/透明度/像素处理；扩展原生图片嵌入，保持既有文字/视频空间指纹 |
| `model_server.py` | GPU 侧自定义服务（`mindex-model-server`，角色独立 venv）：WeMMEngine / ASREngine / TransNetEngine |
| `e2e.py` | `mindex e2e` 分阶段真实验证框架（init/probe/pin/phase/freeze） |
| `evaluation.py` + `benchmark.py` | 评测（Hit@1/5、MRR@10、nDCG@10）与 `model-check` 单模型联调 |
| `film.py` | `inspect` 进度报告与 `export`（JSON/CSV/分页检查页） |
| `media.py` | FFmpeg/ffprobe 封装：探测、精确切片、缩略图、抽帧、帧时间戳 |
| `storage.py` | SQLite `Store`：建表、JSON 检查点、runs 记录、活动索引切换 |
| `align_worker.py` | 强制对齐独立子进程 worker（不导入 mindex/chromadb/GPU 运行时） |
| `analyzers.py` | `PinnedQwenAnalyzer`：把分析检查点绑定到实际 GGUF 部署 |
| `types.py` | Pydantic 模型：Granularity、Filters、TextSearch、Annotation、Utterance |
| `static/` | 原生 HTML/JS/CSS，无前端框架（检索页 + 后台页 + 创作页 + 设置页） |

## 模型适配层

七个 `Protocol`（`adapters.py`）：`MultimodalEmbedder`、`VisualAnalyzer`、`Transcriber`、`Aligner`、`ShotDetector`、`Speaker`（TTS）、`Storyboarder`（拆分镜 LLM）。替换模型 = 实现协议接入新 backend，不改检索管线。

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
├── voices/           # 克隆音色样本与预览音频（设置页）
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
| `settings` | KV：`active` 活动索引指针、`active.profile.<role>` 档案激活指针、`override.search` / `override.studio.voice` 业务覆盖键 |
| `model_profiles` | 设置页的模型服务配置档案（角色/名称/厂商/连接配置 JSON/密钥，UNIQUE(role,name)） |
| `voices` | 音色库：预定义与克隆音色（厂商 voice_id、样本路径、状态） |

向量不是记录主键：表示 ID 是稳定主键，业务数据通过 segment/representation ID 关联回 SQLite；Chroma 显式接收向量（`embedding_function=None`）。

## 核心不变量

1. **嵌入空间身份**：自研 `mindex` 服务在 `/health` 与每次向量响应中携带身份（model/revision/quantization/dims/预处理/implementation 指纹，由 `config.fingerprint` 计算）；`llama_cpp` 后端由部署清单 + 运行时指纹核验；`tokenhub` 后端无服务端 space，身份纯由配置构造（见 [index-versions.md](index-versions.md)）。查询身份与活动索引不符返回 503，禁止跨空间比较向量。
2. **索引不可变**：换模型、量化、维度、预处理或分析配置 → 新建独立索引版本，从不覆盖；发布前 `check`，激活在 SQLite 事务内完成。索引冻结描述与对白快照，后续分析更新不影响已发布索引。
3. **切分布局不可变**：detector/video 切分参数变化须使用新 `data_dir`，不覆盖已有布局（`layout_key` 校验）。
4. **SQLite 是事实来源**：Chroma 只存显式向量，可通过表示 ID 完全重建对账。
5. **对齐环境隔离**：强制对齐用独立 CPU venv `.venv-align`，`align_worker.py` 以独立子进程运行，绝不装入应用环境。

## 在线服务

- `uv run mindex serve` 启动 <http://127.0.0.1:8000>：检索页 `/`、后台 `/admin`、创作页 `/studio`、设置页 `/settings`、OpenAPI 文档 `/docs`。
- 页面与 API 在无素材或服务未就绪时显示明确未就绪状态，**不用模拟向量冒充真实检索**。
- 检索 API 细节见 [search.md](search.md)，管理 API 见 [admin.md](admin.md)，设置页见 [settings.md](settings.md)。

## 进一步阅读

- 各阶段机制与检查点：[pipeline.md](pipeline.md)
- 索引版本与回退：[index-versions.md](index-versions.md)
- GPU 服务部署与 HTTP 协议：[DEPLOYMENT.md](DEPLOYMENT.md)
