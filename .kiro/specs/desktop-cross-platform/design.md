# Design Document

决策记录与整体计划见 [plans/WIN_LINUX_NATIVE_PLAN.md](../../../plans/WIN_LINUX_NATIVE_PLAN.md)；需求见 [requirements.md](requirements.md)。

## 1. 总体结构

平台差异三处收口，业务代码零感知：

| 层 | 收口位置 | 内容 |
|---|---|---|
| 后端 | `backend/src/shared/locks.py`、`backend/src/shared/process_ops.py` | 文件锁、进程操作 |
| 外壳 | `desktop/lib/{paths,sidecar,tray}.js` + `main.js` | 可执行名、进程管理、托盘兜底 |
| 构建 | `scripts/build-desktop-ffmpeg.sh`（三环境分支）、新增 linux/win 编排 | 单一 sha256 锁定表 |

POSIX 路径零改动：现有 mac 代码只加分支不改行为；`SharedFileLock`、`named/acquire_ordered` 公开 API 不变。

## 2. 文件锁：Windows 用 LockFileEx

`fcntl` 条件导入（`try/except ImportError`）；`SharedFileLock.acquire/release` 内部经 `_lock_region(handle, exclusive)` / `_unlock_region(handle)` 分支：

- POSIX：`fcntl.flock`（现状代码平移）。
- Windows：ctypes `kernel32.LockFileEx/UnlockFileEx`（`WinDLL("kernel32", use_last_error=True)`）。
  - flags：`LOCKFILE_FAIL_IMMEDIATELY = 0x1`，独占再加 `LOCKFILE_EXCLUSIVE_LOCK = 0x2`。
  - 失败且 `ctypes.get_last_error() == 33`（`ERROR_LOCK_VIOLATION`）→ 抛 `BlockingIOError`，与 POSIX 分支的轮询结构同构（非阻塞尝试 → deadline → `time.sleep(0.05)` → `Timeout`）。
  - `OVERLAPPED`：`Internal/InternalHigh` 为 `c_size_t`、`Offset/OffsetHigh` 为 `c_ulong`、`hEvent` 句柄；Offset 置 0。
  - **锁区域 offset 0、长度 0（到 EOF）**：与 `filelock` 的 WindowsFileLock（`msvcrt.locking` → LockFile 系）一致——字节范围锁只在重叠时冲突，区域一致才互操作。
  - OS 句柄经 `msvcrt.get_osfhandle(handle.fileno())` 获取；文件以 `open("a+b")` 打开（Windows 下即读写权限，满足独占锁对写权限的要求）。

共享锁（`LOCK_SH` / 不设 EXCLUSIVE 标志）与独占锁的并发语义与 POSIX flock 对齐。

## 3. 进程操作：`shared/process_ops.py`

四函数，`local_services.py`（本机模型服务）与 `cli_persons.py`（人物扫描 worker）共用：

| 函数 | POSIX（现状平移） | Windows |
|---|---|---|
| `spawn_detached(argv, *, cwd, env, stdout, stderr)` | `start_new_session=True` | `creationflags = CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW`（0x200 \| 0x08000000） |
| `alive(pid)` | `os.kill(pid, 0)`（EPERM→存活） | ctypes `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, ...)` + `GetExitCodeProcess` 判 `STILL_ACTIVE(259)`，随即 `CloseHandle`。**不用 `os.kill`**——其在 Windows 上对非 CTRL 信号是 TerminateProcess，语义完全不同 |
| `process_start(pid)` | `ps -o lstart=` | ctypes `OpenProcess` + `GetProcessTimes` 的 CreationTime（FILETIME 元组），格式化为字符串作 pid 指纹。指纹只在同平台的后端重启间比较，无跨平台格式互通问题 |
| `stop_tree(pid, *, grace)` | `os.killpg(SIGTERM)` → 轮询 → `SIGKILL` | 无跨进程 SIGTERM：先尝试 `child.terminate()`/软停路径不适用后，`taskkill /PID <pid> /T /F` 杀整树（孙进程必须 `/T`） |

**Windows 停止语义取舍**：进程层接受强杀；优雅停止依赖外壳/后端退出前先走 REST `stop-on-exit`（停止标记了"退出时停止"的本机服务），SQLite 一致性靠 journal/WAL 恢复兜底。理由：Windows 没有对无控制台进程投递 SIGTERM 的可靠机制（CTRL_BREAK 需共享控制台且 uvicorn 不处理 SIGBREAK），引入自研关停协议超出本轮范围。

## 4. 外壳

### 4.1 可执行名与进程管理（`lib/paths.js`、`lib/sidecar.js`）

