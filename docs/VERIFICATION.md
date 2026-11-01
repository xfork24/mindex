# 实现验证记录

> 历史记录，内容可能与当前代码不符，以当前源码、CLI 帮助与本次实际运行的测试为准。

## 跨平台桌面版验证（2026-11-01）

任务见 `.kiro/specs/desktop-cross-platform/`；计划与决策见 `plans/WIN_LINUX_NATIVE_PLAN.md`。目标平台为 Windows x64 与 Linux x64，功能与 macOS 端一致。

### 本机（Apple M1）侧

- 后端跨平台层：`shared/locks.py` 的 POSIX flock 分支行为逐字节不变（`tests/test_locks.py`、`tests/test_processing_lock.py` 全绿，含与 `filelock` 的互操作用例）；新增 `shared/process_ops.py`（脱离启动、存活、pid 指纹、整树停止）的 POSIX 用例用真实子进程验证（`tests/test_process_ops.py`）。
- 外壳跨平台：`desktop/lib/` 的 `binaryName`、`sidecar.js` 的 Windows 分支（PowerShell 进程查询、taskkill 树杀）、`main.js` 的 `windowsHide`/`AppUserModelId`/托盘不可用降级，均有可在 macOS 上运行的注入式单测（`node --test desktop/test` 27 通过）。
- 构建链：`scripts/build-desktop-mac.sh` 全链零回归（重构后的三环境 ffmpeg 脚本 Darwin 分支：源码编译 + `otool -L` 链接验收 + 冻结后端 + 冻结冒烟含中文字幕烧录 + electron-builder + ad-hoc 签名），产出 `Mindex-0.1.0-arm64.dmg`（324 MB）、`.zip`（315 MB）与 GPL 源码包（37 MB）；对打包产物跑 packaged 冒烟（`MINDEX_SMOKE_APP=…/Mindex.app/Contents/MacOS/Mindex`）全部通过。
- 回归：快速 pytest 1964 通过（含 `person` extra 的人物库用例）；ruff、node --check、messages/assets/文档链接校验通过。

### GitHub Actions 三平台矩阵（CI 运行 41268349954）

`desktop` job 在 macos-14 / windows-2025 / ubuntu-24.04 三个平台**全部通过**：外壳纯逻辑单测、桌面相关后端 pytest（28 通过）、Electron 开发模式冒烟（真实外壳 + 真实 `mindex serve`）。

Windows 首轮暴露并已修复的四类问题（均只在 Windows 出现，macOS/Linux 不可见）：

1. **CI 步骤写法**：`node --test test`（裸目录）在 Node 22 不支持（`MODULE_NOT_FOUND`），改为引号包裹的 glob；冒烟步骤混合 bash/pwsh 语法，改为统一 `shell: bash`。
2. **既有单测的平台假设**：POSIX 信号注入断言加 win32 守卫；真实子进程用例的 `/bin/sh` 改用 `process.execPath` + node 脚本。
3. **Windows 文本模式的换行转换**：子进程输出经文本模式把 `\n` 转成 `\r\n`，而进度行解析用 `rstrip("\n").split("\r")[-1]` 会在尾部多出的 `\r` 上切出空段，导致浏览器组件安装的进度日志整条丢失（安装状态与退出码正常，唯独无输出）。改为 `rstrip("\r\n")` 并抽出 `progress_line()` 供回归测试（Windows CRLF 用例在 macOS 上也可跑）。这是生产问题，不止测试。
4. **Windows 短路径展开**：Electron 的 `app.setPath` 与后端 `Path.resolve()` 会把 8.3 短路径（`RUNNER~1`）展开为长路径（`runneradmin`），与 Node 侧 `realpathSync` 的短路径形式不等；冒烟脚本的两处路径断言改为比较目录名 + 存在性。

另外，生产侧的 `PYTHONUTF8=1`（外壳注入 + CI pytest 步骤）修掉了 Windows 上 Python 按控制台代码页编码输出中文日志的 `UnicodeEncodeError`。

### 待完成

- `desktop-package`（定时/手动触发，三平台）：从源码编译随包 ffmpeg、冻结 sidecar、冻结冒烟、electron-builder 出安装包、安装后 packaged 冒烟与 artifact 上传——结果待该 job 跑完后补记。
- spike S1–S6（Windows 依赖解析、Linux libass 字体后端、MSYS2 meson 来源、LockFileEx 与 filelock 互操作实测、Linux 托盘失败模式、Windows console+windowsHide 闪框）的实机结论，回填 `.kiro/specs/desktop-cross-platform/design.md` §7。

