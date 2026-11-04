# 配置参考

配置从 `config.example.yaml` 复制为 `config.local.yaml`（gitignored），或用 `--config` / `MINDEX_CONFIG` 指定。模型 strict（`extra="forbid"`），未知键校验失败；未写的键用默认值。`data_dir` 与 `deployment_manifest` 的相对路径按**配置文件所在目录**解析。

> **运行时设置**：模型服务连接、音色与检索调参可在 `/settings` 设置页管理（SQLite 覆盖层，保存即热生效，Web 与 CLI 共用），本文档的 yaml 取值是「未激活任何档案时」的基础值。详见 [settings.md](settings.md)。所有服务段共有的可选键 `api_key`（默认空，仅由设置页写入；优先于 `api_key_env`）不参与任何指纹。

> 模型请求超时：每个模型段的 `timeout` 是对该模型单次请求的超时（秒），设置页模型档案的“请求超时（秒）”可覆盖，所有调用该模型的地方（构建、连接测试、音色目录与声音克隆、人物识别、健康探测等）都使用它。缺省值：图片、视频、音乐与动画生成 600 秒，其他模型 180 秒。

## 顶层

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `data_dir` | Path | `data` | 素材库根目录。切分参数变化须换新目录 |
| `runtime_overrides` | bool | true | false 固定 YAML，CLI 和验收 Web 每次读取都禁用动态覆盖；也可全局传 `--no-runtime-overrides` |
| `enforce_gpu_lock` | bool | false | 强加 GPU 锁。false 时 pipeline 锁为共享锁（`shared/locks.py` 的 `SHAREABLE`），/admin 后台任务与手工 CLI 可同时使用 GPU 服务，单 GPU 部署存在争用风险；true 时处理任务互斥，任务启动/重试拿不到锁直接 409，发布子任务经 0.1 秒轮询排队等待 GPU 准入（`PublicationLock.admit`），处理期间素材删除返回 409。同类重复任务和删除正在处理素材不受该开关影响、始终受保护；设置页可热更新。单 GPU 部署建议显式开启 |

## `database` — 元数据库后端（水平扩展）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `sqlite` \| `postgres` | `sqlite` | 元数据库选择。`sqlite` 为本地 `metadata.sqlite3`（零依赖缺省）；`postgres` 为 pg-single 过渡形态（[水平扩展方案](../plans/horizontal-scaling-plan.md) §4：单主机单 API，媒体/向量/锁仍为本地实现，禁止多机写入）。切换需重启进程；写入新后端前可切回，开放写入后按 runbook 走停写/逆向迁移 |
| `postgres.dsn_env` | str | `MINDEX_DATABASE_DSN` | DSN 只从环境变量读取（凭据不落配置文件/日志/任务快照）；远程 PG 使用校验证书的 TLS |
| `postgres.pool_min` / `postgres.pool_max` | int | `0` / `8` | 查询连接池（进程内按 DSN 共享）；总连接预算按「API 副本 × 池 + worker + 维护余量」规划 |
| `postgres.control_pool_max` | int | `2` | 心跳/控制小池（阶段 3 租约用），只跑短事务 |

pg-single 使用步骤：`MINDEX_DATABASE_DSN=postgres://… mindex migrate` 建库（唯一 DDL 入口，API/worker 启动只校验版本）→ 以 `database.backend: postgres` 启动。设置页「部署后端」分区只读展示当前形态与 library_id。备份导出/导入、切片迁移/清理与镜头边界修复在该形态下显式拒绝（阶段 5 交付 PG 一致性导出）。

## `vector` — 向量后端（水平扩展阶段 2）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `chroma_local` \| `chroma_server` | `chroma_local` | `chroma_local` 为本地嵌入 chroma（`<data_dir>/chroma`，零依赖缺省）；`chroma_server` 为独立 Chroma 服务（`chromadb.HttpClient`，集合名/余弦空间/embedding_function=None 与本地一致）。属共享后端：需 `database.backend=postgres`，与 sqlite 单机形态的组合在启动时拒绝 |
| `chroma_server.url` | str | `http://127.0.0.1:8000` | 服务地址（http/https）；服务器镜像版本/digest 与 HNSW 参数由部署固定 |
| `chroma_server.token_env` | str | `MINDEX_CHROMA_TOKEN` | Bearer 凭据只从环境变量读取，不落配置文件/日志 |

派生词法索引（`mindex indexes lexical` 回填；发布时自动构建）让文字检索的统计与 BM25 评分在 SQL 内完成；旧索引或分词器版本不一致时自动回退 Python 参考路径并告警。

## `queue` / `locks` — 任务分发与具名锁（水平扩展阶段 3）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `queue.backend` | `inline` \| `postgres` | `inline` | `inline` 为 API 进程内立即执行（现有行为）；`postgres` 为持久队列（claim 用 `FOR UPDATE SKIP LOCKED`、dedupe 幂等、attempt/fencing token、超时回收、有上限指数退避），由 `mindex worker --queues source,dialogue,… --concurrency N` 消费，至少一次交付。需要 `database.backend=postgres`；补偿扫描兜底「账本已落、入队前崩溃」窗口 |
| `locks.backend` | `file` \| `postgres` | `file` | `file` 为 data_dir 文件锁（跨进程，现有行为）；`postgres` 为 PG 租约锁（epoch 永不归零、fencing token 单调不复用、TTL 90s + 心跳 20s；失去租约即停止提交）。需要 `database.backend=postgres`；选题调度器在该形态下以 leader 租约保证单实例触发 |

