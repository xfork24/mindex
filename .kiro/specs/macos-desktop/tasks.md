# Implementation Plan: macos-desktop

## Overview

分阶段实施，每阶段结束时验证后再进入下一阶段：

- 阶段 A（后端适配）：文档、健康接口、ffmpeg 定位、冻结兼容。
- 阶段 B（外壳开发模式）：Electron 外壳、桌面配置与数据目录。
- 阶段 C（冻结后端）：PyInstaller sidecar 与冒烟。
- 阶段 D（安装包）：GPL ffmpeg、字体、ad-hoc 签名 DMG，在当前账户安装验证。
- 阶段 E（本机服务与动画组件）：后端、设置页、外壳联动、Chromium 按需安装；用脚本启动 WeMM 验证。
- 阶段 F（收尾）：CI、性能对比、文档。

全程约束：
- `desktop/` 不放业务逻辑；业务一律走 REST。
- 界面文字以 `frontend/src/shared/messages.json` 中文为唯一源，改完运行 `python scripts/build-messages.py` 与 `python scripts/build-assets.py`。
- 签名快照（`tests/test_studio_signature_snapshot.py`）不变。
- 依赖锁定精确版本。
- 全部完成后在当前分支提交。

推迟（需要 Apple Developer ID）：签名公证、自动更新，见 `plans/MACOS_NATIVE_PLAN.md` 的 T13、T14。

## Tasks

- [x] 1. 选型与计划文档（阶段 A）
  - [x] 1.1 更新 `plans/ROADMAP.md` M4：推荐 Electron + Python sidecar，Tauri 改为备选，修正更新与差分描述，签名公证/自动更新标为推迟
    - _Requirements: 1.1_
  - [x] 1.2 `plans/README.md` 登记 `MACOS_NATIVE_PLAN.md`；计划文件开头指向本 spec
    - _Requirements: 1.2_

- [x] 2. 后端健康接口（阶段 A）
  - [x] 2.1 `StudioExecutor`、`BackgroundTaskRunner` 登记实例；`create_app` 增加 `GET /api/health`
    - _Requirements: 2.1, 2.3, 2.4_
  - [x] 2.2 编写 `tests/test_desktop_health.py`：认证、busy、无需模型服务
    - _Requirements: 2.2, 2.3_

- [x] 3. ffmpeg/ffprobe 定位（阶段 A）
  - [x] 3.1 `shared/media.py` 增加 `ffmpeg_bin()`/`ffprobe_bin()`，`command()` 解析首参数；替换直接 `subprocess` 调用（`shot_repair.py`、`animation_capture.py`、`model_server.py`）
    - _Requirements: 6.1, 6.2_
  - [x] 3.2 编写 `tests/test_media_binaries.py`：包装脚本记录全部调用、未设置时行为不变
    - _Requirements: 6.1, 6.2, 6.3_

- [x] 4. 冻结运行兼容（阶段 A）
  - [x] 4.1 以自身源码哈希作指纹的模块在 `mindex.spec` 的 `SOURCE_MODULES` 中以源码打包（不修改 `llama_cpp.py`：其文件哈希是嵌入空间身份的一部分）
    - _Requirements: 7.1_
  - [x] 4.2 新增 `mindex/desktop_entry.py`（`-m` 分发）
    - _Requirements: 7.2_
  - [x] 4.3 编写 `tests/test_desktop_frozen.py`：入口分发；AST 扫描自哈希模块必须登记在 `SOURCE_MODULES`
    - _Requirements: 7.1, 7.2_

- [x] 5. Electron 外壳（开发模式，阶段 B）
  - [x] 5.1 `desktop/package.json`（锁定版本）、`lib/*`、`main.js`、`preload.js`、`pages/*`
    - _Requirements: 3.1–3.9, 4.1–4.6_
  - [x] 5.2 `desktop/test/*.test.js`：端口、启动参数、origin、外链协议、状态文件
    - _Requirements: 3.3, 4.3_
  - [x] 5.3 Electron 冒烟 `tests/desktop_smoke.cjs` 与 `tests/test_desktop_smoke.py`（开发模式）：页面、SSE、下载、退出无残留
    - _Requirements: 3.1, 3.8, 4.1, 4.4_

