# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

Mindex：中文视频片段检索（文字找视频优先，支持视频/图片找相似片段）+ 人物库 + 创作工作流（文案生成视频，`/studio`）。本机 CPU 只运行切片、SQLite、FastAPI、ffmpeg 渲染和页面；大模型以独立 HTTP 服务运行在 GPU 机器上（WeMM 嵌入 `:4010`、ASR `:4020`、Qwen 分析 `:4040/v1`、qwen3-tts `:4030/v1`、可选 TransNetV2 `:18203`，本地脚本见 `scripts/`）。单 GPU 分阶段互斥执行，**不能假设多个模型服务同时可用**。

工作语言：文档、CLI 帮助、日志、错误消息均为中文，新代码保持一致。界面中英双语，中文原文是文案唯一源（见 Web/API 的 i18n）。

## 常用命令

```bash
uv sync --extra dev                              # 安装依赖（Python 3.11 + uv，需 ffmpeg/ffprobe；UI 回归另需 node）
uv sync --extra dev --extra person               # 追加人物检测/特征依赖（supervision、av）
uv run pytest -q                                 # 全部测试
uv run pytest tests/test_pipeline.py -q          # 单个文件
uv run pytest tests/test_pipeline.py::test_name  # 单个测试
uv run ruff check backend/src tests                      # lint（line-length 110，target py311）
node --check frontend/src/search/app.js            # 静态 JS 语法检查
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
- `cli.py` — Typer 入口（`mindex`，子命令组 `indexes`/`e2e`/`persons`/`clips`）；重模块在命令函数内延迟导入
- `diagnostics/e2e.py` — `mindex e2e` 分阶段真实验证框架（init/probe/pin/phase/freeze），阶段：smoke → visual → preview → asr → align → analyze(-smoke) → enriched → evaluate → video-evaluate → check
- `models/model_server.py` — GPU 侧服务（`mindex-model-server`，角色独立 venv，见 `docs/DEPLOYMENT.md`）
- `models/llama_cpp.py` — 对用户已有 llama-server 的原生协议适配（`/v1/embeddings` input_video）

### 模型适配层（models/adapters.py）

七个 Protocol：`MultimodalEmbedder`、`VisualAnalyzer`、`Transcriber`、`Aligner`、`ShotDetector`、`Speaker`（TTS）、`Storyboarder`（拆分镜 LLM），由 `make_*` 工厂按配置的 backend 构造（`mindex` 自定义服务 / `llama_cpp` / `openai` / 本地 adaptive / 远程 transnet；线上订阅另有 embedding `tokenhub`、asr `openai`（chat+input_audio）、tts `minimax`/`dashscope` 专用适配器）。替换模型 = 实现协议接入新 backend，不改检索管线。

### 人物库（/persons）

演员/角色分身份建模：角色可跨电影复用，人物操作不绑定具体素材。`persons/persons.py` 库与发布模型、`persons/person_api.py` 页面与 API、`persons/person_server.py` GPU 侧检测/特征服务（独立入口 `mindex-person-server`）、`persons/person_processing.py`/`persons/person_worker.py` 扫描管线（`mindex persons process`，与素材处理工作台共用后台任务机制）、`persons/person_frames.py` 帧布局。图片查询走 `models/image_embedding.py`，不改变既有文本/视频空间指纹。人物依赖 `uv sync --extra person`；确定性跨服务端到端覆盖见 `tests/test_recall_e2e.py`。

人物图片布局：`data/persons/references/`、`imports/` 保存原尺寸 PNG；`frames/<source_id>/` 保存内容寻址的展示 JPEG。旧库用 `mindex persons migrate-frames --apply` 迁移，不需重新索引。冗余帧由 `clean` 的 `person_frames` 规则按确认镜头/身份保留策略清理。

### 创作工作流（studio，`/studio`）

文案 → 成片的五步状态机：`split` 拆分镜 → `recall` 语义召回 → `tts` 配音 → `select` 选片对齐 → `render` 渲染合成。任务即 `data/studio/<job_id>/` 目录（状态即 job.json，不新建 SQLite 表）；全局 `studio.lock` 单步骤互斥；重跑只作废依赖闭包内的下游。召回与配音只依赖拆分镜（可任意先后，适配单 GPU）；选片 = 原片时间区间（可跨连续 shot，长镜头优先裁剪 + 同源可见相邻延长 + 配音有限加速 + 冻结帧兜底）。分镜与选片支持人工编辑（PATCH 级联作废）。烧录字幕依赖 ffmpeg libass（缺失报错可切软字幕）。检查点按输入指纹与文件哈希复用，支持 resume/force 和指定分镜重做；attempt 产物不可变留档，帧/采样共同时间轴避免漂移。模块：`studio/studio_artifacts.py`（检查点/产物/诊断）、`studio/studio.py`（状态机）、`studio/studio_steps.py`（编排）、`studio/studio_select.py`（选片纯算法）、`studio/studio_render.py`（渲染）、`studio/studio_api.py`（路由）。详见 `docs/studio.md`。

### 核心不变量

新版版本工作台支持逐镜 `ShotRecall`（文字/人物、素材过滤、图片、视频）。创建任务时可限定可用素材（`job.json` 的 `source_ids`，全选=不限，创建后只读、不进方案参数）；逐镜素材范围只能在其内二次选择，两者取交集，空交集报错不退化为全库。查询媒体位于任务 `inputs/<sha256>.<suffix>`，执行前校验摘要；召回签名绑定内容索引与人物发布组合。后台镜头详情独立读取 ASR、字幕、LLM、人物覆盖和证据，不触发推理。

- **嵌入空间身份**：服务必须在 `/health` 和每次向量响应中携带身份（model/revision/quantization/dims/预处理/implementation 指纹，`config.fingerprint` 计算）；查询身份与活动索引不符返回 503，禁止跨空间比较向量。
- **索引不可变**：换模型、量化、维度、预处理或分析配置 → 新建独立索引版本，从不覆盖；发布前 `check`，激活在 SQLite 事务内完成。回退 = 先切回旧模型服务/配置，再 `indexes activate 旧索引ID`。索引冻结描述与对白快照，后续分析更新不影响已发布索引。
- **切分布局不可变**：detector/video 切分参数变化须使用新 `data_dir`，不覆盖已有布局。
- **SQLite 是事实来源**：Chroma 只存显式向量（`embedding_function=None`），业务数据通过 segment/representation ID 关联回 SQLite。
- **对齐环境隔离**：强制对齐用独立 CPU venv `.venv-align`（`deploy/requirements-alignment.txt`），`processing/align_worker.py` 以独立子进程运行，绝不装入应用环境；`mindex align .venv-align/bin/python`（解释器为位置参数，`--python` 选项属于 `mindex e2e phase align`）。

### 存储布局（data_dir）

`sources/`（原片，内容哈希去重）、`plans/`（分镜计划，原子写入）、`clips/`、`thumbnails/`、`reports/`、`transcripts/`（30 秒块检查点）、`chroma/`、`context/`（转码缓存）、`studio/`（创作任务目录：storyboard/candidates/audio/timeline/render + job.json 状态机）、`persons/`（人物参考图/导入/展示帧）、`metadata.sqlite3`。各目录的清理分级与保留策略见 `docs/cleanup.md`（`mindex clean`：A 级每次删、B 级按 `clean` 配置过期删、C 级只报告）。

### 检索（search/search.py）

文字查询多路召回：视频、动作场景描述、情绪镜头描述、对白四类向量 + jieba BM25，加权 RRF 融合（默认 video/description/style 1、dialogue/lexical 0.5，k=60），同源 IoU>0.5 去重；三渠道权重可在设置页调参热生效。视频查询拆 4s/3s 窗口只检索画面表示，不入库、临时文件清理。在线只调用嵌入服务，无 Qwen 实时重排；分数不是概率。

### Web/API

`api.py`（应用装配 + create_app）、`search/api.py`（检索与媒体 API）、`admin/admin.py`（素材后台 API）、`settings/settings_api.py`（设置 API）、`studio/studio_api.py`（创作 API）、`persons/person_api.py`（人物 API）、`frontend/src/`（原生 HTML/JS，无前端框架；五个页面：`/` 检索、`/admin` 素材处理工作台、`/studio` 创作、`/settings` 设置、`/persons` 人物库）。`/admin` 直接读 SQLite，不依赖活动索引，支持分步启动分镜/转写/分析/人物处理（`admin/source_tasks.py`/`admin/dialogue_tasks.py` 后台任务，`shared/task_progress.py` 持久化进度，`shared/processing_lock.py` GPU 互斥）；curation（显示名/备注/收藏/隐藏，`admin/curation.py`）与不可变索引快照分离，hidden 在召回前过滤。`/studio` 的召回依赖活动索引与嵌入服务（身份校验由 SearchEngine 继承），步骤执行为后台线程 + 前端轮询。

**i18n**：文案目录 `frontend/src/shared/messages.json`（约 1800 键，中文原文为唯一源），键为 `ui.` 前缀（语义名或中文原文 sha1 前 12 位）；浏览器侧镜像 `messages.js` 必须与 json 同步（`tests/test_i18n.py` 校验）。`frontend/src/shared/i18n.js` 负责语言切换，不重建 DOM、保留未提交输入（`tests/i18n_ui.cjs` 回归）。后端 `shared/i18n.py` 把中文消息反向映射为稳定 `message_key`/`message_params`（未匹配回落 `system.diagnostic`），供前端按键展示译文。

### 设置页与热生效（/settings）

五个模型角色（理解/拆分镜/嵌入/ASR/TTS）的「配置档案」存 SQLite（`settings/runtime_settings.py`：`model_profiles`/`voices` 表 + settings KV 覆盖层），保存即热生效：Web 经 `RuntimeConfig.current()` 每请求/每次任务启动解析，CLI 在 `load_config` 后统一合并（`mindex e2e` 除外，可用 `--no-runtime-overrides` 冻结）。支持线上厂商订阅（智谱/DeepSeek/阿里/腾讯 TokenHub/MiniMax，预设+自定义+测试连接）与自建服务互切；`settings/voice_clone.py` 提供 MiniMax 上传克隆与阿里 URL 复刻。密钥明文存库但接口只出掩码，`Service.api_key` 带 `exclude=True` 不进任何指纹/快照。切换嵌入档案后身份指纹变化，检索 503 直至 `mindex index --activate` 重建（页面横幅常显匹配状态）。详见 `docs/settings.md`。

## 测试约定

- 测试使用真实 FFmpeg、PySceneDetect、SQLite、Chroma 和 HTTP API（集成式，无网络也能跑）；模型用 `conftest.py` 中明确标记的确定性 Fixture 适配器。
- UI 回归测试为 `tests/*_ui.cjs`（Playwright 驱动真实页面，`serve_fixture.py` 起隔离素材库），由对应 `test_*.py` 以子进程运行 `node`，无 node 自动跳过；playwright 模块可用 `MINDEX_PLAYWRIGHT_MODULE` 指定。
- **不能用模拟向量冒充真实检索结果**；服务未就绪时页面/API 显示明确未就绪状态。
- 自动化测试只验证工程链路，不证明真实模型中文检索质量；真实验收走 `mindex e2e` 阶段并记录到 `docs/VERIFICATION.md`。

## 关键文档

- `docs/` — 系统性参考文档：架构、离线管线、CLI、配置、检索、索引版本、e2e 框架、素材后台、人物库、创作工作流、设置、清理、工具（索引见 `docs/README.md`）
- `plans/` — 计划与设计决策文档（索引见 `plans/`）
- `plans/PLAN.md` — 设计决策、验收标准（40 条测试集 Hit@5 ≥85%）
- `docs/FILM_WORKFLOW.md` — 整部电影处理流程（visual 模式只需 WeMM）
- `docs/DEPLOYMENT.md` — GPU 服务部署、各角色环境、HTTP 服务协议
- `docs/E2E_RUNBOOK.md` / `plans/E2E_TEST_PLAN.md` — 端到端真实验证操作手册
- `eval/README.md` — 评测查询标注说明
