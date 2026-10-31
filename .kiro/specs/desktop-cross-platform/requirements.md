# Requirements Document

## Introduction

为 Mindex 桌面版新增 Windows x64 与 Linux x64 支持：安装后双击即用，不依赖用户安装 Python、uv 或 ffmpeg，功能与体验与 macOS 端一致（托盘常驻与后台选题定时、本机服务管理、数据目录选择、随包字体、AI 动画组件按需安装）。macOS（Apple Silicon）现状不回归；桌面版与 Web 版同步演进的约定不变（`desktop/` 只放外壳与打包，业务在后端走 REST）。

决策与实施计划见 [plans/WIN_LINUX_NATIVE_PLAN.md](../../../plans/WIN_LINUX_NATIVE_PLAN.md)；macOS 桌面版背景见 [plans/MACOS_NATIVE_PLAN.md](../../../plans/MACOS_NATIVE_PLAN.md)。

本轮范围：后端跨平台层（文件锁、进程操作）、外壳跨平台（可执行路径、进程管理、托盘兜底）、三平台构建链（ffmpeg 源码编译、sidecar 冻结、编排脚本）、electron-builder win/linux 打包、冒烟与 CI 矩阵、文档。

不在本轮范围：代码签名/公证/自动更新；Windows arm64、macOS x86_64、Linux arm64；打包 GPU 模型服务；随包 `mindex` 命令行；发布自动化（tag→release 聚合）。

## Glossary

- **平台矩阵**：macOS arm64（现状）+ Windows x64 + Linux x64（本轮新增）。
- **process_ops**：后端新模块 `shared/process_ops.py`，进程操作（脱离启动、存活、pid 指纹、树杀）的双平台收口。
- **binaryName**：外壳 `desktop/lib/paths.js` 的可执行名处理（Windows 加 `.exe`）。
- **win-unpacked / linux-unpacked**：electron-builder 的解包目录产物，packaged 冒烟以它为 `MINDEX_SMOKE_APP`。

## Requirements

### Requirement 1: 后端文件锁跨平台

**User Story:** 作为桌面用户，我希望后端在 Windows 上也能用同一套数据目录锁语义，以便管线、索引、创作等互斥保护不因平台缺失。

#### Acceptance Criteria

1. THE `shared/locks.py` 的 `SharedFileLock` SHALL 在 POSIX 上维持 `fcntl.flock` 行为不变（现有测试逐条通过）。
2. WHEN 运行在 Windows，THE `SharedFileLock` SHALL 用 `LockFileEx/UnlockFileEx` 实现共享/独占、非阻塞尝试 + 轮询 + `filelock.Timeout` 语义，锁区域（offset 0、长度 0=到 EOF）与 `filelock` 的 WindowsFileLock 一致以保持互操作。
3. THE `named`/`acquire_ordered`/`check_order`/`ORDER` SHALL 不因平台分支而改变签名或语义（`processing_lock.py` 等调用方零改动）。

### Requirement 2: 后端进程操作跨平台

**User Story:** 作为桌面用户，我希望本机模型服务管理与人物扫描任务在 Windows 上正常启停，以便与 macOS 端能力一致。

#### Acceptance Criteria

1. THE `shared/process_ops.py` SHALL 提供 `spawn_detached`、`alive`、`process_start`、`stop_tree` 四个函数，双平台分支实现，不引入新第三方依赖。
2. THE `local_services.py` 与 `cli_persons.py` SHALL 经 `process_ops` 完成进程启动与停止，业务逻辑与中文错误消息不变。
3. WHEN Windows 上停止进程树，THE `stop_tree` SHALL 用 `taskkill /PID <pid> /T /F`；优雅停止依赖退出前 REST `stop-on-exit`，该取舍 SHALL 记录在设计文档。
4. THE `alive` 在 Windows SHALL 不使用 `os.kill`（其 Windows 语义不同），用 `OpenProcess` + `GetExitCodeProcess` 判断。

### Requirement 3: 外壳跨平台

**User Story:** 作为用户，我希望三平台外壳行为一致：sidecar 启停、端口持久化、认证注入、托盘、下载与退出流程。

#### Acceptance Criteria