## macOS 桌面版验证（2026-10-25）

环境：Apple M1、16 GB、macOS 26.7.1；Electron 45.0.0、PyInstaller 6.22.3、CPython 3.13；随包 ffmpeg 9.0.2（源码编译，GPL）。任务见 `.kiro/specs/macos-desktop/`。

- 安装包：`scripts/build-desktop-mac.sh` 生成 `Mindex-0.1.0-arm64.dmg`（324 MB）与 `.zip`（314 MB）、GPL 对应源码包（53 MB）；`.app` 761 MB，172 个随包 Mach-O 经 ad-hoc 签名，`codesign --verify --deep --strict` 通过。从 DMG 安装到 `/Applications` 后，在 `PATH=/usr/bin:/bin`（无 uv、无 Homebrew）下 Electron 冒烟全部通过：五个页面与模板子页、页面请求与 SSE 认证、下载、同源新窗口、阻止导航到本地文件、后端被杀后错误页与重试、端口持久化、所选数据目录、退出后无残留进程。经 Launch Services 打开与 AppleScript 退出正常；覆盖安装后第一次 `open` 出现过一次 “Launch failed”，`lsregister -f` 后正常（已写入 desktop.md）。
- 冻结后端（`check-desktop-sidecar.py --isolated-path --ffmpeg-dir … --font … --animation`）：chromadb 写入与查询、jieba、opencv/scenedetect、Pillow、playwright、前端与文案模板、align_worker、`llama_cpp.py`/`model_server.py` 源码哈希与仓库一致、pyproject package-data 全部打包、`-m` 分发、prepare 测试视频、serve 健康与页面、SIGTERM 退出、中文字幕烧录、经 `-m playwright` 安装 Chromium headless shell 并录制 AI 动画。
- 随包 ffmpeg：`subtitles`（libass，CoreText 字体后端）、libx264、libdav1d 均存在，只链接系统库与框架（minos 13.0）；用随包 Noto Sans CJK SC 实际烧录中文字幕帧。把它放在 PATH 最前面运行快速 pytest（1343 通过）与媒体慢测（28 通过）。
- 本机服务：在桌面开发模式中经设置页“本机服务”用 `scripts/run-wemm.sh` 启动 WeMM，8 秒后健康；现有 Web 后端（:8000，真实素材库）文字检索返回 200；退出应用后 WeMM 进程停止、8888 端口关闭。
- 回归：快速 pytest 1357 通过（排除缺少 `person` extra 的 `test_person_frame_pruning`，与本次改动无关）；相关慢测 109 通过、7 跳过；`node --test desktop/test` 17 通过；ruff、文档链接（仅 `.kiro/specs/studio-storyboard-strip/design.md` 既有一处）、messages/assets 校验通过。

性能对比（`uv run python scripts/desktop-bench.py`；Web 版 = 未冻结的同一依赖环境 + Homebrew ffmpeg，桌面版 = 冻结后端 + 随包 ffmpeg；计时类指标两者交替运行）：

| 指标 | Web 版 | 桌面版 | 差异 | 说明 |
|---|---|---|---|---|
| 启动到 `/api/health` 就绪 | 1.16 s | 1.18 s | +1.7% | 后端部分；外壳窗口加载另计 |
| prepare（120 秒 1080p 合成视频） | 47.3 s | 41.2 s | −12.8% | 交替 2 轮取最小 |
| 成片编码（120 秒 1080p libx264 + 烧录中文字幕） | 18.98 s | 18.89 s | −0.5% | 交替 3 轮中位数 |
| AI 动画录制（10 秒 25 fps 1280×720） | 4.70 s | 4.70 s | 0% | 交替 5 轮中位数，同一浏览器 |
| 后端空闲常驻内存 | 183.5 MB | 193.2 MB | +5.3%（+9.7 MB） | 略超 5% 门槛 |
| 文字检索 P50 / P95（5359 片段真实库，本机 WeMM） | 414 / 1174 ms | 338 / 977 ms | −18% / −17% | 顺序运行（Web 先），桌面受益于嵌入服务预热；只说明没有劣化 |
| 检索后常驻内存 | 564 MB | 586 MB | +3.8% | |

