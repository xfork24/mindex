# Mindex 桌面版（macOS / Windows / Linux）

Mindex 桌面版是一个 Electron 外壳：双击启动时，外壳在后台运行随包的冻结后端（就是 `mindex serve`），窗口加载它提供的同一套 Web 页面；退出时一并停止。功能与 Web 版完全一致，不需要安装 Python、uv 或 ffmpeg，也不需要手动执行 `mindex serve`。

- 支持的平台：macOS 13+（Apple Silicon，arm64）、Windows 10/11（x64）、Linux x64（AppImage / deb）。
- macOS 决策与取舍见 [plans/MACOS_NATIVE_PLAN.md](../plans/MACOS_NATIVE_PLAN.md)，Windows/Linux 跨平台决策见 [plans/WIN_LINUX_NATIVE_PLAN.md](../plans/WIN_LINUX_NATIVE_PLAN.md)；任务进度见 `.kiro/specs/macos-desktop/` 与 `.kiro/specs/desktop-cross-platform/`。

## 安装与首次打开

### macOS

- 打开 `Mindex-<版本>-arm64.dmg`，把 Mindex 拖到“应用程序”。
- 当前版本没有 Apple Developer ID 签名与公证（只做了 ad-hoc 签名），从网上下载的安装包首次打开会被 Gatekeeper 拦下：
  - 在“系统设置 → 隐私与安全性”底部点“仍要打开”；或
  - 在终端执行 `xattr -dr com.apple.quarantine /Applications/Mindex.app`（提示“已损坏，无法打开”时用这个）。
- 覆盖安装后若首次打开提示“无法打开”（Launch failed），再打开一次；仍不行时执行 `/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/Mindex.app` 重新登记（ad-hoc 签名每次构建都会变化）。

### Windows

- 运行 `Mindex-<版本>-x64.exe`（NSIS 安装器）：向导式安装，默认装到用户目录（不需要管理员权限），可选安装路径；卸载不会删除用户数据。`Mindex-<版本>-x64.zip` 为便携版，解压即用。
- 当前版本没有代码签名，首次运行安装器或应用会触发 SmartScreen（“Windows 已保护你的电脑”）：点“更多信息”→“仍要运行”。

### Linux

- AppImage：`chmod +x Mindex-<版本>-x64.AppImage` 后双击或命令行运行（需要 FUSE；没有 FUSE 的系统可 `./Mindex-*.AppImage --appimage-extract-and-run`）。
- Debian/Ubuntu 系：`sudo apt install ./Mindex-<版本>-x64.deb`。
- 桌面环境没有系统托盘（如 GNOME 默认）时，应用会提示一次并自动改为“关闭窗口即退出”；安装对应发行版的托盘扩展后恢复后台常驻行为。

三平台通用：升级 = 下载新版安装包覆盖安装（数据库结构迁移由新版本启动时自动执行）；暂不支持自动更新（需要各平台签名后才能可靠工作）。

## 文件位置