worker 使用：`MINDEX_DATABASE_DSN=… mindex worker`。GPU 任务按 worker 并发槽位排队（`--concurrency`，起步 1；声明式容量表随阶段 5 集群装配）。

## `embedding` — WeMM 嵌入服务

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `mindex` \| `llama_cpp` \| `tokenhub` | `mindex` | `mindex` 用本项目协议（`/embed/text`、`/embed/video`）；`llama_cpp` 用 llama-server `/v1/embeddings` 原生 `input_video`；`tokenhub` 用腾讯 TokenHub `/v1/embeddings/multimodal`（WeMM 系线上版，客户端抽帧、身份纯配置构造） |
| `deployment_manifest` | str \| null | null | llama_cpp 必填：`e2e pin` 生成的部署清单路径 |
| `base_url` | str | `http://192.168.1.86:4010` | 服务地址 |
| `model` | str | `tencent/WeMM-Embedding-9B` | 模型名；本地权重路径亦可（服务端会哈希） |
| `revision` | str | `main` | `main` 解析为实际 commit；固定后身份校验更严 |
| `quantization` | str | `int8` | 须与部署实际一致（llama_cpp 下填实际 GGUF 量化，如 `Q8_0`） |
| `dimensions` | int ≥1 | 4096 | 嵌入维度 |
| `numerical_implementation` | str | `llama-cpp-wemm-1` | llama_cpp v2 数值实现版本；兼容失败时显式升级并建立新基线/新索引 |
| `timeout` | float >0 | 180 | HTTP 超时秒 |
| `api_key_env` | str | `MINDEX_MODEL_API_KEY` | 从同名环境变量读 Bearer token |

## `analyzer` — Qwen 视觉分析

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `openai` \| `llama_cpp` | `openai` | `openai` 兼容 `/v1/chat/completions`；`llama_cpp` 额外核验部署清单与视觉投影 |
| `llama_template` | bool | `true` | 是否发送 llama-server 专有的 `chat_template_kwargs`（`enable_thinking:false`）；线上厂商档案须设 `false` |
| `deployment_manifest` | str \| null | null | llama_cpp 时必填（`data/deployments/analyzer.json`） |
| `base_url` | str | `http://192.168.1.86:4040/v1` | |
| `model` | str | `qwen3.8:27b` | 部署别名 |
| `revision` | str | `deployed-2026-10-12` | 由部署者维护；换权重必须更新并重跑分析 |
| `api_key_env` | str | `MINDEX_QWEN_API_KEY` | Qwen 单独的 key 变量 |
| `timeout` | float >0 | 180 | HTTP 超时秒 |
| `response_retries` | int 0–20 | 5 | 无效 JSON、字段类型/必填校验失败、无效场景分组、空正文、聊天格式错误或 HTTP 500 的额外重试次数；各类无效响应共用次数，默认共 6 次，间隔 1、2、4、8、8 秒；不改变已有分析检查点身份 |
| `initial_output_tokens` | int 64–131072 | 12768 | 分镜分析、场景分组及场景分析的首次输出预算，含推理 token；不超过 `max_output_tokens`，无效响应重试时逐次翻倍。不改变已有分析检查点身份 |
| `max_output_tokens` | int 64–131072 | 131072 | 重试的输出预算上限，含模型推理 token；按厂商支持范围设置。不改变已有分析检查点身份 |
| `thinking_mode` | `auto` \| `low` \| `provider` | `auto` | 按已知厂商参数优先关闭思考；GLM-5.3 / MiniMax-M3.1 强制思考模型使用 `reasoning_effort:low`。`provider` 保持厂商默认；自定义地址不猜测参数。不改变历史检查点身份 |

## `asr` — 中文转写

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `mindex` \| `llama_cpp` \| `openai` \| `minimax` | `mindex` | `mindex` 直接返回时间戳；`minimax` 走 MiniMax `/speech_to_text` 并解析词级时间戳，无需 `align`；`llama_cpp` 与 `openai`（如百炼 qwen3-asr-flash，chat + `input_audio`）只出原始文本，需再 `align` |
| `deployment_manifest` | str \| null | null | llama_cpp 时必填 |
| `base_url` | str | `http://192.168.1.86:4020` | |
| `model` | str | `Qwen/Qwen3-ASR-1.7B` | |
| `revision` | str | `main` | |
| `enabled` | bool | true | `false` 时 transcribe 记录 `disabled`，无对白召回 |
| `max_tokens` | int >0 | 32768 | 聊天式 ASR（`llama_cpp`、`openai` 后端）单次转写请求的输出 token 上限；不进入转写指纹 |
| `aligner_model` | str | `Qwen/Qwen3-ForcedAligner-0.6B` | CPU 对齐模型 |
| `aligner_revision` | str | 代码默认 `main`；示例配置已固定 commit | 为 `main` 时 `align` 拒绝执行 |
| `timeout` | float >0 | 180 | HTTP 超时秒 |