另测得已安装应用空闲时 Electron 四个进程共 444 MB、后端 196 MB；Web 版对应的是用户浏览器的开销，未纳入对比。

未完成或偏离 ROADMAP 原定规模的项：prepare 用 120 秒合成视频代替 1 小时真实 1080p；检索库为 5359 片段而非 1 万；成片编码用单条 120 秒 1080p 编码代替 30 镜 3 分钟构建；没有渲染中的内存曲线。GitHub Actions 的 macOS 任务（`desktop`、`desktop-package`）已写入 `ci.yml`，需推送后在 runner 上确认。签名公证与自动更新（需要 Apple Developer ID）未实施。

## 2026-10-13 记录

验证日期：2026-10-13。客户端环境为 macOS / Apple Silicon、Python 3.11.16、uv、FFmpeg/ffprobe。GPU 模型运行在用户的远程 16GB CUDA 主机；强制对齐使用本机独立 CPU 环境。

## 真实模型端到端增量验证

本节更新此前“GPU 待验收”的历史状态；实时阶段仍以 `data/e2e/trailer-20261013/run.json` 为准。

| 阶段 | 已观测结果 | 证据 |
|---|---|---|
| WeMM 协议 | 首次健康 ok 但嵌入返回 501；加入 `--embeddings --pooling last --video-fps 4` 后成功 | `data/e2e/wemm-text-probe.json`、部署清单 |
| WeMM 能力 | 六个真实片段均为 4096 维有效单位向量；内容敏感、重复稳定、空间稳定 | `smoke.json`，13.87 秒 |
| trailer 画面索引 | 61 个 moment /61 个视频向量，索引 `948e6b40442b0230cbc529f4`，检查通过 | `visual-latest.json`，100.14 秒 |
| 断点/重跑 | 同规格重跑索引不重新推理，版本不变 | `resume-check.json` |
| 中文相关性 | 用户确认 20 条中文标注（8 dev/12 test）；冻结文件与确认人已保存 | `frozen-queries.reviewed.jsonl` 及对应 manifest |
| 画面基线质量 | test：Hit@1 66.7%、Hit@5 91.7%、MRR@10 0.778、nDCG@10 0.813；dev：Hit@1/5 100% | `evaluation-baseline-replayed-reviewed.json`；按确认目标回顾计分，单 trailer 冒烟 |
| 视频预览 | 12 条 self/crop/crosscut/codec 查询均完成，首位均落在派生目标区间 | `video-engineering-check.json`；属于已知时间映射的工程自检 |
| ASR | Qwen3-ASR Q8_0 处理 4 个真实音频块，原始文本与响应均保留 | `asr-latest.json`，7.50 秒 |
| CPU 对齐 | 固定 ForcedAligner commit，独立 CPU 环境完成；有效时间范围映射回原片 | `asr-alignment-check.json`，37.28 秒；准确度待人工听辨 |
| Qwen 视觉 | kvmem 部署的 3 个真实片段通过图片输入与结构化描述检查 | `analyze-smoke-latest.json`，234.87 秒；批量分析运行中 |
| 新增电影索引 | 2,336 个 moment/向量，4096 维、有限、单位范数、元数据与媒体一致 | `data/e2e/whole-film-index-check.json` |

WeMM 与 ASR 的 GGUF/mmproj SHA256 由用户在部署机计算并固定。Qwen 的两个文件哈希及 `kvmem-v0.16.0-rc3` 启动信息也已保存；服务未暴露 GGUF 量化及真实 build 提交，明确记录为未暴露，不把脚本名 `iq3` 视作量化证明。

真实浏览器已走通中文输入 → 视频排名 → 片段播放（含音频）及详情；“骑自行车的人靠近装着大桶的手推车”第一位是约 68.4–69.2 秒片段。网页也实际上传降码率查询视频并得到结果。上下文请求返回 200/206。结果新增“管理此片段”链接及后台定位；后台首次请求出现 `Failed to fetch`，手动刷新后素材列表恢复，定位流程将在独立测试版本继续验证。

新增指定索引的查询/服务入口和默认不发布的实验构建，允许 trailer 的 visual/enriched 对比与完整电影活动库并存。自动化覆盖固定索引查询、媒体 Range 及活动指针保持独立。最近全量工程检查为 **43 passed**（47.86 秒）；Ruff 检查、sdist/wheel 构建通过。

