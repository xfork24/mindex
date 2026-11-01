# Mindex 项目分析报告（2026-10-26）

> 带日期的审查记录，反映 `HEAD bb57ee6`（工作区 clean）时的代码状态，之后以代码为准。方法：六个并行代码探索（素材管线、人物库、创作工作流、模型与设置、智能助手、检索/选题/桌面）+ 实际运行 CLI 帮助、pytest 收集、ruff 与快速回归核对。上一轮全面审查见 `project-review-2026-10-22.md`（已删除）与本目录的 [2026-10 专项审查](review-2026-10-architecture.md)等报告，本文第 14 节对照其结论。

## 1. 项目定位与总体形态

Mindex 是一套**中文视频片段检索 + 人物库 + AI 视频创作工作台**的单用户私有部署系统，核心链路是「素材管理 → 多模态检索 → 文案驱动的混剪成片」，并经智能助手与 MCP 把全部能力开放给外部 Agent harness。

运行形态分四层：

| 层 | 内容 |
|---|---|
| 本机 CPU | 切片、SQLite（事实来源）、FastAPI、ffmpeg 渲染、全部页面 |
| GPU 机器（可为本机） | 独立 HTTP 模型服务：WeMM 嵌入 `:4010`、ASR `:4020`、qwen3-tts `:4030`、Qwen 分析 `:4040/v1`、TransNetV2 `:18203`、人物检测/特征 `:4050`（InsightFace）。**单 GPU 分阶段互斥**，不假设多服务同时可用 |
| 线上订阅 | 智谱 / DeepSeek / 阿里百炼 / 腾讯混元+TokenHub / MiniMax / Anthropic，与自建服务按角色互换 |
| 桌面版 | Electron 外壳 + PyInstaller 冻结 `mindex serve` sidecar（macOS Apple Silicon，DMG 分发），加载同一套 Web 前端 |

技术栈：Python 3.11–3.13 + uv、FastAPI、Typer、Chroma（`embedding_function=None` 显式向量）、PySceneDetect、jieba（BM25 分词）、PyAV/ffmpeg；前端为**无框架、无打包**的原生 HTML/JS/CSS，`?v=` 内容哈希由脚本写入。

## 2. 规模与活跃度

| 项目 | 数据 |
|---|---|
| 后端 | 62,851 行 Python，252 个文件，16 个功能包（admin/agent/diagnostics/maintenance/models/persons/processing/search/settings/shared/studio/topics + workspace、tools 子包） |
| 前端 | 53 个自有 HTML/JS/CSS 文件（另含仓库内固定版本的 marked/DOMPurify）；`messages.json` 3,104 键（`ui.` 前缀 3,086） |
| 测试 | pytest 收集 **2,364 项**（219 个 `test_*.py`），另有 50 个 Playwright UI 回归 `.cjs`、桌面 node 冒烟 |
| 文档 | `docs/` 机制文档 17 篇 + 历史/审查记录；`plans/` 路线图与设计决策 |
| 活跃度 | 近 30 天 123 个提交；2026-10-22 审查以来 32 个提交 |
| 工程化 | ruff 全绿；CI 三 job（Linux 快速回归 + macOS 桌面 + 定时 DMG 打包冒烟） |

对比 10-22 审查时（26,164 行 / 953 项测试），四天里代码量翻了 1.4 倍、测试翻了 1.5 倍——主要来自 M2 选题、M3 后期与 M4 桌面端的集中落地。

## 3. 功能总览

### 3.1 页面（5 主页 + 4 子页 + 登录）

| 页面 | 功能 |
|---|---|
| `/` 检索 | 文字三渠道检索、图片/视频找相似、人物复合查询、级联筛选、镜头播放器 |
| `/admin` 素材工作台 | 上传导入、分步启动分镜/转写/分析/人物处理、对白任务与字幕上传、进度持久化、整理（显示名/备注/收藏/隐藏）、片段详情 |
| `/admin/music` | 曲库管理（导入、授权信息、情绪标签、节奏分析、推荐） |
| `/persons` 人物库 | 实体管理、参考图、演员表导入（LLM 解析）、出镜审核、发布与版本激活 |
| `/studio` 创作 | 版本工作台：方案/分镜编辑、逐镜阶段执行、构建跟踪、选片窗口、成片下载 |
| `/studio/topics` | 选题挖掘：领域档案、数据源、挖掘、选题卡片、建创作任务、素材采集清单 |
| `/studio/templates` | 文案模板管理（内置 6 个 + 用户模板） |
| `/studio/brands` | 品牌（风格）模板：字幕/文字层样式、片头片尾、水印 |
| `/settings` 设置 | 模型角色档案热切换、厂商订阅、音色克隆、检索调参、索引版本、本机服务、AI 动画组件 |
| 全局面板 | 五页共享的智能助手面板（附件、媒体卡片、定位刷新、输入历史） |

### 3.2 CLI（`mindex`）

