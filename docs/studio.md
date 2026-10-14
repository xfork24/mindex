# 创作工作流（文本生成视频）

入口 `/studio`，与视频检索和素材管理平级。输入中文文案，经拆分镜、召回、配音、选片和渲染生成成片。最新修订依据为 [STUDIO_REFINEMENT_PLAN.md](../plans/STUDIO_REFINEMENT_PLAN.md)；[STUDIO_PLAN.md](../plans/STUDIO_PLAN.md) 保留最初设计，旧检查点及时间轴约定以本文为准。

## 分阶段执行

依赖为 `split → recall/tts → select → render`。recall 与 tts 可任意先后；select 需要两者完成；render 只依赖本地文件和 ffmpeg。

| 步骤 | 服务 | 产物 |
|---|---|---|
| split | 拆分镜 LLM，OpenAI 聊天接口 | storyboard.json |
| recall | 索引对应的嵌入服务 | candidates/&lt;shot&gt;.json |
| tts | TTS 服务 | audio/&lt;shot&gt;.wav、audio.json |
| select | 无 | timeline.json（原片区间及配音变速规划） |
| render | 无 | 视频、PCM 音轨、实际时间轴、MP4、SRT、WebVTT |

单 GPU 环境以单步执行为主，切换服务后再执行对应步骤。“继续执行未完成步骤”在遇到失败时停止，已完成的检查点保留，不要求同时启动全部模型。

步骤状态为 pending/running/complete/failed。任务级 `execution` 记录连跑状态、当前步骤和剩余步骤，避免步骤间空隙停止轮询。API 返回 202 前，首步已落盘为 running。全局 studio.lock 在整个执行过程中持有；编辑、删除、ZIP 导出与执行互斥，冲突返回 409。重启后查询任务会将失去执行锁的残留 running 状态标为 failed，并补齐尝试记录。

## 续跑、重新生成与编辑

- 默认 `resume`：仅复用输入指纹一致、文件哈希一致且有效的检查点。
- `force`：重新生成整步；recall、tts、render 可通过 `shot_ids` 指定分镜。其他镜头仍按 resume 规则处理，缺失或失效的产物会补齐。
- 每次执行采用当前进程加载的配置，attempt 留存实际配置。修改 YAML 后需重启服务；配置变化在下次执行前作废受影响步骤。
- 只改查询：重置 recall/select/render；只改文本：重置 tts/select/render；只换顺序：重置 select/render；增删镜：重置相关分支；仅改标题不影响媒体。
- 编辑分镜及选片时保留草稿，可取消编辑。保存选片只作废 render，未修改的事件保留现有区间。

自动拆分必须按顺序完整覆盖原文，仅忽略空白差异。遗漏、重复、乱序、改写或数量超限会触发模型修正重试；达到上限后失败。人工改稿允许与原文不同，但会展示差异提示。ID 仅允许字母、数字、下划线和连字符，长度不超过 40；所有产物读写均限制在任务目录内。

拆分提示词优先按场景及动作生成 20–60 字分镜，独立短句允许更短；查询只描述核心主体、动作及场景，避免补写拍摄风格或编排多画面。字数和语义要求是模型提示，不能当成硬性校验保证；抽象旁白及人物关系仍需人工审核。提示词版本变化会使旧自动分镜检查点失效。模型返回 finish_reason=length 时失败，诊断保留原始响应，可提高 `studio.generator.max_tokens` 或缩短文案后续跑。

## 检查点与历史