中文标注已由用户确认，SHA256 为 `e3bf72a3d3199f99682b5fb5532c1e4c89217c1843bbe8a12cbd2e042ea1fb3f`。画面基线使用原有真实预览计分：全部文字完全相同、61 个候选和不可变索引一致，使用修订后的确认目标，未重新请求 WeMM，时延仍是历史调用。最终质量验收仍需对白听辨、GPU 数据和后续融合对比；视频相关性草稿尚未确认。未确认草稿只能生成 preview，不能生成质量分数。

## 图片找视频增量验证

新增图片查询 Protocol、两个模型后端的图片适配、`POST /api/search/image` 和页面“图片找视频”入口。支持 JPEG/PNG/WebP 静态图片；实际解码校验、EXIF 方向、透明背景、像素及上传大小限制在 CPU 执行，查询后清理临时文件。原生 llama.cpp 图片方法单独扩展，原文字/视频适配器文件哈希仍为 `a73d18552c62e5ca764af00182a0b0a72321a4605bc6dd4e1fc934ecfe6d0939`，现有空间与索引不变。

工程测试覆盖三个格式到视频结果、Range 播放、无视文字融合权重的图片画面召回、上传伪装/损坏/空文件/超限拒绝、图片方向与透明度、动画拒绝、原生请求模板、HTTP 模型协议、图片标注哈希冻结和评测入口。新增 ASR 对照测试同时验证被排除的 Qwen 描述/风格不会通过 BM25 泄漏。

真实 Chrome 已显示图片入口，上传 `image-queries/01.jpg` 并显示预览。此时远程仅 Qwen 运行，实际点击搜索后 API 返回 503，页面明确显示 WeMM 服务调用失败并恢复按钮，未返回模拟结果。准备了 12 张原片派生图片，待 Qwen 批处理结束、用户切回 WeMM 后执行真实图片召回；尚不宣称图片质量验收通过。

8000 端口已重新启动加载新 API，保留完整电影活动索引；8001 端口是固定 trailer 画面索引的独立测试服务。Qwen 分析进程未受影响。Qwen 收益与海量视频成本评估细则见 [QWEN_BENEFIT_EVALUATION.md](QWEN_BENEFIT_EVALUATION.md)。

## Trailer 分镜与素材后台验收

本次实际处理 `data/verification/film-toolchain/trailer.mp4`，约 117.083333 秒、1280×720、24fps、有音轨。使用默认本地 AdaptiveDetector（adaptive_threshold=3.0、min_content_val=10.0、min_scene_len=1 帧），生成 **61 个镜头及 61 个精细片段**，包含 6 个不足一秒的镜头。最长镜头 5.625 秒，因此所有精细片段均为完整短镜头，没有额外按窗口拆分。

来源 ID：`24699348d2317442e77bb6e7dde4149713bf5796547c38254ebb144851d0f26b`。素材导入默认正式库 `data/`，切片分别位于 `clips/<来源ID>/shot/` 和 `moment/`；清单与离线检查页位于 `reports/<来源ID>/`。本次没有执行嵌入或发布活动索引，原合成样片继续保留在隔离测试库。

逐一检查全部 **122 份 MP4** 与缩略图：音轨保留，无缺失文件，完整连续覆盖 `[0,117.083333]`；片段视频时长与计划的最大误差约 **0.000001 秒**。检查结果保存为 `data/verification/trailer-media-check.json`。额外抽检首镜头、快速剪辑短镜头、标题和片尾黑场缩略图，保留短镜头及原片结尾；这不代表所有切点均已逐帧人工标注。抽检图保存在 `data/verification/trailer-short-tail-check.jpg`。

**用户流程**：后台名称搜索 → FastAPI 管理接口 → SQLite 素材/片段及整理记录 → 分页结果、编辑回显与媒体预览。

