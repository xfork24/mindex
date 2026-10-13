# 创作工作流（文本生成视频）

从一段中文文案生成带配音、字幕的成片：拆分镜 → 语义召回 → 配音 → 选片对齐 → 渲染合成。入口 `/studio`，与视频检索（`/`）、素材管理（`/admin`）平级。所有状态落盘在 `data/studio/`，不新建 SQLite 表。

## 五步工作流

```
split 拆分镜 → recall 语义召回 → tts 生成配音 → select 选片对齐 → render 渲染合成
      ↓             ↓                ↓              ↓                 ↓
 storyboard.json candidates/*.json  audio/*.wav    timeline.json    render/final.mp4 + final.srt
```

步骤依赖：`recall`、`tts` 只依赖 `split`（两者可任意先后，适配单 GPU 分阶段部署）；`select` 需要 `recall + tts` 都完成（选片必须知道配音精确时长）；`render` 依赖 `select`。

| 步骤 | 调用服务 | 产物 | 检查点粒度 |
|---|---|---|---|
| split | 拆分镜 LLM（`studio.generator`，OpenAI 兼容端点） | storyboard.json | 单次调用，重跑即新 attempt |
| recall | 嵌入服务（经 SearchEngine，继承空间身份校验与 hidden 过滤） | candidates/&lt;shot&gt;.json | 逐镜：查询与索引未变即跳过 |
| tts | TTS 服务（`studio.tts`，/audio/speech 风格） | audio/&lt;shot&gt;.wav + audio.json | 逐镜：wav 存在且可探测时长即跳过 |
| select | 无（纯算法） | timeline.json（EDL） | 整步 |
| render | 无（仅本机 ffmpeg） | render/final.mp4 + final.srt | 逐镜 clip 与音频；concat/合成幂等 |

## 任务目录与状态机

```
data/studio/
  studio.lock                 # 全局 FileLock(timeout=0)，同一时刻只允许一个步骤执行
  <job_id>/                   # job_id = YYYYMMDD-HHMMSS-hex6
    job.json                  # 状态机：5 步 status + attempt + history + 配置快照
    attempts/<step>-<attempt>.json
    storyboard.json           # 可人工编辑（PATCH 后下游级联作废）
    candidates/<shot_id>.json
    audio/<shot_id>.wav
    audio.json                # {voice, shots: {shot_id: {file, duration, bytes}}}
    timeline.json             # 可人工换选（PATCH 后仅作废渲染）
    render/{clips,audio,orig}/… video.mp4 audio_track.wav final.mp4 final.srt
```

状态机沿用 [e2e run.json 模式](e2e-framework.md)：`pending / running / complete / failed`，每次执行留档 attempt（含错误与耗时）；重跑某一步只作废**依赖它的下游步骤**（依赖闭包，不是列表顺序）；服务重启后 running 残留状态由恢复扫描标为 failed。

## 选片与时长对齐（select）

选片结果不是"片段文件"，而是**原片上的时间区间**（shot 连续铺满原片，区间可跨多个连续 shot，也可落在 shot 中间）。时长对齐阶梯：

1. 候选池内多目标打分：语义分数（1/rank）+ 时长匹配 − 延长占比惩罚 − 来源重复惩罚 − 同源区间重叠惩罚（权重 `studio.selection.weights` 可配）
2. 同源相邻 shot 延长（向后优先，再向前；`gap_epsilon` 防御缝隙，clamp 到原片范围）
3. 音频 atempo 钳制（默认 ±10%）吸收残差
4. 仍不能对齐 → 视频冻结末帧（tpad）或裁剪兜底，事件带 warning 但不失败

目标时长一律用 TTS 实际时长；分镜的 `estimated_seconds` 仅作 UI 参照。空候选的分镜会让 select 失败并提示"修改查询后重跑召回，或编辑分镜"。

## 渲染（render）

- 逐镜统一切割：统一画布（默认 1280×720）/fps/SAR 后 concat，解决多源参数不一致；区间直接从**原片**精确切割
- 音轨：tts_only（默认，丢弃原声）= atempo → loudnorm → 48k；ducking = 原片区间原声压低（`duck_gain`）后 amix（无音轨素材自动退化为纯配音）
- 字幕：默认烧录（subtitles 滤镜，需 ffmpeg 带 libass；**缺失时报中文错误，可改软字幕**）+ 始终导出 SRT；`burn: false` 走 mov_text 软字幕轨
- 渲染前 1 帧字体烟测，尽早失败

## 人工介入

- **编辑分镜**（split 完成后）：改文本/查询、增删合并移动分镜；保存后 recall/tts/select/render 级联作废
- **换选**（select 完成后）：每镜从候选池换选或指定显式区间，服务端同一公式重算 atempo 与警告；保存后仅作废渲染
- 编辑与步骤执行互斥（studio.lock，冲突 409）

## API（/api/studio）

| 方法 路径 | 语义 |
|---|---|
| POST /jobs | 创建任务（text 1..max_input_chars + options） |
| GET /jobs / GET /jobs/{id} | 列表 / 详情轮询（分镜+候选+配音+选片+成片一次带全） |
| POST /jobs/{id}/steps/{step} / POST /jobs/{id}/run | 单步执行 / 全自动连跑（202；409 忙；422 依赖未完成） |
| PATCH /jobs/{id}/storyboard / PATCH /jobs/{id}/timeline | 编辑分镜（级联作废）/ 换选（仅作废渲染） |
| GET /jobs/{id}/artifacts/{path}?download=true | 任意产物下载（防路径穿越） |
| GET /jobs/{id}/audio/{shot_id} | 逐镜配音试听（支持 Range） |
| DELETE /jobs/{id} | 删除任务 |

候选画面预览复用 `/api/media/{segment_id}`；错误 envelope 沿用全局 `{"detail": …}`。

## 配置要点（studio: 节）

- `generator`：拆分镜 LLM（默认 qwen3.8:27b，:4040/v1），`split_retries` 次校验失败重试（附错误反馈），`finish_reason=length` 直接失败
- `tts`：`speech_path` 可配（默认 /audio/speech），响应按魔数/JSON-base64 自适应解码
- `recall.granularity`：`shot`（需索引含镜头粒度）或 `moment`（纯 visual 索引）
- `subtitle.burn` / `audio.mode` 可被任务级 options 覆盖
- 任务创建时快照 `studio_key` 指纹（模型+选片+音频+字幕+渲染配置）与当时活动索引，仅供追溯

## 测试与验收

- `tests/test_studio_*.py`：状态机/拆分镜/选片纯算法/配音检查点/真实 ffmpeg 渲染/API 全端点，模型走 conftest 确定性 Fixture（不依赖真实服务）
- `uv run python tests/serve_fixture.py --data-dir <目录>` 可起本地 fixture 服务做浏览器人工验收
- 真实模型验收：按单 GPU 分阶段部署逐服务验证（TTS 端点格式 → 拆分镜质量 → 召回）