- **管线**：`register`（哈希去重登记）→ `detect`/`prepare`（分镜+切片）→ `split` → `transcribe` → `align`（独立 venv）→ `enrich-dialogue` → `analyze` → `index --activate`；`ingest` 一键快捷（需多服务同时可用，单 GPU 建议分步）；`inspect`/`export`/`status`/`check`/`evaluate`/`model-check`。
- **管理组**：`indexes`（版本列表/激活/回退）、`clips`（旧切片 migrate/materialize/verify-archives/repair-boundaries/prune）、`clean`（三级磁盘回收，默认预览）、`backup`（export/inspect/import/recover）、`e2e`（init/probe/pin/phase/review/freeze/status 真实验证）。
- **业务组**：`persons`（save/reference/process/status/review/publish/activate/migrate-frames）、`studio`（jobs/plans/preflight/build/status/download + brand/music/overlay/plan/export 子组，`--url` 可连远端服务）、`topics`（profiles/fetch/items/discover/list/outline/coverage/gaps/create-job/job-profile）。
- **集成**：`agent tools`（工具注册表 JSON Schema 输出）、`mcp`（stdio MCP 服务，供 Claude Code、Codex、deepseek-harness 等调用）。

### 3.3 API/集成面

REST 挂载 search/admin/indexes/settings/studio/topics/persons/agent(+MCP) 路由（`backend/src/api.py`）；可选 `MINDEX_WEB_TOKEN` 认证（Bearer + HMAC Cookie + 内部令牌三凭证）；`/mcp` streamable-http（默认关）。智能助手全部工具与 MCP 共用同一注册表（`agent/registry.py`），业务逻辑只在 REST 一份，工具层经进程内 ASGI 回环调用（`agent/loopback.py`）。

## 4. 素材管理

### 4.1 导入与去重

`register` 只探测 + SHA256 内容哈希去重，原子落盘 `sources/<sha256>/<原名>`，不触发任何模型；重复导入复用既有行（`processing/pipeline.py:127-171`）。`detect` 先把完整切点计划原子写入 `plans/`，校验区间连续覆盖全片后才编码切片；≤6s 整镜即 moment，长镜头按 4s 窗/3s 步滑动切片。业务读取媒体一律经 `shared/shot_media.py`（时间基准映射、截图/抽帧、16k PCM 提取、跨镜 H.264/AAC 重建 `materialize()`），不直读旧切片路径，也不解析归档原片。

### 4.2 离线管线（分阶段、可断点续跑）

| 阶段 | 做什么 | 要点 |
|---|---|---|
| detect/split | PySceneDetect Adaptive 或远程 TransNetV2 分镜 | 计划先行、原子写入 |
| transcribe | ASR 三路：minimax 走对白任务、llama_cpp/openai 存 raw 待对齐、其余逐 30s 块检查点 | 断点续跑 |
| align | 独立 CPU venv `.venv-align`，`python -I` 子进程跑 ForcedAligner | aligner_revision 拒绝 main，环境隔离 |
| analyze | Qwen 逐片段（12 帧采样+对白）写 annotations；场景分组检查点、scene 再分析、敏感内容记 skipped | profile=分析配置+对白版本 |
| index | 格式 2 渠道化索引，按缓存键复用向量，只补缺失表示 | `index-write.lock` 串行写 |

`pipeline.lock` 管线互斥；`enforce_gpu_lock` 默认 False，开启后管线锁对 GPU 任务独占。

### 4.3 对白

优先级：内嵌文本字幕 → 上传字幕 → ASR 回退（`fallback_reason` 落库）。上传字幕只做不可变登记（`subtitles/<sid>/uploads/<sha256>/`），对白任务启动才应用，resolve 时复验哈希；`split` 前显式归档全部字幕轨/附件并做 SHA256 校验（`processing/subtitle_archive.py`）。

### 4.4 素材工作台与后台任务

`/admin` 直接读 SQLite、不依赖活动索引。后台任务统一生命周期（`shared/background_tasks.py`）：daemon 线程 + 终态落库、进度持久化（`task_progress.py`，重启可查）、取消 = 进程内 Event + `locks/cancel/<id>` 哨兵跨进程（`task_cancel.py`）。`source_tasks.py` 支持分步启动 shots/analysis/publish 并链式自动发布；`dialogue_tasks.py` 管对白任务与字幕上传；`work_cache.py` 持久工作摘要（60s TTL 后台刷新）；`segment_details.py` 只读聚合片段 ASR/字幕/LLM/人物证据，不触发推理。

### 4.5 整理（curation）

`admin/curation.py`：显示名/备注/收藏/隐藏，存 `source_curation`/`segment_curation`，hidden 沿「素材→子片段、镜头→moment」继承，检索快照叠加装饰后在召回前由 `effective_hidden` 直接过滤——与不可变索引快照分离，整理操作不需要重建索引。

### 4.6 维护

- `clips`：旧切片物理迁移为 shot 媒体引用（迁移时禁原片访问验证可重建、先备份 SQLite、永不自动删）、`materialize --all` 恢复实体、`verify-archives` 归档哈希校验、`repair-boundaries` 切点 MSE 精修（`processing/shot_repair.py`，自动备份、旧媒体永不删）、`prune` 只删已验证项。
- `clean`：A 级每次删（崩溃残留、孤儿媒体）、B 级按策略过期（索引版本保留活动+2 ready+全部 building、检查点与缓存 7–14 天）、C 级只报告；默认预览，`--apply` 才动文件。
- `backup`：export 默认脱密钥 + VACUUM、import 支持空库恢复与合并（复用向量、不调模型、校验 WeMM 空间兼容）、recover 按迁移日志回滚中断。

### 4.7 存储布局与数据表

