# 离线管线详解

默认流程为 `prepare` → `enrich-dialogue --no-publish` → `index --mode auto --activate`。三个渠道独立，分析可选且不强制依赖对白。对白优先字幕，MiniMax 直接保存词级时间戳，旧纯文本 ASR 仍可手动 `align`。管线文件锁与网页后台任务共用。

新版流程、指纹、缓存和任务接口见 [三渠道改造说明](recall-enhancement.md)。下方的完整分析与场景组织流程属于显式 `analyze` / `enriched` 兼容入口。

## 阶段总览

| 阶段 | 需要的模型 | 输入 | 输出 | 检查点粒度 |
|---|---|---|---|---|
| `detect` | 无（本地 CPU；transnet 则需 detector 服务） | 原视频 | `plans/SOURCE_ID.json` | 整片计划（原子写入） |
| `split` | 无 | 分镜计划 | shot/moment 的 MP4+缩略图+segments 行 | 逐片段（已存在即跳过） |
| `transcribe` | ASR 服务 | 有音轨素材 | `transcripts/` 30 秒块 + SQLite 记录 | 逐 30 秒块 |
| `align` | 无（CPU ForcedAligner，独立 venv） | 原始转写文本 | aligned-N.json + 完整对白 | 逐 30 秒块 |
| `analyze` | Qwen 服务 | 片段+对白 | annotations + scene_plans + scene 片段 | 逐片段 |
| `index` | WeMM 服务 | 快照 | chroma 向量 + representations + 索引版本 | 逐表示（复用已写入向量） |

独立渠道更新会先按模型空间、预处理配置与当前输入的缓存键匹配已有嵌入，批量复制到新的不可变检索索引，并优先完成可复用片段；只有缺失的表示才调用嵌入模型。已有缓存键不再逐片重建。可复用但尚未发布的嵌入仍需写入新索引并通过一致性检查，不能省略发布步骤；若缓存元数据对应的实际向量缺失，则补算该表示。

## 指纹键：缓存的命名空间

所有中间结果都挂在某个指纹键下，配置变了就写新键、不覆盖旧结果：

- **`layout_key`** = `fingerprint(detector 配置 + video 切分配置)`。绑定 sources 与 segments。切分参数变化 → 新 `data_dir`（不是新 layout_key 原地覆盖）。
- **`asr_key`** = `fingerprint(asr 配置 + 部署清单)`（部署清单仅 `llama_cpp` 后端附加）。绑定 transcripts 缓存目录与 SQLite 记录。
- **`analysis_key`** = `fingerprint(分析配置 + analyzer 部署 + layout_key)`。显式分析再按实际使用的对白内容版本细分检查点；无对白也可分析。

分析记录保存实际模型与对白输入版本。默认续跑会跳过已完成片段，切换模型或更新对白不会自动覆盖；需要按新输入重做时显式执行 `mindex analyze --source SOURCE_ID --force`。

## detect：先保存计划，再谈编码

1. ffprobe 检查视频轨/时长/帧率/尺寸/音轨，损坏文件明确失败；计算内容 SHA256 作为素材 ID。
2. 已存在的素材若 `layout_key` 不符 → 拒绝（提示用新 `data_dir`）。
3. 原片复制到 `sources/<SHA256><后缀>`（`.partial` 原子改名），重复导入不重复生成素材。
4. 分镜检测（整片扫描）→ **完整切点计划原子写入 `plans/<ID>.json`** → 之后编码失败或中断都不需要重新检测。检测计划已存在时直接读计划。
5. 检测结果校验：必须从 0 连续覆盖到片尾，区间非空。

默认 AdaptiveDetector（PySceneDetect）逐帧检测、`min_scene_len=1` 帧、映射真实帧时间戳（`frame_timestamps` 处理可变帧率）。它是可运行的启动配置，**不是 TransNetV2 已验证的替代品**，正式素材两者都需人工抽检。TransNetV2 远程服务返回转场区间中点，客户端合并相距 ≤2 帧的重复边界。

## split：切片规则

对每个镜头（shot）：

- **moment 粒度**：镜头 ≤6 秒（`video.short_shot_seconds`）→ 整镜头即一个 moment；更长镜头按 4 秒窗口、3 秒步长滑动，并补齐贴近结尾的窗口，不跨镜头、无覆盖缺口。
- 短镜头与其整镜头 moment 内容完全相同时**复用硬链接**（不支持硬链接时复制），减少重复编码与磁盘占用。
- 切片重新编码为 H.264、yuv420p、AAC MP4，开始时间归零；SQLite 保留原片 `[start,end)` 坐标。奇数边长补到偶数。每个单元有独立 MP4 与 JPEG 缩略图。
- 中断后重跑 `split`：已存在的媒体跳过（`cut` 先查输出存在）；已完成素材若个别文件缺失，`split` 也会修复。

