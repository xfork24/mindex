# 素材上传与四项工作台验收

日期：2026-10-18。用户流程：在素材管理页批量上传 → 分别启动分镜、对白、LLM、人物任务 → 更新检索 → 检索与镜头详情核对。

本次追加验收：GPU 锁默认 false；设置保存、刷新持久化、Web/CLI 生效；运行中不同类型任务可启动，同类重复任务及当前素材删除返回409，其他素材可删除；第二个管理器不误判活跃任务为中断。Playwright 验证开关、并行按钮、390px 窄屏和无页面错误。LLM 低思考参数按厂商契约测试，GLM-5.3-Flash 实际最小文本分组请求以 low + JSON 模式返回有效结果，约0.83秒；隔离的2秒工程视频实际视觉分析以 low 返回有效注释，约3.55秒；这些不是整片性能基准。场景分组第二批失败重试复用第一批检查点，旧注释保持兼容。最新相关回归115项通过。

场景分组修复验收（Chinatown.MP4，31.598233秒、34个精细片段）：旧协议用真实第一批32条描述复现正常结束却只返回0–27，遗漏28–31；输出181 token，并非预算耗尽。改为显式输入 `index` 和总数、仅返回 `scene_starts` 后，同一 GLM-5.3-Flash 第一批连续3次调用均覆盖全部32条（2.99/2.90/4.21秒），最后2条的批次也有效。原失败任务续跑复用68条 shot/moment 注释，分组两批均首次成功（1.56/3.07秒），生成10个连续场景，覆盖0–31.598233秒，10条场景注释全部保存。场景注释有一次独立 JSON 语法错误，自动重试成功。原始/修复请求响应、输入与续跑日志保存在忽略目录 `data/verification/scene-grouping/`，不含鉴权头。这些调用验证实际模型输出契约与工程续跑，不代表场景边界质量基准。相关回归119项通过（analysis_validation/cloud_adapters/contracts/source_work/pipeline），Ruff 和差异检查通过。

外部字幕验收：真实 FFmpeg 解析 SRT/ASS/SSA/VTT/SMI/SAMI/MicroDVD SUB，以及 UTF-8 BOM、UTF-16、GB18030、Big5；字幕文件上传不改已有对白，开始任务后保存原视频时间轴对白，镜头详情正确裁切。验证空/损坏/超限/全片外字幕拒绝、临时文件清理、素材绑定、内容摘要校验、重复上传修复、同类任务锁、版本变化与检索发布失败续跑。浏览器在隔离工程素材库验证错误恢复、上传期间禁用开始、上传后自动选择、刷新恢复、任务请求与结果、镜头详情、桌面与390px窄屏、英文切换、无页面错误。截图 `/tmp/mindex-subtitle-upload-desktop.png`、`/tmp/mindex-subtitle-upload-mobile.png`。相关 Python 回归84项通过；没有对真实素材运行字幕处理。

四项工作进度验收：分镜、字幕对白、LLM、人物处理各运行一次并自动发布，再分别运行四项独立检索更新，验证 SQLite → API → 网页两阶段进度保存、计数、完成状态和进度条。工程模型仅用于隔离测试库；额外受控网页状态验证四项运行中数量、ASR 音频块、场景分组、字幕轨道、等待写入锁的不定进度条和失败后保留计数。真实线程持有索引写入锁，验证另一个构建任务在锁释放前已报告等待状态；真实任务 API 验证嵌入失败后重试保留处理进度并从完整片段检查点继续。人物发布去重统计当前素材镜头，不计入其他素材。相关 Python 回归144项通过（work_progress/persons/source_work/channel_recall/dialogue_tasks/subtitle_uploads/i18n）；分镜管线回归亦通过，Ruff、JS语法、消息目录一致性与差异检查通过。两份浏览器验收脚本通过，桌面/390px窄屏、刷新及无页面错误。截图 `/tmp/mindex-work-progress-desktop.png`、`/tmp/mindex-work-progress-mobile.png`。真实服务仍有任务运行，未重启或中断；新增后端阶段和双阶段保存需重启后生效。

## 已验证链路

2026-10-19 追加验收：明确的HTTP内容安全错误与聊天拒绝不自动重试；普通参数、鉴权与权限错误不误判为跳过。真实 FFmpeg/SQLite/Chroma 工程链路验证拒绝后继续、相同范围复用、全部拒绝、场景/分组拒绝、同模型检查点续跑、增量发布删除旧分析文本并保留视觉渠道、API详情和跳过数量。161项相关Python回归通过，Ruff、JS语法和差异检查通过。`admin_skip_scroll_ui.cjs` 在隔离服务 `--progress-fixture --reject-blue-analysis` 验证蓝色分镜被拒绝后任务及发布完成、详情原因和刷新后的计数；仅扩充渲染列表到48项测试独立滚动、边缘不带动页面、面板延伸至视口底部/滚动后全高、窗口缩放、390px窄屏及英文，无页面错误。`admin_upload_ui.cjs` 四项工作完整流程再次通过。截图 `/tmp/mindex-skip-scroll-desktop.png`、`/tmp/mindex-skip-scroll-mobile.png`。工程拒绝为明确模拟，不调用真实内容审核服务。真实后端仍在分析/嵌入，未中断重启；新增拒绝处理需下次重启生效。