`data_dir`：sources/plans/clips/thumbnails/transcripts/dialogue/analysis/subtitles/subtitle-archives/cache/context/chroma/deployments/capabilities/backups/migration-reports/shot-repairs/model-cache/music/studio/reports/verification/locks + `metadata.sqlite3` + `agent.sqlite3`（助手会话/事件/密钥保险库/附件，不进备份）。

SQLite 表约 40 张（`shared/storage.py` METADATA_STEPS + `shared/person_schema.py`）：sources、segments、media_references、clip_migration_files、双 curation 表、transcripts、annotations、scene_plans、index_versions/index_members/representations/channel_data（渠道化索引）、dialogue_tasks/source_tasks/settings/model_profiles/voices/runs、script_templates、brand_templates、music_tracks、tool_sources、topic_*（7 张）、person_*（9 张）、search_releases。库结构变更一律走版本化迁移（`PRAGMA user_version`），不改已发布步骤。

### 4.8 锁与并发

多把锁统一经 `shared/locks.py`，获取顺序 pipeline → index-write → library → register → studio → topics，逆序拒绝；任务级锁 `locks/<sha>.<work>.lock`；studio 构建另有进程内单执行线程 + `studio.lock`。跨服务 GPU 互斥用 `~/.cache/mindex/gpu.lock`（GPU 角色持锁，CPU TransNet 免锁）。

## 5. 检索与索引

### 5.1 三渠道召回（格式 2）

文字查询按 **visual / dialogue / analysis** 三渠道（`search/search.py:391-466`）：渠道内语义向量与 BM25（jieba 分词 + Okapi 长度归一）等权 RRF 融合，渠道间按 `search.channel_weights`（默认 1:1:1）再 RRF，最后同源 time-IoU>0.5 去重；dialogue 渠道附 top3 台词证据。旧格式索引仍走五路加权（`search.weights`），两组配置不混用。嵌入不可用或空间不匹配时，存在可用文本渠道则显式降级 BM25 并返回 `degraded`/`warnings`，绝不绕过空间校验比较向量。

### 5.2 多模态与相似查询

- 图片查询：归一化后只比画面向量，不影响文本/视频空间指纹；人物图片查询另有独立权重（`models/image_embedding.py` 复用多模态消息前缀）。
- 视频查询：4s 窗/3s 步切窗，临时目录自动清理，不入库。
- 相似片段：直接读库内种子向量，不调嵌入服务；不可用返回 409。
- 人物复合：onscreen 按发布证据覆盖+质量评分，mentioned 仅对白字面匹配。
- `warmup.py`：serve 启动后台预热首查代价（jieba、模型哈希、快照、BM25、HNSW），按 serving 签名自动重预热并淘汰旧缓存。

### 5.3 索引版本与空间身份

索引**不可变**：spec 含全部快照指纹，index_id = fingerprint(spec)；发布前 `check` 核向量/元数据/媒体引用/字幕依赖一致性；激活在 SQLite 事务内完成；格式 2 冻结渠道快照（visual=起止、dialogue=转录 content_version、analysis=注解指纹）。嵌入服务必须在 `/health` 与每次响应携带身份指纹（model/dims/quantization/预处理/implementation），查询身份与活动索引不符即 503（`api.py:182-184`）；换模型/量化/维度/预处理 = 新建索引，从不覆盖，回退 = 切回旧服务再激活旧索引。

## 6. 人物管理

### 6.1 身份模型

**actor / work / role 三种独立身份**：角色必须归属至少一部作品（跨电影角色多作品复用 `work_ids`），演员跨作品共用，姓名永不建立身份（`persons/person_models.py:15-36`）。身份与参考图全局、不绑定具体素材；素材仅经「所属作品」参与关联。删除身份会被未排除的关联分镜拦截（422）。

### 6.2 识别管线

- 人物服务（`persons/person_server.py`，独立 venv `.venv-person`，127.0.0.1:4050）：InsightFace cheetah_l V2（校验 manifest/SHA256/期望空间），接口 `/health`、`/extract/image|frames|video`，Bearer 鉴权，启动持 GPU 锁。
- 扫描（`person_processing.py`）：PyAV 按 15 帧间隔抽原片 PTS 帧、16 帧一批发服务；ByteTrack 镜头内关联；人脸按「角色聚合最高分 + 置信差」判定身份，track 继承在交叉框/缺检/人脸冲突时撤销；检测/特征/匹配分层缓存，人工锚点重跑保留。
- 帧布局：`persons/references|imports/` 原尺寸 PNG、`frames/<source_id>/` 内容寻址展示 JPEG（长边 ≤640）；旧库 `migrate-frames --apply` 迁移，不需重新索引。

### 6.3 审核与发布

分镜级 `shot_review`：每勾选 (actor_id, role_id) 生成覆盖整镜的 manual accepted 证据（score 1.0、冻结 original_score），**角色必填**并校验演员-作品-角色关系；空选择 = 整镜排除。姓名解析支持别名与角色后缀模糊（「叶淑柔」联合召回「老年叶淑柔」），同名歧义报错；文档提及（如「关于…的照片」）降为 reference 不要求出镜。

