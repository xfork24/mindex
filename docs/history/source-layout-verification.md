# 前后端目录重组验证

验证日期：2026-10-21。

## 最终结构与接口

- 前端位于 `frontend/src/{search,admin,persons,studio,settings,shared}/`。
- 后端模块直接位于 `backend/src/`，不再嵌套 `mindex/` 目录。
- `pyproject.toml` 的 Setuptools `package-dir` 将后端映射为 Python 包 `mindex`，前端资源映射到安装包的 `mindex/web/`。开发环境和安装环境共用 `shared/web_assets.py` 定位资源。
- 仓库内导入、动态导入、子进程入口、脚本、测试和文档已切换到新路径；不保留旧 Python 模块转发层。
- 页面地址及 API 保持原样；静态资源使用 `/assets/<模块>/<文件名>`。

## 已通过的验证

| 检查 | 结果 |
|---|---|
| Python 测试收集 | 822 个测试正常收集 |
| 页面、国际化及媒体源码约束检查 | 20 项通过 |
| 独立浏览器检查 | 五个页面、资源、样式、导航及语言切换通过，无脚本异常或资源加载失败 |
| Node 检查 | 13 个断言回归脚本、国际化适配模块，以及全部前端 JS 语法检查通过 |
| OpenAPI 与重构前版本对比 | 109 个接口路径及全部 schema 完全一致 |
| wheel 与源码分发包 | 构建成功；后端及全部 24 个前端资源完整，源码包保持扁平目录 |
| 安装包隔离加载 | 应用导入、五个页面、消息目录及导出检查页样式通过 |
| CLI、模型服务、人物服务、独立对齐 worker | 帮助入口通过 |
| 跨进程备份恢复 | `test_full_restore_relocated_and_cross_process_vectors` 通过 |
| 源码 lint 与差异空白检查 | 通过 |

上轮目录验证使用 `127.0.0.1:18763`、`18764` 与 `/tmp` 下的独立数据，这两个服务已关闭。下文的真实视频验收服务 `18765` 保留供查看，未操作原有服务。
主环境、人物环境和检测环境中的项目可编辑安装均已刷新，未修改模型依赖。

## 较广范围回归的限制

非备份测试分批覆盖了全部 745 项，最终合并结果为 **738 项通过、7 项失败、0 项未执行**。
结果按分批运行及复测合并，7 项失败均已在重构前副本复现；77 项备份测试未完整重跑，已单独验证跨进程恢复场景。
首次批量运行中完成 553 项，其中 544 项通过、9 项失败；随后补完剩余批次，并重跑修正或波动的用例。

9 项失败中：

- `test_business_modules_do_not_resolve_source_path` 使用拼接的旧源码路径，已更新并重跑通过。
- `test_pipeline_stores_only_valid_annotations_and_reuses_checkpoints` 单独重跑通过；对应验证模块的 44 项测试在新旧版本中单独运行均通过，批量运行存在波动。
- 下列 7 项已分别在重构前的独立源码副本中复现：
  - `test_live_curation_filters_text_video_without_mutating_index`
  - `test_task_continues_after_rejection_and_publishes_without_rejected_text`
  - `test_default_upgrade_all_granularities_without_media_cache`
  - `test_visual_index_does_not_require_asr_or_analysis`
  - `test_pinned_preview_index_keeps_published_library`
  - `test_subtitle_person_task_publish_strict_mentions_and_studio_media`
  - `test_studio_job_snapshots_active_generator`

首次验证中，`test_query_media_upload_dedupe_and_validation` 在新旧版本中都超过 45 秒。补测定位到上传测试夹具漏注入标题生成器，创建任务时意外调用外部模型；已补充 `FixtureStoryboarder`。查询文件上传、召回校验和并发上传三项相关回归现已全部通过（24.63 秒）。音色默认值测试同样漏注入标题生成器，已补充测试替身并重跑通过。此前剩余的非备份用例现已补测完成；上述 745 项结果按最终复测结果合并。
既有浏览器长流程 `tests/i18n_ui.cjs` 在新旧版本中均等待 `.work-card` 超时；上表的五页浏览器检查为独立的资源与导航验证。

完整 `ruff check backend/src tests` 仍报告 `tests/test_persons.py` 中 4 个重构前已存在的未使用变量；`ruff check backend/src` 通过。


## Little India 真实视频补测

使用用户提供的完整 `Little India.MP4`，未截短、未替换处理实现：

| 项目 | 结果 |
|---|---|
| 原始视频 | 266,533,039 字节，260.76 秒，1920×1080，H.264 + AAC |
| 独立服务与数据 | `http://127.0.0.1:18765`，`/tmp/mindex-little-india-20261021` |
| 浏览器上传 | 完整视频上传成功；重复上传返回相同内容 ID，资产未重复登记 |
| 错误输入 | 损坏视频、空文件均返回 422，临时上传文件已清理 |
| 实际处理 | 本地 AdaptiveDetector 检测并生成 130 个镜头、130 个精细片段 |
| 原片完整性 | 原文件和独立库副本 SHA256 均与素材 ID 一致 |
| 镜头覆盖与媒体 | 130 个切片均通过 ffprobe，起止连续覆盖全片，时长偏差小于 0.15 秒，均含视频及音轨 |
| 解码与缩略图 | 首、中、末三个切片完整解码通过；260 张 JPEG 均通过校验 |
| 访问与页面 | Range 请求返回 206，缩略图返回 200；刷新后处理状态保留；桌面及移动端检查通过，无脚本异常或资源加载错误 |
| 重复处理 | 再次处理完成，镜头数量不变，130 个文件的大小及 mtime 均未改变 |
| 处理期间响应 | 状态接口返回 200，浏览器可持续轮询进度 |

素材 ID：`3457543fd7281f8b521f46a107623f9c34487870c59f1d300311557fe52ab239`。
原有服务及当前项目的 `data/` 资产库未导入该视频。测试资产仅保留在独立目录，端口不同本身不会导致资产库不同，决定因素是 `data_dir`。
报告和截图位于 `/tmp/mindex-real-media-results/`（`browser-report.json`、`artifact-report.json`、`desktop.png`、`mobile.png`）。

本次验证了真实上传、CPU 分镜、媒体产物和页面流程。配置中的 TransNet、嵌入、ASR 与语义分析服务无法连接，未完成这些服务的真实推理验收；本次未执行真实人物识别及模型检索质量验证。

复现方式（仅使用独立数据目录）：

```bash
uv run python tests/serve_media_validation.py \
  --data-dir /tmp/mindex-real-media-validation --port 18765 --local-detector

MINDEX_MEDIA_TEST_VIDEO="$HOME/Downloads/Little India.MP4" \
MINDEX_PLAYWRIGHT_MODULE=/path/to/playwright \
node tests/real_video_upload_ui.cjs
```

真实浏览器脚本的默认输出目录为 `/tmp/mindex-real-media-results`。独立服务未注入模拟模型；`--local-detector` 仅选择项目已有的本地 CPU 分镜实现。
