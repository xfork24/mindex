# Requirements Document

## Introduction

为 Mindex 提供 macOS（Apple Silicon）桌面应用：DMG 安装后双击即用，功能与 Web 版完全一致，并与 Web 版同步演进。外壳为 Electron；后端仍是现有的 `mindex serve`，以 PyInstaller 冻结的 sidecar 随包分发；窗口加载 sidecar 提供的同一套前端，不维护第二份业务代码。桌面端可以管理本机已安装的模型服务。

决策、调查与取舍见 [plans/MACOS_NATIVE_PLAN.md](../../../plans/MACOS_NATIVE_PLAN.md)（本 spec 的背景资料）。

本轮范围：外壳、后端平台适配（健康接口、二进制定位、冻结兼容、本机服务管理、动画组件安装）、sidecar 与 ffmpeg 构建、ad-hoc 签名 DMG、CI 与文档。

不在本轮范围：Windows、Intel、Developer ID 签名与公证、自动更新、打包 GPU 模型服务、随包提供 `mindex` 命令行。

## Glossary

- **Shell**：Electron 主进程与窗口（`desktop/`）。
- **Sidecar**：Shell 启动的后端进程，运行 `mindex serve`；开发模式为 `uv run mindex`，生产模式为冻结后端 `Resources/backend/mindex`。
- **App_Origin**：`http://127.0.0.1:<端口>`，Sidecar 的监听地址。
- **Session_Token**：Shell 每次启动生成的随机令牌，以 `MINDEX_WEB_TOKEN` 传给 Sidecar。
- **User_Data_Dir**：`~/Library/Application Support/Mindex/`，含 `config.yaml`、`data/`、`fonts/`、`browsers/`、`desktop-state.json`。
- **Local_Service**：配置文件 `local_services.services` 中定义的本机模型服务（WeMM、ASR、TTS 等）。
- **Frozen**：在 PyInstaller 冻结环境中运行（`sys.frozen` 为真，模块没有 `.py` 源码）。

## Requirements

### Requirement 1: 选型与计划文档

**User Story:** 作为维护者，我希望路线图记录桌面端选型改为 Electron 及理由，以便后续决策有据可查。

#### Acceptance Criteria

1. THE `plans/ROADMAP.md` M4 SHALL 推荐 Electron 外壳 + Python sidecar，列出与 Tauri 2 的取舍，并把签名公证与自动更新标为需要 Apple Developer ID 后再做。
2. THE `plans/README.md` SHALL 登记 `MACOS_NATIVE_PLAN.md`，且 `python scripts/check-doc-links.py` 不新增失效链接。

### Requirement 2: 后端健康接口

**User Story:** 作为 Shell，我需要一个轻量的就绪检查，以便确认连到的是自己启动的后端，并在退出前知道是否有任务在运行。

#### Acceptance Criteria

1. THE Sidecar SHALL 提供 `GET /api/health`，返回 `version`、`pid`、`data_dir`、`busy`（布尔）与 `tasks`（运行中的任务类别列表）。
2. WHILE 设置了 `MINDEX_WEB_TOKEN`，THE `/api/health` SHALL 与其他 `/api/*` 一样要求认证（无凭证返回 401，Bearer 正确返回 200）。
3. WHEN studio 执行线程或素材、对白、人物后台任务正在运行，THE `/api/health` SHALL 返回 `busy: true` 并在 `tasks` 中列出对应类别。
4. THE `/api/health` SHALL 不读取素材表或探测模型服务，耗时与素材规模无关。

### Requirement 3: 外壳生命周期

**User Story:** 作为用户，我希望双击应用就能用，不需要手动启动或关闭后端。

#### Acceptance Criteria

1. WHEN 用户启动应用，THE Shell SHALL 启动 Sidecar，等待 `/api/health` 就绪（校验 `pid` 与子进程一致、`version` 与 Shell 一致）后在窗口中加载 App_Origin。
2. THE Shell SHALL 只运行一个实例；再次启动时聚焦已有窗口。
3. THE Shell SHALL 在首次启动时选择空闲端口并持久化到 `desktop-state.json`，之后复用；端口被占用时改用新端口并提示界面偏好会重置。
4. THE Shell SHALL 每次启动生成新的 Session_Token，只经环境变量传给 Sidecar，不写入磁盘或日志。
5. THE Shell SHALL 把 Sidecar 的输出写入 `~/Library/Logs/Mindex/backend.log`，并按大小轮转。
6. WHILE Sidecar 启动中，THE Shell SHALL 显示启动页；IF 启动超时或 Sidecar 意外退出，THEN THE Shell SHALL 显示错误页，提供“重试”和“打开日志”。
7. WHEN 用户退出应用且 `/api/health` 报告 `busy`，THE Shell SHALL 弹窗确认（说明任务会中断、下次启动自动恢复）。
8. WHEN 应用退出，THE Shell SHALL 先请求停止标记为退出时停止的 Local_Service，再向 Sidecar 发 SIGTERM，10 秒内未退出则 SIGKILL；退出后不残留 Sidecar 进程。
9. THE Shell SHALL 提供开发模式：用仓库内的 `uv run mindex` 作为 Sidecar，便于调试。

