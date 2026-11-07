# 测试约定

Mindex 的自动化测试是**集成式**的：用真实 FFmpeg、PySceneDetect、SQLite、Chroma 和 HTTP API 验证工程链路，不靠层层 mock。它们证明工程行为正确，**不证明真实模型的中文检索或人物识别质量**——真实质量验收走 [`mindex e2e` 分阶段框架](e2e-framework.md) 并记录到 [实现验证记录](verification.md)。

## 命令与分层

```bash
uv run pytest -q                    # 手动运行全部测试（包括环境门控用例）
uv run pytest --ci-suite fast -x -q --timeout=120
uv run pytest --ci-suite media --ci-shard 1/3 -x -q --timeout=240
uv run pytest --ci-suite backup --ci-shard 1/2 -x -q --timeout=240
uv run pytest tests/test_pipeline.py::test_name   # 单个测试
```

- 推送、PR 和默认手动触发：静态检查、两个常规回归分片和三平台桌面冒烟。
- 每晚或手动启用 `full`：额外运行媒体三片、备份两片；每个测试 job 上限 20 分钟。手动启用 `package` 才构建三平台安装包；代码完整回归不再自动重复打包。
- `slow` 用例及依赖 `video_file` 的集成测试进入媒体组；备份文件和备份行为优先进入备份组；其余离线用例进入常规组。PG/S3/Chroma 环境门控用例交给 `scaling.yml` 真实后端专项。
- 集群后端验收与 CI 是独立工作流，可以同时运行。`scripts/verify-scaling.py` 按 Python 语法识别外部后端标记与环境门控，核对专项是否漏收测试，不把分组规则测试中的示例字符串当作后端依赖。预检失败仍会在 `artifacts/scaling/` 下保存诊断报告，供工作流上传。
- 分组在 `tests/ci_selection.py` 定义，历史模块平均耗时 `tests/ci-durations.json` 只用于均衡分片，不控制用例去留。新测试没有时间记录时仍会执行。`--ci-plan` 可保存实际选择清单。
- 首次失败立即输出；常规组单用例上限 120 秒，媒体/备份为 240 秒，超时输出线程栈。每个分片保存选择清单、JUnit、日志与最慢 15 项；不再等全部测试结束才看到首个失败。
- UI 回归需要 Node.js 与 Playwright；桌面外壳纯逻辑用 `node --test desktop/test`，Electron 冒烟用 `tests/desktop_smoke.cjs`。
- 完整 pytest 的通过数随测试增长而变化，以本次实际运行结果为准。过时的展示样式与提示词逐字断言不作为产品契约；失败仍须定位，不能只删掉失败用例。

## 模型替身与边界

- 模型一律用 `tests/conftest.py` 中明确标记的**确定性 Fixture 适配器**（脚本化输出，结果可复现）。
- **不能用模拟向量冒充真实检索结果**；服务未就绪时页面/API 必须显示明确的未就绪状态，而不是给出假数据。
- 智能助手的脚本化模型（`agent/llm/scripted.py`）经 `create_app(agent_llm=...)` 注入；选题模型同理（`create_app(topic_llm=...)`）。

## 一致性与测试取舍

- 已删除交付模板引入前的整套不透明签名/时间线黄金快照；版本复用、路径迁移、参数失效由 `test_studio_portable_paths.py`、`test_studio_workspace.py` 和分阶段行为测试验证。保留实际帧/采样边界与媒体输出断言。
- 已删除模块 700 行上限、旧文件必须不存在、README 截图必须折叠等实现/排版约束，以及示例配置必须等于某个魔法数的重复检查；配置模型边界、文档链接、静态资源哈希和真实页面行为继续检查。
- 桌面 Node 单测与 Electron 冒烟直接由桌面 CI 执行，删除重复的 pytest 包装。全页面多尺寸视觉矩阵及顶栏颜色/间距/全页溢出检查保留手动脚本；导航、语言保持、刷新与错误恢复等功能浏览器回归继续在媒体专项运行。
- 备份仅精简重复的完整/基本验证笛卡尔组合；保留每类冲突、攻击、脱敏、回滚与跨进程恢复。种子库每个进程只创建一次，每个用例复制独立数据，测试间不共享可变库。
- 每个用例结束关闭其 `tmp_path` 内创建的本地 Chroma 客户端，释放 SQLite 文件句柄及后台资源；模块/会话级共享库保留原有生命周期。长套件不应依赖进程退出才释放这些资源。
- Parity 测试：同一能力的多入口必须同时存在，逐项检查（如 `tests/test_topics_parity.py` 核对选题的页面/CLI/REST/agent 四入口；agent 工具数量断言在 `tests/test_agent_*.py`）。
- `tests/test_i18n.py` 校验 [文案目录](i18n.md) 与镜像一致；`scripts/build-assets.py --check` 校验资源版本哈希。

## 浏览器回归（tests/*_ui.cjs）

UI 回归由对应的 `test_*.py` 以子进程运行 `node`（无 node 自动跳过），Playwright 驱动**真实页面**：`tests/serve_fixture.py` 起一个隔离素材库（临时 data_dir + 真实 `mindex serve`），页面行为在真实 HTTP 与 DOM 上断言。Playwright 模块路径可用 `MINDEX_PLAYWRIGHT_MODULE` 指定。

- 顶栏导航与语言切换：`tests/test_header_ui.py` 驱动 `tests/header_navigation_ui.cjs`（桌面/窄屏导航顺序、选中态、语言保持和刷新错误恢复）与 `tests/i18n_ui.cjs`（语言切换不丢输入、不触发额外请求），两个用例在媒体专项执行。原始 60 种顶栏像素布局矩阵保存在 `tests/manual_header_layout.cjs`，不作为默认回归门槛。

### 全页面布局回归（复现方法）

全页面控件布局矩阵不进入自动回归，可显式执行（需要 Playwright）：

```bash
uv run pytest tests/manual_control_layout.py -q --timeout=240
```

另一个手动布局测量入口使用专用种子与脚本：

```bash
.venv/bin/python tests/serve_fixture.py --data-dir /tmp/mindex-layout --port 18329
.venv/bin/python tests/seed_layout_fixture.py --data-dir /tmp/mindex-layout
MINDEX_LAYOUT_DATA=/tmp/mindex-layout MINDEX_LAYOUT_URL=http://127.0.0.1:18329 \
  MINDEX_PLAYWRIGHT_MODULE=/path/to/playwright node tests/ui_layout_browser.cjs
```

- 复核页面 URL 从隔离 fixture 文件自动读取；`MINDEX_LAYOUT_REVIEW_URL` 可指定导出文件 HTTP 服务地址。
- `MINDEX_LAYOUT_RESUME=1` 跳过覆盖记录中已有布局组、继续执行；**完整验收不要设置**。
- 覆盖范围为 Chromium 响应式视口（1440/1000/800/540/390/320px）× 中英文；不验证 Safari/Firefox 与手机触屏原生 resize。
