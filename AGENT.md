# AGENT.md

本文件面向在 Mindex 仓库工作的编码代理。项目介绍与启动步骤见 [README.md](README.md)，详细技术导航见 [CLAUDE.md](CLAUDE.md)，机制说明见 [文档中心](docs/README.md)。

## 开始工作

- 先检查 `git status`，阅读相关源码与测试，保留用户已有改动。
- 文档、CLI 帮助、日志与错误消息使用中文。界面保持中英双语，以中文原文为文案唯一源。
- 以当前源码、配置模型和 CLI 帮助核对行为。`plans/` 是设计资料，历史验证记录不能直接作为本次测试结论。
- 默认使用隔离测试数据；不要以用户的 `data/`、`config.local.yaml` 或正在运行的模型服务做破坏性验证。不要将本地密钥、媒体、模型权重或运行数据提交到仓库。

## 代码位置与约定

- 后端直接位于 `backend/src/`，由 Setuptools 映射为 `mindex`；例如 `from mindex.search.search import SearchEngine`。使用当前分包路径，不增加旧模块转发层。
- 前端位于 `frontend/src/`，采用原生 HTML/JS/CSS，无独立构建步骤。两侧按 search/admin/persons/studio/settings 功能组织，共享资源位于各自的 shared/。
- API 装配在 `backend/src/api.py`；CLI 入口在 `backend/src/cli.py`（全局选项、核心命令与 `indexes`），`e2e`、`persons`、`clips` 命令组在 `cli_e2e.py`、`cli_persons.py`、`cli_clips.py`，`backup` 在 `maintenance/backup_cli.py`；重依赖保留在命令函数内延迟导入。
- 静态资源统一通过 `shared/web_assets.py` 定位；同时考虑源码目录与 wheel 中的 `mindex.web`，打包规则在 `pyproject.toml`、`MANIFEST.in`。
- UI 文案只改 `frontend/src/shared/messages.json`，然后运行 `python scripts/build-messages.py` 生成 `messages.js`（`--check` 仅校验）。保留语言切换前的输入和页面状态。
- 新模型通过 `models/adapters.py` 的协议与 `make_*` 工厂接入，具体后端各在一个模块（`embedders`、`qwen`、`speech`、`storyboard`、`transcribers`、`detectors`）；模型依赖使用独立环境，不混入应用环境。单 GPU 按阶段轮换服务，不假设全部服务同时在线。

## 必须保持的行为

- SQLite 是业务事实来源；Chroma 只保存显式向量，通过稳定 ID 关联元数据。
- 嵌入模型、预处理与空间身份必须匹配，禁止跨空间比较向量。新版文本查询允许有明确告警的 BM25 降级，图片与视频查询仍需可用的匹配嵌入服务。
- 发布索引保留不可变快照；更换空间或分析配置建立新版本。切分布局参数变化使用新 `data_dir`。
- 业务读取镜头媒体经 `shared/shot_media.py`，避免直接依赖历史切片路径。保留检查点、输入指纹、文件哈希、原子写入与任务互斥语义。
- Studio 任务以任务目录和 `job.json` 持久化；保留方案、依赖失效规则及历史构建产物。逐镜素材范围与任务范围取交集，空交集报错，不扩大到全库。
- 无素材、索引或模型时明确报告未就绪，不以模拟向量冒充真实结果。
- 清理先预览；备份迁移使用 `mindex backup` 的校验、暂存与恢复机制，不手动删除迁移日志绕过检查。

## 开发与验证

要求 Python 3.11–3.13、uv、ffmpeg/ffprobe。仓库没有 CI，提交前在本地运行下面的统一检查：

```bash
uv sync --extra dev
uv run ruff check backend/src tests                 # 代码规范（规则集见 pyproject.toml）
python scripts/check-doc-links.py                   # Markdown 相对链接与锚点
python scripts/build-messages.py --check            # messages.js 与 messages.json 一致
for f in frontend/src/*/*.js; do node --check "$f"; done   # 前端 JS 语法
uv run mindex --help
uv run pytest tests/test_pipeline.py -q             # 按改动范围选择测试
uv run pytest -q                                    # 完整回归，约 20 分钟
```

人物相关主环境依赖使用 `uv sync --extra dev --extra person`；推理服务安装与启动见 CLAUDE.md。`--config` 放在子命令前，也可使用 `MINDEX_CONFIG`。配置拒绝未知键。

按改动选择验证：

| 改动 | 优先检查 |
| --- | --- |
| 管线、索引、检索 | 对应 `test_pipeline.py`、`test_vector_index.py`、`test_channel_recall.py` 等用例 |
| 页面、静态资源、文案 | `test_page_assets.py`、`test_i18n.py`、改动 JS 的语法检查及对应 UI 回归 |
| Studio、人物、备份 | 对应功能测试，必要时覆盖媒体与跨进程恢复链路 |
| 目录、导入或打包 | CLI、资源加载、独立服务入口及 wheel/源码包内容 |
| 仅文档 | 核对命令、文件路径、相对链接，并执行 `git diff --check` |

UI 回归使用 Node.js 与 Playwright，可通过 `MINDEX_PLAYWRIGHT_MODULE` 指定模块路径。模型测试使用明确标记的确定性适配器；工程测试通过不代表真实模型质量，真实验收使用 `mindex e2e` 并记录模型与索引版本。

已知历史失败与检查范围见 [目录重组验证记录](docs/history/source-layout-verification.md)。遇到失败应说明是否与本次修改相关，不为得到全绿结果随意跳过或修改测试。

交付时简述改动、实际运行的验证与尚未验证的范围。行为、配置或命令变化时同步相关文档。