发布 `publish()` 单事务冻结 entities + accepted 证据 + coverage 为内容寻址 `person_versions`（不可变），写 `search_releases` 绑定活动内容索引；**改名只需重新发布、不重跑推理**；模型空间任务内固定，变化拒绝发布，跨空间特征绝不混用。检索时 onscreen 只认发布证据（EvidenceIndex 区间索引 O(log n)），演员经 cast 扩展为其全部角色。

### 6.4 页面与 API

实体 CRUD、参考图上传/配对/删除/原片截图、演员表导入（图片 + LLM 解析草稿 + 确认）、图片识人 `/identify`、出镜审核列表与证据截图回退链（当前→历史快照→镜头缩略图）、发布与版本激活/删除；`MINDEX_PROFILE_PERSONS=1` 可输出 Server-Timing 分段 profiling。无身份合并/拆分操作（仅删除 + 配对编辑）。

## 7. 创作工作流（文案生成视频）

### 7.1 数据模型

任务即目录 `data/studio/<job_id>/`：`job.json`（大纲/可用素材 `source_ids`）、`workspace.json`（方案/版本索引/builds，schema 2）、`versions/<vid>/`（不可变 version.json + 产物 + debug）、`builds/<id>/events.jsonl`、`inputs/<sha256>.<suffix>`（查询媒体/参考文稿，执行前复验摘要）。业务状态不进 SQLite（仅文案/品牌/曲库/工具源四张配置表）。方案（plan）含分镜板、options（模板/画幅/品牌）、ranges；创建任务时限定可用素材，逐镜二次选择取交集、空交集报错不退化为全库。依赖树经版本 `inputs` 递归，人工编辑 PATCH 级联作废下游。

### 7.2 阶段注册表与画面来源

`studio/workspace/stages.py` 定义 `StageHandler`（specification/dependencies/preflight/run/conflict_labels 钩子）+ `REGISTRY`，handlers/ 导入即注册，登记顺序即执行序（plan→shot→final）。**新增阶段或画面来源只需实现处理器并 `register()`**，不改编排代码。15 个内置处理器：script、split、recall、tts、select、render、compose、generate（AIGC 视频）、animate（LLM 写 HTML 动画）、invoke（第三方工具）、draw（AI 生图）、overlay（HTML 文字层）、music（BGM 生成/曲库）、export（多画幅导出）、publish（发布包）。

七种画面来源的阶段链（`stages.py:166-204`）：

| Shot.source | 阶段链 |
|---|---|
| recall | recall → tts → select → render |
| clip | tts → select → render |
| image | tts → render |
| aigc_image | tts → draw → render |
| aigc | tts → generate → select → render |
| animation | tts → animate → render |
| tool | tts → invoke → select → render（图片产物无 select） |

BGM 提示点在 tts 后插 music，HTML 文字层在 render 前插 overlay（均镜头级）；export/publish 为方案级 final 阶段。

### 7.3 文案与模板

新任务只存大纲；文案是方案级版本化 `script` 阶段，由 writer 角色（`QwenWriter`，温度 0.7，提示词版本化 `WRITER_PROMPT_VERSION`）按模板生成。内置 6 个模板（base/history/hot/film/science/interview），用户模板存 `script_templates` 表；script 签名绑定模板内容哈希，换模板只提示 `script_template_changed` 不自动重写。参考文稿存 `inputs/<sha256>.txt` 并进签名。`material_digest.py` 从活动索引只读生成素材摘要（画面词/故事线/字数预算、指纹逐字节稳定），供文案、拆分镜与召回词重写使用。

### 7.4 召回与选片

逐镜召回（recall 阶段）支持文字/人物/图片/视频查询与素材过滤，签名绑定内容索引与人物发布组合，与检索页同一引擎。选片是纯算法（`studio/studio_select.py`）：打分 = 语义 + 时长匹配 − 延长/重复/同源重叠惩罚；阶梯 = 候选窗口 → 同源可见相邻延长 → 配音 atempo 钳制加速 → 冻结帧/裁剪兜底，每级给出 alignment 标签与警告。人工选片走 `studio_window.py`（锚点冲突检查 + 定长窗口 + 预览确认）。

### 7.5 渲染与后期

- 画幅：16:9（恒为 render.width/height）/9:16/1:1，pad/crop 适配；竖屏字幕按短边缩放、MarginV 适配。
- 后期管线（`workspace/post.py`）：assemble → join_video → join_audio → 片头片尾 → clean → 字幕 → 水印 → finish；BGM 淡入 0.5s/淡出 1.5s + sidechaincompress ducking（MUSIC_MIX_VERSION=2）；软字幕 mov_text + SRT/VTT 导出，烧录依赖 libass（缺失报错可切软字幕）。
- 转场（`workspace/transitions.py`）：enter/leave 镜内截半镜头，next 重叠转场靠尾部余量 + compose xfade 只重编码相连组。
- 卡点（`workspace/beat_align.py`）：段落内切点推迟到最近节拍，画面用余量、配音补静音。
- 品牌（`brands.py`）：统一字幕样式/文字层样式/片头片尾/水印；内置 plain/news/tech 只读 + `brand_templates` 用户模板；只影响合成，改动仅触发重合成。
- 多画幅导出（`variants.py`/`handlers/export.py`）：derive 快速派生 / rerender 重渲染子方案（variant_of，共享上游 recall/tts/select）；平台预设 bilibili/douyin/youtube（`platforms.py`）。
- 发布（`handlers/publish.py`）：**不直传平台**，生成发布包目录 + zip（video.mp4 + cover + srt/vtt + metadata.json + SHA256 manifest）。

