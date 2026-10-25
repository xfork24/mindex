# CLAUDE.md

本文件为 Claude Code 提供项目技术导航。修改前先阅读 [AGENT.md](AGENT.md) 的通用协作约定；安装与用户操作见 [README.md](README.md)。历史验证记录不代表本次检查结果。

## 项目概览

Mindex：中文视频片段检索（文字找视频优先，支持视频/图片找相似片段）+ 人物库 + 创作工作流（文案生成视频，`/studio`）。本机 CPU 只运行切片、SQLite、FastAPI、ffmpeg 渲染和页面；大模型以独立 HTTP 服务运行在 GPU 机器上（WeMM 嵌入 `:4010`、ASR `:4020`、Qwen 分析 `:4040/v1`、qwen3-tts `:4030/v1`、可选 TransNetV2 `:18203`，本地脚本见 `scripts/`）。单 GPU 分阶段互斥执行，**不能假设多个模型服务同时可用**。

工作语言：文档、CLI 帮助、日志、错误消息均为中文，新代码保持一致。界面中英双语，中文原文是文案唯一源（见 Web/API 的 i18n）。

## 常用命令

```bash
uv sync --extra dev                              # 安装依赖（Python 3.11–3.13 + uv，需 ffmpeg/ffprobe；UI 回归另需 node）
uv sync --extra dev --extra person               # 追加人物检测/特征依赖（supervision、av）
uv run pytest -q                                 # 全部测试
uv run pytest -m "not slow" -q                   # 快速回归（跳过 slow 标记的重媒体用例，CI 默认）
uv run pytest tests/test_pipeline.py -q          # 单个文件
uv run pytest tests/test_pipeline.py::test_name  # 单个测试
uv run ruff check backend/src tests                      # lint（line-length 110，target py311）
for f in frontend/src/*/*.js; do node --check "$f"; done   # 静态 JS 语法检查
(cd desktop && npm ci && node --test test/*.test.js)      # 桌面外壳纯逻辑；node tests/desktop_smoke.cjs 为 Electron 冒烟
scripts/build-desktop-mac.sh                     # macOS 桌面版 DMG（ffmpeg 源码编译 → 冻结后端 → 冒烟 → electron-builder）
python scripts/build-messages.py --check           # messages.js 与 messages.json 一致
python scripts/build-assets.py --check             # 页面 /assets/ 的 ?v= 与内容哈希一致（不带 --check 则改写）
uv run mindex serve                              # 启动 http://127.0.0.1:8000（/docs、/admin）
uv run mindex clean [--apply]                    # 磁盘清理：默认预览，--apply 才删（docs/cleanup.md）
```

配置从 `config.example.yaml` 复制为 `config.local.yaml`（gitignored）。`--config` 必须放在子命令之前（`uv run mindex --config x.yaml index`），或设 `MINDEX_CONFIG`。配置模型为 strict（`extra="forbid"`），未知键会校验失败。

## 架构

前端位于 `frontend/src/`，后端位于 `backend/src/`；两侧按 `search/`、`admin/`、`persons/`、`studio/`、`settings/` 划分页面功能，前端公共资源在 `frontend/src/shared/`。后端处理管线、模型、维护及诊断分别位于 `processing/`、`models/`、`maintenance/`、`diagnostics/`。使用新导入路径，不保留旧模块转发；完整目录及资源打包方式见 `docs/architecture.md`。下文 Python 模块路径相对于 `backend/src/`。

### 离线管线（分阶段、可断点续跑）

`prepare`（=detect+split，纯本地 CPU）→ `transcribe` → `align` → `analyze` → `index --activate`。各阶段有检查点，失败后重跑同一命令即可恢复；`data/pipeline.lock` 文件锁互斥。`ingest` 快捷命令要求全部服务同时可用，单 GPU 场景禁用。