```
data/studio/<job_id>/
  job.json                       # 步骤、执行状态、创建快照、当前成片版本
  checkpoints.json               # 输入指纹、不可变路径、哈希及媒体元数据
  attempts/<step>-<attempt>.json  # 尝试状态及结果
  attempts/<step>-<attempt>/
    debug/*.json                 # 实际配置、请求响应、校验及 ffmpeg 诊断
    manifest.json                # 生成或复用的产物清单
    ...                          # 此次生成的分镜、候选、音频或渲染文件
  attempts/edit-*/...             # 人工编辑前的版本
  storyboard.json / audio.json / timeline.json
  candidates/*.json / audio/*.wav / render/*
  previews/*.mp4                 # 最终选片区间的画面预览
  exports/*.zip                  # 下载包，后续打包不会递归包含这些 ZIP
```

根目录逻辑文件保持兼容，通过原子替换引用当前输出；attempt 内保留历史版本。删镜不会删除历史音频，当前 audio.json 只列出有效分镜。失败尝试中已成功生成的单镜产物也可供下一次续跑复用。

TTS 指纹包含文本、声音及服务配置；召回包含查询、步骤开始时固定的索引、检索参数与可见性；渲染包含原片身份及文件信息、实际区间、音频内容、变速和处理参数。文件存在本身不再代表检查点有效。

旧任务仍可读取和下载；第一次执行时，无可信指纹的旧下游产物会标为未验证并要求重建。旧成片保留为历史版本。

诊断保存模型请求、原始 JSON 响应、校验反馈、ffmpeg 命令和 stderr；认证头、密钥及 URL 中的凭据不写入快照。原始素材不复制到任务目录，渲染清单记录其身份和路径。任务 ZIP 包含中间产物、历史和调试记录，不包含原始素材、临时文件及导出包本身。

## 选片与时长对齐

候选打分综合语义排名、时长匹配、延长比例、来源重复和同源区间重叠。候选为空时 select 明确失败，不生成占位成片。

视频足够长时优先裁剪，配音保持原速；不足时向后优先、再向前延长同源连续镜头，然后按 atempo 上限加速配音，最后冻结末帧补齐并给出警告。延长遇到隐藏片段停止，不跨过隐藏内容继续找邻镜；选片、预览和渲染前重新检查所选区间的可见性。

人工可换选候选，或提供 source_id/start/end 指定区间。空区间、倒置区间、片尾起点和非有限数值会被拒绝；超出原片结尾的 end 会钳制并提示。候选媒体预览复用 `/api/admin/segments/{id}/media`，不依赖活动索引。每镜还可预览延长、裁剪或冻结后的实际选片画面。

## 渲染与字幕

渲染先处理配音为 48kHz PCM，根据实际采样数向上计算输出帧数。音轨只补齐至累计帧边界，不裁掉配音；视频先裁到所选原片区间，再冻结末帧，输出确定帧数。所有事件的实际帧数、采样边界、时间范围保存在 `render/timeline.json`。

视频拼接、音轨拼接及字幕共同使用实际时间轴，避免逐镜按浮点秒累计产生漂移。每镜可有不到一帧的补齐。最终合成不以 `-shortest` 隐藏误差，发布前校验轨道、时长和完整解码。

- tts_only：配音 atempo → loudnorm → 48kHz，丢弃原声。
- ducking：原声压低后与配音混合，背景不足时补齐，关闭 amix 默认归一化；无原声素材退化为纯配音。
- burn=true：烧录字幕，需要 ffmpeg subtitles/libass；显式字体文件不存在时提前失败。滤镜烟测不等于中文字形及可读性验收，仍需部署环境视觉确认。
- burn=false：MP4 内嵌 mov_text，同时网页播放器加载 WebVTT。
- 两种字幕模式均导出 SRT 和 WebVTT。成片通过验证后更新发布版本；失败保留上一版成片。

技术拼接连续不代表叙事自动连贯。首期通过区间预览和人工换选判断人物、场景及动作衔接，不进行自动叙事重排或逐字强制对齐。

## API（/api/studio）