### 7.6 音乐库

`music_tracks` 表，曲目 ID = 音频 SHA256，文件内容寻址存 `data_dir/music/`；导入必填授权来源/许可/署名。节奏分析纯 numpy（`beats.py`）：22050Hz 单声道 → STFT → 半波整流谱通量 → 自相关（60–200BPM，120 对数先验）→ DP 节拍跟踪，输出 bpm/beats/energy/brightness；导入自动分析，卡点复用。CLI 与 Agent 均可按文案/分镜段落推荐曲目。

### 7.7 第三方工具源

`tool_sources` 表登记外部工具：协议 http / mcp-stdio / mcp-http、工具白名单（output/allow_upload/timeout/max_mb）、下载域名白名单、`secret:名称` 密钥引用（tool_hash 不含密钥）。invoke 阶段替换 `{{text}}/{{visual}}/{{query}}` 占位，产物流式下载后经 ffprobe/Pillow 校验再存入不可变版本。自带 MCP 客户端适配（`studio/tools/mcp.py`）。

### 7.8 构建执行与版本复用

签名 = fingerprint({stage, payload, parameters})，payload 可用历史参数重建验证；复用时优先钉住版本，再按签名 + 文件完好从新到旧匹配。版本不可变（version.json 已存在即拒绝），失败 attempt 留档诊断。构建顺序 script → split → recall/tts → select → render → compose；召回与配音只依赖拆分镜（可任意先后，适配单 GPU）。`StudioExecutor` 进程内单执行线程 + 跨进程 `studio.lock`（冲突 409）；取消 = Event + 哨兵文件，已开始节点完成、pending 标「已取消」；重启 recover 将 running 构建标 partial。

### 7.9 老任务兼容

五步状态机（split→recall→tts→select→render）保留；首次 load 确定性迁移：`input_text` → `origin=legacy` 文案版本，哈希可验证的旧产物导入为 incomplete 版本、不猜测依赖；`/steps`、`/run` 对新任务 422。

## 8. 选题挖掘（topics）

定位：给垂直领域频道补上游——「今天做什么视频」。

- **数据源**（`topics/sources/`）：RSS（RSS2.0/RDF/Atom，拒 DOCTYPE 防 XXE）、热榜（通用 JSON 映射或 DailyHotApi 预设）、Brave 搜索（news/web，查询词缺省档案关键词，max_queries 计费上限）、日历（ICS VEVENT+RRULE YEARLY/CSV，「历史上的今天」类）、本地（素材库主题摘要 + 已做视频去重，无网络）。全部网络请求经 `base.http_get` 统一管控（协议白名单、超时、max_bytes、重定向检查），测试可替换 TRANSPORT。
- **流水线**（discover）：抓取 → 候选（近 7 天/事件 14 天/未用/上限 200）→ 贪心余弦聚类 → 每簇 LLM 归纳选题卡（标题/角度/理由/3–6 关键画面）→ 打分（时效 72h 半衰、受众 LLM 评估、素材覆盖走真实检索路径、与已做视频嵌入去重、成本）→ 前 N 名写大纲 + 推荐文案模板。
- **素材覆盖与缺口**：coverage 报告逐关键画面列命中片段与相似度；gaps 合并（阈值 0.85/Jaccard 0.6）成 open/done 采集清单，指导素材积累方向。
- **衔接创作**：create_job 在 studio 锁内建任务，预置大纲/推荐模板/档案默认画幅/来源条目参考文稿/`profile.json` 档案快照，失败回滚删目录；档案写作约束仅在方案有 `profile_hash` 时进入 script 签名与 writer 提示词，**其余任务签名逐字节不变**。
- **运行**：`topic_runs` 表 + 全局 topics 锁单任务（409/取消/重试/中断恢复）；scheduler 纯函数 due + 启动补跑，随 `topics.scheduler.enabled` 挂载。
- **四个入口**（页面/CLI/REST/智能助手+MCP）由 `capabilities.py` 13 项登记，`test_topics_parity.py` 逐项断言 parity——新增能力先登记。

## 9. 模型兼容与设置

### 9.1 角色与协议

实际 **12 个模型角色**（`settings/runtime_settings.py:38-55`）：理解(analyzer)、文案创作(writer)、拆分镜(generator/storyboarder)、嵌入(embedding)、ASR(asr)、TTS(tts)、图片生成(image_generator)、视频生成(video_generator)、音乐生成(music_generator)、动画生成(animator)、人物识别(person)、智能助手(agent)。每个角色由 Protocol 抽象（`models/adapters.py` 及各后端模块）：MultimodalEmbedder、VisualAnalyzer、Transcriber、Aligner、ShotDetector、Speaker、Storyboarder、Writer、VideoGenerator（异步 submit/poll/fetch/cancel 任务制）、ImageGenerator、MusicGenerator、Animator。**替换模型 = 实现协议接入新 backend，不改检索与创作管线。**

### 9.2 backend 支持矩阵