- `processing/pipeline.py` — 各阶段编排（导入去重、分镜、切片、转写、分析）
- `shared/shot_media.py` — 统一 shot 媒体服务（时间基准转换、截图/抽帧、PCM 提取、跨镜 H.264/AAC 重建）；业务读取一律经它，不直读旧切片路径
- `maintenance/clips.py` — `mindex clips migrate/materialize/verify-archives/repair-boundaries/prune`：旧切片物理迁移与恢复，默认只读，`--apply` 才动文件
- `processing/subtitle_uploads.py` / `processing/subtitle_archive.py` — 外部字幕上传（不可变登记，对白任务启动才应用）与归档校验（SHA256）
- `processing/indexing.py` — 索引构建、发布前检查、`VectorIndex`（Chroma 封装）
- `cli.py` — Typer 入口（`mindex`）：全局选项、核心命令与 `indexes` 组，并注册子命令组 `e2e`（`cli_e2e.py`）、`persons`（`cli_persons.py`）、`clips`（`cli_clips.py`）、`topics`（`cli_topics.py`）、`backup`（`maintenance/backup_cli.py`）、`agent` 与顶层 `mcp`（`cli_agent.py`）；`shared/cli_support.py` 放共用命令组与输出函数；重模块在命令函数内延迟导入
- `diagnostics/e2e.py` — `mindex e2e` 分阶段真实验证框架（init/probe/pin/phase/freeze），阶段：smoke → visual → preview → asr → align → analyze(-smoke) → enriched → evaluate → video-evaluate → check
- `models/model_server.py` — GPU 侧服务（`mindex-model-server`，角色独立 venv，见 `docs/DEPLOYMENT.md`）
- `models/llama_cpp.py` — 对用户已有 llama-server 的原生协议适配（`/v1/embeddings` input_video）

### 模型适配层（models/adapters.py 与各后端模块）

十个 Protocol：`MultimodalEmbedder`、`VisualAnalyzer`、`Transcriber`、`Aligner`、`ShotDetector`、`Speaker`（TTS）、`Storyboarder`（拆分镜 LLM）、`Writer`（文案创作 LLM，`models/writer.py`）、`VideoGenerator`（AIGC 视频生成，`models/video_generators.py`：comfyui/minimax/dashscope）、`Animator`（AI 动画 LLM，`models/animator.py`），由 `make_*` 工厂按配置的 backend 构造（`mindex` 自定义服务 / `llama_cpp` / `openai` / 本地 adaptive / 远程 transnet；线上订阅另有 embedding `tokenhub`、asr `openai`（chat+input_audio）与 `minimax`（词级时间戳）、tts `minimax`/`dashscope` 专用适配器）。替换模型 = 实现协议接入新 backend，不改检索管线。

### 人物库（/persons）

演员/角色分身份建模：角色可跨电影复用，人物操作不绑定具体素材。`persons/persons.py` 库与发布模型、`persons/person_api.py` 页面与 API、`persons/person_server.py` GPU 侧检测/特征服务（独立入口 `mindex-person-server`）、`persons/person_processing.py`/`persons/person_worker.py` 扫描管线（`mindex persons process`，与素材处理工作台共用后台任务机制）、`persons/person_frames.py` 帧布局。图片查询走 `models/image_embedding.py`，不改变既有文本/视频空间指纹。人物依赖 `uv sync --extra person`；确定性跨服务端到端覆盖见 `tests/test_recall_e2e.py`。

人物图片布局：`data/persons/references/`、`imports/` 保存原尺寸 PNG；`frames/<source_id>/` 保存内容寻址的展示 JPEG。旧库用 `mindex persons migrate-frames --apply` 迁移，不需重新索引。冗余帧由 `clean` 的 `person_frames` 规则按确认镜头/身份保留策略清理。

### 创作工作流（studio，`/studio`）