### Requirement 4: 外壳安全与浏览器行为

**User Story:** 作为用户，我希望桌面应用在功能上与浏览器中的 Web 版一致，同时不会因为页面内容执行本机代码。

#### Acceptance Criteria

1. THE Shell SHALL 只对 App_Origin 的请求附带 Session_Token（Bearer 请求头或会话 Cookie），页面请求、`EventSource`、媒体与下载都能通过认证。
2. THE 窗口 SHALL 启用 `contextIsolation`、`sandbox`，禁用 `nodeIntegration`；preload 只暴露只读的 `window.MindexDesktop`（`version`、`platform`）。
3. THE 窗口 SHALL 只允许导航到 App_Origin；同源的新窗口链接在应用内新窗口打开，http/https 外链交给系统浏览器，其他协议拒绝。
4. WHEN 页面触发下载，THE Shell SHALL 弹出保存对话框。
5. WHEN 文件被拖到页面拖放区之外，THE 窗口 SHALL 不导航到该文件；页面已有的拖放区行为不变。
6. THE 应用菜单 SHALL 包含标准“编辑”菜单（复制、粘贴、全选等快捷键），以及“打开日志目录”“打开数据目录”“选择数据目录…”“重新加载”。
7. THE 前端代码 SHALL 不为桌面端做分支；桌面专属能力只能经 `window.MindexDesktop`，且前端在没有它时保持现有行为。

### Requirement 5: 桌面配置与数据目录

**User Story:** 作为用户，我希望数据保存在系统用户数据目录，并可以改用已有的数据目录。

#### Acceptance Criteria

1. WHEN 首次启动且 User_Data_Dir 中没有 `config.yaml`，THE Shell SHALL 用 `desktop/config.template.yaml` 生成它；之后不覆盖用户的修改。
2. THE 桌面配置模板 SHALL 通过严格配置校验，`data_dir` 解析到 User_Data_Dir 之下，`studio.subtitle.font_file` 解析到 User_Data_Dir 内的随包字体，模型服务地址默认指向本机端口。
3. WHEN 用户在菜单中选择数据目录，THE Shell SHALL 接受空目录或含 `metadata.sqlite3` 的目录，持久化选择，以 `--data-dir` 重启 Sidecar，并提示不要让 Web 端与桌面端同时使用同一数据目录。

### Requirement 6: 外部二进制可配置

**User Story:** 作为桌面端，我需要使用随包的 ffmpeg，而不是依赖 PATH。

#### Acceptance Criteria

1. THE 后端 SHALL 通过 `MINDEX_FFMPEG`、`MINDEX_FFPROBE` 定位 ffmpeg 与 ffprobe，未设置时为 `ffmpeg`、`ffprobe`（行为与现在相同）。
2. THE 后端所有调用 ffmpeg/ffprobe 的位置（经 `shared/media.command` 与直接 `subprocess` 调用）SHALL 使用该定位结果。
3. THE 二进制路径 SHALL 不进入任何阶段签名或缓存键；`tests/test_studio_signature_snapshot.py` 不变。

### Requirement 7: 冻结运行兼容

**User Story:** 作为维护者，我希望冻结后端与源码运行时行为和缓存完全一致。

#### Acceptance Criteria

1. THE 以自身源码文件哈希作为指纹的模块（`models/llama_cpp.py`、`models/model_server.py`）SHALL 在 Frozen 时得到与源码运行时完全相同的哈希，且不得为此修改这些模块（`llama_cpp.py` 的哈希属于嵌入空间身份）。
2. WHEN 冻结后端以 `-m <模块> [参数…]` 启动，THE 入口 SHALL 以 `runpy` 运行该模块；否则运行 `mindex` CLI。现有 `sys.executable -m …` 调用无需修改即可工作。
3. THE `processing/align_worker.py`、前端资源、文案模板 SHALL 在 Frozen 时可以按现有代码路径找到。

### Requirement 8: Sidecar 构建

**User Story:** 作为发布者，我希望一条命令构建出不依赖 uv 的冻结后端。

#### Acceptance Criteria

1. THE `scripts/build-desktop-sidecar.sh` SHALL 在锁定依赖的干净环境中生成 arm64 onedir 冻结后端（含 `animation` extra 的 Python 包，不含 `dev`、`person` extra）。
2. THE `scripts/check-desktop-sidecar.py` SHALL 用临时配置与数据目录启动冻结后端并验证：`/api/health` 就绪、五个页面返回 200、`-m` 分发、注册并 prepare 测试视频、文案模板非空、pyproject package-data 都已打包。

### Requirement 9: ffmpeg 与字体随包

**User Story:** 作为用户，我希望在没有安装 ffmpeg 的 Mac 上也能渲染带烧录字幕的成片。