| 角色 | 可选 backend |
|---|---|
| 嵌入 | mindex 自研 WeMM HTTP（默认）/ llama_cpp（含部署清单 schema 2）/ tokenhub |
| ASR | mindex / llama_cpp / openai（chat+input_audio）/ minimax（词级时间戳） |
| 理解分析 | mindex Qwen（openai 兼容）/ llama_cpp（Pinned 部署） |
| 拆分镜 / 文案 | Qwen 系 openai 兼容 chat（固定） |
| TTS | mindex（qwentts llama-server /audio/speech）/ minimax / dashscope / openai |
| 视频生成 | comfyui（默认）/ minimax / minimax_h3 / dashscope |
| 图片生成 | comfyui / minimax |
| 音乐生成 | comfyui / audiocpp / minimax |
| 动画生成 | LlmAnimator（openai / anthropic） |
| 镜头检测 | transnet（远程 HTTP）/ adaptive（本地 PySceneDetect） |
| 人物识别 | 仅 mindex（InsightFace 服务） |

### 9.3 运行时热切换

配置三层：YAML（strict，`extra="forbid"`）→ SQLite `model_profiles`（每角色多档案 + 密钥）+ `voices` → KV `settings` 覆盖层。保存即热生效：Web 经 `RuntimeConfig.current()` 每请求重解析，CLI 在 `load_config` 后统一合并（`e2e`/`clips`/`backup` 用 YAML 原值，`--no-runtime-overrides` 冻结）。切换嵌入档案后身份指纹变化，检索 503 直至重建索引激活（页面横幅常显匹配状态）。

### 9.4 线上厂商

PROVIDERS：custom/self/zhipu/deepseek/aliyun/tencent/minimax/anthropic，按角色预填 base_url/model 预设；测试连接分级探测（LLM 发最小 chat、agent 强制 echo 工具调用、视频/图片/音乐仅 health 探测不付费生成、ASR 静音 wav、TTS 合成「测试」）。密钥明文存库但接口只出掩码（尾 4 位 + 三态 CLEAR_KEY），`Service.api_key` 带 `exclude=True` 不进任何指纹/快照/备份。

### 9.5 嵌入空间身份（跨模型兼容的核心安全机制）

- mindex 后端：`/health` + 每批向量响应核验 model/dimensions/quantization/fps/max_frames/max_pixels/revision/implementation，space_id 不符即拒。
- llama_cpp 后端：部署清单 schema 2（GGUF/mmproj 内容 SHA256 + pooling + 预处理契约），**`llama_cpp.py` 自身代码哈希参与身份**（`adapter_sha256`），推理前后双重核验——保证「换适配器代码 = 换空间」，绝不跨空间比较向量。
- `models/compatibility.py`：固定文本+视频基线 12 项检查、余弦 ≥0.999 门槛（require_gate），防止「能跑但语义漂移」的模型混入同一索引。
- `settings/deployment_pin.py`：设置页 probe（只读 /health /props /v1/models）+ pin（本机进程自动定位 GGUF/mmproj，复用或新建清单）。

### 9.6 其他设置能力

音色克隆（MiniMax 上传克隆返回预览音频 / 阿里公网 URL 复刻 / 本地 qwentts 注册）、音色目录（按后端聚合静态 + live 列表，失败降级不 500）、本机服务启停（只允许配置文件预定义命令，防远程执行；独立会话 + pid 接管 + 日志轮转）、AI 动画 Chromium 组件按需安装（`--only-shell` 约 100MB）。

## 10. 智能助手与 Agent 兼容性

### 10.1 工具层

10 个领域、**148 个工具**（`shared/agent_domains.py` + `agent/tools/`）：studio 28（最大）、topics 25、studio_post 25、settings 19、persons 15、studio_detail 12、library 12、search 5、meta 4、index 3。工具与运行方式无关：内置助手面板、`mindex agent tools`、MCP 导出共用同一注册表；业务逻辑只在 REST 一份，工具经 `loopback.py` 进程内 ASGI 回环调用（随机内部令牌，拒绝含 `.`/`..`/`?`/`#` 的路径）。

### 10.2 分级确认与动态加载

- read 直接执行；write（可逆写）预览确认、可会话内授权；**danger 每次显式确认**——共 27 个：删除类（任务/方案/人物/品牌/音色/档案/模板/参考图）、构建类（run_build/run_node/run_column）、激活类（索引/模型档案）、发布类（人物发布/检索组合切换/发布包）、模型与设置类、GPU 任务与选题挖掘、第三方工具源保存。不提供任何物理删除素材的工具。
- 每步只下发 meta + 已加载领域工具；`load_tools` 按需加载，非 meta 领域 LRU 最多保持 3 个（持久化在会话），未加载领域调用返回 TOOL_NOT_LOADED——控制单步工具规模在模型能稳定选用的范围。
- 工具输出扩展：`preview`（写前预览）、`focus`（页面定位）、`media`（面板媒体卡片）、`images`（给模型看图）、`mutation`（触发页面刷新）、watch_build/watch_task（任务跟踪）。

### 10.3 LLM 运行时兼容

`agent/llm/factory.py` 双运行时：**OpenAI 兼容**（`/chat/completions` SSE，delta.tool_calls 按 index 拼参数，图片转 image_url 数据 URL）与 **Anthropic 原生**（`/v1/messages` 流式 content block，tool_result 内嵌 image 块，相邻同角色合并）。事件信封对齐 dsh 形状（扩展走 `plugin:mindex/*`）。轮次循环**无步数上限**，操作历史经 256 步滚动窗口 + token 预算（默认 60000）控制上下文；`scripted.py` 提供测试注入。视觉输入：发请求前把 `attachment://` 或本站 `/api/` 引用解析为 ≤1024px JPEG（最多 8 图），日志只存引用不存图。