| 边界 | 结果与证据 |
|---|---|
| 页面加载 | `/admin`、`admin.js`、`admin.css` 实际 HTTP 请求均为 200，Chrome 显示素材列表和分镜缩略图 |
| 名称搜索与分页 | 输入 `trailer` 找到素材；API 返回 61 个镜头，第一页 48 条、第二页 13 条 |
| 编辑与持久化 | 浏览器实际保存临时名称及备注，素材列表和详情即时更新；SQLite/API 持久化正常 |
| 收藏、隐藏与恢复 | 浏览器收藏回显成功；隐藏素材后默认列表和片段列表均为空，恢复后重新显示 61 镜头 |
| 父子关联 | 浏览器点击镜头，显示对应 1 个精细片段；返回父镜头切回镜头列表并定位目标 |
| 媒体播放 | 浏览器实际播放精细片段，出现视频画面、播放进度和音频播放状态；HTTP Range 返回 206/128 字节 |
| 隐藏对检索的影响 | 自动化测试覆盖文字及视频召回前排除、父级隐藏继承、恢复时保留自身状态 |
| 索引兼容 | 自动化验证整理操作不改变索引 ID、空间身份、冻结快照或向量，重建相同索引不重复嵌入 |

浏览器扩展连接未能加载请求策略，改用已授权的原生 Chrome 窗口完成以上交互验证。临时名称、备注、收藏和隐藏状态均已恢复；当前素材名仍为 `trailer.mp4`，未隐藏、未收藏。

最终检查：**26 passed**（28.34 秒），有一项上游 TestClient 弃用警告；Ruff 检查与格式检查通过，两个页面的 JavaScript 语法检查通过，sdist/wheel 构建成功并确认包含管理页面资产与新增模块。新增管理测试同时覆盖旧数据库升级、非法编辑字段、404、分页边界、缺失媒体、父镜头归属和无活动索引访问。

## 电影工具链增量验证

用户确认 WeMM、ASR、TTS 已部署，地址分别为 `192.168.1.86:4010`、`:4020`、`:4030`。客户端默认配置和内置服务的默认端口已更新；TTS 本阶段不参与处理。

新增独立的 `detect`、`split`、`inspect`、`export` 和 `index --mode visual`。完整测试 **21 passed**，26.40 秒；Ruff 与 JavaScript 语法检查通过，wheel/sdist 构建成功。新增测试覆盖 MKV 输入、计划先保存、编码中断后不重新检测、缺失文件修复、JSON/CSV/检查页导出、无 ASR/Qwen 的画面索引，以及通过模型服务 HTTP 序列化与视频上传的完整索引/查询流程。GPU 推理仍使用明确的测试适配器。

另以实际命令处理 65.023 秒带音轨的合成 MKV，生成 **3 个镜头、22 个精细片段、25 份 MP4 和缩略图**；`inspect` 显示切片完成且无缺失文件。清单与分页检查页保存在 `data/verification/film-toolchain/library/reports/981ae56a27b50fc2a602470b2c5a88918193e251d818519587f4e4817f998c58/`。该目录独立于正式素材库。导出页脚本已通过 Node 语法检查。

本机探测新的 WeMM `:4010` 时，`/health` 与 `/openapi.json` 返回 502，`/v1/models` 超时。因此已部署模型的真实接口、量化配置与视频推理仍待服务可达后联调；不能把测试适配器结果视为真实电影嵌入效果。操作步骤见 [电影处理指南](FILM_WORKFLOW.md)。

新增导出页已检查 HTML 内容、媒体路径及脚本语法，本次未完成其浏览器可视化验收：原生 Chrome 窗口被用户操作切换，独立标签页工具的请求头策略加载失败。初次检索网页的播放验证记录见下文，不将其视为新增导出页的验收结果。

## 初次实现通过的本地检查

| 检查 | 结果与范围 |
|---|---|
| `uv sync --extra dev` | 安装成功，依赖已记录在 `uv.lock` |
| `uv run pytest -q` | **16 passed**，21.40 秒；有一项上游 Starlette TestClient 的弃用警告 |
| `uv run ruff check .` | 通过 |
| `uv run ruff format --check .` | 24 个 Python 文件格式通过 |
| `node --check frontend/src/search/app.js` | 通过 |
| `uv build` | sdist 与 wheel 构建成功；确认 wheel 包含 HTML、CSS、JS 和 favicon |

自动化测试使用真实 FFmpeg、PySceneDetect、SQLite、Chroma 和 FastAPI。模型部分采用确定性测试适配器；测试向量为 8 维，**仅用于工程测试**，实际默认配置始终是 WeMM / 4096 维。

覆盖的关键行为：