新任务走版本工作台（`studio/workspace/`）：方案级 `script` 文案 → `split` 拆分镜 → 逐镜阶段（按分镜画面来源 `Shot.source`：recall 为 recall/tts/select/render，clip 为 tts/select/render，image 为 tts/render，aigc 为 tts/generate/select/render，animation 为 tts/animate/render）→ `compose` 成片合成；老任务保留 `split → recall → tts → select → render` 五步状态机（`studio/studio.py` + `studio_steps.py`）。任务即 `data/studio/<job_id>/` 目录（`job.json` + `workspace.json` + `versions/` + `builds/` + `inputs/`，业务状态不进 SQLite；只有用户文案模板在 `script_templates` 表）；全局 `studio.lock` 单执行互斥；重跑只作废依赖闭包内的下游。召回与配音只依赖拆分镜（可任意先后，适配单 GPU）；选片 = 原片时间区间（可跨连续 shot，长镜头优先裁剪 + 同源可见相邻延长 + 配音有限加速 + 冻结帧兜底）。分镜与选片支持人工编辑（PATCH 级联作废）。烧录字幕依赖 ffmpeg libass（缺失报错可切软字幕）。检查点按输入指纹与文件哈希复用，支持 resume/force 和指定分镜重做；attempt 产物不可变留档，帧/采样共同时间轴避免漂移。模块：`studio/studio_artifacts.py`（检查点/产物/诊断）、`studio/studio.py`（状态机）、`studio/studio_steps.py`（编排）、`studio/studio_select.py`（选片纯算法）、`studio/studio_render.py`（渲染）、`studio/executor.py`（单执行线程、studio 锁、取消事件与哨兵、后台 worker 包装）、`studio/workspace/`（版本工作台包：`versions` 版本记录与落盘、`plans` 方案与分镜编辑、`persistence` 读取与旧任务迁移、`signatures` 阶段参数/签名/复用、`preflight` 预检、`media_nodes` 选片对齐/渲染/合成、`builder` 执行/构建/恢复、`details` 详情视图、`stages` 阶段注册表（`StageHandler`/`REGISTRY`/`shot_stages`，处理器在 `handlers/`，新增阶段或镜头来源只需实现并 `register()`）；调用方 `from mindex.studio import workspace as ws`，签名快照见 `tests/test_studio_signature_snapshot.py`）、`studio/studio_api.py`（路由）、`studio/studio_templates_api.py`（`/api/studio/templates`）、`studio/studio_diagnostics.py`（方案诊断）、`studio/animation_capture.py`（AI 动画沙箱逐帧录制，可选 extra `animation`）；来源文件定位与复验在 `studio/workspace/sources.py`。方案画幅 `options.canvas`/`fit`（分辨率 `studio.render.canvases`，16:9 恒为 `render.width/height`）只进 render/compose 签名且仅非默认时加键。详见 `docs/studio.md`。

**文案创作（`script` 阶段）**：新任务只保存视频大纲（`job.json` 的 `outline`，无 `input_text`）；文案是版本工作台里方案级的版本化阶段 `script`（`studio/workspace/` 包），由 `writer` 模型角色（`studio.writer`，缺省复制 `studio.generator`；`models/writer.py` 的 `QwenWriter`，温度 0.7，改提示词须递增 `WRITER_PROMPT_VERSION`）按方案选择的文案模板（`options.template_id`，缺省 `base`；内置六个在 `studio/templates/*.yaml`，用户模板在 `script_templates` 表，`studio/script_templates.py`；script 签名含模板内容哈希，换模板只提示 `script_template_changed`、不自动重写）按大纲与方案参考文稿（`options.references`，`studio/workspace/references.py`，存 `inputs/<sha256>.txt`，执行前复验摘要，有登记时签名加 `references`）生成，可手动保存（`origin=manual`）。构建顺序为 script → split → recall/tts → select → render → compose：split 版本的 `inputs` 记录所用 `script` 版本，依赖树因此追溯到文案；方案的 `board_script != script` 即分镜过期（`script_stale`），预检把 split 列为重建；已有文案时构建直接复用，不重新生成。老任务的 `input_text` 在 `workspace.load()` 里确定性迁移为 `origin=legacy` 的已完成文案版本，行为不变。旧 `/steps`、`/run` 与 `studio_steps` 只服务老任务（`studio.legacy_text`），新任务返回 422。测试里用 `conftest.legacy_job` / `convert_to_legacy` 构造老任务形态。

### 选题挖掘（topics，`/studio/topics`）

