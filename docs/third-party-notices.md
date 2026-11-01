# 第三方组件声明

仓库内随前端分发的第三方文件。文件按原样复制自 npm 公共仓库的发布包，未做修改；升级时请同步更新本表与 `docs/agent.md`，并运行 `python scripts/build-assets.py` 更新页面里的资源版本号（`?v=`）。

| 文件 | 组件与版本 | 许可证 | 来源（npm 包内路径） | 发布包 integrity | 文件 SHA-256 |
|---|---|---|---|---|---|
| `frontend/src/shared/marked.umd.js` | [marked](https://github.com/markedjs/marked) 18.1.1 | MIT | `marked-18.1.1.tgz` → `package/lib/marked.umd.js` | `sha512-zEGgrtUG8crk+TrMacJSVABU4DYDDkS8uqgU3ohrn/2fW9C2AEfUrO3m6IhpIpQpl7YcVpiXXC9cSMWDANT3dg==` | `b82b9334406d8bdd16b6236c08de1cb65b2a859873ca9503d9cc0b292ca3c12a` |
| `frontend/src/shared/purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.17 | MPL-2.0 OR Apache-2.0（本项目按 Apache-2.0 使用） | `dompurify-3.4.17.tgz` → `package/dist/purify.min.js` | `sha512-B/pSlYVHQ58kqISD/yDjuJ1yfzI34m1gF4lAod2bwSRhiN4K5Cl3H8Af7hqB2PmTL911tdEudnnXn5FnhRoz0w==` | `cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31` |

两个文件都保留了原始的版权与许可证头注释。用途：智能助手面板把模型回复的 Markdown 用 marked 转成 HTML，再经 DOMPurify 按白名单净化后显示（见 `frontend/src/shared/agent-markdown.js`）。

许可证全文：

- marked（MIT）：Copyright (c) 2018+, MarkedJS；Copyright (c) 2011-2018, Christopher Jeffrey。全文见 <https://github.com/markedjs/marked/blob/v18.1.1/LICENSE.md>。
- DOMPurify（Apache-2.0 / MPL-2.0）：Copyright (c) Cure53 and other contributors。全文见 <https://github.com/cure53/DOMPurify/blob/3.4.17/LICENSE>。

## 桌面版随包组件

以下组件只随桌面版安装包（macOS `Mindex.app`、Windows/Linux 安装目录）分发，不在仓库中。构建脚本见 `scripts/build-desktop-ffmpeg.sh`（三平台同一张源码锁定表）、`scripts/build-desktop-sidecar.{sh,ps1}` 与 `scripts/build-desktop-{mac,linux}.sh`、`scripts/build-desktop-win.ps1`。

### FFmpeg 与 ffprobe（GPL 构建）

随包的 `ffmpeg`/`ffprobe`（macOS `Contents/Resources/bin/`、Windows/Linux `resources/bin/`，Windows 带 `.exe` 后缀）由 `scripts/build-desktop-ffmpeg.sh` 从源码编译，静态链接下列库；因为包含 libx264，整个构建按 **GNU GPL v2 或更高版本** 分发。

- libass 的字体后端按平台：macOS CoreText、Windows DirectWrite（均为系统自带），Linux 不启用系统字体后端（字幕字体由 `studio.subtitle.font_file` 显式指定）。三平台都不链接 fontconfig。
- Windows 构建经 MSYS2/MinGW（UCRT64）并以 `-static -static-libgcc -static-libwinpthread` 全静态链接 MinGW 运行时，产物只依赖 Windows 系统 DLL，不携带任何 MinGW 运行时库。

| 组件 | 版本 | 许可证 | 来源 |
|---|---|---|---|
| FFmpeg | 9.0.2（`--enable-gpl`） | GPL-2.0-or-later（含 GPL 组件时） | https://ffmpeg.org/ |
| x264 | stable 分支提交 `b35605ace3ddf7c1a5d67a2eb553f034aef41d55` | GPL-2.0-or-later | https://code.videolan.org/videolan/x264 |
| libass | 0.17.5 | ISC | https://github.com/libass/libass |
| FreeType | 2.15.0 | FreeType License（FTL） | https://freetype.org/ |
| FriBidi | 1.0.17 | LGPL-2.1-or-later | https://github.com/fribidi/fribidi |
| HarfBuzz | 15.0.0 | MIT（“Old MIT”） | https://github.com/harfbuzz/harfbuzz |
| dav1d | 1.5.4 | BSD-2-Clause | https://code.videolan.org/videolan/dav1d |

对应源码：每个桌面版发布都附带 GPL 源码包（macOS/Linux 为 `mindex-desktop-ffmpeg-sources-<版本>.tar`，Windows 为 `.zip`），内含上表全部源码包（版本与 sha256 固定在构建脚本中）以及 `build-desktop-ffmpeg.sh`（Windows 包另含 `build-desktop-win.ps1` 与 `build-desktop-sidecar.ps1` 副本），可完整重现随包二进制。ffmpeg 与 FreeType 源码包已核对官方 GPG 签名。

### Noto Sans CJK SC 字体

`Mindex.app/Contents/Resources/fonts/NotoSansCJKsc-Regular.otf`（Noto CJK Sans 2.004，© Google / Adobe），按 **SIL Open Font License 1.1** 分发，许可全文随字体附带（`LICENSE-NotoSansCJK.txt`）。来源：https://github.com/notofonts/noto-cjk 。首次启动时复制到 `~/Library/Application Support/Mindex/fonts/`，用作烧录字幕字体。

### 冻结后端与外壳

- DeepSeek Harness SDK 与原生运行时 `0.2.1rc2`（MIT）随三平台后端提供，包含 Node SEA 可执行文件、ripgrep、macOS spawn-helper 与轻量技能资源。上游及所含第三方组件许可收集在 `deepseek_harness_{sdk,runtime_bin}-*.dist-info/licenses/`，保留上游 `third-party-notices.md`。来源：https://github.com/deepseek-ai/deepseek-harness 。
- 后端以 PyInstaller（GPL-2.0 with bootloader exception；冻结产物不受 GPL 约束）冻结，内含 CPython（PSF License）与 `pyproject.toml`、`uv.lock` 锁定的 Python 依赖，各依赖的许可随其包元数据（`*.dist-info`）一同打包。
- 外壳为 Electron（MIT，内含 Chromium，BSD-3-Clause 及其第三方组件），许可文件位于 `Mindex.app/Contents/Resources/`（`LICENSE`、`LICENSES.chromium.html`）。
- AI 动画录制所需的 Chromium（Playwright 构建）不随包分发，首次使用时下载到用户数据目录。
