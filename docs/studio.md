# Studio 分镜与版本工作台

入口 `/studio`。设计依据为 [STUDIO_UI_PLAN.md](../plans/STUDIO_UI_PLAN.md)。保留 Python、文件存储和原生前端；首次进入任务工作台时接入版本模型，原文件和执行历史保留。

## 工作区

任务标题可以独立保存，不改变方案、分镜或媒体版本。任务包含多个构建方案，可以新建、命名、复制和切换。方案分别保存分镜表、内容版本、阶段选择、人工素材范围及参数，共享任务内的产物历史。重新拆分使用新分镜身份，不影响其他方案。

分镜矩阵的列依次为文本与查询、召回、配音、选片与对齐、分镜渲染。格内显示已选版本、摘要、最近执行结果和兼容性提示，详情展开后可浏览候选、试听、查看历史或选为构建输入。这两个历史操作互相独立；选择版本不会执行模型或渲染。矩阵支持横向滚动，固定分镜标识和表头，也可以整体折叠。下方依次为成片、产物与调试记录。

## 版本与依赖

每次实际执行保存独立的不可变版本，包含阶段、分镜身份、成功或失败结果、输入版本、参数快照、文件路径及 SHA-256。成功后更新当前方案的对应选择；失败只新增记录，保留原有选择和全部下游产物。

兼容性按实际使用的内容和阶段参数检查：查询影响召回，旁白与声音影响配音，选片依赖其实际配音与召回版本，人工素材范围不依赖召回。分镜渲染绑定选片与音频版本、原片文件身份及处理参数。上游变化只展示具体冲突；构建时才决定复用和重建。

构建依赖为 `内容 → 召回 / 配音 → 选片 → 分镜渲染 → 成片合成`。分镜表另外记录身份、数量和顺序。每个新成片保存实际使用的完整依赖图及方案快照；依赖树可展开、查看节点详情，并可复制为新方案。

## 构建与人工控制

“构建成片”先显示复用、重建、阻塞和无需执行的清单，确认后执行。预检令牌绑定方案、输入版本与生效参数，提交时变化会要求重新预检。每格也支持单独执行或复用，强制重新生成是单独入口。

某节点失败后，其他独立镜头及分支继续执行，依赖失败节点的后续节点标为阻塞。必需依赖未齐时不合成新成片，已有成片始终可播放和下载。再次预检续建会复用输入指纹和文件哈希均匹配的成功版本。构建状态为 running/complete/partial/failed，节点另有 reused/blocked/skipped 状态。

人工选片按时间列出同一素材的镜头。允许勾选连续镜头并在其范围内微调边界；跨素材、不连续镜头、素材空隙和隐藏区间均拒绝。预览显示选定范围、实际使用范围、裁剪秒数、配音速度和冻结秒数；确认令牌仍有效时才保存新的选片版本。自动选择同时保存锚点、涉及镜头和向前/向后的扩展量。重新对齐保留人工选择的完整素材范围。

方案参数包括当前配音服务的声音 ID、召回数量、原声混音及增益、字幕模式、配音速度和补齐策略。速度留空时自动选择；冻结策略保持原速配音；严格策略要求范围与配音时长匹配。没有新增跨素材拼接、音色克隆、情感控制、版本删除或自动清理。

仍使用全局 `studio.lock` 串行执行。方案编辑、选择、删除任务、ZIP 导出与执行互斥，冲突返回 409。服务重启后的未完成构建保留历史并标为中断，可重新预检续建。

## 文件与迁移

```text
data/studio/<job_id>/
  job.json                  # 任务标题、原文及旧流程状态
  workspace.json            # schema=2：方案指针、版本索引、构建执行与节点结果
  versions/<version_id>/
    version.json            # 不可变版本记录
    debug/*.json            # 请求、响应、ffmpeg 命令及错误诊断
    ...                     # 此版本的候选、音频、视频或成片
  migration-v2/             # 迁移前 job/storyboard/timeline/audio/checkpoints 快照
  attempts/...              # 原执行产物与历史，原样保留
  previews/...              # 由明确版本/参数生成的预览缓存
  exports/*.zip             # 导出包不递归包含导出包
```

迁移校验旧 manifest 的文件哈希，导入可验证的召回、配音、分镜视频及历史成片。无法证明输入关系和参数的旧版本标为“记录不完整”，不会按当前分镜猜测旧依赖，也不会自动视为可复用版本。旧成片仍可下载；依赖关系不完整时不能恢复为可验证方案。所有新版本均保存明确依赖。迁移是按任务进行的，打开前不会批量修改现有任务。

