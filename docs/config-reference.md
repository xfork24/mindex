# 配置参考

配置从 `config.example.yaml` 复制为 `config.local.yaml`（gitignored），或用 `--config` / `MINDEX_CONFIG` 指定。模型 strict（`extra="forbid"`），未知键校验失败；未写的键用默认值。`data_dir` 与 `deployment_manifest` 的相对路径按**配置文件所在目录**解析。

> **运行时设置**：模型服务连接、音色与检索调参可在 `/settings` 设置页管理（SQLite 覆盖层，保存即热生效，Web 与 CLI 共用），本文档的 yaml 取值是「未激活任何档案时」的基础值。详见 [settings.md](settings.md)。所有服务段共有的可选键 `api_key`（默认空，仅由设置页写入；优先于 `api_key_env`）不参与任何指纹。

## 顶层

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `data_dir` | Path | `data` | 素材库根目录。切分参数变化须换新目录 |
| `runtime_overrides` | bool | true | false 固定 YAML，CLI 和验收 Web 每次读取都禁用动态覆盖；也可全局传 `--no-runtime-overrides` |

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
| `max_input_chars` | int ≥1 | 2000 | 创作文案字数上限 |
| `max_shots` | int ≥1 | 60 | 单任务分镜数量上限 |

### `studio.generator` — 文本拆分镜 LLM

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backend` | `openai` | `openai` | OpenAI 兼容 `/v1/chat/completions` |
| `llama_template` | bool | `true` | 是否发送 llama-server 专有的 `chat_template_kwargs`；线上厂商档案设 `false` |
| `base_url` / `model` / `revision` | str | `http://192.168.1.86:4040/v1` / `qwen3.8:27b` / `deployed-2026-10-12` | 本地默认复用理解模型；可切线上文本模型（如 deepseek-chat） |
| `timeout` / `api_key_env` | | 300 / `MINDEX_QWEN_API_KEY` | |
| `max_tokens` | int >0 | 4000 | 单次拆分镜输出上限，截断会明确报错 |
| `split_retries` | int ≥0 | 2 | 分镜校验失败后的带反馈重试次数 |

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
| `weights` | 五键非负 | video/description/style=1，dialogue/lexical=0.5 | 五路权重；某路设 0 即关闭该路 |
| `lexical_exclude` | list | `[]` | BM25 文档中排除的字段（评测消融用：`description`/`style`/`dialogue`） |
| `max_upload_mb` | int ≥1 | 100 | 视频查询上传上限 |
| `max_image_upload_mb` | int 1–100 | 20 | 静态图片查询上传上限（MB） |
| `max_query_seconds` | float >0 | 30 | 视频查询时长上限 |

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
| `MINDEX_GPU_LOCK` | GPU 文件锁路径（各角色须一致） |
| `MINDEX_TRANSNET_DIR` | 服务端 TransNetV2 `inference` 目录 |
