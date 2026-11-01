# 设置页指南

`/settings`（顶部导航「设置」进入）把模型服务连接、音色库、检索调参与索引版本集中在一个页面，**保存即热生效，无需重启**：配置档案存 SQLite（`model_profiles` / `voices` 表 + `settings` KV），Web API 与 CLI 命令统一经覆盖层合成「生效配置」。

动画生成、视频生成、图片生成与音乐生成在升级后第一次启动时各建好“默认生效配置卡”（只对还没有任何档案的角色，只做一次，之后删改不会被再次覆盖）：动画生成为智谱 GLM-5.3-Flash（`glm-5.3-flash`；卡片不填密钥且未设环境变量时，复用“多模态理解”里同名模型档案的密钥与 Base URL（二者成对：例如智谱 GLM Coding Plan 的密钥只能用于 `/api/coding/paas/v4`，用于通用端点会报 429“余额不足”）——激活的档案优先，其次最近更新的，按模型名不区分大小写匹配，概览显示“密钥复用多模态理解同名模型”），视频生成为 MiniMax H3，图片生成为 MiniMax image-01，音乐生成为 MiniMax music-3.0，均激活为默认，需要在卡片里填入对应厂商的 API Key；视频/图片/音乐生成另建一张未激活的“本地 ComfyUI”卡（`http://127.0.0.1:8188`，导入工作流与节点映射后可激活）。

文案创作档案在升级后第一次启动时自动从当前激活的“文本拆分镜”档案复制（含厂商与密钥）并激活，只做一次；之后可单独修改或删除，不会被再次覆盖。没有激活的拆分镜档案时，`studio.writer` 在 yaml 里省略即复制 `studio.generator`。

相关模块：`settings/runtime_settings.py`（档案/覆盖层/预设）、`settings/settings_api.py`（API）、`settings/voice_clone.py`（厂商克隆客户端）、`frontend/src/settings/settings.*`（页面）。

## 十二个模型角色