### 10.4 MCP 与外部 harness 兼容

`mindex mcp`（stdio）与可选 `/mcp`（streamable-http + Bearer，默认关）导出工具；按 `agent.mcp.domains` / `--domain` 过滤领域，`mcp=False` 的界面交互类工具（load_tools、ask_user_question、navigate、save_stdio_tool_source）不导出；风险注解映射 readOnlyHint/destructiveHint；MCP 侧另有 `agent.mcp.policy=read/write/all` 服务端兜底。显式面向 deepseek-harness、Claude Code、Codex 等 harness 设计（CLI 帮助原文）。

### 10.5 面板与全局联动

五页共享 `agent-panel.js`：Markdown 渲染（仓库内固定版本 marked + DOMPurify 白名单，图片仅本站 /api/）、附件选择/拖放/粘贴（12 个上限）、媒体缩略图网格与大图/音视频、ArrowUp/Down 输入历史导航、SSE 按 seq 断线续传。页面经 `MindexAgentContext` 提供上下文、`MindexAgentFocus(focus)` 定位、监听 `mindex:agent-mutation` 刷新。`watcher.py` 1s 轮询跟踪创作构建与素材/对白/人物/选题任务，终态自动 followup 总结（重启 resume）；`changes.py` 全局改动表 + `/api/agent/changes` SSE（保留 5000 条）。

## 11. 桌面版（macOS）

Electron 外壳（`desktop/main.js` + `lib/` 9 个纯逻辑模块，node --test 可测）+ PyInstaller onedir arm64 冻结 `mindex serve` sidecar：

- **安全**：每次启动随机 32 字节 `MINDEX_WEB_TOKEN`，对本 origin 注入 Bearer + HMAC Cookie；contextIsolation/sandbox、导航限本 origin；回环端口持久化（端口变化会丢 localStorage 偏好）。
- **兼容**：`desktop_entry.py` 用 runpy 分发 `mindex -m <模块>`（既有 `sys.executable -m` 子进程不改）；以源码哈希作指纹的模块登记进 SOURCE_MODULES，冻结后读构建时摘要；健康轮询校验 pid 与版本一致，防连接到错误后端。
- **常驻**：托盘 + 关窗只隐藏，`keep_in_background` 默认开（每日选题定时继续跑）；退出先确认 busy → 停本机服务 → 停进程组。
- **分发**：随包 ffmpeg 从固定源码编译（GPL：libx264/libass/dav1d，附源码 tar），`MINDEX_FFMPEG/FFPROBE` 定位、路径不进签名；CI 定时从源码出 DMG 并安装冒烟 + codesign 校验。
- **同步演进约定**：desktop/ 只放外壳，业务全在后端经 REST；桌面专属能力只经 preload `MindexDesktop`，前端无它时保持 Web 行为——不分叉第二份业务代码。Windows 为路线图未完成项（锁跨平台、二进制定位等已列前置条件）。

## 12. Web/API、安全与 i18n

- **认证**：可选 `MINDEX_WEB_TOKEN`；`/login` 表单 → HttpOnly samesite=strict Cookie（存 HMAC 摘要，常量时间比较），Bearer / 内部令牌 / Cookie 三凭证；`next` 只收站内相对路径；`/logout` 仅 POST 防 CSRF；API 401、页面 303。
- **输入校验**：配置 strict（`extra="forbid"`、禁 inf/nan）；上传上限（源 20GiB/文件、字幕 20MiB）；loopback 路径黑名单；RSS 拒 DOCTYPE；local_services 命令白名单。
- **i18n**：`messages.json` 中文原文唯一源（3,104 键，`ui.` 语义名或中文 sha1 前 12 位），`build-messages.py` 生成浏览器侧镜像 `messages.js`（CI --check 校验）；`i18n.js` 切换语言不重建 DOM、保留未提交输入；后端把中文消息反向映射为稳定 `message_key/message_params` 供前端按键展示。

## 13. 测试与质量保障

- **pytest 2,364 项**（219 文件）：真实 FFmpeg/PySceneDetect/SQLite/Chroma/HTTP API 集成式，模型用 conftest 确定性 Fixture 适配器；`slow` 标记分级（CI 推送跑 `not slow`，每晚 UTC 18:00 全量）；本次本地 `ruff check` 全绿。
- **UI 回归**：50 个 `.cjs`（Playwright 驱动真实页面），`serve_fixture.py` 用 ffmpeg 生成样本视频 + Fixture 索引起隔离服务；无 node 自动跳过。
- **桌面**：外壳纯逻辑 node --test + Electron 冒烟 + CI 安装 DMG 冒烟。
- **真实验收**：`mindex e2e` 分阶段框架（smoke 六项能力门槛 → visual → preview → asr → align → analyze → enriched → evaluate → video-evaluate → check），probe/pin 信任模型 + 人工 review 后 freeze 标注（SHA256 清单）；明确约定「自动化测试只验证工程链路，不证明真实模型中文检索质量」。
- **parity 测试**：topics 四入口逐项断言；MCP 导出集合 == mcp=True 工具集；agent LRU/滚动窗口等行为均有断言。

