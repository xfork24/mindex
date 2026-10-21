# 「文本生成视频」创作工作流（studio 子系统）实施计划

> 本文保留初始设计记录。静态审查后的改进计划见 [STUDIO_REFINEMENT_PLAN.md](STUDIO_REFINEMENT_PLAN.md)，当前运行行为与 API 以 [docs/studio.md](../docs/studio.md) 为准。

## Context

mindex 目前只能"找片段"，用户希望新增**文字生成视频**能力：输入一段中文文本，系统自动拆分镜 → TTS 配音 → 从已索引素材库召回视频片段 → 音视频字幕三轨合成一条可播放视频。这是全新子系统（架构级），但必须**完全复用现有项目的技术方案与页面风格**（用户明确要求）：零新增依赖、原生 HTML/JS、沿用 adapters/router/状态机/原子写惯例。

**用户已确认的决策**：
- 原声处理：可选（tts_only / ducking），**默认丢弃原声只留 TTS**
- 字幕：可选（烧录/软字幕），**默认烧录 + 始终导出 SRT**
- 人工介入：**关键步骤可人工审核**（分镜编辑、选片换选），也支持全自动连跑
- 模型服务：qwen3:27b 与 qwen3-tts 均为 **llama-server GGUF + OpenAI 兼容端点**
- 页面形态：**独立页面 /studio**（同 /admin 模式，与素材管理、视频检索同级）

**流程改进（相对用户原始 4 步方案）**：拆成 5 步并把"召回"与"选择"分离——`split → recall → tts → select → render`。理由：只有 TTS 完成后才知道精确音频时长，选择必须在 tts 之后；recall 只依赖语义可提前跑。单 GPU 分阶段互斥下每步独立执行/重试。

## 核心架构

### 工作流状态机（沿用 diagnostics/e2e.py run.json 模式）

- 任务 = `data/studio/<job_id>/` 目录，`job.json` 记录 5 步状态（pending/running/complete/failed）+ 尝试历史 + 配置快照
- 每次执行留档 `attempts/<step>-<attempt>.json`（同 diagnostics/e2e.py:498）；原子写 `.partial` + `replace()`（diagnostics/e2e.py:64-68）
- 全局 `data/studio/studio.lock` FileLock(timeout=0)（同 processing/pipeline.py:44-45），同时只允许一个步骤执行，冲突返回 409
- 步骤依赖：split 无依赖；recall、tts 依赖 split；select 依赖 recall+tts；render 依赖 select
- **不新建 SQLite 表**；可选 `Store.run()`（shared/storage.py:105-122）记录运行历史
- 后台执行：endpoint 同步抢锁 → 标 running → `threading.Thread(daemon=True)` 执行 → finally 释放并落盘终态；启动恢复扫描（锁空闲但状态 running → 标 failed"服务重启导致步骤中断"）

### 任务目录布局

```
data/studio/<job_id>/
  job.json                          # 状态机 + 输入 + 配置快照
  attempts/<step>-<attempt>.json
  storyboard.json                   # split 产物（可人工编辑）
  candidates/<shot_id>.json         # recall 产物（逐镜检查点）
  audio/<shot_id>.wav               # tts 产物（逐镜检查点）
  audio.json                        # 时长清单
  timeline.json                     # select 产物（EDL，可人工编辑）
  render/
    clips/<shot_id>.mp4             # 逐镜统一切割（检查点）
    audio/<shot_id>.wav             # 逐镜处理后音频（检查点）
    video.mp4 / audio_track.wav     # concat 中间产物
    final.mp4 / final.srt
```

## 文件级改动

### 新增后端模块（backend/src/ 平铺，不建子包）