1. THE 外壳 SHALL 在 Windows 上解析 `mindex.exe`/`ffmpeg.exe`/`ffprobe.exe` 与 `uv.exe` 路径，spawn 带 `windowsHide`，停止用 `taskkill /T`（软停 → 宽限 → 强杀）。
2. THE packaged 模式的健康检查 pid 校验 SHALL 三平台保持严格（exe 即服务进程）；仅 dev+win32 且父链查询失败时允许放宽为 version 匹配。
3. WHEN Linux 无托盘守护进程导致 `new Tray` 失败，THE 外壳 SHALL 降级：记日志、首次提示一次、关闭窗口即退出应用（不隐藏），不崩溃不假死。
4. THE Windows 外壳 SHALL 设置 `app.setAppUserModelId`，系统通知正确归属应用。
5. THE `tests/desktop_smoke.cjs` SHALL 三平台可跑（platform 断言、file:// 导航用例、杀进程方式按平台分支）。

### Requirement 4: 三平台构建链

**User Story:** 作为维护者，我希望在任一目标平台上用一条命令构建该平台的安装包，ffmpeg 版本三平台同源锁定。

#### Acceptance Criteria

1. THE `scripts/build-desktop-ffmpeg.sh` SHALL 以单一脚本支持 Darwin/Linux/MSYS2 三环境，依赖包版本与 sha256 锁定表三平台同一套；libass 字体后端按平台（CoreText/DirectWrite/显式 font_file）。
2. THE ffmpeg 产物验收 SHALL 按平台检查：mac `otool -L` 仅系统库；Linux 静态或仅系统库；Windows `objdump -p` 仅系统 DLL 白名单（含 `-static-libgcc -static-libwinpthread` 不携带 MinGW 运行时 DLL）。
3. THE `scripts/build-desktop-sidecar.sh` SHALL 支持 mac 与 Linux；Windows 用新增 `build-desktop-sidecar.ps1`；两者调用同一 `mindex.spec`，仅 darwin 传 `target_arch`/`codesign_identity`。
4. THE 编排脚本（`build-desktop-mac.sh` 现状、新增 `build-desktop-linux.sh` 与 `build-desktop-win.ps1`）SHALL 各自完成 字体 → ffmpeg → sidecar → 冒烟 → electron-builder → GPL 源码包 全链。
5. THE `scripts/check-desktop-sidecar.py` SHALL 三平台可跑（Windows backend 路径带 `.exe`、隔离 PATH 用系统目录）。

### Requirement 5: 安装包与分发

**User Story:** 作为用户，我希望 Windows 上有安装器与便携版、Linux 上有 AppImage 与 deb，安装/解包即用。

#### Acceptance Criteria

1. THE electron-builder 配置 SHALL 产出 Windows NSIS（向导式、per-user、可改安装目录、卸载不删用户数据）+ 便携 zip，与 Linux AppImage + deb。
2. THE 三平台安装包 SHALL 均不签名（SmartScreen/Gatekeeper 提示同级），`docs/desktop.md` 写明各平台绕过方法。
3. THE GPL 对应源码包 SHALL 随三平台发布各自附带。

### Requirement 6: CI 三平台矩阵

**User Story:** 作为维护者，我希望 CI 在三平台跑桌面测试与打包，作为默认产出方式。

#### Acceptance Criteria

1. THE `desktop` job SHALL 以三平台矩阵（macos-14 / windows-2025 / ubuntu-24.04）运行外壳单测、桌面相关 pytest 与 dev 冒烟（Linux 用 xvfb）。
2. THE `desktop-package` job（schedule/workflow_dispatch）SHALL 三平台构建安装包并上传 artifact（含 ffmpeg 与 electron 二进制缓存）。
3. THE packaged 冒烟 SHALL 用 `win-unpacked`/`linux-unpacked`（Linux 配 xvfb），不直接 launch AppImage。

### Requirement 7: 文档

**User Story:** 作为用户与维护者，我希望文档覆盖三平台的安装、文件位置、构建与已知限制。

#### Acceptance Criteria

1. THE `docs/desktop.md` SHALL 改为三平台文档（安装分节、文件位置三平台列、构建章节、已知限制更新）。
2. THE `CLAUDE.md`、`README.md`、`docs/README.md`、`THIRD_PARTY_NOTICES.md`、`plans/ROADMAP.md` SHALL 同步更新，不残留"仅 macOS"表述。
3. THE 真机/CI 验证结论 SHALL 记录到 `docs/VERIFICATION.md`。