- 逐帧镜头检测保留 1 秒短镜头；单帧、奇数分辨率片段可以切片、取缩略图及分析采样帧。
- 长镜头窗口覆盖尾部，镜头、精细片段和场景均有实际媒体文件。
- 重跑不重复调用已完成分析或嵌入；30 秒 ASR 块失败后能从检查点恢复，并正确偏移时间戳。
- 文字和视频查询、元数据筛选、RRF 融合、重复窗口去重及同分排名处理。
- 模型量化改变产生新空间与独立索引，禁止跨空间查询；发布、回退和中断建索引恢复正常。
- 无索引、空查询、无效上传和缺失媒体提供明确错误；查询临时视频清理正常。
- HTTP Range、媒体 HEAD、片段详情、缩略图及上下文播放接口。
- 模型服务请求/响应协议、鉴权、向量维度/范数/空间身份校验，以及相关组 nDCG 重复奖励限制。

## 浏览器与真实 HTTP 验证

通过 Chrome 原生界面验证测试库页面：输入“红色画面”，显示 4 个按相关性排序的结果，第一条是 0–1 秒的红色片段；点击播放后显示红色视频画面、1 秒时长及播放进度。也检查过片段详情弹窗。测试素材名明确标记为“工程测试专用_颜色样本.mp4”。这证明页面到 API、数据库、结果展示和媒体播放的链路正常，不代表真实模型的语义效果。

测试库隔离保存在 `data/verification/browser-final/`，没有写入正式素材库。实际 HTTP 检查结果：

- 正式服务 `/api/status` 返回 200，空素材库且没有活动索引。
- 测试库文字查询返回 200；片段、缩略图和上下文的 HEAD 均返回 200 及正确媒体类型。
- 上下文 `Range: bytes=0-127` 返回 206、128 字节和合法 `Content-Range`。
- 浏览器发现的媒体 HEAD 问题已修复；上下文接口按需生成 H.264/AAC MP4，避免原视频容器不受浏览器支持。

可重现工程界面验证（不用于评估模型）：

```bash
uv run python tests/serve_fixture.py --data-dir data/verification/browser-new --port 8001
```

打开 `http://127.0.0.1:8001`。该测试脚本显式打印确定性测试向量提示；主程序不会回退到它。重复运行时使用相同未改动的配置，或另选测试目录。

## 已有 Qwen 服务联调

`http://192.168.1.86:18200/v1/models` 返回 200。已有服务的模型 ID 为 `sha256-1a3cf19ae0825ca8a485cabd595ee149ddd5b7b75381c9df96ca6e76ccd33d19`，应用配置使用用户提供的别名 `qwen3.8:27b`。

实际通过 `QwenAnalyzer` 上传合成红色视频的有序采样帧，请求 `/v1/chat/completions`，得到可通过结构化校验的中文描述、纯色场景、固定镜头及不确定性说明。联调结果保存在本机忽略目录 `data/verification/qwen-smoke.json`。这只验证服务兼容与结构化输出，不验证复杂动作、情绪判断或长视频场景分组质量。

## 需要 GPU 与真实素材完成的验收

本次检查中，待部署 WeMM `:18201` 和 ASR `:18202` 的健康请求超时。未完成真实 WeMM、ASR 或 TransNetV2 的推理验收，也没有真实素材与人工相关性标注。因此以下结果保持未验证状态：

1. WeMM 的 INT8/NF4 加载兼容性、4096 维实际输出、16GB 显存峰值与推理延迟。
2. 中文素材上文字找视频的 Hit@5 / nDCG@10，以及融合相对单路的提升。
3. ASR 时间对齐准确度、真实短镜头分镜召回率与 Qwen 情绪/动作描述质量。

[部署说明](DEPLOYMENT.md) 提供独立环境、服务启动命令、互斥调度及验收步骤。先运行 `mindex model-check` 验证真实文字/视频空间身份与输出，再导入实际素材，按 [评测说明](../eval/README.md) 标注并执行固定的 40 条文字测试集。Hit@5 ≥85% 是验收目标，尚无实测分数。

内置 WeMM 调用形式与[官方模型卡](https://huggingface.co/tencent/WeMM-Embedding-9B)核对一致；量化运行是否可用仍以目标 GPU 实测为准。ASR 采用[官方 Python 包的时间戳接口](https://github.com/QwenLM/Qwen3-ASR)。