| 文件 | 职责 | 行数估 |
|---|---|---|
| `studio/studio.py` | pydantic 数据模型（Storyboard/Shot/Timeline/TimelineEvent）+ job.json 状态机 + 锁 + invalidation 级联 + 恢复扫描 | ~380 |
| `studio/studio_steps.py` | 五步编排 `run_split/run_recall/run_tts/run_select/run_render` + 统一 `execute_step` 包装（attempt/状态迁移/异常记录） | ~300 |
| `studio/studio_select.py` | 选片与时长对齐**纯算法**（候选打分、相邻延长、atempo 钳制、惩罚项），无 I/O 副作用便于单测 | ~180 |
| `studio/studio_render.py` | ffmpeg 命令构造（切割/concat/音频/字幕烧录）、SRT 生成、CJK 字体预检 | ~230 |
| `studio/studio_api.py` | `studio_router(config, store)` 全部端点 + 后台线程执行器 | ~340 |

### 新增前端（frontend/src/studio/，复用 style.css 骨架）

| 文件 | 职责 |
|---|---|
| `studio.html` | 页面骨架：header（brand + "创作" + `/`、`/admin` 互链）+ intro + notice + 左栏（创建表单 + 任务列表）+ 右栏（步骤条/分镜编辑器/候选区/选片区/成片区）+ 原生 `<dialog>`（复用 admin.html:36 播放器模式） |
| `studio.js` | 基础层（`$`/`element()`/`api()`/请求计数器防竞态，照 admin.js:3-5）+ 状态层 + **轮询层**（running 时 setInterval 1500ms 调 GET job，全空闲即停——全新模式）+ 渲染层 + 编辑层；`?v=` cache-busting |
| `studio.css` | 页面专属样式，沿用 style.css 的 CSS 变量，压缩单行风格照 admin.css |

### 修改既有文件

| 文件 | 改动 |
|---|---|
| `models/adapters.py` | ① 新增 `request_bytes()`（同 request() 重试语义但返回二进制，models/adapters.py:26-49 不适用 /audio/speech 二进制响应）；② `Speaker` Protocol + `OpenAISpeaker`（POST `{base_url}{speech_path}`，body `{model, input, voice, response_format:"wav"}`，按 Content-Type 自适应二进制/JSON-base64）+ `make_speaker()`；③ `Storyboarder` Protocol + `QwenStoryboarder`（**照抄 QwenAnalyzer._chat 模式** models/adapters.py:177-247：httpx 手写请求 + `chat_template_kwargs enable_thinking False` + schema 内嵌 prompt + parse_json + pydantic 校验 + 失败附错误重试）+ `make_storyboarder()` |
| `shared/config.py` | **删除占位 TTSConfig（63-67 行，无代码引用）**；新增 `StudioConfig` 树挂到 `Config.studio`；注意 config.local.yaml 中旧 `tts:` 节需删除（strict forbid 会校验失败） |
| `shared/media.py` | 新增 `audio_info(path)`（ffprobe `-select_streams a:0`，probe() 要求视频轨不能用于 wav） |
| `api.py` | `include_router(studio_router(config, store))`（49 行旁）；`GET /studio` FileResponse（照 55-58 行 /admin 模式）；`create_app` 增加可选 `storyboarder`/`speaker` 注入参数（同 `embedder` 模式，供测试） |
| `config.example.yaml` | 删 `tts:` 节（33-36 行），加 `studio:` 节 |
| `index.html` / `admin.html` | header 加 `/studio` 入口链接（照现有 nav-link 惯例） |
| `CLAUDE.md` / `docs/` | 阶段 5 补架构章节与 studio 参考文档 |
| `cli.py`（可选 PR6） | `mindex studio create/run-step/list` 薄封装，延迟导入 |

## 关键数据格式

**storyboard.json**（split 产物，shot id 由代码分配不信任 LLM 编号）：
```json
{"schema": 1, "coverage_warning": null, "shots": [
  {"id": "shot-01", "text": "原文分镜文本", "query": "面向检索优化的查询", "estimated_seconds": 8.5}]}
```