`inspect` 报告计划镜头数、预计 moment 数、时长 min/median/p95/max、短镜头数、已落盘与缺失；`export` 导出 `manifest.json`、`shots.csv`、`segments.csv` 和浏览器可直接打开的分页检查页 `review.html`（48 单元/页，媒体用相对路径）。

## transcribe：30 秒块

- 有音轨素材按 30 秒块提取单声道 16kHz WAV 上传 ASR；每块独立保存 `transcripts/<ID>/<asr_key>/<N>.json`，失败重跑只补缺失块，并校验时间戳不越界。
- `mindex` 自定义 ASR 后端直接得到带时间戳对白，状态 `complete`。
- `llama_cpp` 与 `openai` 后端（如百炼 qwen3-asr-flash，chat + `input_audio`）都走 `transcribe_raw`：只保存原始文本与原始响应到 `.../raw/<N>.json`，状态置 **`pending_alignment`**——原始文本不伪造成对白。
- 无音轨记录 `no_audio`；显式 `asr.enabled: false` 记录 `disabled`（无对白召回，不能用于宣称对白检索达标）。
- 无音轨素材记录空内容状态；新版建库和可选分析不依赖转写阶段放行。

## align：独立 CPU 对齐（仅 llama_cpp / openai 路径）

把 `pending_alignment` 的原始文本（`llama_cpp` / `openai` 后端产生）交给 Qwen3-ForcedAligner 换取时间戳：

```bash
uv venv .venv-align --python 3.11
uv pip install --python .venv-align/bin/python 'qwen-asr>=0.0.6' 'huggingface-hub>=0.36' 'torch>=2.8'
uv run mindex align .venv-align/bin/python            # 解释器是位置参数
uv run mindex align .venv-align/bin/python --source SOURCE_ID   # 只处理一个素材
```

- `asr.aligner_revision` 必须固定为实际权重 commit（默认 `main` 会被拒绝）。
- worker 以独立子进程运行（`python -I`），强制 CPU/float32，空文本不制造时间戳；零时长 token 按相邻边界合并、保留全部文本。
- 每块对齐结果存 `aligned-N.json`（可续跑），完成后回写 SQLite：utterances + `status: complete`。
- 对齐环境绝不装入应用环境——这是核心不变量之一。

## analyze：逐片段描述 + 场景组织

对每个 shot 与 moment（不含 scene）调用 Qwen：

- 输入 = 均匀采样帧（默认 12 帧，带时间戳的 image_url 数据 URL）+ 片段内对白。
- 输出结构化 Annotation：自然中文 description、动作顺序、人物、对象、场景、情绪、氛围、景别、角度、镜头运动、画面依据（visual_evidence）、对白推断（dialogue_inference）、不确定性。未知留空、不确定明确记录、禁止虚构声音与身份。
- **默认跳过已完成片段**：即使切换分析模型，也沿用同一片段或相同类型、时间范围的有效检查点，只补做未完成片段，保留原模型和输入信息。覆盖当前基础片段的旧场景计划也可续用。
- CLI 只有显式指定 `--force` 才重新分析片段与场景、重新组织场景分组；UI 的重新生成始终按续跑处理，不提供强制重做入口。已发布索引保持不变，需另行发布才能更新。
- 分析完成后用连续 moment 描述组织**语义场景**：分组必须按顺序完整覆盖、不重不漏；每组目标 10–30 秒、**强制 ≤45 秒**（超时由客户端分批约束 + 服务端校验）。场景生成真实 MP4/缩略图并单独分析。
- 首版没有动作起止定位网络；采样帧不一定捕获每个短暂事件。

`e2e phase analyze-smoke` 只分析 3 个抽样片段，用于批量前的输出核对。

## index：两种模式

- `--mode visual`：不要求转写与分析，默认只索引 moment 的**原生视频向量**；未分析字段留空，不嵌入占位说明。可选 `--source` 与 `--kind shot/moment`（不支持 scene）。
- `--mode enriched`（显式兼容模式）：要求全部素材完成 transcribe+analyze；索引视频、动作场景描述、情绪镜头描述、对白四类表示（空文本不生成向量），可选粒度过滤。

长镜头与场景在嵌入时再拆成 ≤6 秒窗口。构建/检查/发布机制见 [index-versions.md](index-versions.md)。

## 单 GPU 执行顺序（互斥）

```
prepare（本地 CPU，无需模型）
→ 启动 ASR :4020        → transcribe → 停止
→ (llama_cpp / openai 路径) CPU align，无 GPU 占用
→ 启动 Qwen :4040/v1    → analyze → 停止
→ 启动 WeMM :4010       → index --activate → 保持常驻供在线查询
```

项目模型服务（`mindex-model-server` 三个角色）之间用 GPU 文件锁互斥（`~/.cache/mindex/gpu.lock`）；已有 llama-server/Qwen 服务不受该锁管理，由用户手动切换。详见 [deployment.md](deployment.md)。