路线图 M2，详见 `docs/topics.md`，spec 在 `.kiro/specs/topic-discovery/`。业务只在 `topics/` 服务层：`profiles`（领域档案与写作约束快照）、`sources/`（`TopicSource` 注册表：rss/hotlist/brave/calendar/local，网络一律经 `sources/base.http_get`，测试替换 `base.TRANSPORT`）、`datasources`（来源配置、试抓、抓取、日历导入）、`items`（去重入库）、`cluster`/`summarize`/`scoring`/`coverage`/`outline`（流水线各步，选题模型经 `llm.make_llm`，缺省复用 `studio.writer`，测试经 `create_app(topic_llm=...)` 注入）、`cards`、`runner`（`TopicRuns(BackgroundTaskRunner)`，表 `topic_runs`，全局 `topics` 锁）、`create_job`（选题 → 创作任务：方案 `template_id`/画幅、参考文稿、任务目录 `profile.json` 与方案顶层键 `profile_hash`）、`gaps`、`scheduler`（`mindex serve` 按 `topics.scheduler.enabled` 传 `create_app(scheduler=True)`，启动时补跑）、`api`（`/api/topics`）。四个入口（页面、`mindex topics`、REST、智能助手 `topics` 领域与 MCP）对照表在 `topics/capabilities.py`，`tests/test_topics_parity.py` 逐项检查；新增能力先登记。档案写作约束只在方案有 `profile_hash` 时进入 script 签名与 writer 提示词（`Writer.write(profile=...)`），其余任务签名逐字节不变。数据在 metadata 迁移 v3 的 `topic_*` 表。

### 核心不变量

新版版本工作台支持逐镜 `ShotRecall`（文字/人物、素材过滤、图片、视频）。创建任务时可限定可用素材（`job.json` 的 `source_ids`，全选=不限，创建后只读、不进方案参数）；逐镜素材范围只能在其内二次选择，两者取交集，空交集报错不退化为全库。查询媒体位于任务 `inputs/<sha256>.<suffix>`，执行前校验摘要；召回签名绑定内容索引与人物发布组合。后台镜头详情独立读取 ASR、字幕、LLM、人物覆盖和证据，不触发推理。

- **嵌入空间身份**：服务必须在 `/health` 和每次向量响应中携带身份（model/revision/quantization/dims/预处理/implementation 指纹，`config.fingerprint` 计算）；查询身份与活动索引不符返回 503，禁止跨空间比较向量。
- **索引不可变**：换模型、量化、维度、预处理或分析配置 → 新建独立索引版本，从不覆盖；发布前 `check`，激活在 SQLite 事务内完成。回退 = 先切回旧模型服务/配置，再 `indexes activate 旧索引ID`。索引冻结描述与对白快照，后续分析更新不影响已发布索引。
- **切分布局不可变**：detector/video 切分参数变化须使用新 `data_dir`，不覆盖已有布局。
- **SQLite 是事实来源**：Chroma 只存显式向量（`embedding_function=None`），业务数据通过 segment/representation ID 关联回 SQLite。
- **对齐环境隔离**：强制对齐用独立 CPU venv `.venv-align`（依赖安装在该独立环境中，仓库已不提供对齐依赖清单），`processing/align_worker.py` 以独立子进程运行，绝不装入应用环境；`mindex align .venv-align/bin/python`（解释器为位置参数，`--python` 选项属于 `mindex e2e phase align`）。

### 存储布局（data_dir）

`sources/`（原片，内容哈希去重）、`plans/`（分镜计划，原子写入）、`clips/`、`thumbnails/`、`reports/`、`transcripts/`（30 秒块检查点）、`chroma/`、`context/`（转码缓存）、`studio/`（创作任务目录：job.json、workspace.json、versions/、builds/、inputs/；老任务另有 storyboard/candidates/audio/timeline/render）、`persons/`（人物参考图/导入/展示帧）、`locks/`（素材库锁、处理锁、取消哨兵 `cancel/`）、`metadata.sqlite3`、`agent.sqlite3` 与 `agent-attachments/`（智能助手，不进备份）。各目录的清理分级与保留策略见 `docs/cleanup.md`（`mindex clean`：A 级每次删、B 级按 `clean` 配置过期删、C 级只报告）。

库结构变更走版本化迁移（`shared/migrations.py`，`PRAGMA user_version`）：metadata 库在 `shared/storage.py` 的 `METADATA_STEPS`、`agent.sqlite3` 在 `agent/schema.py` 的 `STEPS` 末尾追加步骤，不改已发布步骤、不在 `Store` 初始化里散写 ALTER。数据目录锁统一经 `shared/locks.py`（`named` / `acquire_ordered`），多把锁按 pipeline → index-write → library → register → studio → topics 顺序获取，逆序会被拒绝。

### 检索（search/search.py）