**timeline.json**（select 产物，EDL）：
```json
{"schema": 1, "mode": "auto|manual", "total_seconds": 63.4, "warnings": [], "events": [
  {"shot_id": "shot-01", "order": 1,
   "audio": {"file": "audio/shot-01.wav", "duration": 8.42, "atempo": 1.026, "render_duration": 8.21},
   "video": {"source_id": "…", "segment_id": "锚点", "segment_ids": ["seg-a","seg-b"],
             "start": 12.3, "end": 20.51, "alignment": "nearest|extended|atempo|extended+atempo|padded|trimmed"},
   "subtitle": {"start": 0.0, "end": 8.21, "text": "分镜文本"}}]}
```

## 各步骤实现要点

### split（拆分镜）
- System prompt 要点：目的（配 TTS 与字幕的检索混剪）；**每镜 20~70 字（5~15 秒）时长感知**；text 必须取自原文；query 风格对齐库内 Qwen 分析描述（人物/动作/场景/情绪/景别关键词）
- 校验：shots 非空、text ≤200 字、estimated_seconds ∈ (0,60]、max_shots=60 防御；`finish_reason=="length"` → 显式失败"文本过长导致输出截断"
- 重试 ≤2 次，重试时附上次错误供自我修正；软校验 coverage_warning（拼接重叠度低只警告不失败）

### recall（召回）
- 步骤开始解析一次活动索引 id，此后固定 `SearchEngine(config, index_id=该id)` 避免中途换索引
- 逐镜 `engine.text(TextSearch(text=query, granularity="shot", limit=20))`——身份校验/hidden 过滤全部由 SearchEngine 继承（search/search.py:112-150）
- 检查点：`candidates/<shot_id>.json` 存在且 query/index_id 匹配则跳过
- 空候选镜写 `status:"empty"`，步骤 complete 但显式列出空镜；select 遇空镜失败并报"分镜 shot-03 无召回候选，请修改查询后重跑召回，或编辑分镜"

### tts（配音）
- 逐镜：wav 存在且 `audio_info()` 可读 → 跳过；否则 `speaker.speak()` → 原子写 → 探测精确时长；`audio.json` 全量重建清单；孤儿清理（人工删镜后残留）
- job.options.voice 可任务级覆盖

### select（选片对齐）——核心算法
- **选片结果 = 原片时间区间**（shot 连续铺满原片，区间可跨多个连续 shot，渲染时从原片一次切割，天然无缝）
- 时长对齐阶梯：候选池内按时长匹配打分 → 相邻 shot 延长（`segments_timeline` 索引查 prev/next，向后优先再向前，clamp 到 [0, source_duration]，gap_epsilon=0.5 防御缝隙）→ 音频 atempo 钳制 ±10% → 残差 tpad 冻结帧/裁剪兜底（保证 render 永不因对齐失败，产 warning）
- 多目标打分：`semantic(1/rank) + duration_fit − extension占比惩罚 − 来源重复惩罚 − 同源区间重叠惩罚`，权重可配置
- 目标时长一律用 TTS 实际时长（estimated_seconds 仅 UI 参照）
- 人工换选：限该镜候选池内选 segment_id 或显式区间，服务端同公式重算 atempo/warning

### render（渲染）
- 前置字体预检：subtitles filter 渲 1 帧黑图，失败报"缺少可用的 CJK 字体"（font_file 可显式配置）
- 阶段 A 逐镜切割（检查点）：`-ss {start} -i {source} -t {dur}` + `setpts,fps,scale=1280:720:force_original_aspect_ratio=decrease,pad,setsar=1[,tpad]` + libx264 crf20 + aac 48k 立体声（统一画布解决多源不一致；timeout=max(600,dur*10) 同 media.cut）
- 阶段 B 视频轨：concat demuxer `-c copy`（参数一致）
- 阶段 C 音频轨：tts_only = `atempo→loudnorm→48k`；ducking = 原片区间原声 loudnorm 后 `volume={duck_gain}` + `amix duration=first`；逐镜检查点后 concat
- 阶段 D 合成：Python 累计偏移生成 final.srt（始终导出）；默认烧录 `subtitles=…:force_style='FontName=…,FontSize=…'`；`burn:false` 时改 `-c:s mov_text` 软字幕

## invalidation 级联

