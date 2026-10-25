# macOS 桌面版

Mindex 桌面版是一个 Electron 外壳：双击启动时，外壳在后台运行随包的冻结后端（就是 `mindex serve`），窗口加载它提供的同一套 Web 页面；退出时一并停止。功能与 Web 版完全一致，不需要安装 Python、uv 或 ffmpeg，也不需要手动执行 `mindex serve`。

决策与取舍见 [plans/MACOS_NATIVE_PLAN.md](../plans/MACOS_NATIVE_PLAN.md)，任务进度见 `.kiro/specs/macos-desktop/`。

## 安装与首次打开

- 系统要求：Apple Silicon（arm64），macOS 13 或更新。
- 打开 `Mindex-<版本>-arm64.dmg`，把 Mindex 拖到“应用程序”。
- 当前版本没有 Apple Developer ID 签名与公证（只做了 ad-hoc 签名），从网上下载的安装包首次打开会被 Gatekeeper 拦下：
  - 在“系统设置 → 隐私与安全性”底部点“仍要打开”；或
  - 在终端执行 `xattr -dr com.apple.quarantine /Applications/Mindex.app`（提示“已损坏，无法打开”时用这个）。
- 升级：下载新版 DMG 覆盖安装。数据库结构迁移由新版本启动时自动执行。暂不支持自动更新（需要签名后才能可靠工作）。覆盖安装后若首次打开提示“无法打开”（Launch failed），再打开一次；仍不行时执行 `/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/Mindex.app` 重新登记（ad-hoc 签名每次构建都会变化）。

## 文件位置

| 内容 | 位置 |
|---|---|
| 配置文件 | `~/Library/Application Support/Mindex/config.yaml`（首次启动由模板生成，之后不覆盖） |
| 素材库（数据目录） | `~/Library/Application Support/Mindex/data`；菜单“Mindex → 选择数据目录…”可改用其他目录 |
| 字幕字体 | `~/Library/Application Support/Mindex/fonts/`（随包 Noto Sans CJK SC，首次启动复制） |
| AI 动画录制浏览器 | `~/Library/Application Support/Mindex/browsers/`（按需安装） |
| 外壳状态 | `~/Library/Application Support/Mindex/desktop-state.json`（端口、所选数据目录） |
| 日志 | `~/Library/Logs/Mindex/backend.log`（按 10 MB 轮转），菜单“Mindex → 打开日志目录” |

“选择数据目录…”接受空目录（新建素材库）或已有的 Mindex 数据目录（含 `metadata.sqlite3`），选定后重启后端。不要让 Web 版与桌面版**同时**使用同一个数据目录。

模型服务的连接在“设置”页以配置档案管理，档案保存在数据目录的数据库里，激活后优先于配置文件；改用已有数据目录时，原来的档案随之生效。修改 `config.yaml` 后用菜单“Mindex → 重启后端”生效（“打开配置文件”可直接打开它）。

## 本机模型服务

设置页“本机服务”可以启停配置文件 `local_services` 里定义的本机模型服务（WeMM 嵌入、Qwen3 ASR、TTS、音乐生成、TransNetV2、人物识别），查看健康与日志，并设置“随后端自动启动”“退出时停止”。桌面版缺省开启该功能；模板里给出了对应 `scripts/run-*.sh` 的示例，把路径换成你的 Mindex 仓库目录（含 `bin/` 与 `models/`）并取消注释即可。

- 页面与接口只能启停，不能修改命令、工作目录或环境变量；命令只能写在配置文件里。
- 退出应用时，先停止标记为“退出时停止”的服务，再停止后端。
- 端口上已有其他程序启动的服务时显示“外部运行”，这里不会启动或停止它。
- 单 GPU 环境不要同时启动多个大模型服务。

配置键见 [配置参考 · local_services](config-reference.md#local_services--本机模型服务)，接口见 [设置页指南 · 本机服务](settings.md#本机服务)。

## AI 动画录制组件

AI 动画分镜在无头 Chromium（Playwright 的 headless shell）里逐帧录制。安装包不带浏览器：首次使用前在设置页“本机服务 → AI 动画录制组件”一键安装（约 100 MB，需要联网），或在录制报错时按提示前往。

## 随包组件与许可

- 冻结后端：PyInstaller onedir，含 CPython 与 `uv.lock` 锁定的依赖（主依赖 + `animation`、`mcp` extra）。
- ffmpeg/ffprobe：从源码编译的 GPL 构建（含 libx264、libass[CoreText 字体后端]、dav1d），支持烧录中文字幕；对应源码随每个发布附带。
- Noto Sans CJK SC（SIL OFL 1.1）。

详见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 构建（维护者）

在 Apple Silicon Mac 上（需要 Xcode 命令行工具、pkg-config、uv、node 22+）：

```bash
cd desktop && npm ci && cd ..
scripts/build-desktop-mac.sh            # ffmpeg（缺失时从源码编译）→ 冻结后端 → 冒烟 → electron-builder → ad-hoc 签名
# 产物：desktop/dist/Mindex-<版本>-arm64.{dmg,zip}、mindex-desktop-ffmpeg-sources-<版本>.tar
```

分步执行：

| 命令 | 作用 |
|---|---|
| `scripts/build-desktop-ffmpeg.sh` | 从固定版本与 sha256 的源码编译 ffmpeg（约 3 分钟） |
| `scripts/build-desktop-sidecar.sh` | 在独立环境中冻结后端到 `desktop/build/backend/` |
| `python scripts/check-desktop-sidecar.py [--isolated-path --ffmpeg-dir … --font … --animation]` | 冻结后端冒烟：自检、package-data、`-m` 分发、prepare、serve、字幕烧录、动画录制 |
| `cd desktop && npm start` | 开发模式：外壳以 `uv run mindex` 运行仓库后端；`MINDEX_DESKTOP_BACKEND=frozen` 改用已构建的冻结后端 |
| `node tests/desktop_smoke.cjs` | Electron 冒烟（`MINDEX_SMOKE_APP=/Applications/Mindex.app/Contents/MacOS/Mindex` 针对已安装应用） |
| `uv run python scripts/desktop-bench.py` | 与同机 Web 版的性能对比 |

外壳版本号来自 `desktop/package.json`，必须与 `pyproject.toml` 的 `version` 一致（测试检查）；外壳启动时校验后端 `/api/health` 报告的版本与 pid，确认连到的是自己启动的后端。

## 同步演进约定

- `desktop/` 只放外壳与打包，不放业务逻辑；业务一律在后端，经 REST 访问。
- 桌面专属能力只经 preload 的 `window.MindexDesktop`，前端必须在没有它时保持 Web 行为。
- ffmpeg/ffprobe 调用一律经 `shared/media.py` 的 `command()`、`ffmpeg_bin()`、`ffprobe_bin()`。
- 以自身源码哈希作指纹的模块须登记在 `desktop/sidecar/mindex.spec` 的 `SOURCE_MODULES`（`tests/test_desktop_frozen.py` 检查）；只经 `mindex -m` 运行的入口加进 `hiddenimports`。
- 新增 package-data 后运行 `check-desktop-sidecar.py`，它会核对 pyproject 声明的文件都已打包。

## 已知限制

- 只支持 Apple Silicon；Windows 版需要先把 `shared/locks.py` 改为跨平台。
- 强制对齐仍需要用户自备的 `.venv-align` 解释器（与 Web 版相同）。
- 没有随包的 `mindex` 命令行；需要 CLI 时使用源码安装。
- 签名公证与自动更新需要 Apple Developer ID，取得后实施（计划的 T13、T14）。