新版索引文字查询按 `visual`、`dialogue`、`analysis` 三渠道召回：对白与分析内部先融合语义向量和 BM25，再按 `search.channel_weights`（默认 1:1:1）做 RRF，最后同源 IoU 去重。旧格式索引仍保留 video/description/style/dialogue 向量与 lexical 的融合路径（`search.weights`），不要混淆两组配置。新版文本查询在嵌入不可用或空间不匹配时，可在有可用文本渠道的条件下显式降级 BM25，并返回 `degraded`/`warnings`；禁止绕过空间校验比较向量。图片与视频查询只检索画面表示；视频拆 4s/3s 窗口，不入库、清理临时文件。在线无 LLM 实时重排，分数不是概率。

### Web/API

`api.py`（应用装配 + create_app）、`search/api.py`（检索与媒体 API）、`admin/admin.py`（素材后台 API）、`admin/indexes.py`（`/api/indexes` 索引版本列表/检查/激活）、`agent/api.py`（`/api/agent`）、`shared/web_auth.py`（可选 `MINDEX_WEB_TOKEN`：`/login`，`next` 只收站内路径，`/logout` 仅 POST）、`settings/settings_api.py`（设置 API）、`studio/studio_api.py`（创作 API）、`persons/person_api.py`（人物 API）、`frontend/src/`（原生 HTML/JS，无前端框架与打包；请求统一经 `shared/api.js` 的 `window.MindexApi`；创作页拆为按序加载的 `studio/studio-*.js` 经典脚本，启动代码在最后的 `studio-main.js`；资源 `?v=` 由 `scripts/build-assets.py` 写为内容哈希；五个页面：`/` 检索、`/admin` 素材处理工作台、`/studio` 创作（子页面 `/studio/topics` 选题挖掘，`studio/topics.html` + `topics.js`；`/studio/templates` 文案模板管理，`studio/templates.html` + `templates.js`；标签顺序为创作任务、选题挖掘、文案模板）、`/settings` 设置、`/persons` 人物库）。`/admin` 直接读 SQLite，不依赖活动索引，支持分步启动分镜/转写/分析/人物处理（`admin/source_tasks.py`/`admin/dialogue_tasks.py` 后台任务，`shared/background_tasks.py` 公共生命周期，`shared/task_progress.py` 持久化进度，`shared/processing_lock.py` GPU 互斥）；curation（显示名/备注/收藏/隐藏，`admin/curation.py`）与不可变索引快照分离，hidden 在召回前过滤。`/studio` 的召回依赖活动索引与嵌入服务（身份校验由 SearchEngine 继承），步骤执行为后台线程 + 前端轮询。

**i18n**：文案目录 `frontend/src/shared/messages.json`（约 2290 键，中文原文为唯一源），键为 `ui.` 前缀（语义名或中文原文 sha1 前 12 位）；浏览器侧镜像 `messages.js` 由 `python scripts/build-messages.py` 从 json 生成，不要手工编辑（`tests/test_i18n.py` 用 `--check` 校验）。`frontend/src/shared/i18n.js` 负责语言切换，不重建 DOM、保留未提交输入（`tests/i18n_ui.cjs` 回归）。后端 `shared/i18n.py` 把中文消息反向映射为稳定 `message_key`/`message_params`（未匹配回落 `system.diagnostic`），供前端按键展示译文。

### 桌面版（desktop/，macOS Apple Silicon）

Electron 外壳 + PyInstaller 冻结的 `mindex serve`（sidecar），窗口加载同一套 Web 前端，与 Web 版同步演进、不维护第二份业务代码（`docs/desktop.md`，决策见 `plans/MACOS_NATIVE_PLAN.md`，任务见 `.kiro/specs/macos-desktop/`）。外壳（`desktop/main.js` + `lib/`）负责单实例、持久化回环端口（`localStorage` 按 origin 隔离）、每次启动的随机 `MINDEX_WEB_TOKEN`（对本 origin 注入 Bearer 与会话 Cookie）、轮询 `GET /api/health`（`version`/`pid`/`busy`）、窗口安全（`contextIsolation`、`sandbox`、导航限本 origin）、下载与菜单、菜单栏/托盘常驻（`lib/tray.js`：关闭窗口只隐藏，sidecar 继续运行每日选题定时；偏好 `desktop-state.json` 的 `keep_in_background`，页面经 `MindexDesktop.background` 读取）、退出时先 `POST /api/settings/local-services/stop-on-exit` 再停后端进程组；用户数据在 `~/Library/Application Support/Mindex`。冻结入口 `desktop_entry.py` 分发 `mindex -m <模块>`（现有 `sys.executable -m` 子进程无需改），`desktop_check.py` 是冻结环境自检；`desktop/sidecar/mindex.spec` 的 `SOURCE_MODULES` 以源码收集自哈希模块（`llama_cpp.py` 的文件哈希属于嵌入空间身份，不要为打包改它）。随包 ffmpeg 由 `scripts/build-desktop-ffmpeg.sh` 从固定源码编译（GPL：libx264、libass[CoreText]、dav1d），后端经 `MINDEX_FFMPEG`/`MINDEX_FFPROBE` 定位（`shared/media.py`），路径不进签名。本机模型服务管理（`settings/local_services.py`，配置 `local_services`，`exclude=True` 不进快照）只能启停配置文件里定义的命令；AI 动画录制组件按需安装见 `settings/browser_install.py`。