## 14. 自 2026-10-22 审查以来的演进

上次审查的主要发现与当前状态：

| 上次发现 | 现状 |
|---|---|
| Web/API 无认证 | ✅ 已解决：`MINDEX_WEB_TOKEN` 三凭证认证 + 登录页；桌面版每启动随机 token |
| 测试不全绿、无 CI | ✅ 已解决：CI 三 job（ruff/文档链接/文案镜像/资源哈希/JS 语法/快速回归 + macOS 桌面 + 定时 DMG 打包冒烟）；ruff 全绿 |
| `messages.js` 手工镜像 | ✅ 已解决：脚本生成 + CI --check |
| `shared/work_cache.py` 反向依赖 admin | ✅ 已移至 `admin/work_cache.py` |
| `models/adapters.py` 导入 studio、超大（1,060 行） | ✅ 已拆分（embedders/transcribers/analyzers/detectors/speech/storyboard/writer/video_generators/image_generators/music_generators/animator），工厂保留 adapters.py；对 studio 的模块级导入已消除 |
| `cli.py` 1,360 行、`studio.js` 2,559 行 | ✅ 已拆分（cli_studio/cli_topics/cli_persons/cli_clips/cli_e2e/cli_agent；studio-*.js 按序加载）。当前最大文件 `cli_studio.py` 1,812 行，全部 <2,000 |
| 无 LICENSE | ❌ 仍缺（见建议 2） |

功能层面，上次「功能集审查」列的三块缺口已全部交付：**M2 选题挖掘**（五源 + 流水线 + 四入口 parity）、**M3 后期**（BGM/转场/文字层/卡点/品牌/封面/多画幅/发布包 + 音乐库 + 第三方工具源）、**M4 桌面端 macOS**（DMG 分发）；「画面只能来自素材召回」的限制也已通过 aigc/aigc_image/animation/tool/image 五种新画面来源解除。路线图 M1/M2/M3 已完成，M4 剩 Windows。

## 15. 观察与建议

**架构优点（值得保持的不变量）**

1. **身份即安全**：嵌入空间指纹（含适配器代码哈希）+ 索引不可变 + 发布检查 + 503 拒绝跨空间比较，从机制上杜绝「换模型后旧向量悄悄混用」这一向量检索最隐蔽的坑。
2. **SQLite 单一事实来源**：Chroma 只存显式向量，业务数据全可从 SQLite 追溯；备份/合并因此可靠。
3. **注册表驱动的扩展点**：阶段注册表、工具领域、TopicSource、模型 Protocol——四处扩展都不改编排代码，测试用注入（create_app(agent_llm=…)）也统一。
4. **破坏性操作全部默认预览**：clean/clips/prune/backup import 先预演，`--apply` 才动文件；物理删除不暴露给 Agent。

**问题与建议**

1. ~~**CLAUDE.md 已有漂移**~~（**已于 2026-10-26 修正**）：模型角色实为 12 个（原文缺图片生成、音乐生成）；i18n 键实为 3,104（原文写「约 2290」）；「五个页面」未计 /admin/music、/studio/brands 等子页；「`ingest` 单 GPU 场景禁用」实为「建议分步」；studio 画面来源链缺 aigc_image；适配层后端列表缺 minimax_h3 与图片/音乐生成模块；厂商列表缺 Anthropic。同批修正了 AGENT.md、README.md、docs/README.md、docs/architecture.md、docs/settings.md、docs/cli-reference.md 中的同类漂移（十→十二个角色/协议、五→七种画面来源、studio 子命令组汇总、视频生成后端清单、回归耗时估计）。
2. **无 LICENSE**：沿袭上次审查结论，缺省等于保留全部权利，是否开源需所有者表态。
3. **`enforce_gpu_lock` 默认 False**：单 GPU 互斥目前依赖操作纪律；/admin 后台任务与手工 CLI 并行时可能同时打 GPU 服务。对典型单 GPU 部署建议默认开启或首次启动引导开启。
4. **studio 领域 28 个工具已触及自定上限**（CLAUDE.md 亦约定「过大时拆分领域」），topics/studio_post 各 25 也在增长；继续加工具前先拆领域，否则模型选用准确率会先于数量见顶。
5. **密钥明文存 SQLite**：本机/桌面场景已由随机 token、接口掩码、exclude 出指纹与备份脱敏缓解，文档已声明；若未来走向多用户或服务器部署，应换系统密钥库。
6. **循环依赖未全面复查**：上次两处模块级反向依赖已消除，但延迟导入形成的环（shared→persons 等）本次未逐一验证，建议下次审查用脚本按导入图复查一遍。
7. **文档量与历史档案**：docs/ 顶层仍有 19+ 篇带日期记录与机制文档混放（上次建议移 history/，部分已移）；`docs/reviews/2026-10/` 与本报告并存后，建议在 docs/README.md 索引中标注相互取代关系。

## 附：本次核对方式

CLI 帮助逐组运行采集；pytest `--collect-only` 计数 2,364；`ruff check backend/src tests` 通过；快速回归 `uv run pytest -m "not slow" -q` 实测 **1731 通过、4 跳过、629 个 slow 用例剔除，10 分 45 秒，无失败**（输出中的模型服务超时 traceback 来自容错路径用例的预期日志，不影响结果）。
