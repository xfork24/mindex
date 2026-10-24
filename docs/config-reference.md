# 配置参考

配置从 `config.example.yaml` 复制为 `config.local.yaml`（gitignored），或用 `--config` / `MINDEX_CONFIG` 指定。模型 strict（`extra="forbid"`），未知键校验失败；未写的键用默认值。`data_dir` 与 `deployment_manifest` 的相对路径按**配置文件所在目录**解析。

> **运行时设置**：模型服务连接、音色与检索调参可在 `/settings` 设置页管理（SQLite 覆盖层，保存即热生效，Web 与 CLI 共用），本文档的 yaml 取值是「未激活任何档案时」的基础值。详见 [settings.md](settings.md)。所有服务段共有的可选键 `api_key`（默认空，仅由设置页写入；优先于 `api_key_env`）不参与任何指纹。

## 顶层

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `data_dir` | Path | `data` | 素材库根目录。切分参数变化须换新目录 |
| `runtime_overrides` | bool | true | false 固定 YAML，CLI 和验收 Web 每次读取都禁用动态覆盖；也可全局传 `--no-runtime-overrides` |
| `enforce_gpu_lock` | bool | false | 强加 GPU 锁；开启后处理任务互斥、处理期间素材删除返回 409。关闭时允许不同素材/不同类型并行，同类重复任务和删除正在处理素材仍受保护；设置页可热更新 |

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
| `backend` | `mindex` \| `llama_cpp` \| `openai` | `mindex` | `mindex` 直接返回时间戳；`llama_cpp` 与 `openai`（如百炼 qwen3-asr-flash，chat + `input_audio`）只出原始文本，需再 `align` |
| `deployment_manifest` | str \| null | null | llama_cpp 时必填 |
| `base_url` | str | `http://192.168.1.86:4020` | |
| `model` | str | `Qwen/Qwen3-ASR-1.7B` | |
| `revision` | str | `main` | |
| `enabled` | bool | true | `false` 时 transcribe 记录 `disabled`，无对白召回 |
| `aligner_model` | str | `Qwen/Qwen3-ForcedAligner-0.6B` | CPU 对齐模型 |
| `aligner_revision` | str | 代码默认 `main`；示例配置已固定 commit | 为 `main` 时 `align` 拒绝执行 |
| `timeout` | float >0 | 300 | HTTP 超时秒 |

## `tts` — 创作配音（studio.tts）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `openai` \| `minimax` \| `dashscope` | `openai` | `openai` 走 `/audio/speech`（本地 llama-server）；`minimax` 走 t2a_v2；`dashscope` 走阿里原生协议（后两者为线上专用适配器） |
| `base_url` / `model` / `voice` | str | `http://192.168.1.86:4030/v1` / `Qwen3-TTS` / `Cherry` | `voice` 可被设置页默认音色覆盖 |
| `response_format` | str | `wav` | 三种后端共用的音频格式 |
| `speech_path` | str | `/audio/speech` | 仅 openai 后端使用 |
| `timeout` | float >0 | 300 | HTTP 超时秒 |

## `studio` — 创作工作流

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `max_input_chars` | int ≥1 | 2000 | 视频大纲和文案的字数上限 |
| `max_shots` | int ≥1 | 1024 | 单任务分镜数量上限；拆分镜按意象拆细，条数会明显多于按场景拆分 |
| `material_digest_chars` | int 0–100000 | 12000 | 可用素材摘要的总字符预算（按 Unicode 字符计）。摘要只取任务素材范围内、未隐藏、已分析片段的画面分析文字，供文案创作与拆分镜参考；`0` 表示关闭（不生成摘要，文案请求与未接入摘要时一致）。必须是整数，越界或非整数在加载配置时报错 |

### `studio.generator` — 文本拆分镜 LLM

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `openai` | `openai` | OpenAI 兼容 `/v1/chat/completions` |
| `llama_template` | bool | `true` | 是否发送 llama-server 专有的 `chat_template_kwargs`；线上厂商档案设 `false` |
| `base_url` / `model` / `revision` | str | `http://192.168.1.86:4040/v1` / `qwen3.8:27b` / `deployed-2026-10-12` | 本地默认复用理解模型；可切线上文本模型（如 deepseek-chat） |
| `timeout` / `api_key_env` | | 300 / `MINDEX_QWEN_API_KEY` | |
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
| `subtitle.burn` / `subtitle.font_family` / `subtitle.font_file` / `subtitle.font_size` | | true / `Noto Sans CJK SC` / null / 7 | 烧录字幕（依赖 ffmpeg libass） |
| `render.width` / `render.height` | 偶数 | 1280 / 720 | 成片画布（x264 要求偶数） |
| `render.fps` / `render.crf` / `render.preset` | | 25 / 20 / `fast` | crf 0–51；preset 限 x264 枚举 |

## `detector` — 分镜

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `adaptive` \| `transnet` | `adaptive` | adaptive 本地 CPU；transnet 用远程服务 |
| `base_url` | str | `http://192.168.1.86:18203` | transnet 服务地址 |
| `threshold` | 0<float<1 | 0.5 | transnet 转场阈值 |
| `adaptive_threshold` | float >0 | 3.0 | AdaptiveDetector 灵敏度 |
| `min_content_val` | float ≥0 | 10.0 | AdaptiveDetector 内容阈值 |
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
| `rrf_k` | int ≥1 | 60 | RRF 常数 |
| `channel_weights` | 三键非负：`visual`/`dialogue`/`analysis` | 均为 1 | 新版（format 2）索引的三渠道 RRF 权重，必须同时给出三个键；见 [三渠道说明](recall-enhancement.md) |
| `weights` | 五键非负 | video/description/style=1，dialogue/lexical=0.5 | 旧版（format 1）索引的五路权重；某路设 0 即关闭该路。与 `channel_weights` 互不影响 |
| `lexical_exclude` | list | `[]` | BM25 文档中排除的字段（评测消融用：`description`/`style`/`dialogue`） |
| `max_upload_mb` | int ≥1 | 100 | 视频查询上传上限 |
| `max_image_upload_mb` | int 1–100 | 20 | 静态图片查询上传上限（MB） |
| `max_query_seconds` | float >0 | 30 | 视频查询时长上限 |

## `clean` — 磁盘清理（`mindex clean`）

所有天数为 mtime 年龄；分级判定与锁行为详见 [cleanup.md](cleanup.md)。默认预览，`--apply` 才执行删除。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `index_keep_versions` | int ≥1 | 2 | 除活动版本外额外保留的最近 ready 索引版本数（向量复用与回退来源）；building 一律保留 |
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

全局助手面板与 MCP 导出：`enabled`、`max_steps`、`max_context_tokens`、`secret_ttl_seconds`、附件（`attachment_ttl_hours`、`attachment_max_mb`）与视觉（`max_images`）、`llm.*`（`backend: openai|anthropic`、`vision`，设置页「智能助手」档案可覆盖）与 `mcp.*`（`http_enabled`、`policy: read|write|all`、`token_env`、`domains`）。键说明与默认值见 [智能助手 · 配置](agent.md#配置)。

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
| `MINDEX_QWEN_API_KEY` | Qwen 服务 token |
| `MINDEX_WEB_TOKEN` | 设置后 `mindex serve` 的页面和 API 需要访问令牌，见 [CLI 参考](cli-reference.md#mindex-serve---host-127001---port-8000---index-id-index_id) |
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
| `timeout` | float >0 | 600 | 请求超时（秒） |
| `model_root` | str | `~/.insightface` | 服务端模型文件根目录 |
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