| 边界 | 验证证据 |
|---|---|
| 上传窗口 → API | Playwright 顺序上传三个文件：无效视频失败、有效视频登记、同内容改名文件定位已有素材；失败不阻塞队列。 |
| API → 文件/数据库 | 原名保存在内容哈希目录；重复不覆写；空文件、无视频流、超限、读取中断清理临时文件；登记未调用检测器。 |
| 页面 → 后台任务 | 四项状态、新素材前置条件、启动/轮询、结果和进度、刷新恢复；默认允许不同类型任务并行，同类重复启动受保护，“强加 GPU 锁”开启时全局互斥。 |
| 任务 → 处理/发布 | 分镜修复保留旧布局；分析无需对白；字幕严格模式、ASR、自动回退、无音轨、空对白；人物资料缺失禁用启动、补充资料后可执行整片。 |
| 处理 → 发布失败重试 | 分镜、对白、人物成功结果先落库，发布失败独立记录；重试发布未重复切片/转写/人物识别；独立发布任务可查询和重试。 |
| 发布 → 检索 | 真实 SQLite、Chroma、FFmpeg，确定性工程测试适配器；增量发布保留其他素材和已有 shot/moment 粒度；索引检查通过，文本检索返回测试素材。 |
| 数据 → 页面详情 | 镜头详情读取 LLM/人物结果；桌面 1440×1000 与窄屏 390×844，无浏览器脚本错误或横向溢出。 |
| 重启 → 重试 | 持久任务标记中断，手动续接检查点；冻结模型配置和历史 int/float 指纹形式，凭据不进入配置快照。 |

测试适配器明确标记为工程测试用途，不评估实际 ASR、LLM、人物识别或中文检索质量；真实远程模型没有重新处理旧素材。

受影响模块回归通过 135 项；发布重试边界补充检查通过 10 项。全量运行其余 421 项通过，当次三项新测试失败已修复并纳入上述回归。Ruff、JS 语法与 `git diff --check` 均通过。

## 旧素材只读验收

通过 `mode=ro` SQLite 连接、禁用 Store 初始化迁移，读取真实 `data/metadata.sqlite3`。约 5.1 秒完成素材摘要，未改写数据、索引、人物身份、参考图或文件布局。

| 《给阿嬷的情书》工作 | 读取结果 |
|---|---|
| 分镜 | 完成；1,126 个镜头，2,336 个精细片段，缺失 0；画面检索覆盖 2,336/2,336，版本当前。 |
| 对白 | ASR 完成；MiniMax `asr-1.0`，2,061 条对白，1,820 个有对白片段已进入检索，版本当前。 |
| LLM | 待处理；0/3,462 个片段，无场景。 |
| 人物 | 完整覆盖 1,126/1,126 个分镜；854 个已接受、0 个待确认、272 个已排除；当前人物发布与库内证据一致。 |

## 复现工程验收

```bash
uv run pytest tests/test_source_work.py tests/test_admin.py tests/test_dialogue.py \
  tests/test_dialogue_tasks.py tests/test_persons.py tests/test_pipeline.py \
  tests/test_segment_details.py tests/test_channel_recall.py -q
uv run ruff check src tests
node --check src/mindex/static/admin.js
```

浏览器验收使用独立临时目录；每次运行使用一个新的目录，避免先前结果影响新素材断言。安装 Playwright 并准备 Chromium 后：

```bash
uv run python tests/serve_upload_fixture.py --data-dir /tmp/mindex-upload-check --port 8018
# 另一终端；Playwright 可从 NODE_PATH 加载，或用 MINDEX_PLAYWRIGHT_MODULE 指定模块路径。
MINDEX_UPLOAD_TEST_VIDEO=/tmp/mindex-upload-check/工程样本.mp4 node tests/admin_upload_ui.cjs
```

截图输出 `/tmp/mindex-upload-desktop.png` 与 `/tmp/mindex-upload-mobile.png`。测试服务仅在本机 `8018`，关闭服务后即可删除临时目录。

嵌入进度验收使用三个分镜，并让每次工程模型调用延迟 4 秒以覆盖网页轮询。必须使用新的临时目录：

```bash
uv run python tests/serve_upload_fixture.py --data-dir /tmp/mindex-progress-check \
  --port 8018 --progress-fixture --embedding-delay 4
MINDEX_UPLOAD_TEST_VIDEO=/tmp/mindex-progress-check/工程样本.mp4 \
  node tests/admin_embedding_progress_ui.cjs
```

浏览器确认 `0/3 → 1/3 → 2/3 → 检索已更新 3/3`，刷新后继续轮询、发布前检索覆盖保持原值、窄屏无横向溢出，并检查控制台无页面错误。截图为 `/tmp/mindex-embedding-progress-desktop.png` 与 `/tmp/mindex-embedding-progress-mobile.png`。`test_source_work.py` 通过真实任务 API 模拟第三次嵌入失败，验证处理完成与发布失败分开保存，以及重试从 `2/M` 继续；`test_channel_recall.py` 验证长分镜多窗口按片段计数与就绪索引的最终进度。

全部八项进度验收另用新临时目录，增加分析延迟以覆盖处理轮询：

```bash
uv run python tests/serve_upload_fixture.py --data-dir /tmp/mindex-all-progress-check \
  --port 8018 --progress-fixture --embedding-delay 4 --analysis-delay 1
MINDEX_UPLOAD_TEST_VIDEO=/tmp/mindex-all-progress-check/工程样本.mp4 \
  node tests/admin_work_progress_ui.cjs
```