| 触发 | 效果 |
|---|---|
| PATCH storyboard | recall/tts/select/render → pending，history 记原因，旧产物文件保留 |
| PATCH timeline | 仅 render → pending，timeline 标 mode:"manual" |
| 重跑某步 | 该步及全部下游 → pending（先前状态归档进 history，同 e2e run_phase:370-372） |
| input_text | 创建后不可变（改文案走 storyboard 编辑） |
| running 中 PATCH/run-step | 锁冲突 → 409"已有创作步骤在执行" |

## API 端点（studio_router，prefix=/api/studio）

| 方法 路径 | 语义 |
|---|---|
| POST /jobs | 创建（text 1..2000 字 + options），201 |
| GET /jobs | 任务列表 `{items:[…]}`（照 admin envelope） |
| GET /jobs/{id} | **轮询端点**：完整状态 + 分镜/候选/选片/成片摘要一次带全 |
| POST /jobs/{id}/steps/{step} | 单步执行：202；409 忙；422 依赖未完成 |
| POST /jobs/{id}/run | 全自动连跑（遇失败停），202 |
| PATCH /jobs/{id}/storyboard | 编辑分镜（增删改合并前端组装数组），级联作废 |
| PATCH /jobs/{id}/timeline | 换选/微调（须覆盖全部 shot 恰好一次），仅作废 render |
| GET /jobs/{id}/artifacts/{path}?download=true | 产物下载；resolve 后 `is_relative_to(job_dir)` 防穿越（照 admin/admin.py:54-67 download 模式） |
| GET /jobs/{id}/audio/{shot_id} | 试听 wav（FileResponse 支持 Range） |
| DELETE /jobs/{id} | 删除任务（运行中 409） |

候选视频预览**不设新端点**：candidates 带 `media_url=/api/media/{segment_id}` 直接复用现有端点（api.py:135-141）。错误 envelope 沿用 `{"detail":…}`，异常处理器复用 api.py:51-66。

## 配置节（shared/config.py StudioConfig，extra=forbid）

```yaml
studio:
  max_input_chars: 2000
  max_shots: 60
  generator: {backend: openai, base_url: "http://192.168.1.86:4040/v1", model: "qwen3.8:27b",
              timeout: 300, api_key_env: MINDEX_QWEN_API_KEY, max_tokens: 4000, split_retries: 2}
  tts:       {backend: openai, base_url: "http://192.168.1.86:4030/v1", model: "Qwen3-TTS",
              voice: Cherry, response_format: wav, speech_path: /audio/speech,
              timeout: 300, api_key_env: MINDEX_QWEN_API_KEY}
  recall:    {granularity: shot, limit: 20}
  selection: {atempo_limit: 0.1, gap_epsilon: 0.5,
              weights: {semantic: 1.0, duration: 1.0, extension: 0.3, repeat: 0.5, overlap: 1.0}}
  audio:     {mode: tts_only, duck_gain: 0.15, loudnorm: "I=-16:TP=-1.5:LRA=11"}
  subtitle:  {burn: true, font_family: "Noto Sans CJK SC", font_file: null, font_size: 7}
  render:    {width: 1280, height: 720, fps: 25, crf: 20, preset: fast}
```

generator/tts 继承 Service 基类（shared/config.py:22-31）；校验：宽高正偶数、atempo_limit ∈ (0,0.25]、weights 非负。job.json 快照含 generator/tts 指纹（`fingerprint(model_dump(exclude={timeout,api_key_env}))`）+ 创建时 index_id（仅供追溯）。

## 测试计划（原则：真实 ffmpeg/SQLite/Chroma + conftest 确定性 Fixture）

conftest 扩展：`FixtureSpeaker`（stdlib wave 合成精确时长正弦波，时长 = 0.3s + 0.12s×字数）、`FixtureStoryboarder`（按句号确定性切分）+ `FlakyStoryboarder`（首次坏 JSON 验证重试）。