| 内容 | macOS | Windows | Linux |
|---|---|---|---|
| 配置文件 | `~/Library/Application Support/Mindex/config.yaml` | `%APPDATA%\Mindex\config.yaml` | `~/.config/Mindex/config.yaml` |
| 素材库（数据目录） | 同目录 `data`（菜单可改） | `%APPDATA%\Mindex\data` | `~/.config/Mindex/data`¹ |
| 字幕字体 | `…/Mindex/fonts/` | `%APPDATA%\Mindex\fonts\` | `~/.config/Mindex/fonts/` |
| AI 动画录制浏览器 | `…/Mindex/browsers/` | `%APPDATA%\Mindex\browsers\` | `~/.config/Mindex/browsers/` |
| 外壳状态 | `…/Mindex/desktop-state.json` | `%APPDATA%\Mindex\desktop-state.json` | `~/.config/Mindex/desktop-state.json` |
| 日志 | `~/Library/Logs/Mindex/backend.log` | `%APPDATA%\Mindex\logs\backend.log` | `~/.cache/Mindex/logs/backend.log` |

¹ 桌面版的用户数据目录由 Electron 按平台惯例统一划分（macOS `~/Library/Application Support`、Windows `%APPDATA%`、Linux `~/.config`），数据目录缺省在用户数据目录之下，菜单“选择数据目录…”可改到任意位置。

配置文件只在首次启动由模板生成，之后不覆盖。菜单“选择数据目录…”接受空目录（新建素材库）或已有的 Mindex 数据目录（含 `metadata.sqlite3`），选定后重启后端。不要让 Web 版与桌面版**同时**使用同一个数据目录。

模型服务的连接在“设置”页以配置档案管理，档案保存在数据目录的数据库里，激活后优先于配置文件；改用已有数据目录时，原来的档案随之生效。修改 `config.yaml` 后用菜单“重启后端”生效（“打开配置文件”可直接打开它）。

## 本机模型服务

设置页“本机服务”可以启停配置文件 `local_services` 里定义的本机模型服务（WeMM 嵌入、Qwen3 ASR、TTS、音乐生成、TransNetV2、人物识别），查看健康与日志，并设置“随后端自动启动”“退出时停止”。桌面版缺省开启该功能；模板里给出了对应 `scripts/run-*.sh` 的示例（Windows 上命令需为 `.bat`/`.ps1`/exe 形式），把路径换成你的 Mindex 仓库目录并取消注释即可。

- 页面与接口只能启停，不能修改命令、工作目录或环境变量；命令只能写在配置文件里。
- 退出应用时，先停止标记为“退出时停止”的服务，再停止后端（Windows 上进程停止为强杀整树，业务层的优雅停止由退出前的停止接口完成）。
- 端口上已有其他程序启动的服务时显示“外部运行”，这里不会启动或停止它。
- 单 GPU 环境不要同时启动多个大模型服务。

配置键见 [配置参考 · local_services](config-reference.md#local_services--本机模型服务)，接口见 [设置页指南 · 本机服务](settings.md#本机服务)。

## AI 动画录制组件

AI 动画分镜在无头 Chromium（Playwright 的 headless shell）里逐帧录制。安装包不带浏览器：首次使用前在设置页“本机服务 → AI 动画录制组件”一键安装（约 100 MB，需要联网），或在录制报错时按提示前往。

## 随包组件与许可

- 冻结后端：PyInstaller onedir，含 CPython 与 `uv.lock` 锁定的依赖（主依赖 + `animation`、`mcp` extra）。
- ffmpeg/ffprobe：从源码编译的 GPL 构建（含 libx264、libass、dav1d），支持烧录中文字幕；libass 字体后端按平台（macOS CoreText、Windows DirectWrite、Linux 显式字体文件）；对应源码随每个发布附带（macOS/Linux 为 tar，Windows 为 zip）。
- Noto Sans CJK SC（SIL OFL 1.1）。

详见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 构建（维护者）

三平台的构建都在目标平台上运行（PyInstaller 不能交叉编译）：

```bash
# macOS（Apple Silicon；需要 Xcode 命令行工具、pkg-config、uv、node 22+）
scripts/build-desktop-mac.sh
# 产物：desktop/dist/Mindex-<版本>-arm64.{dmg,zip}、mindex-desktop-ffmpeg-sources-<版本>.tar

# Linux（x64；需要 gcc/g++、make、pkg-config、nasm、libbz2-dev、zlib1g-dev、uv、node 22+）
scripts/build-desktop-linux.sh
# 产物：desktop/dist/Mindex-<版本>-x64.{AppImage,deb}、mindex-desktop-ffmpeg-sources-<版本>.tar