- `binaryName(name)`：`process.platform === "win32" ? name + ".exe" : name`；`layout()` 的 backend/ffmpeg/ffprobe 三处经它。
- `sidecar.js` 以 `IS_WIN` 内部分支，延续可注入风格（`isAlive`/`kill`/`command` 参数化）：
  - `parentPid`/`processCommand`：win32 用 PowerShell 单次 `Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>'`（一次取 ParentProcessId 与 CommandLine），失败返回 0/""。
  - `stopGroup(pid, {graceMs})`：win32 = `taskkill /PID x /T`（软停尝试，预期对无窗进程立即无效）→ 轮询 grace → `taskkill /PID x /T /F`；返回值（是否强制结束）与 POSIX 一致。
  - spawn 选项：`detached: true`（现状）+ `windowsHide: true`（转 CREATE_NO_WINDOW，配合 PyInstaller `console=True` 不闪黑框且管道 stdio 可用）。
  - pid 校验：packaged 模式 exe 即服务进程，`pid === child.pid` 严格校验不变；dev+win32 且 PowerShell 父链查询失败时放宽为 version 匹配（uv.exe → python.exe 的间接启动）。
- `main.js`：win32 下 `app.setAppUserModelId(process.execPath)`（Windows 通知归属）。

### 4.2 托盘兜底（Linux）

Linux 无 libappindicator/libayatana 或托盘守护进程时 `new Tray`/`setContextMenu` 可能抛错。`createTray()` 整体 try/catch：

- 失败 → 记日志到后端日志、置 `trayUnavailable`、首次弹一次 dialog 说明；
- 窗口 close 判定（`trayLib.shouldHide`）增加 `trayAvailable`（缺省 true，mac 行为不变）：无托盘时关闭窗口 = 直接退出（防"窗口隐藏后无法唤回"的假死）；
- `Notification.isSupported()` 已有判断，libnotify 缺失自然降级。

### 4.3 PyInstaller spec

`target_arch="arm64"`、`codesign_identity=None` 仅 darwin 传入（`sys.platform` 条件 kwargs）；`name="mindex"`（Windows 自动 `.exe`）；`console=True` 保留——管道 stdio 依赖它，黑框由外壳 `windowsHide` 消除。

## 5. ffmpeg 三平台构建

单脚本 `build-desktop-ffmpeg.sh` 三环境分支（`uname -s`：Darwin / Linux / MINGW64_NT*）；依赖包版本 + sha256 锁定表三平台同一套（单一真相源）。

| 维度 | Darwin（现状） | Linux | Windows（MSYS2 UCRT64） |
|---|---|---|---|
| 工具链 | clang + uv 装的 meson/ninja | gcc/make/pkg-config/nasm + 同一 uv meson 机制 | UCRT64 gcc/make/nasm/pkgconf；meson 按 S3 结论（pacman 或 uv） |
| libass 字体后端 | CoreText | 无 fontconfig，显式 `font_file`（S2 验证；兜底静态编译 fontconfig+expat） | DirectWrite（系统自带） |
| ffmpeg configure 差异 | videotoolbox/audiotoolbox + `-mmacosx-version-min` | `--enable-pthreads --enable-iconv` | `--enable-w32threads`、`--extra-ldflags="-static -static-libgcc -static-libwinpthread"` |
| 链接验收 | `otool -L` 仅系统库 | `ldd`/`file` 静态或仅系统库 | `objdump -p` DLL 白名单（KERNEL32/ucrtbase/api-ms-* 等） |

Windows 编排（`build-desktop-win.ps1`）用 PowerShell（uv/python/node 为原生工具链），ffmpeg 构建经 `msys2_shell.cmd -ucrt64 -c "bash scripts/build-desktop-ffmpeg.sh"`——bash 脚本在 MSYS2 内自洽（不调 Windows 原生 uv）。

## 6. 打包与 CI

- electron-builder：`win` = nsis + zip（x64）；`nsis` = `oneClick:false, perMachine:false, allowToChangeInstallationDirectory:true, deleteAppDataOnUninstall:false`（per-user 安装无需 UAC，与不签名绿色分发定位一致）。`linux` = AppImage + deb（x64），`category: AudioVideo`。图标维持默认 Electron 图标（与 mac 现状一致）。
- CI：`desktop` 与 `desktop-package` 两 job 三平台矩阵（macos-14 / windows-2025 / ubuntu-24.04）；缓存 `desktop/build/ffmpeg`（key 含 `runner.os` + 脚本 hash）与 electron 二进制缓存；Linux dev 冒烟 `xvfb-run -a`，packaged 冒烟用 `linux-unpacked`（不直接 launch AppImage，规避 FUSE/沙箱问题）。

## 7. Spike 结论

（实施时回填：S1 Windows 依赖解析 / S2 libass 无 fontconfig / S3 MSYS2 meson 来源 / S4 LockFileEx 互操作 / S5 Tray 失败模式 / S6 console+windowsHide 闪框）