| 方法与路径 | 语义 |
|---|---|
| POST /jobs | 创建任务，支持 voice/audio_mode/recall_limit/burn |
| GET /jobs、GET /jobs/{id} | 列表和详情；详情包含完整候选池、执行状态及成片版本 |
| POST /jobs/{id}/steps/{step} | 可选 body：`{"mode":"resume"}` 或 `{"mode":"force","shot_ids":["shot-01"]}`；202 |
| POST /jobs/{id}/run | 继续未完成步骤，失败即停止 |
| PATCH /jobs/{id}/storyboard | 编辑分镜，按实际字段变化作废依赖 |
| PATCH /jobs/{id}/timeline | 全量覆盖当前分镜，每镜恰好一次；候选 ID 与显式区间二选一 |
| GET /jobs/{id}/preview/{shot_id} | 实际选片画面预览，支持 Range |
| GET /jobs/{id}/artifacts | 当前及历史产物列表，含大小、attempt、步骤、下载 URL |
| GET /jobs/{id}/artifacts/{path}?download=true | 单文件下载，限制在任务目录内 |
| GET /jobs/{id}/archive | 空闲时生成并下载任务 ZIP，执行中返回 409 |
| GET /jobs/{id}/audio/{shot_id} | 配音试听，支持 Range |
| DELETE /jobs/{id} | 删除整个任务及历史产物，执行中返回 409 |

指定分镜仅允许 force 模式的 recall/tts/render；空列表、重复 ID、不存在的 ID 或不适用步骤返回 422。现有无请求体调用按 resume 处理。错误沿用 `{"detail":...}`。

## 验证与真实模型接入

`tests/test_studio_*.py` 使用确定性模型 Fixture 和真实 ffmpeg/SQLite/Chroma 验证工程链路。`uv run python tests/serve_fixture.py --data-dir <测试目录>` 可启动专用浏览器验收服务；其颜色向量与正弦波配音不证明真实模型质量。

真实模型按单 GPU 条件依次部署：拆分镜 Qwen → Qwen3-TTS → 索引对应嵌入模型，最后执行本地选片和渲染。各阶段先探测实际模型 ID、端点、响应格式和声音选项。当前默认名称不是部署兼容性的证明；真实中文拆分、发音、召回质量及烧录字形仍需单独验收。

2026-10-14 已完成 Qwen 真实接口、2000 字样本完整覆盖、续跑/force 及截断恢复验证；抽象旁白查询质量尚未全部验收。详细结果及下一阶段任务见 [验收记录](studio-verification.md)。实际活动索引若只包含 moment，应将 `studio.recall.granularity` 设为 `moment`；错误粒度会得到空候选。

CustomVoice / uncle_fu 已完成真实配音、部分失败续跑、逐镜 force、改稿、音频格式与下载验证；五镜测试旁白 53.20 秒，日期数字样本 41.76 秒。发音质量与跨镜音色仍需人工试听。任务声音在创建时固化，更改全局默认不会自动改变已有任务；本次联调已保存快照并迁移测试任务，尚未增加任务声音编辑页面或 API。

WeMM 真实召回及软字幕成片也已完成验证：五镜各 20 个候选，53.20 秒成片，网页预览、人工修改区间、仅重建受影响镜头、单镜强制渲染、字幕播放及 API 下载通过；最新 Studio 回归为 76 通过、1 跳过。自动候选的人物匹配和延长区间有质量问题，素材也自带硬字幕与水印，仍需人工审核；具体证据与后续改进见验收记录和修订计划。新增动态配置功能按用户要求未纳入本轮验收。

新增 476 字潮汕侨批文案复测，使用任务选项 `voice=serena`，人工修整分镜后完成 11 镜、103.36 秒软字幕成片；真实拆分/配音/召回、网页渲染、完整解码及任务 ZIP 通过。自动选片仍出现看手机代替读信、抵达代替相拥等内容偏差。当前 OpenAI TTS 适配器未传递情感指令，不能显式控制“温柔含情”；声音自然度和情感效果仍需试听，不能将选中 Serena 等同于情绪控制验收通过。