### 智能助手（agent/，全局面板）

`agent/` 是与运行方式无关的工具层 + 可替换运行时，详见 `docs/agent.md`。`llm/` 为 dsh 对齐的词汇与 OpenAI 兼容 / Anthropic 两种流式工具调用适配器（`scripted.py` 是测试专用脚本化模型，经 `create_app(agent_llm=...)` 注入）；`registry.py` 工具定义（read/write/danger，每个工具属于一个领域）；`tools/` 全部经 `loopback.py` 的进程内 ASGI 回环调用既有 REST 接口（内部凭证由 `web_auth` 放行；拒绝含 `.`/`..`/`?`/`#` 的路径），不复制业务逻辑；`policy.py` 分级确认；`runtime.py` 的 `BuiltinRuntime` 跑轮次循环，事件为 dsh 形状、存 `data_dir/agent.sqlite3`（不进备份，含密钥保险库）；`watcher.py` 的 `TaskWatcher` 跟踪构建与素材/对白/人物任务并在终态发 followup；`attachments.py` 附件暂存（`attachment://`，文件在 `data_dir/agent-attachments/`，clean 规则 `agent_attachments` 回收）；`vision.py` 在发请求前把图片引用解析为图片块（`agent.llm.vision`，日志只存引用）；`api.py` 为 `/api/agent`；`mcp_server.py` 导出 MCP（`mindex mcp`、可选 `/mcp`，按 `agent.mcp.domains` / `--domain` 过滤领域）。`changes.py` 是全局改动与定位通道（`agent_changes` 表，`/api/agent/changes` SSE，内置助手与 MCP 都发布）；工具与预览返回 `focus` 定位描述，`tools/navigate.py` 供模型主动切页。前端 `frontend/src/shared/agent-panel.js` 被五个页面引入（Markdown 经 `agent-markdown.js` + 仓库内固定版本的 `marked.umd.js`/`purify.min.js` 渲染，见 `THIRD_PARTY_NOTICES.md`；支持附件选择、拖放与粘贴）；页面通过 `window.MindexAgentContext` 提供上下文、实现 `window.MindexAgentFocus(focus, {passive})` 定位，并监听 `mindex:agent-mutation` 刷新。工具可附带 `media`（`tools/media.py` 构造，只允许本站 `/api/` 地址），面板据此显示缩略图网格、大图浏览与音视频。

工具按领域动态加载（领域名在 `shared/agent_domains.py`：`meta`、`studio`、`studio_detail`、`library`、`search`、`index`、`persons`、`settings`、`topics`）：每步只下发 meta 与会话已加载领域的工具；当前页面领域每轮自动加载，模型用 `load_tools` 加载其他领域，非 meta 领域按 LRU 最多保持 3 个（持久化在 `agent_sessions.loaded_domains`），调用未加载领域的工具返回 `TOOL_NOT_LOADED`。新增工具：在对应领域的 `tools/` 模块实现并在 `specs()` 注册（新领域须同时加入 `shared/agent_domains.py` 的 `AGENT_DOMAINS` 与 `agent/domains.py` 的 `DOMAIN_INFO`），写操作提供 `preview`，读写都给出 `focus`，有媒体时给出 `media`、需要模型看图时给出 `images`；决定是否导出 MCP（依赖界面交互的设 `mcp=False`）；不提供调用物理删除接口（删除素材、删除演员表导入图片）的工具，只删记录的删除与 GPU 任务、发布、激活、构建类工具为 danger；同步 `docs/agent.md` 的工具表与相关测试中的工具数量断言。单个领域的工具数保持在模型能稳定选用的规模（目前最大的 studio 领域为 28 个），过大时拆分领域。

