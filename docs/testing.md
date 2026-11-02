# 测试约定

Mindex 的自动化测试是**集成式**的：用真实 FFmpeg、PySceneDetect、SQLite、Chroma 和 HTTP API 验证工程链路，不靠层层 mock。它们证明工程行为正确，**不证明真实模型的中文检索或人物识别质量**——真实质量验收走 [`mindex e2e` 分阶段框架](e2e-framework.md) 并记录到 [实现验证记录](verification.md)。

## 命令与分层

```bash
uv run pytest -q                    # 全部测试
uv run pytest -m "not slow" -q      # 快速回归（CI 默认，跳过 slow 标记的重媒体用例）
uv run pytest tests/test_pipeline.py::test_name   # 单个测试
```

- 重媒体/长耗时用例打 `slow` 标记；CI 只跑快速回归。
- UI 回归需要 Node.js 与 Playwright；桌面外壳纯逻辑用 `node --test desktop/test`，Electron 冒烟用 `tests/desktop_smoke.cjs`。
- 完整 pytest 的通过数随测试增长而变化，以本次实际运行结果为准；历史上有少量**有意保留的过期基线断言**（见 [验证记录](verification.md#已知基线断言记录2026-10-31)），遇到失败先确认是否与本次修改相关，不为全绿改写基线。

## 模型替身与边界

- 模型一律用 `tests/conftest.py` 中明确标记的**确定性 Fixture 适配器**（脚本化输出，结果可复现）。
- **不能用模拟向量冒充真实检索结果**；服务未就绪时页面/API 必须显示明确的未就绪状态，而不是给出假数据。
- 智能助手的脚本化模型（`agent/llm/scripted.py`）经 `create_app(agent_llm=...)` 注入；选题模型同理（`create_app(topic_llm=...)`）。

## 快照与一致性测试

- 签名快照（`tests/test_studio_signature_snapshot.py` 等）：重构前用 `MINDEX_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_studio_signature_snapshot.py -q` 生成基线；平时缺快照即失败。
- Parity 测试：同一能力的多入口必须同时存在，逐项检查（如 `tests/test_topics_parity.py` 核对选题的页面/CLI/REST/agent 四入口；agent 工具数量断言在 `tests/test_agent_*.py`）。
- `tests/test_i18n.py` 校验 [文案目录](i18n.md) 与镜像一致；`scripts/build-assets.py --check` 校验资源版本哈希。

## 浏览器回归（tests/*_ui.cjs）

UI 回归由对应的 `test_*.py` 以子进程运行 `node`（无 node 自动跳过），Playwright 驱动**真实页面**：`tests/serve_fixture.py` 起一个隔离素材库（临时 data_dir + 真实 `mindex serve`），页面行为在真实 HTTP 与 DOM 上断言。Playwright 模块路径可用 `MINDEX_PLAYWRIGHT_MODULE` 指定。

- 顶栏布局与语言切换：`tests/test_header_ui.py` 驱动 `tests/header_navigation_ui.cjs`（顶栏导航顺序、选中态、按钮组靠右与间距）与 `tests/i18n_ui.cjs`（语言切换不丢输入、不触发额外请求、窄屏不溢出），两个用例标记 slow，夜间完整回归执行。

### 全页面布局回归（复现方法）

全页面控件布局测量使用专用种子与脚本：

```bash
.venv/bin/python tests/serve_fixture.py --data-dir /tmp/mindex-layout --port 18329
.venv/bin/python tests/seed_layout_fixture.py --data-dir /tmp/mindex-layout
MINDEX_LAYOUT_DATA=/tmp/mindex-layout MINDEX_LAYOUT_URL=http://127.0.0.1:18329 \
  MINDEX_PLAYWRIGHT_MODULE=/path/to/playwright node tests/ui_layout_browser.cjs
```

- 复核页面 URL 从隔离 fixture 文件自动读取；`MINDEX_LAYOUT_REVIEW_URL` 可指定导出文件 HTTP 服务地址。
- `MINDEX_LAYOUT_RESUME=1` 跳过覆盖记录中已有布局组、继续执行；**完整验收不要设置**。
- 覆盖范围为 Chromium 响应式视口（1440/1000/800/540/390/320px）× 中英文；不验证 Safari/Firefox 与手机触屏原生 resize。
