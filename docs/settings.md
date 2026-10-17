# 设置页指南

`/settings`（顶部导航「设置」进入）把模型服务连接、音色库与检索调参集中在一个页面，**保存即热生效，无需重启**：配置档案存 SQLite（`model_profiles` / `voices` 表 + `settings` KV），Web API 与 CLI 命令统一经覆盖层合成「生效配置」。

相关模块：`runtime_settings.py`（档案/覆盖层/预设）、`settings_api.py`（API）、`voice_clone.py`（厂商克隆客户端）、`static/settings.*`（页面）。

## 五个模型角色

| 角色 | 配置段 | 说明 |
|---|---|---|
| 多模态理解 | `analyzer` | 视觉帧分析（动作/情绪/场景），需要视觉模型 |
| 文本拆分镜 | `studio.generator` | 创作文案拆分镜，纯文本任务，可选高性价比文本模型（本地默认复用理解模型） |
| 多模态嵌入 | `embedding` | 文本/图片/视频统一向量空间，**切换后需重建索引** |
| 语音识别 | `asr` | 线上模型只出纯文本，时间戳仍由本地 CPU ForcedAligner 补齐 |
| 语音合成 | `studio.tts` | 创作配音；支持预定义与克隆音色 |

每个角色可保存多个「配置档案」（名称 + 厂商 + 连接字段 + 密钥），任意时刻激活其一；未激活时直接用 `config.local.yaml` 的取值。档案只覆盖白名单连接字段（`runtime_settings.PROFILE_FIELDS`），`data_dir`、切分布局、对齐器等本地设施永不被触碰。

## 线上厂商支持（调研结论，2026-10）

| 角色 | 智谱 | DeepSeek | 阿里百炼 | 腾讯 | MiniMax |
|---|---|---|---|---|---|
| 多模态理解 | GLM-4.5V（OpenAI 兼容） | vision 模型 | Qwen-VL（compatible-mode） | 混元 vision | chat OpenAI 兼容 |
| 文本拆分镜 | GLM 文本 | **deepseek-chat（性价比首选）** | qwen 文本 | 混元文本 | M 系列 |
| 多模态嵌入 | — | — | — | **TokenHub**（WeMM 系，`/v1/embeddings/multimodal`） | — |
| ASR | — | — | qwen3-asr-flash（chat + input_audio） | —（自有签名协议） | asr-1.0（speech_to_text 专用适配器） |
| TTS | — | — | DashScope 原生（专用适配器） | —（自有协议） | t2a_v2（专用适配器） |

要点：

- **多模态嵌入的线上选择基本只有腾讯 TokenHub**（`kinfra-vl-embedding-2b/8b`，与本地 WeMM 同系）。适配器在客户端按 `video` 配置抽帧后以图片序列发送，保证预处理指纹诚实；身份纯由配置构造，进入索引空间指纹。
- **TTS 五家都没有标准 OpenAI `/audio/speech` 端点**，因此写了两个专用适配器：`minimax`（t2a_v2，hex 音频解码）与 `dashscope`（原生协议，音频 URL 二次下载或 base64）。
- **ASR 线上协议分两类**：OpenAI 兼容 chat + input_audio（阿里）与 MiniMax `speech_to_text`（multipart 表单上传，单文件 ≤500 秒/50MB，管线 30 秒切块远小于上限）；两者都只出纯文本，时间戳由本地对齐补齐。
- 理解/拆分镜的 OpenAI 兼容端点会自动省略 llama-server 专有的 `chat_template_kwargs`（档案里 `llama_template=false`）；本地服务保持默认 `true` 不变。
- 预设的模型名是「最佳已知」预填值，会随平台演进漂移——页面所有字段可改，每个档案有「测试连接」按钮（最小真实调用）兜底。

## 热生效机制

- CLI：`mindex` 每条命令在 `load_config` 后应用覆盖层（`runtime_settings.apply_runtime_overrides`）；`mindex e2e` 除外（验证框架保持 yaml 原值可复现）。
- Web：`create_app` 持有 `RuntimeConfig`，检索/状态路由每请求解析 `current()`；创作任务在 launch 时解析一次（一次任务运行绑定同一配置，改配置下次生效，studio 的 reconcile 指纹机制会级联作废受影响步骤）。
- 覆盖层无长驻缓存：每次打开页面看到的即是当前生效版本。