认证头、密钥和 URL 中的凭据经过诊断脱敏。原片不复制进任务目录；版本记录素材身份及文件指纹。产物下载 URL 指向确定版本文件，不依靠可变别名或缓存查询参数代表版本。

## 媒体与字幕

分镜渲染独立保存确定帧数的视频和处理后的 48kHz PCM 音频。帧数依据实际处理后采样数计算，保证在任意成片位置都能容纳完整配音。合成混选版本时，根据累计帧边界统一计算各镜的采样补齐和字幕时间，避免逐镜浮点累计或独立取整造成漂移。

视频先裁到实际原片范围，再冻结末帧。音频支持变速、响度归一化及原声压低混音。成片发布前检查音视频轨道、字幕模式、时长和完整解码；失败保留已有成片。软字幕同时保存 MP4 mov_text、SRT 和 WebVTT；烧录字幕需要部署环境的 ffmpeg subtitles/libass 和可用中文字体。

技术对齐不保证叙事、人物或查询质量，仍需人工审阅。确定性测试音频也不代表真实发音质量。

## API（前缀 `/api/studio`）

| 方法与路径 | 语义 |
|---|---|
| POST /jobs | 创建任务，支持标题、原文和默认选项 |
| GET /jobs | 任务列表 |
| GET /jobs/{id}/workspace | 版本工作台详情；首次访问进行迁移 |
| PATCH /jobs/{id}/title | 独立编辑任务标题 |
| POST /jobs/{id}/plans | 新建、copy_from 复制方案或 from_final 恢复成片方案 |
| POST /jobs/{id}/plans/{plan}/activate | 切换方案，不执行任何阶段 |
| PATCH /jobs/{id}/plans/{plan} | 携 revision 保存名称、options 或完整 shots |
| GET /jobs/{id}/versions?stage=tts&shot_id=... | 查询阶段历史版本 |
| GET /jobs/{id}/versions/{version} | 版本详情 |
| GET /jobs/{id}/versions/{version}/dependencies | 明确输入依赖图 |
| POST /jobs/{id}/plans/{plan}/select-version | revision、stage、shot_id、version_id；仅成功版本可选 |
| GET /jobs/{id}/plans/{plan}/preflight?force=false | 构建清单与确认 token |
| POST /jobs/{id}/plans/{plan}/build | 提交 token、force；后台执行，202 |
| POST /jobs/{id}/plans/{plan}/nodes | revision、stage、shot_id、inputs、force；明确输入版本，202 |
| GET /sources/{source}/shots | 按时间排列的镜头及可见性 |
| POST /jobs/{id}/plans/{plan}/range-preview | revision、shot_id、source_id、start、end、segment_ids；返回对齐结果与 preview_token |
| POST /jobs/{id}/plans/{plan}/range-confirm | 原预览请求及 preview_token；保存新选片版本 |
| GET /jobs/{id}/versions/{version}/preview | 对确定的选片或分镜视频版本预览 |
| GET /jobs/{id}/artifacts | 全部产物与诊断文件列表 |
| GET /jobs/{id}/artifacts/{path}?download=true | 确定路径的播放或下载，支持 Range |
| GET /jobs/{id}/archive | 空闲时导出任务 ZIP |
| DELETE /jobs/{id} | 删除整个任务，仍使用现有互斥限制 |

错误为 `{"detail": ...}`。已有旧任务的阶段 API 保留给尚未接入工作台的调用；迁移后的任务使用方案 API，旧阶段执行及 storyboard/timeline 编辑入口返回 409，防止可变别名覆盖版本工作台。旧任务详情、旧文件下载与历史 URL 仍保留。

## 验证

`tests/test_studio_workspace.py` 覆盖版本选择、失败保护、混选依赖、方案隔离、续建、连续/隐藏镜头、预览确认、迁移和执行锁；使用真实 ffmpeg、SQLite、Chroma 及确定性模型。非整数帧率测试校验帧数、累计采样边界、字幕、完整解码和依赖文件哈希。浏览器及本轮检查见 [工作台验收记录](studio-workspace-verification.md)；此前真实模型的质量结论见 [历史验收记录](studio-verification.md)。

浏览器专用服务可通过 `uv run python tests/serve_fixture.py --data-dir <临时测试目录> --port 8001` 启动，勿将确定性颜色向量用于真实素材索引。