## `tts` — 创作配音（studio.tts）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `openai` \| `minimax` \| `dashscope` | `openai` | `openai` 走 `/audio/speech`（本地 llama-server）；`minimax` 走 t2a_v2；`dashscope` 走阿里原生协议（后两者为线上专用适配器） |
| `base_url` / `model` / `voice` | str | `http://192.168.1.86:4030/v1` / `Qwen3-TTS` / `Cherry` | `voice` 可被设置页默认音色覆盖 |
| `response_format` | str | `wav` | 三种后端共用的音频格式 |
| `speech_path` | str | `/audio/speech` | 仅 openai 后端使用 |
| `timeout` | float >0 | 180 | HTTP 超时秒 |

## `video_generator` — 视频生成（studio.video_generator）

AI 生成视频（`aigc`）分镜 `generate` 阶段使用的视频生成服务，见 [创作工作流](studio.md#ai-生成视频)。设置页首次启动建好的默认卡为 MiniMax H3（`minimax_h3`）。设置页激活“视频生成”档案时自动启用并覆盖下列连接字段（`enabled` 不在档案白名单内）。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | false | 是否启用；关闭时 aigc 分镜的预检阻塞为“未配置视频生成服务” |
| `backend` | `minimax_h3` \| `minimax` \| `dashscope` \| `comfyui` | `comfyui` | MiniMax H3 视频生成 V2（生成方式等逐镜设置）/ MiniMax 海螺 video_generation / 阿里通义万相异步任务 / 自建 ComfyUI |
| `base_url` | str | `http://127.0.0.1:8188` | MiniMax H3 用 `https://api.minimax.cn`（末尾 `/v1` 自动去掉），海螺用 `https://api.minimax.cn/v1`，阿里用 `https://dashscope.aliyuncs.com` |
| `model` | str | 空 | H3 可留空（缺省 `MiniMax-H3`，可逐镜覆盖）；其余线上后端必填（如 `MiniMax-Hailuo-2.3`、`wan2.6-t2v`）；ComfyUI 可留空（以工作流为准） |
| `api_key_env` | str | `MINDEX_VIDEO_API_KEY` | 环境变量名；设置页档案的密钥优先 |
| `timeout` | float >0 | 600 | 单次 HTTP 请求超时秒（提交、轮询与下载结果都用它） |
| `poll_interval` | float 0–300 | 5 | 首次轮询间隔秒，之后按 1.5 倍退避，上限为 4 倍 |
| `max_wait` | float 0–86400 | 900 | 单个分镜从提交到完成的总等待上限秒；超时撤销远端任务并失败 |
| `durations` | list[float]，至少 1 项 | `[5.0]` | 后端允许的生成时长（秒，自动去重排序）：配音时长向上取整到最近的允许值，都更短时取最大值 |
| `fps` | int 1–60 | 16 | 仅 ComfyUI：映射 `frames` = 时长 × fps |
| `comfyui_workflow` | dict \| null | null | ComfyUI「导出（API）」格式工作流（`{节点ID: {class_type, inputs}}`）；界面格式（含 `nodes`/`links`）在加载时报错 |
| `comfyui_mapping` | dict[str,str] \| null | null | 请求字段 → `节点ID.inputs.输入名`；字段限 `prompt`（必填）、`negative`、`seed`、`frames`、`width`、`height`、`image`，每个目标节点与输入都必须存在于工作流，否则加载/保存时报中文错误 |

签名与配置指纹只含 backend、model、durations（ComfyUI 另含 fps、工作流摘要与映射），不含密钥、超时与轮询参数；`studio_key` 只在 `enabled` 时加入该段，未启用的配置指纹与旧版一致。

## `image_generator` — 图片生成（studio.image_generator）

AI 生成图片（`aigc_image`）分镜 `draw` 阶段使用的图片生成服务，见 [创作工作流](studio.md#ai-生成图片)。激活“图片生成”档案时自动启用。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | false | 关闭时 aigc_image 分镜的预检阻塞为“未配置图片生成服务” |
| `backend` | `minimax` \| `comfyui` | `minimax` | MiniMax image-01 / 本地 ComfyUI |
| `base_url` / `model` | str | `https://api.minimax.cn/v1` / `image-01` | ComfyUI 用 `http://127.0.0.1:8188`，`model` 可留空 |
| `api_key_env` | str | `MINDEX_IMAGE_API_KEY` | 环境变量名；设置页档案的密钥优先 |
| `timeout` | float >0 | 600 | 单次请求超时秒 |
| `poll_interval` / `max_wait` | float | 2 / 900 | 仅 ComfyUI：轮询间隔与总等待上限秒 |
| `comfyui_workflow` / `comfyui_mapping` | dict \| null | null | ComfyUI「导出（API）」工作流与映射；字段限 `prompt`（必填）、`negative`、`seed`、`width`、`height`、`image`（主体参考图） |

签名只含 backend、model（ComfyUI 另含工作流摘要与映射）；`studio_key` 只在 `enabled` 时加入该段。

## `music_generator` — 音乐生成（studio.music_generator）

配音列[背景音乐](studio.md#背景音乐)的 `music` 阶段与歌词生成使用的服务。激活“音乐生成”档案时自动启用。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | false | 关闭时背景音乐提示点的预检阻塞为“未配置音乐生成服务” |
| `backend` | `minimax` \| `comfyui` \| `audiocpp` | `minimax` | MiniMax 音乐生成（同步接口）/ 本地 ComfyUI / 本地 audio.cpp 的 MiniMax-Music3-GGUF（后两者不支持歌词生成，audiocpp 也不支持翻唱） |
| `base_url` / `model` | str | `https://api.minimax.cn/v1` / `music-3.0` | 音乐生成模型 |
| `cover_model` | str | `music-cover` | 翻唱模型 |
| `api_key_env` | str | `MINDEX_MUSIC_API_KEY` | 环境变量名；设置页档案的密钥优先 |
| `timeout` | float >0 | 600 | 单次请求超时秒（音乐同步生成较慢） |
| `poll_interval` / `max_wait` | float | 2 / 900 | 仅 ComfyUI |
| `num_inference_steps` | int 1–200 | 30 | 仅 audiocpp：flow matching 步数，越小越快 |
| `max_seconds` | int 1–300 | 20 | 仅 audiocpp：单次生成时长上限（耗时近似成正比；成片时背景音乐循环/截断到段落长度）。实测 Mac Metal 30 秒音频约 15 分钟 |
| `comfyui_workflow` / `comfyui_mapping` | dict \| null | null | 映射字段限 `prompt`（必填）、`lyrics`、`seconds`（段落预估时长）、`seed`、`audio`（翻唱参考音频） |

`audiocpp` 后端：`base_url` 指向 `audiocpp_server`（如 `http://127.0.0.1:8891`，启动见 `scripts/run-music-gen.sh`、`deploy/audiocpp-music3.json`），`model` 为服务配置里的模型 id（`minimax-music3`），无需密钥；调用 `POST /v1/tasks/run`，纯音乐以 `[Instrumental]` 作歌词，`duration_sec` 取段落预估时长（上限 300 秒）；Apple Silicon 的 Metal 后端实时率约 30 倍，`timeout` 必须大于预计耗时（否则客户端超时放弃，服务端仍继续计算）；服务端同一时刻只处理一个请求，`deploy/audiocpp-music3.json` 已把 `busy_timeout_ms` 设为 0，排队请求不会 503。

签名只含 backend、model、cover_model（ComfyUI 另含工作流摘要与映射，audiocpp 另含 `num_inference_steps`、`max_seconds`）；`studio_key` 只在 `enabled` 时加入该段。

## `animator` — 动画生成（studio.animator）

AI 动画分镜 `animate` 阶段使用的 LLM：按画面说明写自包含 HTML/JS 动画源码，渲染阶段在沙箱里逐帧录制，见 [创作工作流](studio.md#ai-动画)。协议与智能助手相同（OpenAI 兼容 `/chat/completions` 或 Anthropic `/v1/messages`，非流式、无工具调用）。

设置页首次启动建好并激活默认卡智谱 GLM-5.3-Flash（`backend: openai`、`base_url: https://open.bigmodel.cn/api/paas/v4`、`model: glm-5.3-flash`），激活后覆盖下述继承；档案与环境变量都没有密钥时，运行时复用多模态理解里同名模型档案的密钥与 `base_url`（`runtime_settings.animator_shares_analyzer_key`）。**yaml 省略 `studio.animator` 且没有激活动画生成档案时复制 `agent.llm` 的 `backend`、`base_url`、`model`、`revision`、`api_key_env`**，其余字段取下表默认值；设置页没有激活“动画生成”档案而激活了“智能助手”档案时，同样跟随该档案（含密钥）。显式写了 `studio.animator` 段则不再跟随。激活“动画生成”档案即启用（`enabled` 不在档案白名单内）。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | true | 关闭时 animation 分镜的预检阻塞为“未配置动画生成服务” |
| `backend` | `openai` \| `anthropic` | 同 `agent.llm`（`openai`） | 协议 |
| `base_url` / `model` / `revision` | str | 同 `agent.llm` | 连接字段 |
| `api_key_env` | str | 同 `agent.llm`（`MINDEX_AGENT_API_KEY`） | 环境变量名；设置页档案的密钥优先 |
| `timeout` | float >0 | 600 | 单次请求超时秒 |
| `max_tokens` | int >0 | 16000 | 输出上限；被截断时报错提示调大 |
| `retries` | int 0–10 | 2 | 生成的源码未通过静态校验时，带违规项反馈重试的次数 |
| `max_html_kb` | int 1–5000 | 200 | 动画源码大小上限（KiB），生成与人工保存都受限 |
| `max_capture_seconds` | float 0–3600 | 60 | 单个分镜逐帧录制的墙钟时长上限秒；画幅大、镜头长时可调大 |

animate 签名只含 backend、model、`max_html_kb` 与提示词版本，不含密钥与超时；该段不进入 `studio_key`。录制依赖为可选 extra `animation`（见 [部署](deployment.md#ai-动画录制可选)）。

## `studio` — 创作工作流

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_input_chars` | int ≥1 | 2000 | 视频大纲和文案的字数上限 |
| `max_shots` | int ≥1 | 1024 | 单任务分镜数量上限；拆分镜按意象拆细，条数会明显多于按场景拆分 |
| `material_digest_chars` | int 0–100000 | 12000 | 可用素材摘要的总字符预算（按 Unicode 字符计）。摘要只取任务素材范围内、未隐藏、已分析片段的画面分析文字，供文案创作与拆分镜参考；`0` 表示关闭（不生成摘要，文案请求与未接入摘要时一致）。必须是整数，越界或非整数在加载配置时报错 |
| `max_reference_chars` | int 1–1000000 | 30000 | 单个方案登记的[参考文稿](studio.md#参考文稿)总字符数上限（规范化后按 Unicode 字符计），超限的上传或粘贴返回 422。必须是整数 |
| `max_clip_seconds` | float 0–3600 | 300 | [分镜画面来源](studio.md#分镜画面来源)“上传片段”的视频时长上限（秒），超过返回 422“画面视频不能超过 N 秒”；查询视频仍按 `search.max_query_seconds` |
| `max_clip_mb` | int 1–10000 | 500 | 上传片段的文件大小上限（MB），超过返回 413；来源图片沿用 `search.max_image_upload_mb` |

### `studio.generator` — 文本拆分镜 LLM

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `openai` | `openai` | OpenAI 兼容 `/v1/chat/completions` |
| `llama_template` | bool | `true` | 是否发送 llama-server 专有的 `chat_template_kwargs`；线上厂商档案设 `false` |
| `base_url` / `model` / `revision` | str | `http://192.168.1.86:4040/v1` / `qwen3.8:27b` / `deployed-2026-10-12` | 本地默认复用理解模型；可切线上文本模型（如 deepseek-chat） |
| `timeout` / `api_key_env` | | 180 / `MINDEX_QWEN_API_KEY` | |
| `max_tokens` | int >0 | 4000 | 单次请求允许模型输出的最大 token 数（原样作为 chat/completions 的 `max_tokens` 发送），截断会明确报错；设置页里激活的“文本拆分镜”档案会覆盖这个值。分镜按意象拆细后输出更长，文案较长时请调高 |
| `split_retries` | int ≥0 | 2 | 分镜校验失败后的带反馈重试次数 |

### `studio.writer` — 文案创作 LLM

按视频大纲写旁白正文。字段与 `studio.generator` 完全相同（`GeneratorConfig`）；yaml 省略本段时复制 `studio.generator`，升级后无需改配置。其中 `split_retries` 在这里表示输出为空或超过 `max_input_chars` 时的带反馈重试次数，`max_tokens` 截断会明确报错。文案采样温度固定为 0.7（拆分镜为 0），提示词版本号（`WRITER_PROMPT_VERSION`）和本段配置都进入文案阶段的版本签名。

### `studio.recall` / `studio.selection` / `studio.audio` / `studio.subtitle` / `studio.render`

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `recall.granularity` | `shot` \| `moment` | `shot` | 召回粒度 |
| `recall.limit` | int 1–50 | 20 | 每镜召回候选数 |
| `selection.atempo_limit` | float ≤0.25 | 0.1 | 配音最大加速比 |
| `selection.gap_epsilon` | float >0 | 0.5 | 同源相邻延长判定容差 |
| `selection.weights` | 五键 dict | semantic 1 / duration 1 / extension 0.3 / repeat 0.5 / overlap 1 | semantic 与 duration 必须为正 |
| `audio.mode` | `tts_only` \| `ducking` | `tts_only` | 配音与原声混合策略 |
| `audio.duck_gain` / `audio.loudnorm` | | 0.15 / `I=-16:TP=-1.5:LRA=11` | ducking 压低增益 / 响度归一参数 |
| `subtitle.burn` / `subtitle.font_family` / `subtitle.font_file` / `subtitle.font_size` | | false / `Noto Sans CJK SC` / null / 7 | `burn: true` 时烧录字幕（依赖 ffmpeg libass）；默认输出软字幕轨道 |
| `render.width` / `render.height` | 偶数 | 1280 / 720 | 成片画布（x264 要求偶数） |
| `render.fps` / `render.crf` / `render.preset` | | 25 / 20 / `fast` | crf 0–51；preset 限 x264 枚举 |
| `render.canvases` | `{画幅: [宽, 高]}` | `9:16` → 720×1280，`1:1` → 1080×1080 | 方案[画幅](studio.md#画幅)的分辨率。键只能是 `16:9`、`9:16`、`1:1`，宽高为正偶数整数；未列出的画幅用默认值。`16:9` 恒为 `render.width/height`（写出时必须与之一致），旧配置与 16:9 方案的渲染签名因此不变。该键不进入配置指纹与任务快照（非默认画幅的实际宽高由 render/compose 阶段写进签名参数） |

## `detector` — 分镜

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `adaptive` \| `transnet` | `adaptive` | adaptive 本地 CPU；transnet 用远程服务 |
| `base_url` | str | `http://192.168.1.86:18203` | transnet 服务地址 |
| `threshold` | 0<float<1 | 0.5 | transnet 转场阈值 |
| `adaptive_threshold` | float >0 | 3.0 | AdaptiveDetector 灵敏度 |
| `min_content_val` | float ≥0 | 10.0 | AdaptiveDetector 内容阈值 |
| `timeout` | float >0 | 1800 | transnet 整段视频检测的请求超时秒（该服务不在设置页，只能在 yaml 配置） |
| `api_key_env` | str | `MINDEX_MODEL_API_KEY` | |

## `video` — 切分与采样（变化 = 新 `data_dir`）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `short_shot_seconds` | float >0 | 6 | ≤ 此长度的镜头整体成为一个 moment；也是单次嵌入上限 |
| `window_seconds` | float >0 | 4 | 长镜头滑动窗口长度 |
| `stride_seconds` | float >0（≤window） | 3 | 窗口步长 |
| `fps` | float >0 | 4 | 嵌入采样帧率 |
| `max_frames` | int ≥2 | 24 | 单次嵌入最多帧数 |
| `max_pixels` | int ≥4096 | 262144 | 单帧最大像素（512×512） |
| `analysis_frames` | int 2–24 | 12 | Qwen 分析采样帧数 |

## `search` — 检索

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `candidates` | int 1–1000 | 100 | 每路召回候选数 |
| `rrf_k` | int ≥1 | 30 | RRF 常数 |
| `channel_weights` | 三键非负：`visual`/`dialogue`/`analysis` | visual 0.35、dialogue 0.15、analysis 0.5 | 新版（format 2）索引的三渠道 RRF 权重，必须同时给出三个键；见 [三渠道说明](recall-enhancement.md) |
| `weights` | 五键非负 | video/description/style=1，dialogue/lexical=0.5 | 旧版（format 1）索引的五路权重；某路设 0 即关闭该路。与 `channel_weights` 互不影响 |
| `lexical_exclude` | list | `[]` | BM25 文档中排除的字段（评测消融用：`description`/`style`/`dialogue`） |
| `max_upload_mb` | int ≥1 | 100 | 视频查询上传上限 |
| `max_image_upload_mb` | int 1–100 | 20 | 静态图片查询上传上限（MB） |
| `max_query_seconds` | float >0 | 30 | 视频查询时长上限 |

## `clean` — 磁盘清理（`mindex clean`）

所有天数为 mtime 年龄；分级判定与锁行为详见 [cleanup.md](cleanup.md)。默认预览，`--apply` 才执行删除；CLI `--days N` 可把全部 `*_days` 阈值临时替换为 N 天（不改本文件，硬保护不受影响）。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `index_keep_versions` | int ≥1 | 2 | 除活动版本外额外保留的最近 ready 索引版本数（向量复用与回退来源） |
| `index_building_max_age_days` | int ≥1 | 7 | building 索引版本的保留天数：超期视为中断残留并入 `index_versions` 删除（真实构建远短于此）；删除即放弃同 spec 的跨运行续跑 |
| `temp_grace_hours` | int ≥1 | 24 | 未注册上传文件 / 空上传目录 / 无记录助手附件目录与附件上传暂存的宽限小时数，防正在进行的上传 |
| `transcripts_max_age_days` | int ≥1 | 30 | 旧 `asr_key` 转写块检查点保留天数（换配置后永不命中） |
| `analysis_max_age_days` | int ≥1 | 30 | 旧分析 profile 的场景分组检查点保留天数 |
| `dialogue_max_age_days` | int ≥1 | 30 | 旧对白 profile 的 30 秒块检查点保留天数（按 `channel_data` 现行 profile 判定） |
| `context_max_age_days` | int ≥1 | 14 | 在线播放转码缓存 `context/*.mp4` 保留天数，可按需重建 |
| `shot_media_max_age_days` | int ≥1 | 7 | shot 媒体合成与探测缓存 `cache/shot-media/` 保留天数，按内容指纹可重新合成；读取中（缓存租约锁被持）跳过 |
| `studio_preview_max_age_days` | int ≥1 | 14 | studio 分镜预览保留天数，可按需重建 |
| `studio_attempt_max_age_days` | int ≥1 | 30 | 未被检查点或当前 attempt 引用的旧 attempt 目录保留天数 |
| `studio_export_max_age_days` | int ≥1 | 60 | studio 导出 zip 保留天数，可重新打包 |
| `reports_max_age_days` | int ≥1 | 60 | `reports/` 导出产物保留天数 |
| `subtitles_max_age_days` | int ≥1 | 30 | 字幕轨道解析缓存保留天数（不含 `uploads/` 用户原件） |
| `runs_max_age_days` | int ≥1 | 90 | `runs` 审计表已完成记录保留天数（running 一律保留） |

## `agent` — 智能助手

全局助手面板与 MCP 导出：`enabled`、`context_window_steps`（默认 256，执行步数不限）、`max_context_tokens`、`secret_ttl_seconds`、附件（`attachment_ttl_hours`、`attachment_max_mb`）与视觉（`max_images`）、`llm.*`（`backend: openai|anthropic`、`vision`，设置页「智能助手」档案可覆盖）与 `mcp.*`（`http_enabled`、`policy: read|write|all`、`token_env`、`domains`）。旧 `max_steps` 配置忽略。键说明与默认值见 [智能助手 · 配置](agent.md#配置)。

## `local_services` — 本机模型服务

设置页“本机服务”能启停的本机服务（见 [设置页指南 · 本机服务](settings.md#本机服务)、[桌面版](desktop.md#本机模型服务)）。该段不进入任何 `model_dump`（`exclude=True`），任务快照、备份配置与指纹不受影响。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | false | 开启管理；关闭时接口全部返回 403。桌面版配置模板为 true |
| `services[].name` | str | — | 服务名，`^[a-z0-9][a-z0-9-]{0,31}$`，不能重复 |
| `services[].label` / `role` | str | 空 | 显示名与用途说明（如 embedding、asr、tts、music、detector、person） |
| `services[].argv` | list[str] | — | 启动命令（至少一项）；`argv[0]` 含路径分隔符时相对 `cwd`，否则从 PATH 查找，`~` 展开 |
| `services[].cwd` | str \| null | null | 工作目录；相对路径按配置文件所在目录解析 |
| `services[].env` | dict | {} | 追加的环境变量 |
| `services[].port` | int | — | 本机监听端口，不能重复；健康检查访问 `http://127.0.0.1:<port><health_path>` |
| `services[].health_path` | str | `/health` | 健康检查路径（2xx 为健康） |
| `services[].autostart` / `stop_on_exit` | bool | false / true | 缺省值；设置页修改后保存在数据目录，优先于这里 |

```yaml
local_services:
  enabled: true
  services:
    - {name: wemm, label: WeMM 嵌入, role: embedding, port: 8888,
       argv: [/path/to/mindex/scripts/run-wemm.sh], cwd: /path/to/mindex}
```

## `topics` — 选题挖掘

创作页「选题挖掘」（见[选题挖掘](topics.md)）。领域档案、数据源与定时时刻在页面里设置并存放在数据库里，这里只有全局参数。该段不进入任何 `model_dump`（`exclude=True`），不影响任务快照、备份配置与指纹。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `llm` | 同 `studio.writer` \| null | null | 选题模型（归纳、受众匹配、大纲）；null 时复用 `studio.writer`（含设置页「文案创作」档案） |
| `scheduler.enabled` | bool | true | `mindex serve` 运行时按档案定时每天挖掘，启动时补跑当天错过的定时 |
| `scheduler.interval_seconds` | int 5–3600 | 60 | 定时检查间隔 |
| `fetch.timeout` | float | 15 | 单次请求超时（秒） |
| `fetch.max_bytes` | int | 2097152 | 单次响应与日历上传上限 |
| `fetch.item_days` | int 1–365 | 30 | 候选条目保留天数（被卡片引用的不清理） |
| `fetch.max_items_per_source` | int | 100 | 每个来源每次最多条目 |
| `cluster.threshold` | float (0, 1] | 0.82 | 聚类的余弦相似度阈值 |
| `cluster.max_clusters` | int | 40 | 每次挖掘最多候选簇 |
| `cluster.batch` | int | 8 | 每次请求模型归纳的簇数 |
| `coverage.min_similarity` | float 0–1 | 0.3 | 关键画面首个检索结果的向量相似度达到该值记为素材命中 |
| `coverage.limit` | int 1–10 | 3 | 每条关键画面取几个候选片段 |
| `brave.base_url` | str | `https://api.search.brave.com` | Brave Search 接口地址 |
| `brave.max_queries` | int 1–100 | 10 | 每次抓取调用 Brave 的次数上限（计费保护） |

## 常用组合

**整部电影画面索引（只有 WeMM）**：默认 adaptive detector + `embedding` 指向 WeMM，`index --mode visual --source ID --activate`。

**llama.cpp 路径（当前 config.local.yaml）**：三个 backend 均为 `llama_cpp`，各自配 `deployment_manifest`；先用 `e2e probe`/`e2e pin` 生成 `data/deployments/{wemm,asr,analyzer}.json`，ASR 后须跑 CPU `align`；llama_cpp 嵌入在建正式索引前必须通过 `e2e phase smoke` 能力门槛。

**TransNetV2 分镜**：`detector.backend: transnet`，prepare 之前先启动 detector 服务。

**暂不部署 ASR**：`asr.enabled: false` 后 transcribe 记录 `disabled`；此索引没有对白召回，开启 ASR 后需重跑转写、分析和建索引。

## 环境变量

| 变量 | 用途 |
|---|---|
| `MINDEX_CONFIG` | 默认配置文件路径 |
| `MINDEX_MODEL_API_KEY` | 模型服务 Bearer token（embedding/asr/detector 共用默认） |
| `MINDEX_QWEN_API_KEY` | Qwen 服务 token（分析、拆分镜、文案、TTS 默认） |
| `MINDEX_AGENT_API_KEY` | 智能助手 LLM 的密钥（`agent.llm.api_key_env` 默认；`studio.animator` 缺省时跟随） |
| `MINDEX_MCP_TOKEN` | `/mcp` 的 Bearer 令牌（`agent.mcp.token_env` 默认），见 [智能助手](agent.md#mcp-导出与外部-harness) |
| `MINDEX_VIDEO_API_KEY` | 视频生成服务密钥（`studio.video_generator.api_key_env` 默认） |
| `MINDEX_PLAYWRIGHT_MODULE` | 仅测试：UI 回归（Node）使用的 Playwright 模块路径 |
| `MINDEX_WEB_TOKEN` | 设置后 `mindex serve` 的页面和 API 需要访问令牌，见 [CLI 参考](cli-reference.md#mindex-serve---host-127001---port-8000---index-id-index_id---allow-insecure)；监听非回环地址时必须设置，否则 `serve` 拒绝启动（除非传 `--allow-insecure`） |
| `MINDEX_FFMPEG` / `MINDEX_FFPROBE` | ffmpeg / ffprobe 可执行文件路径（桌面版指向随包构建）；缺省从 PATH 查找。不进入任何签名 |
| `MINDEX_DESKTOP` | 桌面外壳为后端设置为 `1`：AI 动画录制缺少浏览器时提示到设置页一键安装 |
| `MINDEX_GPU_LOCK` | GPU 文件锁路径（各角色须一致） |
| `MINDEX_TRANSNET_DIR` | 服务端 TransNetV2 `inference` 目录 |

## 素材上传限额

顶层 `source_upload_max_bytes` 默认 `21474836480`（20 GiB，每文件），适用于 `/api/admin/sources/upload`。独立于 `search.max_upload_mb` 的检索查询视频限额。


## `person` — 人物服务

独立人物检测/特征服务的客户端配置，不改变内容分镜或 WeMM 嵌入空间。安装和启动见 [人物库](persons.md)。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `mindex` | `mindex` | 目前仅支持自带人物服务 |
| `base_url` | URL | `http://127.0.0.1:4050` | 人物服务地址 |
| `model` | `cheetah_l` | `cheetah_l` | 人物模型 |
| `revision` | str | `manifest-v2` | 模型清单版本 |
| `timeout` | float >0 | 180 | 请求超时（秒） |
| `model_root` | str | `~/.insightface` | 服务端模型文件根目录；环境变量 `MINDEX_PERSON_MODEL_ROOT` 优先（`run-person-service.sh` 默认指向仓库 `models/insightface`） |
| `expected_space` | object 或 null | null | 把服务 `/health` 返回的 `space` 完整复制到此处，可固定模型文件摘要与预处理；不一致时拒绝处理 |
| `frame_interval` | int ≥1 | 15 | 每个分镜每隔多少帧抽取一帧，见下文 |
| `fps` | float 0–5 | 5 | 兼容旧配置，不参与抽帧 |
| `face_threshold` | float 0–1（开区间） | 0.55 | 人脸匹配分数门槛 |
| `face_margin` | float 0–1（开区间） | 0.1 | 最佳与次佳候选的最小分差 |
| `min_face_quality` | float 0–1 | 0.6 | 视频帧人脸质量门槛，低于此值不接受自动确认 |
| `min_reference_quality` | float 0–1 | 0 | 参考图质量门槛，低于此值不参与匹配；0 表示全部参与 |
| `body_threshold` | float 0–1（开区间） | 0.8 | 人体匹配分数门槛 |

## person 展示帧

| 配置 | 默认值 | 含义 |
|---|---|---|
| `frames_max_edge` | 640 | 展示图长边上限，整数 ≥0，0 保持原尺寸 |
| `frames_jpeg_quality` | 85 | JPEG 质量，整数 1–100 |

仅影响新展示图和显式 `persons migrate-frames --apply`，不改变模型空间、特征缓存或匹配缓存。
原尺寸模型输入不受影响，references/imports 仍存 PNG。详见 [人物库](persons.md)。

人物配置 `person.frame_interval`（设置页“间隔帧数”）：整数且 ≥1，默认 15。每个分镜从首个有效帧开始每隔该帧数抽取，保留实际 PTS；1 表示逐帧。旧 `person.fps` 仅兼容旧配置，不叠加抽帧限制。

## `storage` — 媒体存储后端（水平扩展阶段 4）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `local` \| `s3` | `local` | `local` 为单机文件系统（现有行为）；`s3` 为 S3 兼容对象存储（`S3MediaStore`：不可变键 + sha256 元数据、multipart 大文件、版本命名空间 `versions/<v>/<key>`、物化到节点 staging 并 pin、presign 短期读 URL）。需要 `database.backend=postgres`；媒体出口与检查点迁移随阶段 5 交付，当前为单节点验证形态 |
| `staging_quota_gb` | float | `0` | 本地形态的节点工作集预算（0=不限，保持现有行为）；`reserve`/`pin`/LRU 语义见 `shared/backends/staging.py` |
| `s3.endpoint_url` / `s3.bucket` / `s3.prefix` | str | — | 服务地址与命名空间（prefix 建议 `libraries/<library_id>/`）；凭据只从环境变量读取 |
| `s3.staging_quota_gb` / `s3.min_free_gb` | float | `100` / `20` | s3 形态固定启用的工作集预算与磁盘最低余量（超限明确拒绝） |
| `s3.presign_expire_seconds` | int | `900` | 预签名读 URL 有效期（可转交能力，过期前无法靠登出撤销） |

NFS 为后续可选共享媒体后端，不能存放 SQLite/嵌入式 Chroma 或作为分布式锁依据；启用前须实测 rename/同步/错误恢复语义（plans §4）。