## 嵌入切换与索引（重要约束）

切换嵌入档案后，新身份指纹 ≠ 已激活索引的 `spec.space`，检索会返回 503「向量空间不同」。设置页顶部横幅会持续显示匹配状态；恢复路径：

1. 用新配置重建并发布：`mindex index --activate`；
2. 或切回原嵌入档案（旧索引立即恢复可用）。

回退详见 [index-versions.md](index-versions.md)。

## 音色库与声音克隆

- 音色分**预定义**（厂商/本地服务自带，直接录入 voice_id）与**克隆**（上传样本生成）。
- **音色目录**（`GET /api/settings/voices/catalog`，`voices_catalog.py`）：按当前 TTS 后端聚合可选音色——本地 Qwen3-TTS 为静态 CustomVoice 九音色表 + qwentts `GET /audio/voices` 动态合并；MiniMax 经 `POST /get_voice`（voice_type=all）实时查询全量系统/复刻/生成音色，按 voice_id 前缀分组。设置页默认音色下拉与创作页「方案参数」的音色下拉均由此驱动，支持 voice_id 字面量设默认（`PUT business` 的 `default_voice` 字段，目录/音色库内校验）。
- **本地 Qwen3-TTS 克隆**（qwentts.cpp tts-server）：上传样本 + 可选参考文本 → `POST /audio/voices` 注册，试听音频回存；注册表存于服务进程内存，**服务重启后丢失**——mindex 持久化样本于 `data/voices/`（ref_text 存 `voices.notes`），目录加载时自动重注册自愈；任务执行途中若服务重启会报 unknown voice，重开设置页/方案参数即恢复。
- **MiniMax**：页面直接上传样本（wav/mp3/m4a，10 秒~5 分钟）→ `/files/upload` + `/voice_clone` → 自定义 voice_id 即可用于 t2a_v2；样本与预览音频存 `data/voices/`。有资料称快速复刻音色 7 天未使用可能被回收，页面有提示。
- **阿里 DashScope**：复刻 API 要求**公网可访问的样本 URL**（不支持本地直传），页面提供「URL 复刻」模式；也可在百炼控制台复刻后用「录入已有」登记 voice_id。复刻音色与合成模型绑定。
- 克隆必须勾选授权确认。默认音色（覆盖 `studio.tts.voice`）须与当前 TTS 后端厂商匹配，页面强制校验；进行中任务按创建时快照继续；创作页方案参数可改音色（改后重跑 tts 列即级联作废下游）。

## 检索调参

新版三渠道权重 `search.channel_weights`（画面/对白/分析，默认 1:1:1），旧版五路召回权重 `search.weights`（画面/动作场景/情绪镜头/对白/词法 BM25）、RRF k、候选数；保存为 `override.search`，每请求生效，与索引无关。

## 密钥安全

- API Key 明文存 `metadata.sqlite3` 的 `model_profiles.api_key`（本机单用户工具的务实选择）。
- 对外只输出掩码（尾 4 位）；`Service.api_key` 带 `exclude=True`，指纹（`analysis_key`/`studio_key` 等）、任务快照、任何 `model_dump()` 都不会携带。
- 审计日志只记录 URL 与请求体（截断 8KB），不记录 headers。
- 测试注入的适配器（fixture）优先于覆盖层，测试不受影响。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/settings` | 生效视图：五角色档案+掩码+生效值、预设目录、嵌入/索引匹配、音色库、调参当前值 |
| POST/PUT/DELETE | `/api/settings/profiles[/{id}]` | 档案 CRUD（api_key：留空保留，`__clear__` 清除） |
| POST | `/api/settings/profiles/{id}/activate` | 激活；嵌入角色附索引不一致警告 |
| POST | `/api/settings/profiles/{id}/test` | 测试连接（最小真实调用，回报时延/维度/回复） |
| PUT/DELETE | `/api/settings/business` | 检索调参与默认音色 / 恢复默认 |
| POST/DELETE | `/api/settings/voices[/{id}]` | 录入/删除音色 |
| POST | `/api/settings/voices/clone` | 克隆（multipart 上传样本 或 URL 复刻；须 consent） |