#### Acceptance Criteria

1. THE `scripts/build-desktop-ffmpeg.sh` SHALL 从锁定版本、校验 sha256 的源码编译 arm64 的 ffmpeg/ffprobe，包含 libx264 与 libass，且只依赖 macOS 系统库。
2. THE 发布物 SHALL 附带对应源码与构建脚本，`THIRD_PARTY_NOTICES.md` SHALL 写明 ffmpeg、x264、libass 及依赖与 Noto Sans CJK 字体的许可。
3. WHEN 首次启动，THE Shell SHALL 把随包字体复制到 User_Data_Dir 的 `fonts/`，并为 Sidecar 设置 `MINDEX_FFMPEG`、`MINDEX_FFPROBE`。
4. WHEN PATH 中没有 ffmpeg，THE 冻结后端 SHALL 通过 `check_font` 并合成带烧录字幕的成片。

### Requirement 10: DMG 安装包

**User Story:** 作为用户，我希望通过 DMG 安装后即可使用。

#### Acceptance Criteria

1. THE `scripts/build-desktop-mac.sh` SHALL 产出 arm64 的 `.dmg` 与 `.zip`，应用版本号与 `pyproject.toml` 的 `version` 一致。
2. THE `.app` 内全部 Mach-O SHALL 经 ad-hoc 签名，`codesign --verify --deep --strict` 通过。
3. WHEN 从 DMG 安装到 `/Applications` 并启动，THE 应用 SHALL 不依赖 uv、Homebrew ffmpeg 或仓库目录，五个页面可用。
4. THE `docs/desktop.md` SHALL 说明未公证应用的首次打开方式。

### Requirement 11: 本机服务管理（后端）

**User Story:** 作为用户，我希望在应用里启停本机已安装的模型服务并查看状态，而不必在终端运行脚本。

#### Acceptance Criteria

1. THE 配置 SHALL 支持 `local_services.enabled`（缺省 false）与 `local_services.services`（名称、角色、argv、cwd、env、端口、健康路径、autostart、stop_on_exit），严格校验。
2. THE 后端 SHALL 提供列表（含运行状态与健康）、启动、停止、日志尾部、修改 `autostart`/`stop_on_exit`、批量“退出时停止”接口；IF `local_services.enabled` 为 false，THEN 这些接口返回 403。
3. THE 接口 SHALL 不允许修改 `argv`、`cwd`、`env`；只能运行配置文件中预先定义的命令。
4. THE 后端 SHALL 以独立进程组启动服务，日志写入 `data_dir/logs/services/<name>.log`，停止时终止整个进程组；后端重启后 SHALL 重新识别仍在运行的服务。
5. WHEN `serve` 启动，THE 后端 SHALL 在后台拉起 `autostart` 的服务。
6. THE 智能助手 SHALL 提供只读的本机服务列表工具与 danger 级别的启动、停止工具。

### Requirement 12: 本机服务管理（界面）

**User Story:** 作为用户，我希望在设置页看到并操作本机服务。

#### Acceptance Criteria

1. THE 设置页 SHALL 显示“本机服务”区块：名称、端口、运行状态、健康、启动/停止、日志尾部、自启与退出时停止开关；功能未开启时显示说明。
2. THE 区块文案 SHALL 在 `messages.json` 中提供中英文，并通过 `build-messages.py --check` 与 `build-assets.py --check`。

### Requirement 13: AI 动画录制组件按需安装

**User Story:** 作为桌面用户，我希望第一次使用 AI 动画录制时一键安装所需浏览器。

#### Acceptance Criteria

1. THE Shell SHALL 为 Sidecar 设置 `PLAYWRIGHT_BROWSERS_PATH` 为 User_Data_Dir 的 `browsers/`。
2. THE 后端 SHALL 提供后台安装 Chromium 的接口（状态、日志、成功/失败），在 Frozen 时经 `-m playwright` 分发运行。
3. WHEN 动画录制因缺少浏览器失败，THE 页面 SHALL 提供前往安装的入口。

### Requirement 14: CI、性能与文档

**User Story:** 作为维护者，我希望桌面端持续可构建、性能不劣于 Web 版、约定写进项目文档。

#### Acceptance Criteria

1. THE CI SHALL 新增 Apple Silicon macOS 任务，运行快速 pytest、`node --test desktop/test` 与 Electron 开发模式冒烟；定时任务构建 sidecar、ffmpeg 与 DMG 并冒烟。
2. THE `scripts/desktop-bench.py` SHALL 按 ROADMAP 指标对比桌面版与同机 Web 版，结果与未能在本机完成的指标记入 `docs/VERIFICATION.md`。
3. THE `docs/desktop.md`、`CLAUDE.md`、`AGENT.md`、`README.md` SHALL 说明桌面架构与同步演进约定（`desktop/` 不放业务逻辑、ffmpeg 经 `ffmpeg_bin()`、自哈希模块登记在 `SOURCE_MODULES`、新增包数据须同步 spec）。
