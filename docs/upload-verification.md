# 素材上传与四项工作台验收

日期：2026-10-18。用户流程：在素材管理页批量上传 → 分别启动分镜、对白、LLM、人物任务 → 更新检索 → 检索与镜头详情核对。

本次追加验收：GPU 锁默认 false；设置保存、刷新持久化、Web/CLI 生效；运行中不同类型任务可启动，同类重复任务及当前素材删除返回409，其他素材可删除；第二个管理器不误判活跃任务为中断。Playwright 验证开关、并行按钮、390px 窄屏和无页面错误。LLM 低思考参数按厂商契约测试，GLM-5.3-Flash 实际最小文本分组请求以 low + JSON 模式返回有效结果，约0.83秒；隔离的2秒工程视频实际视觉分析以 low 返回有效注释，约3.55秒；这些不是整片性能基准。场景分组第二批失败重试复用第一批检查点，旧注释保持兼容。最新相关回归115项通过。

## 已验证链路

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