- [x] 6. 桌面配置与数据目录（阶段 B）
  - [x] 6.1 `desktop/config.template.yaml`、`lib/firstrun.js`、“选择数据目录…”菜单
    - _Requirements: 5.1, 5.3_
  - [x] 6.2 编写 `tests/test_desktop_config.py`：模板严格校验与路径解析
    - _Requirements: 5.2_

- [x] 7. PyInstaller sidecar（阶段 C）
  - [x] 7.1 `desktop/sidecar/mindex.spec`、`scripts/build-desktop-sidecar.sh`
    - _Requirements: 7.3, 8.1_
  - [x] 7.2 `scripts/check-desktop-sidecar.py` 并通过（冻结自检模块 `mindex.desktop_check`：chromadb、jieba、opencv、playwright、前端、模板、源码哈希）
    - _Requirements: 8.2_
  - [x] 7.3 外壳生产模式使用冻结后端
    - _Requirements: 3.1_

- [x] 8. GPL ffmpeg 与字体（阶段 D）
  - [x] 8.1 `scripts/build-desktop-ffmpeg.sh`（x264、libass[CoreText]、freetype、fribidi、harfbuzz、dav1d 静态链接）并构建；验收 subtitles 滤镜、libx264、仅链接系统库
    - _Requirements: 9.1_
  - [x] 8.2 字体与许可文件、`THIRD_PARTY_NOTICES.md`；外壳首次运行复制字体、注入 `MINDEX_FFMPEG`/`MINDEX_FFPROBE`
    - _Requirements: 9.2, 9.3_
  - [x] 8.3 无 Homebrew PATH 下冒烟烧录中文字幕；随包 ffmpeg 跑快速 pytest（1343 通过）与媒体慢测（28 通过）
    - _Requirements: 9.4_

- [x] 9. DMG（阶段 D）
  - [x] 9.1 electron-builder 配置、`scripts/build-desktop-mac.sh`、ad-hoc 签名与校验
    - _Requirements: 10.1, 10.2_
  - [x] 9.2 安装到 `/Applications` 并对已安装应用运行冒烟（PATH 仅 /usr/bin:/bin）；经 Launch Services 真实启动与 AppleScript 退出
    - _Requirements: 10.3_

- [x] 10. 本机服务管理后端（阶段 E）
  - [x] 10.1 配置模型、`settings/local_services.py`、REST 路由、`serve` 启动时 autostart
    - _Requirements: 11.1–11.5_
  - [x] 10.2 智能助手工具与 `docs/agent.md`、工具数量断言
    - _Requirements: 11.6_
  - [x] 10.3 编写 `tests/test_local_services.py`
    - _Requirements: 11.1–11.5_

- [x] 11. 本机服务管理界面与外壳联动（阶段 E）
  - [x] 11.1 设置页“本机服务”区块与文案
    - _Requirements: 12.1, 12.2_
  - [x] 11.2 外壳退出时调用 `stop-on-exit`
    - _Requirements: 3.8_
  - [x] 11.3 控制器测试；用 `scripts/run-wemm.sh` 实测：设置页启动 WeMM（8 秒健康）→ Web 后端文字检索 200 → 退出应用后 WeMM 停止
    - _Requirements: 12.1_

- [x] 12. 动画录制组件按需安装（阶段 E）
  - [x] 12.1 后端安装任务与接口、外壳 `PLAYWRIGHT_BROWSERS_PATH`、页面入口
    - _Requirements: 13.1, 13.2, 13.3_
  - [x] 12.2 测试：替代命令验证进度与失败（`tests/test_browser_install.py`）；冻结后端经 `-m playwright` 实装 Chromium headless shell 并录制 AI 动画（`check-desktop-sidecar.py --animation`）
    - _Requirements: 13.2_

- [x] 13. CI、性能与文档（阶段 F）
  - [x] 13.1 `ci.yml` macOS 任务与定时构建（YAML 已校验；GitHub runner 上的实际运行需推送后确认）
    - _Requirements: 14.1_
  - [x] 13.2 `scripts/desktop-bench.py` 并记录 `docs/VERIFICATION.md`（规模缩小的项与未完成项在记录中列明）
    - _Requirements: 14.2_
  - [x] 13.3 `docs/desktop.md`、`docs/README.md`、`CLAUDE.md`、`AGENT.md`、`README.md`
    - _Requirements: 10.4, 14.3_
  - [x] 13.4 全量验证：ruff、快速 pytest、`node --check`、messages/assets/文档链接检查；提交
    - _Requirements: 4.7_