### 设置页与热生效（/settings）

十个模型角色（`settings/runtime_settings.py` 的 `ROLES`：理解/文案创作/拆分镜/嵌入/ASR/TTS/视频生成/动画生成/人物识别/智能助手）的「配置档案」存 SQLite（`settings/runtime_settings.py`：`model_profiles`/`voices` 表 + settings KV 覆盖层），保存即热生效：Web 经 `RuntimeConfig.current()` 每请求/每次任务启动解析，CLI 在 `load_config` 后统一合并（`mindex e2e`、`clips`、`backup` 除外，可用 `--no-runtime-overrides` 冻结）。支持线上厂商订阅（智谱/DeepSeek/阿里/腾讯 TokenHub/MiniMax，预设+自定义+测试连接）与自建服务互切；`settings/voice_clone.py` 提供 MiniMax 上传克隆与阿里 URL 复刻。密钥明文存库但接口只出掩码，`Service.api_key` 带 `exclude=True` 不进任何指纹/快照。切换嵌入档案后身份指纹变化，检索 503 直至 `mindex index --activate` 重建（页面横幅常显匹配状态）。详见 `docs/settings.md`。

### 独立服务与数据维护

- 人物服务：`scripts/setup-person-service.sh` 安装 `.venv-person`，`scripts/run-person-service.sh` 启动；依赖在 `deploy/person.txt`。主环境的 `--extra person` 不等于已安装人物推理服务。
- 本地 TransNetV2：`scripts/setup-transnet-service.sh` 安装 `.venv-detector` 并核验权重，`scripts/run-transnet.sh` 启动；依赖在 `deploy/detector-local.txt`。安装脚本会下载依赖或权重。
- 备份：`maintenance/backup_cli.py` 注册 CLI，`backup.py` 编排，`backup_db.py`/`backup_vectors.py` 处理数据。使用 `mindex backup export/inspect/import/recover`，不要直接复制运行中的 SQLite/Chroma 作为一致备份。导入前停写并预览，保留迁移日志供中断恢复，详见 [数据备份与迁移](docs/backup.md)。
- 目录迁移后运行 `uv sync --extra dev` 刷新主环境；独立服务环境可用 `uv pip install --python .venv-person/bin/python --no-deps -e .` 刷新项目映射，检测环境同理。

## 测试约定

- 测试使用真实 FFmpeg、PySceneDetect、SQLite、Chroma 和 HTTP API（集成式，无网络也能跑）；模型用 `conftest.py` 中明确标记的确定性 Fixture 适配器。
- UI 回归测试为 `tests/*_ui.cjs`（Playwright 驱动真实页面，`serve_fixture.py` 起隔离素材库），由对应 `test_*.py` 以子进程运行 `node`，无 node 自动跳过；playwright 模块可用 `MINDEX_PLAYWRIGHT_MODULE` 指定。
- **不能用模拟向量冒充真实检索结果**；服务未就绪时页面/API 显示明确未就绪状态。
- 自动化测试只验证工程链路，不证明真实模型中文检索质量；真实验收走 `mindex e2e` 阶段并记录到 `docs/VERIFICATION.md`。

## 关键文档

- `docs/` — 系统性参考文档：架构、离线管线、CLI、配置、检索、索引版本、e2e 框架、素材后台、人物库、创作工作流、设置、智能助手、清理（索引见 `docs/README.md`）；附带工具见 `tools/m4s-converter/README.md`；`docs/reviews/2026-10/` 为最近一次审查报告
- `plans/` — 计划与设计决策文档（索引见 `plans/README.md`）；`plans/ROADMAP.md` 为功能路线图
- `plans/PLAN.md` — 设计决策、验收标准（40 条测试集 Hit@5 ≥85%）
- `docs/FILM_WORKFLOW.md` — 整部电影处理流程（visual 模式只需 WeMM）
- `docs/DEPLOYMENT.md` — GPU 服务部署、各角色环境、HTTP 服务协议
- `docs/E2E_RUNBOOK.md` / `plans/E2E_TEST_PLAN.md` — 端到端真实验证操作手册
- `eval/README.md` — 评测查询标注说明