| 角色 | 配置段 | 说明 |
|---|---|---|
| 多模态理解 | `analyzer` | 视觉帧分析（动作/情绪/场景），需要视觉模型 |
| 文案创作 | `studio.writer` | 创作任务内按视频大纲写旁白文案，纯文本任务；配置字段与文本拆分镜相同，可共用同一个模型 |
| 文本拆分镜 | `studio.generator` | 创作文案拆分镜，纯文本任务，可选高性价比文本模型（本地默认复用理解模型） |
| 多模态嵌入 | `embedding` | 文本/图片/视频统一向量空间，**切换后需重建索引** |
| 语音识别 | `asr` | 阿里 qwen3-asr-flash 等 OpenAI 兼容后端只出纯文本，时间戳由本地 CPU ForcedAligner 补齐；MiniMax（`backend: minimax`）解析词级时间戳，无需对齐 |
| 语音合成 | `studio.tts` | 创作配音；支持预定义与克隆音色 |
| 图片生成 | `studio.image_generator` | AI 生成图片分镜的生成图片（`backend`：`minimax` image-01 文生图/主体参考图生图 / `comfyui` 本地工作流，映射 `prompt`、`negative`、`seed`、`width`、`height`、`image`）；激活档案即启用。测试连接只发送缺少提示词的请求验证鉴权（ComfyUI 读 `/system_stats`），**从不生成图片**；见 [创作工作流](studio.md#ai-生成图片) |
| 视频生成 | `studio.video_generator` | AI 生成视频分镜的生成画面（`backend`：`minimax_h3` MiniMax H3 视频生成 V2（默认卡）/ `minimax` 海螺 / `dashscope` 通义万相 / `comfyui` 本地）；H3 在设置页只需 endpoint（`https://api.minimax.cn`，误填 `/v1` 也可）与密钥，模型名可留空，文生/首帧/尾帧/首尾帧/主体参考等生成方式、模型档位、分辨率、比例与时长在创作页逐镜设置；激活档案即启用（`enabled=true`），无激活档案时沿用 yaml 的 `enabled`（缺省关闭）。测试连接只调用 `health()`（MiniMax/DashScope 查询一个不存在的任务以验证鉴权，ComfyUI 读 `/system_stats` 并校验工作流映射），**从不提交生成**。ComfyUI 档案在表单里导入「导出（API）」格式工作流 JSON（粘贴或选文件）并逐项填写节点映射（`prompt`、`negative`、`seed`、`frames`、`width`、`height`、`image` → `节点ID.inputs.输入名`），页面先做 JSON 与映射预校验，保存时服务端再校验并返回中文错误；见 [创作工作流](studio.md#ai-生成视频) |
| 音乐生成 | `studio.music_generator` | 配音列背景音乐的生成（`backend`：`minimax` music-3.0 音乐生成、`cover_model`（缺省 `music-cover`）翻唱与歌词生成 / `comfyui` 本地工作流，映射 `prompt`、`lyrics`、`seconds`、`seed`、`audio`，不支持歌词生成 / `audiocpp` 本地 audio.cpp 的 MiniMax-Music3-GGUF，不支持翻唱与歌词生成）；激活档案即启用。测试连接只验证鉴权，**从不生成音乐**。MiniMax 自 2026-08-20 起付费音乐/歌词接口仅对历史付费用户开放；见 [创作工作流](studio.md#背景音乐) |
| 动画生成 | `studio.animator` | AI 动画分镜的生成动画（写 HTML/JS 动画源码，渲染时沙箱逐帧录制）；协议与智能助手相同（`openai` / `anthropic`）。默认卡为智谱 GLM-5.3-Flash；没有激活动画生成档案且 yaml 省略该段时复制 `agent.llm` 并跟随激活的智能助手档案；激活档案即启用（`enabled=true`）。档案字段另含 `max_tokens`、`retries`（校验重试）、`max_html_kb`（源码上限）、`max_capture_seconds`（录制墙钟上限）。测试连接只发一次最小对话；预设沿用智能助手的 DeepSeek、阿里百炼与 Anthropic，智谱为 `glm-5.3-flash`（`max_tokens` 预填 16000）；见 [创作工作流](studio.md#ai-动画) |
| 人物识别 | `person` | 独立人物检测/特征服务（`mindex-person-server`）的连接与匹配参数，见 [人物库](persons.md) |
| 智能助手 | `agent.llm` | 全局助手面板的对话模型，须支持工具调用（OpenAI 兼容 tools 或 Anthropic 原生协议）；测试连接会验证工具调用，见 [智能助手](agent.md) |

每个角色可保存多个「配置档案」（名称 + 厂商 + 连接字段 + 密钥），任意时刻激活其一；未激活时直接用 `config.local.yaml` 的取值。档案只覆盖白名单连接字段（`runtime_settings.PROFILE_FIELDS`），`data_dir`、切分布局、对齐器等本地设施永不被触碰。

## 线上厂商支持（调研结论，2026-10）

| 角色 | 智谱 | DeepSeek | 阿里百炼 | 腾讯 | MiniMax |
|---|---|---|---|---|---|
| 多模态理解 | GLM-4.5V（OpenAI 兼容） | vision 模型 | Qwen-VL（compatible-mode） | 混元 vision | chat OpenAI 兼容 |
| 文案创作 / 文本拆分镜 | GLM 文本 | **deepseek-chat（性价比首选）** | qwen 文本 | 混元文本 | M 系列 |
| 多模态嵌入 | — | — | — | **TokenHub**（WeMM 系，`/v1/embeddings/multimodal`） | — |
| ASR | — | — | qwen3-asr-flash（chat + input_audio） | —（自有签名协议） | asr-1.0（speech_to_text 专用适配器） |
| 语音合成 | — | — | DashScope 原生（专用适配器） | —（自有协议） | t2a_v2（专用适配器） |
| 图片生成 | — | — | — | — | image-01（image_generation 专用适配器） |
| 视频生成 | — | — | 通义万相 wan 系列（DashScope 异步任务，专用适配器） | — | **MiniMax H3**（视频生成 V2 专用适配器）；海螺 MiniMax-Hailuo（video_generation） |
| 音乐生成 | — | — | — | — | music-3.0 / music-cover / 歌词生成（专用适配器） |

要点：

- **多模态嵌入的线上选择基本只有腾讯 TokenHub**（`kinfra-vl-embedding-2b/8b`，与本地 WeMM 同系）。适配器在客户端按 `video` 配置抽帧后以图片序列发送，保证预处理指纹诚实；身份纯由配置构造，进入索引空间指纹。
- **语音合成五家都没有标准 OpenAI `/audio/speech` 端点**，因此写了两个专用适配器：`minimax`（t2a_v2，hex 音频解码）与 `dashscope`（原生协议，音频 URL 二次下载或 base64）。
- **ASR 线上协议分两类**：OpenAI 兼容 chat + input_audio（阿里）与 MiniMax `speech_to_text`（multipart 表单上传，单文件 ≤500 秒/50MB，管线 30 秒切块远小于上限）；阿里只出纯文本，时间戳由本地对齐补齐；MiniMax 请求词级时间戳（`timestamp_level=word`），直接得到带时间戳的对白。
- 理解/文案创作/拆分镜的 OpenAI 兼容端点会自动省略 llama-server 专有的 `chat_template_kwargs`（档案里 `llama_template=false`）；本地服务保持默认 `true` 不变。
- 动画生成预设与智能助手相同（同为工具无关的聊天调用，`max_tokens` 预填 16000，源码较长）。
- 视频生成预设：MiniMax 为 `minimax_h3` / `MiniMax-H3`（时长 4–15 秒，按配音时长或逐镜设置；海螺 `MiniMax-Hailuo-2.3` 可用自定义档案选 `minimax` 后端）、阿里云百炼 `wan2.6-t2v`（5/10/15 秒，base_url 与 DashScope 语音合成相同；有参考图的分镜需改用图生视频模型如 `wan2.6-i2v-flash`）；密钥沿用各厂商同一 API Key。协议出处与核对日期记录在 `models/video_generators.py` 文件头。
- 图片/音乐生成预设：MiniMax `image-01` 与 `music-3.0`（翻唱 `music-cover`），base_url 与 MiniMax 语音合成相同；协议出处见 `models/image_generators.py`、`models/music_generators.py` 文件头。
- 预设的模型名是「最佳已知」预填值，会随平台演进漂移——页面所有字段可改，每个档案有「测试连接」按钮（最小真实调用）兜底。

## 热生效机制

“处理设置 → 强加 GPU 锁”默认关闭。保存后影响后续任务启动、重试和素材删除，Web 与 CLI 共用 `enforce_gpu_lock` 覆盖层。开启恢复全局处理互斥；关闭允许不同素材、不同类型并行，同类重复启动与正在处理素材的删除保护仍保留。运行中的任务继续持有启动时取得的锁，改为关闭不会强制释放正在使用的独占锁。

- CLI：`mindex` 每条命令在 `load_config` 后应用覆盖层（`runtime_settings.apply_runtime_overrides`）；`mindex e2e`、`mindex clips`、`mindex backup` 除外（验证框架保持 yaml 原值可复现；切片迁移与备份按 YAML 原值运行，备份另行携带数据库覆盖层）。
- Web：`create_app` 持有 `RuntimeConfig`，检索/状态路由每请求解析 `current()`；创作任务在 launch 时解析一次（一次任务运行绑定同一配置，改配置下次生效，studio 的 reconcile 指纹机制会级联作废受影响步骤）。
- 覆盖层无长驻缓存：每次打开页面看到的即是当前生效版本。

## 嵌入切换与索引（重要约束）

切换嵌入档案后，新身份指纹 ≠ 已激活索引的 `spec.space`，检索会返回 503「向量空间不同」。设置页顶部横幅会持续显示匹配状态；恢复路径：

1. 用新配置重建并发布：`mindex index --activate`；
2. 或切回原嵌入档案（旧索引立即恢复可用）。

回退详见 [index-versions.md](index-versions.md)。

## 音色库与声音克隆

- 音色分**预定义**（厂商/本地服务自带，直接录入 voice_id）与**克隆**（上传样本生成）。
- **音色目录**（`GET /api/settings/voices/catalog`，`settings/voices_catalog.py`）：按当前语音合成后端聚合可选音色——本地 Qwen3-TTS 为静态 CustomVoice 九音色表 + qwentts `GET /audio/voices` 动态合并；MiniMax 经 `POST /get_voice`（voice_type=all）实时查询全量系统/复刻/生成音色，按 voice_id 前缀分组。设置页默认音色下拉与创作页「方案参数」的音色下拉均由此驱动，支持 voice_id 字面量设默认（`PUT business` 的 `default_voice` 字段，目录/音色库内校验）。
- **本地 Qwen3-TTS 克隆**（qwentts.cpp tts-server）：上传样本 + 可选参考文本 → `POST /audio/voices` 注册，试听音频回存；注册表存于服务进程内存，**服务重启后丢失**——mindex 持久化样本于 `data/voices/`（ref_text 存 `voices.notes`），目录加载时自动重注册自愈；任务执行途中若服务重启会报 unknown voice，重开设置页/方案参数即恢复。
- **MiniMax**：页面直接上传样本（wav/mp3/m4a，10 秒~5 分钟）→ `/files/upload` + `/voice_clone` → 自定义 voice_id 即可用于 t2a_v2；样本与预览音频存 `data/voices/`。有资料称快速复刻音色 7 天未使用可能被回收，页面有提示。
- **阿里 DashScope**：复刻 API 要求**公网可访问的样本 URL**（不支持本地直传），页面提供「URL 复刻」模式；也可在百炼控制台复刻后用「录入已有」登记 voice_id。复刻音色与合成模型绑定。
- 克隆必须勾选授权确认。默认音色（覆盖 `studio.tts.voice`）须与当前语音合成后端厂商匹配，页面强制校验；进行中任务按创建时快照继续；创作页方案参数可改音色（改后重跑 tts 列即级联作废下游）。

## 检索调参

新版三渠道权重 `search.channel_weights`（画面/对白/分析，默认 0.35:0.15:0.5），旧版五路召回权重 `search.weights`（画面/动作场景/情绪镜头/对白/词法 BM25）、RRF k、候选数；保存为 `override.search`，每请求生效，与索引无关。

## 索引版本

设置页最底部的“索引版本”（原人物库页的“发布与回退”）管理**检索组合**：内容索引 + 人物快照。折叠状态显示当前检索发布版 ID；展开后包含：

- 当前生效：检索发布版、内容索引、人物快照 ID（正文取前 8 位，悬停显示完整值）。内容索引没有匹配的发布版时发布版显示“未绑定”。
- 最近 30 条检索组合（新到旧），每行同样显示三个 ID；非生效组合可“恢复此组合”，生效组合不可恢复，也不能直接删除。
- “发布当前检索组合”：冻结已接受证据并绑定当前内容索引。进度与结果显示在本区域内，不占用页面顶部提示；恢复、发布、删除后会同步刷新顶部嵌入与索引匹配横幅。

该区域读取的“当前生效”与创作召回签名（`index_id`、`person_release`）同源，用于排查召回节点出现“选用版本兼容性”提示的原因。本区域不构建或删除内容索引本身，构建见 [索引版本与空间身份](index-versions.md)，清理旧索引见 [清理](cleanup.md)。

## 本机服务

设置页最底部的“本机服务”启停配置文件 `local_services` 中定义的本机模型服务（桌面版缺省开启，Web 版需要在配置文件中设置 `local_services.enabled: true`）。每个服务显示端口、用途、pid、运行状态（运行中 / 已停止 / 外部运行）与健康（请求 `http://127.0.0.1:<端口><health_path>`，2xx 为健康），提供启动、停止、日志尾部，以及“随后端自动启动”“退出时停止”两个开关。有服务正在启动（进程在、健康检查未通过）时每 3 秒刷新。

- 只能启停配置文件里定义的服务；接口不接受命令、工作目录或环境变量，因此不会成为远程执行任意命令的入口。功能未开启时全部操作返回 403。
- 服务以独立进程组启动，日志写入 `data_dir/logs/services/<name>.log`，pid 与进程启动时刻记在 `data_dir/locks/services/<name>.json`；后端重启后据此重新接管仍在运行的服务。停止时向整个进程组发 SIGTERM，8 秒后 SIGKILL。
- 端口上已有不是由本应用启动的服务时显示“外部运行”，不能在这里启动或停止它。
- 两个开关保存在数据目录 settings 表的 `local_services.flags`（不属于业务覆盖，“恢复默认”不会清除），没有保存时取配置文件的缺省值。`mindex serve` 启动时在后台拉起“随后端自动启动”的服务，退出时停止“退出时停止”的服务；桌面版退出前还会主动调用 `stop-on-exit`。
- 智能助手工具：`list_local_services`（只读）、`start_local_service`、`stop_local_service`（需确认）。

同一区块下的“AI 动画录制组件”检查 Playwright 的 Chromium headless shell 是否已安装（目录取 `PLAYWRIGHT_BROWSERS_PATH`，桌面版为用户数据目录的 `browsers/`），未安装时可一键安装（后台运行 `python -m playwright install --only-shell chromium`，显示进度与结果）。创作页 AI 动画阶段的详情提供“管理 AI 动画录制组件”入口（`/settings#browser`）。

## 密钥安全

- API Key 明文存 `metadata.sqlite3` 的 `model_profiles.api_key`（本机单用户工具的务实选择）。
- 对外只输出掩码（尾 4 位）；`Service.api_key` 带 `exclude=True`，指纹（`analysis_key`/`studio_key` 等）、任务快照、任何 `model_dump()` 都不会携带。
- 审计日志只记录 URL 与请求体（截断 8KB），不记录 headers。
- 测试注入的适配器（fixture）优先于覆盖层，测试不受影响。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | 生效视图：十二个角色档案+掩码+生效值、预设目录、嵌入/索引匹配、音色库、调参当前值 |
| POST/PUT/DELETE | `/api/settings/profiles[/{id}]` | 档案 CRUD（api_key：留空保留，`__clear__` 清除） |
| POST | `/api/settings/profiles/{id}/activate` | 激活；嵌入角色附索引不一致警告 |
| POST | `/api/settings/profiles/{id}/test` | 测试连接（最小真实调用，回报时延/维度/回复） |
| POST | `/api/settings/profiles/{id}/deployment/probe` | llama_cpp 嵌入/ASR/多模态理解档案的 e2e probe：只读探测，自动定位本机 GGUF/mmproj，列出可复用的一致清单 |
| POST | `/api/settings/profiles/{id}/deployment/pin` | e2e pin：复用一致清单或生成新清单（`data/deployments/`，不覆盖），写入档案 `deployment_manifest`（数据目录内存相对路径）；嵌入档案返回 smoke 门槛状态 |
| GET | `/api/settings/embedding-status` | 嵌入身份与活动索引是否匹配（页面横幅） |
| PUT/DELETE | `/api/settings/business` | 检索调参与默认音色 / 恢复默认 |
| GET/POST | `/api/settings/voices/catalog[/preview]` | 当前语音合成后端的音色目录 / 试听 |
| POST/DELETE | `/api/settings/voices[/{id}]` | 录入/删除音色 |
| POST | `/api/settings/voices/clone` | 克隆（multipart 上传样本 或 URL 复刻；须 consent） |
| GET | `/api/persons/releases` | 当前生效组合与最近 30 条检索组合（索引版本区域使用；恢复 / 删除 / 发布仍为 `/api/persons/releases/{id}/activate`、`DELETE /api/persons/releases/{id}`、`POST /api/persons/publish`） |
| GET | `/api/settings/local-services` | 本机服务列表与状态 `{enabled, items}`（未开启时 `items` 为空） |
| POST | `/api/settings/local-services/{name}/start` · `/stop` | 启动 / 停止（未开启 403；未知服务 404；端口被外部服务占用 409；可执行文件缺失或启动后立即退出 422） |
| GET | `/api/settings/local-services/{name}/logs?tail=200` | 服务日志尾部 |
| PATCH | `/api/settings/local-services/{name}` | 只接受 `autostart`、`stop_on_exit` |
| POST | `/api/settings/local-services/stop-on-exit` | 停止所有“退出时停止”且由本应用启动的服务（桌面版退出前调用） |
| GET / POST | `/api/settings/animation-browser` · `/install` | AI 动画录制组件的安装状态 / 后台安装（安装中再次请求 409） |
| GET | `/api/health` | 轻量就绪检查 `{version, pid, data_dir, busy, tasks}`（桌面外壳轮询；受访问令牌保护，不读素材表、不探测模型服务） |

人物配置 `person.frame_interval`（设置页“间隔帧数”）：整数且 ≥1，默认 15。每个分镜从首个有效帧开始每隔该帧数抽取，保留实际 PTS；1 表示逐帧。旧 `person.fps` 仅兼容旧配置，不叠加抽帧限制。


## 智能助手运行时

「智能助手运行时」可选内置（默认）或 DeepSeek Harness。先运行 `uv sync --extra dsh` 安装固定版本 SDK 与二进制，再在设置页选择 dsh 并保存。可用状态只检查依赖和模型配置，不产生模型费用。模型、连接地址、密钥和视觉能力继续由智能助手档案决定。

切换只影响新会话，已有会话保留所属运行时。缺少依赖会显示安装命令；已保存的 dsh 选择不会被自动替换。配置文件对应 `agent.runtime: builtin | dsh`；禁用 `runtime_overrides` 时请直接修改配置文件。详见[助手运行时](agent.md#与-deepseek-harness-的兼容设计)。
