# 整部电影分镜、切片与向量索引

本阶段的最短流程是 **本地 CPU 分镜 → 本地 CPU 切片 → 单独启动 WeMM → 视频向量索引**。ASR、TTS、Qwen 分析均不是这个流程的前置条件。后续需要对白、情绪与镜头描述时，再分阶段丰富索引。

## 环境与配置

```bash
uv sync --extra dev
cp config.example.yaml config.local.yaml
```

本机需有 `ffmpeg` 和 `ffprobe`。已有 `config.local.yaml` 时直接修改它，不必覆盖。服务地址配置：

```yaml
embedding:
  base_url: http://192.168.1.86:4010
  model: tencent/WeMM-Embedding-9B
  quantization: int8
  dimensions: 4096
asr:
  base_url: http://192.168.1.86:4020
```

量化配置必须与部署实际一致；若服务为 NF4，需要改为 `nf4`。上述为配置片段，完整文件以 `config.example.yaml` 为准。TTS 的 `:4030` 本阶段无需配置或启动。

客户端使用 [服务协议](deployment.md#服务协议) 的 `/health`、`/embed/video`、`/embed/text`。仅设置 IP 和端口不会自动兼容任意 vLLM 或 OpenAI 接口；现有服务若不同，需要提供对应的适配层。媒体通过 HTTP 上传，GPU 机器无需挂载本机电影目录。

为一部完整电影准备足够磁盘空间：原片会复制到 `data/sources/`，还会输出镜头、重叠短片段与缩略图。重新编码的体积依赖分辨率、内容和原片编码，不能只按原文件大小固定估算。输入保留在原位置，后续可从受管理副本续跑。

## 1. 扫描电影，只保存切点

```bash
uv run mindex detect '/绝对路径/电影.mkv'
```

最后一行输出完整的 `SOURCE_ID`，即电影内容 SHA256。复制这个 ID 用于后续命令。文件名可包含中文或空格。

默认使用 PySceneDetect AdaptiveDetector，在本地 CPU 逐帧扫描，并映射实际帧时间戳。镜头最短可为 1 帧；不会为了满足最短秒数把快速剪辑强行合并。它检测视觉镜头切换，不等同于故事场景或完整动作边界，淡入淡出、闪光和快速运动需抽检。

扫描结果原子保存到 `data/plans/SOURCE_ID.json`，包含全部原片起止秒数。扫描完成后，后续编码失败或中断不会要求重新检测整部电影。首次扫描过程本身中断，则重跑扫描。

```bash
uv run mindex inspect SOURCE_ID
uv run mindex export SOURCE_ID
```

`inspect` 给出计划镜头数、预计精细片段数、镜头时长 min/median/p95/max、短镜头数量、已落盘数量和缺失文件。尚未切片时，导出的 `shots.csv` 已含完整切点，`segments.csv` 仅含已实际落盘的单元。

## 2. 按计划精确切片

```bash
uv run mindex split SOURCE_ID
uv run mindex inspect SOURCE_ID
uv run mindex export SOURCE_ID
```

也可以将前两步合为 `uv run mindex prepare '/路径/电影.mp4'`。

- `shot` 保留完整视觉镜头。
- `moment`：镜头不超过 6 秒时完整保留；更长镜头按 4 秒窗口、3 秒步长拆分，并补齐结尾，不跨镜头。
- 切片重新编码为 H.264、yuv420p、AAC MP4，开始时间归零；SQLite 和清单保留原片时间。默认选第一视频轨和第一音频轨，不烧录字幕。
- 极短镜头和奇数分辨率输入有专门回归测试；奇数边长补到偶数以适配 H.264。
- 短镜头与内容完全相同的 moment 尝试复用硬链接，减少重复编码和磁盘占用；不支持硬链接时复制。
- 每个单元有独立 MP4 路径与 JPEG 缩略图。日志显示当前镜头序号、总数和原片时间。

导出目录默认 `data/reports/SOURCE_ID/`，包含 `manifest.json`、`shots.csv`、`segments.csv`、`review.html`。直接用浏览器打开 `review.html`，按镜头或精细片段筛选并播放，48 个单元一页。页面使用相对媒体路径，移动报告时应同时保留数据目录的相对布局；CSV 中路径相对于 `data_dir`。

切片时按 Ctrl+C 停止后，重跑 `split SOURCE_ID`。已成功保存的媒体不会重新编码；未完成文件使用临时文件名，重跑时重建。若已完成素材的个别切片或缩略图缺失，`split` 也会修复。更换切分参数必须使用新的 `data_dir`，保留已有布局与索引。

## 3. 只启动 WeMM，嵌入并建索引

在 16GB GPU 上停止其他模型，启动 WeMM `192.168.1.86:4010`。无需先运行 `transcribe` 或 `analyze`：

```bash
uv run mindex status --health
uv run mindex index --mode visual --source SOURCE_ID --activate
uv run mindex check
```

`visual` 默认只嵌入 `moment`，避免默认同时索引镜头与完全相同的短片段。按需增加粒度：

```bash
uv run mindex index --mode visual --source SOURCE_ID --kind moment --kind shot --activate
```

长镜头会继续分窗口嵌入，单次请求不超过默认 6 秒；服务按配置采样帧。推理逐片段执行，客户端不会并行调度多个 GPU 模型。**视觉向量来自 WeMM 的原生视频编码**，没有转写或模型生成的描述，不会把占位文字作为嵌入内容。

索引包含模型空间身份、来源 ID、片段 ID、原片起止时间、文件路径和缩略图，保存在 Chroma 与 SQLite。输出的索引 ID 用于检查、发布和回退。建索引失败重跑相同命令，已写入的向量会复用；检查全部通过才发布。旧活动索引在新索引完成前保持可用。

`--source SOURCE_ID` 仅选择这一部电影，不是向现有索引追加。想发布多部电影的集合，完成它们的切片后省略 `--source`。发布后只会检索新活动索引中的素材。`--kind` 可重复使用；visual 不支持语义 `scene`。

如需先验证模型本身，可从导出的 `segments.csv` 选择一个不超过 6 秒的 MP4：

```bash
uv run mindex model-check 'data/clips/SOURCE_ID/moment/SEGMENT_ID.mp4' --text '对应画面描述' --output data/model-check.json
```

默认验证 4096 维、有限数值、单位范数与一致空间身份，并记录延迟和可用的显存指标。INT8/NF4 更换后建立新索引；同维度不意味着同空间。

## 4. 文字检索并播放结果

```bash
uv run mindex search '一个人推门走进房间'
uv run mindex serve
```

打开 `http://127.0.0.1:8000`。当前仅有画面检索时，页面明确显示该状态；对白、情绪和镜头标签为空，不用未运行的分析结果冒充完整语义索引。情绪、动作顺序等复杂查询的效果要用真实电影评测。

素材整理页面为 `http://127.0.0.1:8000/admin`，切片完成后即可按素材名找到镜头和精细片段，无需先嵌入。页面支持播放、下载、显示名称、备注、收藏、隐藏及恢复，点击镜头查看关联片段，或从片段返回父镜头。隐藏内容立即退出检索，恢复时不需要重建向量；原文件和切分时间不变。素材也可以在该页面“添加新素材”上传并分步启动处理，见 [素材后台](admin.md#四项处理工作台)。

## 后续丰富索引

依次单独运行 ASR `:4020` → 现有 Qwen 分析服务 → WeMM `:4010`，每次切换先释放上一模型显存：

```bash
uv run mindex transcribe --source SOURCE_ID
uv run mindex analyze --source SOURCE_ID
uv run mindex index --mode enriched --source SOURCE_ID --activate
```

完整模式会加入描述、对白和语义场景，并生成独立索引版本。TTS 不参与任何上述步骤。CPU 分镜无需抢占显存；若改用可选 TransNetV2 服务，再为它单独安排 GPU 阶段。
