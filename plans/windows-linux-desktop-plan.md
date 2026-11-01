# Windows / Linux 桌面端实施计划（跨平台：Electron 外壳 + Python sidecar）

状态：已完成（Windows/Linux 桌面版随 2026-11 发布，见 [docs/verification.md](../docs/verification.md)）。本文件是决策与实施计划。macOS 桌面版的背景见 [macos-native-plan.md](macos-native-plan.md)。

## 1. 目标

- 桌面版新增 Windows x64 与 Linux x64：安装后双击即用，不依赖用户安装 Python、uv 或 ffmpeg，功能与体验与 macOS 端一致（托盘常驻、后台选题定时、本机服务管理、数据目录选择、随包字体、AI 动画组件按需安装）。
- macOS（Apple Silicon）现状不回归；桌面版与 Web 版同步演进的约定不变（`desktop/` 只放外壳与打包，业务在后端走 REST）。

不在本轮范围：代码签名/公证/自动更新；Windows arm64、macOS x86_64、Linux arm64；打包 GPU 模型服务；随包 `mindex` 命令行；发布自动化（tag→release 聚合）。

## 2. 决策记录

| # | 问题 | 决定 |
|---|---|---|
| D1 | 平台矩阵 | 新增 Windows x64、Linux x64；macOS 维持 arm64 |
| D2 | 构建方式 | 三平台脚本本地可跑 + GitHub Actions 三平台矩阵为默认产出（PyInstaller 不能交叉编译，每平台在目标 OS 上构建） |
| D3 | 随包 ffmpeg | 三平台源码自编译（锁定版本 + sha256，GPL、libx264、libass、dav1d），延续 macOS D4；GPL 对应源码包随发布 |
| D4 | 安装包格式 | Windows = NSIS 安装器 + 便携 zip；Linux = AppImage + .deb |
| D5 | 不签名 | SmartScreen / Gatekeeper 提示同级，文档写明绕过方法 |
| D6 | Windows 锁与进程抽象 | `shared/locks.py` 用 ctypes LockFileEx（锁区域与 filelock 一致保持互操作）；进程操作收口新模块 `shared/process_ops.py`（stdlib + ctypes，不引入 psutil/pywin32） |
| D7 | libass 字体后端 | Windows 用 DirectWrite（系统自带）；Linux 首选禁 fontconfig、靠显式 `studio.subtitle.font_file`（spike S2 验证，失败兜底静态编译 fontconfig+expat） |
| D8 | Windows 构建编排 | PowerShell 编排（uv/python/node 原生工具链），ffmpeg 构建经 MSYS2/UCRT64 bash 跑与 Linux/macOS 共享的脚本（sha256 锁定表单一真相源） |

### 设计原则

- 平台差异内聚收口：后端收在 `backend/src/shared/`，外壳收在 `desktop/lib/`，构建收在 `scripts/`；业务代码零感知。
- POSIX 路径零改动：现有 mac 代码只加分支不改行为。
- 顺序：后端跨平台层 → 外壳跨平台 → 构建链（Linux 先打通再 Windows）→ 打包 → 冒烟 → CI → 文档。每阶段在至少一个真实目标平台验证后才进入下一阶段。

## 3. 关键技术设计

### 3.1 `shared/locks.py`（Windows 阻塞点）

fcntl 条件导入；`_lock_region(handle, exclusive)` / `_unlock_region(handle)` 内部平台分支：POSIX 保持 flock；Windows 用 ctypes `LockFileEx/UnlockFileEx`（kernel32、`use_last_error=True`；`LOCKFILE_FAIL_IMMEDIATELY=0x1`、独占加 `LOCKFILE_EXCLUSIVE_LOCK=0x2`；`ERROR_LOCK_VIOLATION=33` 映射 BlockingIOError；OVERLAPPED Offset=0，锁区域 offset 0 长度 0=到 EOF，与 filelock 的 WindowsFileLock 一致以保持互操作；OS 句柄经 `msvcrt.get_osfhandle` 取）。轮询/deadline/Timeout 语义与 POSIX 同构；`SharedFileLock` 公开 API、`named/acquire_ordered` 不变（`processing_lock.py` 直接用 `SharedFileLock`，零改动获益）。

### 3.2 `shared/process_ops.py`（新）

四函数双平台分支，`local_services.py` 与 `cli_persons.py` 共用：

