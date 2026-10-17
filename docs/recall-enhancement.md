# 三渠道独立处理与召回

新版 `auto` 索引不要求 ASR、LLM 分析或语义场景分组全部完成。可用画面、对白、分析分别建表示；空内容不创建文本向量。模型客户端按需初始化。原有 `enriched` 命令保留为 format 1 兼容入口，原有快照、排名配置及回退命令继续有效。

## 对白

```sh
mindex enrich-dialogue [SOURCE] --no-publish
mindex enrich-dialogue [SOURCE] --subtitle-stream 2
mindex enrich-dialogue [SOURCE] --workers 4
```

默认补充全部素材并自动发布；`--no-publish` 仅生成对白，适合先完成 ASR 再部署嵌入模型的流程。轨道编号是 ffprobe 的全局 stream index。每次重新探测旧素材，不重新分镜或切片。自动选择中文、默认、其他完整文本字幕，自动排除 forced 和位图字幕；显式选择 forced 文本轨道仍可提取。FFmpeg 转 SRT，清除样式，保留原片时间、语言及轨道。无可用文本字幕、提取失败或空结果回退 ASR，并记录原因；烧录字幕不做 OCR。

MiniMax 使用 `response_format=verbose_json` 和 `timestamp_level=word`；保存原始响应、词时间和说话人编号。编号仅在所属请求分块内有效，不代表姓名，也不建立跨块人物身份。非空文本无有效时间戳即失败。30 秒核心分块前后最多扩展 1 秒，按词中点归属核心分块；成功分块原子保存，重试复用检查点。按标点、停顿、说话人及分块边界组织对白，再按时间相交关联片段，不扩散到整个场景。

字幕内容指纹与 ASR 模型无关；ASR 指纹不包含本地 ForcedAligner 配置。`transcribe` 的 MiniMax 分支直接使用服务时间戳；`transcribe_raw` 和 `align` 仍可处理旧的纯文本流程。静音、无音轨、未启用 ASR 有明确状态。

MiniMax 协议依据：[官方语音识别文档](https://platform.minimax.cn/docs/api-reference/speech-to-text)。

## 建库与发布

```sh
mindex index --mode auto --activate
mindex index --source SOURCE --channel dialogue --activate
mindex index --mode visual --kind moment --activate
mindex check
mindex indexes activate OLD_INDEX_ID
```

指定素材或渠道时，合并活动版本的其他素材和渠道。缺失或失败的处理数据不会删除已经发布的其他渠道。视频按原片、片段及推理时间窗口、预处理、模型空间匹配；文本按渠道、正文和模型空间匹配。兼容的 format 1 视频向量可直接复制，不重复推理。新版本向量集合独立，完成表示清单、向量与媒体检查、最终模型空间检查后才原子切换活动版本。失败保留原活动索引。

`settings` 中的 `index_report.<INDEX_ID>` 保存处理耗时、复用/新增/恢复向量数、渠道覆盖、片段数与回退版本。`index_members.snapshot` 保存实际表示清单，`representations.cache_key` 用于兼容性匹配；活动版本快照不随运行时设置或新处理数据改变。

分析仅使用结构化描述与风格字段组成 `analysis` 文本，不拼入对白原文。显式 `analyze` 支持可选对白，并记录实际对白输入版本及分析模型身份。补充对白与自动建库不调用分析模型。

## 检索

`POST /api/search/text` 可附加 `channels: ["visual", "dialogue", "analysis"]`，省略则使用所有可用渠道。新版对白和分析分别等权融合向量与 BM25，再按 `search.channel_weights`（默认 1:1:1）进行渠道 RRF。关闭渠道同时排除该渠道词法文本。旧版索引继续使用 `search.weights`。图片、视频只查询画面表示。

结果包含 `matches[].channel`、渠道排名、内部 `rank_details`、`matched_text`、原片 `evidence` 时间。保留隐藏继承、筛选和同源时间重叠去重。查询嵌入不可用时，选中的文本渠道仍可 BM25 检索；响应 `degraded=true` 并带 `warnings`。无可用词法渠道时返回明确错误，不静默返回空结果。

## 网页与任务

素材管理详情显示三个渠道的处理状态及已发布覆盖，区分处理完成和进入检索。可选择轨道、补充对白、关闭自动发布及重试失败任务。

- `POST /api/admin/sources/{id}/dialogue`：`{subtitle_stream:null,publish:true,workers:2}`，立即返回 202 与任务 ID。
- `GET /api/admin/dialogue-tasks/{id}`：持久化状态、分块进度、错误与发布结果。
- `POST /api/admin/dialogue-tasks/{id}/retry`：仅重试失败或中断任务。

任务冻结生效配置；密钥不写入任务快照或响应，重试按原配置档案引用读取密钥。后台与 CLI、删除共享 `pipeline.lock`，冲突返回 409。服务重启只将无锁的未完成任务标记为中断，不自动重提交 ASR。删除素材同时清理对白记录、任务及分块缓存。

## 验证范围

自动化测试覆盖七种渠道组合、部分分析、空对白、无音轨、字幕优先与真实 FFmpeg 时间定位、MiniMax 时间戳和中断续跑、旧向量复用、其他素材/渠道保留、隐藏与回退、BM25 降级与渠道隔离，以及任务即时返回、刷新、配置冻结和重试。

评测按索引格式选择方案：新版比较纯画面、纯对白、纯分析、画面＋对白、全渠道以及关闭对白/分析；旧版保留原方案。仅人工确认的相关片段可计质量指标，工程验证不等于准确率提升。