| 文件 | 覆盖 |
|---|---|
| test_studio_state.py | 状态迁移、attempts 留档、invalidation 级联、锁互斥 409、重启恢复 |
| test_studio_split.py | schema 校验、重试、超长 422、max_shots 防御 |
| test_studio_select.py | **纯算法全边界**：最近时长/相邻延长/向前补齐/越界 clamp/atempo 钳制/padded/trimmed/空候选聚合错误/重复与重叠惩罚/人工换选重算 |
| test_studio_tts.py | 检查点跳过（Speaker 调用计数不变）、孤儿清理、audio.json 精确时长 |
| test_studio_render.py | 真实 ffmpeg：2 事件 timeline → final.mp4 probe 时长 ±0.3s、srt 条数/时间轴、检查点二次跳过；烧录 skipif 无 CJK 字体 |
| test_studio_api.py | TestClient 全端点、PATCH 级联、路径穿越 404、audio Range 206、无索引中文错误 |

library fixture 复用 test_admin.py 模式（Pipeline.prepare + IndexBuilder.build(activate=True) + FixtureEmbedder）。

## 实施顺序（6 个 PR，各自可合并）

| 阶段 | 内容 | 验收 |
|---|---|---|
| PR1 基础层 | StudioConfig（删 TTSConfig）、adapters 三件（request_bytes/Speaker/Storyboarder）、media.audio_info、studio/studio.py 状态机、conftest Fixture | pytest 全绿、ruff 无告警、strict 校验通过 |
| PR2 split+recall+API 骨架 | studio_steps（split/recall）、studio_api、api.py 挂载 | curl/docs 走通 split/recall（Fixture 注入）；断点续跑与空候选标记 |
| PR3 tts+select | tts 步骤、studio/studio_select.py、select 步骤 | 纯算法单测全过；链路产出合法 timeline.json |
| PR4 render | studio/studio_render.py、SRT、字体预检 | 真实 ffmpeg 渲染 final.mp4/srt，断言通过 |
| PR5 前端+编辑闭环 | studio.html/js/css、PATCH 两端点、入口链接、docs | 浏览器手工验收全流程（创建→连跑→编辑→换选→渲染→下载）；node --check |
| PR6 可选打磨 | CLI 薄封装、Store.run 记录、DELETE | CLI 冒烟；/api/status 可见 studio-* 运行记录 |

## 风险与缓解

1. **llama-server TTS 端点响应格式**（最高风险）：各版本行为不一 → speech_path 可配 + 按 Content-Type 自适应（audio/* 直用、JSON 尝试 base64）；PR1 做真机探测回填默认值
2. **qwen3-tts voice 集合未知**：可配 + 错误消息列出服务可用项
3. **CJK 字体**：Linux 部署机可能缺 → font_file 显式路径 + 渲前 1 帧烟测（错误指明装 fonts-noto-cjk）
4. **超长文本截断**：finish_reason 检测 + max_input_chars/max_tokens 上限 + 中文引导
5. **recall 性能**：SearchEngine 每次 text() 重建快照，20-30 镜串行重复构建——首期接受，后续可优化
6. **旧候选 media_url 404**（recall 后索引切换）：UI 显明确缺文件提示，candidates 元数据已存盘不丢
7. **config.local.yaml 迁移**：旧顶层 `tts:` 节必须删除，否则 strict 校验失败——README/错误消息需提示

## 验证方式

1. `uv run pytest -q` 全绿 + `uv run ruff check backend/src tests` 无告警 + `node --check frontend/src/studio/studio.js`
2. PR1-4 阶段用 curl / OpenAPI docs（uv run mindex serve → /docs）逐步验收：创建任务 → run-step split/recall/tts/select/render → GET job 状态 → artifacts 下载
3. PR5 浏览器手工验收：/studio 创建任务 → 全自动连跑 → 编辑分镜（看到级联作废提示）→ 重跑 → 换选候选 → 渲染 → 播放/下载 final.mp4 + final.srt
4. 真实模型验收（GPU 服务就绪时）：qwen3:27b 拆分镜 + qwen3-tts 配音走真机，确认 OpenAISpeaker 对实际响应格式的兼容性，回填默认配置