- `spawn_detached(argv, *, cwd, env, stdout, stderr)`：POSIX `start_new_session=True`；Windows `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`。
- `alive(pid)`：POSIX `os.kill(pid, 0)`；Windows ctypes `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + `GetExitCodeProcess != STILL_ACTIVE`（不用 `os.kill`，其在 Windows 语义不同）。
- `process_start(pid)`：POSIX `ps -o lstart=`；Windows ctypes `GetProcessTimes` CreationTime 作 pid 指纹（防 pid 复用）。
- `stop_tree(pid, *, grace)`：POSIX killpg SIGTERM → 轮询 → SIGKILL；Windows 无跨进程 SIGTERM，`taskkill /PID x /T /F` 杀整树——优雅停止依赖退出前 REST `stop-on-exit`，进程层接受强杀（SQLite journal 恢复兜底）。

### 3.3 外壳（desktop/）

- `lib/paths.js`：`binaryName(name)`（win32 加 `.exe`），backend/ffmpeg/ffprobe 三处经它处理。
- `lib/sidecar.js`：`IS_WIN` 分支——`parentPid`/`processCommand` 用 PowerShell `Get-CimInstance Win32_Process`（单次取父 pid + 命令行）；`stopGroup` = `taskkill /T` 软停 → 轮询 → `/T /F`；dev+win32 父链查询失败时 `pidMatches` 放宽为 version 匹配（packaged 模式 pid 严格校验不变）；spawn 加 `windowsHide: true`。
- `main.js`：win32 `app.setAppUserModelId(process.execPath)`（通知需要）；`createTray()` 整体 try/catch——Linux 无托盘守护进程时降级（记日志、首次弹提示、关闭窗口=退出，防"隐藏后无法唤回"假死）。
- `lib/tray.js`：`shouldHide` 增 `trayAvailable` 参数（缺省 true，mac 行为不变）。

### 3.4 构建链（scripts/）

- `build-desktop-ffmpeg.sh` 保持单文件平台分支（依赖包版本+sha256 锁定表三平台同一套）：`uname -s` 分派 Darwin/Linux/MINGW64；libass 后端 DirectWrite/CoreText/无 fontconfig（D7）；ffmpeg configure 平台差异（Linux `--enable-pthreads --enable-iconv`；Windows `--enable-w32threads` + `-static -static-libgcc -static-libwinpthread`）；验收：mac `otool -L`（现状）、Linux `ldd`/`file`、Windows `objdump -p` DLL 白名单。
- `build-desktop-sidecar.sh` 去 Darwin 硬检查（mac/Linux 共用）；新增 `build-desktop-sidecar.ps1`（Windows 原生）。
- 新增编排 `build-desktop-linux.sh` 与 `build-desktop-win.ps1`；`build-desktop-mac.sh` 不动。
- `desktop/sidecar/mindex.spec`：`target_arch`/`codesign_identity` 仅 darwin 传入；`console=True` 保留（管道 stdio），黑框由外壳 `windowsHide` 消除。
- `check-desktop-sidecar.py`：backend 路径 win32 加 `.exe`；`--isolated-path` win32 用系统 PATH。

### 3.5 打包与 CI

- `desktop/package.json` build 段加 `win`（nsis+zip, x64）与 `linux`（AppImage+deb, x64）；NSIS `oneClick:false, perMachine:false`（per-user 安装无需 UAC）；图标维持默认（与 mac 现状一致）。
- `ci.yml` 的 `desktop`（每 push）与 `desktop-package`（schedule/dispatch）两 job 加三平台矩阵（macos-14 / windows-2025 / ubuntu-24.04）；缓存 `desktop/build/ffmpeg`（key 含 OS + 脚本 hash）与 electron 二进制缓存；packaged 冒烟用 `win-unpacked`/`linux-unpacked`（Linux 配 `xvfb-run`，不直接 launch AppImage）。

## 4. 前置 Spike（失败则调整对应方案）

| 编号 | 验证内容 | 影响 |
|---|---|---|
| S1 | Windows 上 `uv export --frozen --no-dev --extra animation --extra mcp` 全有 wheel 且可安装——第一验证项 | Windows sidecar 可行性 |
| S2 | Linux libass 禁 fontconfig + 显式 font_file 渲染中文 | ffmpeg Linux 分支 |
| S3 | MSYS2 UCRT64 下 meson/ninja 来源 | ffmpeg Windows 分支 |
| S4 | Windows LockFileEx 与 filelock 同文件互斥（区域 offset 0 长 0） | locks.py 锁参数 |
| S5 | Electron 45 在无托盘守护进程 GNOME 上 `new Tray` 失败模式 | 托盘兜底 |
| S6 | Windows PyInstaller console=True + windowsHide 零闪框；taskkill 行为 | sidecar 停止时序 |

## 5. 风险

1. S1 Windows 依赖解析（高，全链前置）。
2. Windows 强杀的 SQLite 一致性（中）：taskkill /F 绕过优雅停机，依赖 journal 恢复；退出前已先 REST `stop-on-exit`。
3. S2 libass Linux 无 fontconfig（中）：兜底加 fontconfig+expat（MIT 系许可，GPL 兼容）。
4. Electron Tray Linux 兜底（中）：try/catch 全覆盖 + close 判定联动。
5. MSYS2/原生工具链边界（中）：S3 spike；ffmpeg 脚本在 MSYS2 内自洽。
6. PyInstaller Windows 隐性差异（中低）：check 脚本 package-data 核对兜底。
7. ffmpeg Windows 静态链接不彻底（低）：DLL 白名单验收强制。

## 6. 明确不改的东西

不做签名/公证/自动更新；不改 `after-pack.js`、`shared/media.py`、`browser_install.py`、`desktop/lib/{auth,ports,state,security,firstrun}.js`；不动 mac 构建脚本结构与产物名；不新增平台图标资源；不引入 psutil/pywin32/Node 新依赖；不改业务后端 API 与前端页面；不做发布自动化。

## 7. 验证

- 每阶段 mac 回归集：`uv run pytest -m "not slow" -q`；`cd desktop && node --test test/*.test.js`；`node tests/desktop_smoke.cjs`（dev 与 frozen）。
- 目标平台：Linux/Windows 各跑全量快速 pytest、外壳单测、dev 冒烟、packaged 冒烟（各自 `MINDEX_SMOKE_APP`）、安装包实测（NSIS/AppImage/deb、托盘、选题定时提示）。
- 最终：CI 三平台矩阵全绿产三类 artifact；真机验证记录进 `docs/verification.md`。