# Windows（x64；PowerShell；需要 MSYS2[UCRT64 工具链，MINDEX_MSYS2 可指定安装目录]、uv、node 22+）
powershell scripts/build-desktop-win.ps1
# 产物：desktop/dist/Mindex-<版本>-x64.{exe,zip}、mindex-desktop-ffmpeg-sources-<版本>.zip
```

Linux 的 deb 构建要求 `desktop/package.json` 提供 `homepage`，维护包元数据时须保留。包内的可执行文件名由 `build.linux.executableName` 显式指定为 `mindex`；CI 从该配置读取 `linux-unpacked` 的冒烟启动路径。

CI（`.github/workflows/ci.yml`）的 `desktop-package` 任务在定时/手动触发时按三平台矩阵构建并上传安装包，是默认产出方式；上述脚本在本地各平台同样可跑。MSYS2 工具链安装：`pacman -S make mingw-w64-ucrt-x86_64-toolchain mingw-w64-ucrt-x86_64-nasm mingw-w64-ucrt-x86_64-pkgconf mingw-w64-ucrt-x86_64-meson mingw-w64-ucrt-x86_64-ninja`（mingw 的 make 包只装 `mingw32-make`，GNU make 用 msys 仓库的 `make`）。)

分步执行：

| 命令 | 作用 |
|---|---|
| `scripts/build-desktop-ffmpeg.sh` | 从固定版本与 sha256 的源码编译 ffmpeg（三平台同一张锁定表；约 3–10 分钟） |
| `scripts/build-desktop-sidecar.sh`（mac/Linux）、`scripts/build-desktop-sidecar.ps1`（Windows） | 在独立环境中冻结后端到 `desktop/build/backend/` |
| `python scripts/check-desktop-sidecar.py [--isolated-path --ffmpeg-dir … --font … --animation]` | 冻结后端冒烟：自检（含品牌模板、叠加层模板与成片后期的转场、水印 ffmpeg 路径，曲目节奏分析、HTTP 工具替身调用与随包 mcp 客户端）、package-data、`-m` 分发、prepare、serve（含 `/studio/brands`、`/admin/music` 与 `mindex studio --url` 连接）、字幕烧录、动画录制（含 HTML 文字层的透明录制） || `cd desktop && npm start` | 开发模式：外壳以 `uv run mindex` 运行仓库后端；`MINDEX_DESKTOP_BACKEND=frozen` 改用已构建的冻结后端 |
| `node tests/desktop_smoke.cjs` | Electron 冒烟（三平台；`MINDEX_SMOKE_APP` 指定已安装应用，Linux 上 AppImage 自动以解包模式运行） |
| `uv run python scripts/desktop-bench.py` | 与同机 Web 版的性能对比 |

外壳版本号来自 `desktop/package.json`，必须与 `pyproject.toml` 的 `version` 一致（测试检查）；外壳启动时校验后端 `/api/health` 报告的版本与 pid，确认连到的是自己启动的后端。

## 后台运行与每日选题

关闭窗口时，Mindex 缺省不会退出，而是留在菜单栏/系统托盘继续运行后端，所以「选题挖掘」的每日定时会准点执行（见[选题挖掘 · 每日定时](topics.md#每日定时)）。第一次缩到后台时会弹出一次系统通知。

- 托盘图标的菜单：「打开 Mindex」「今日选题」（打开 `/studio/topics`）「关闭窗口时保留在后台」（勾选开关）「退出 Mindex」。只有「退出 Mindex」或 ⌘Q/Alt+F4 走正常退出流程：有任务时先确认，再停止本机服务与后端。
- macOS 点 Dock 图标、Windows 单击托盘图标，都会重新显示窗口。
- 取消勾选「关闭窗口时保留在后台」后，关闭窗口即退出应用（旧行为）。这时错过的定时会在下次启动时补跑。
- Linux 桌面没有系统托盘时（见上文安装说明），关闭窗口即退出。
- 偏好保存在 `desktop-state.json` 的 `keep_in_background`。页面通过 `window.MindexDesktop.background` 读取它，选题页据此提示"后台运行中"；Web 端没有这个对象，不显示该提示。

## 同步演进约定

- `desktop/` 只放外壳与打包，不放业务逻辑；业务一律在后端，经 REST 访问。
- 桌面专属能力只经 preload 的 `window.MindexDesktop`，前端必须在没有它时保持 Web 行为。
- ffmpeg/ffprobe 调用一律经 `shared/media.py` 的 `command()`、`ffmpeg_bin()`、`ffprobe_bin()`。
- 以自身源码哈希作指纹的模块须登记在 `desktop/sidecar/mindex.spec` 的 `SOURCE_MODULES`（`tests/test_desktop_frozen.py` 检查）；只经 `mindex -m` 运行的入口加进 `hiddenimports`。
- 新增 package-data 后运行 `check-desktop-sidecar.py`，它会核对 pyproject 声明的文件都已打包。
- 成片后期（转场、字幕样式、文字层、品牌模板、片头片尾、水印）全部在后端实现，页面与 Web 版共用；页面不使用 `window.prompt`（Electron 不支持），文件选择与下载用标准文件输入和 `Content-Disposition`。HTML 文字层依赖可选的动画录制组件，未安装时分镜渲染在预检阶段阻塞并提示安装。
- 曲库分析是纯 numpy（经随包 ffmpeg 解码），桌面版无需额外组件；曲目文件在用户数据目录的 `music/` 下。第三方工具的 MCP 客户端随包（`mcp` extra）；`mcp-stdio` 工具源必须登记命令的绝对路径，环境变量只在最小默认环境上追加，不依赖 GUI 应用的 PATH。
- 平台差异只收口在三处：后端 `shared/locks.py`（文件锁）与 `shared/process_ops.py`（进程操作）、外壳 `desktop/lib/`、构建 `scripts/`；业务代码不得直接用 fcntl/killpg/taskkill 等平台 API。

## 已知限制

- 三平台安装包均未签名（macOS ad-hoc、Windows/Linux 无签名），首次运行的系统提示见上文各平台安装节；签名与自动更新待各平台开发者账号后实施。
- Linux 桌面无系统托盘时后台常驻不可用（关闭即退出）；Windows 上的进程停止为强杀整树（退出前会先调停止接口做业务层清理）。
- 强制对齐仍需要用户自备的 `.venv-align` 解释器（与 Web 版相同）。
- 没有随包的 `mindex` 命令行；需要 CLI 时使用源码安装。
- macOS 只出 Apple Silicon；Windows/Linux 只出 x64（arm64 未排期）。
