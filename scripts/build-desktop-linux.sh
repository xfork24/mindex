#!/usr/bin/env bash
# 构建 Linux 桌面版安装包（x64）：desktop/dist/Mindex-<版本>-x64.{AppImage,deb}，
# 以及 GPL 对应源码包 desktop/dist/mindex-desktop-ffmpeg-sources-<版本>.tar。
#
#   scripts/build-desktop-linux.sh [--rebuild-ffmpeg] [--skip-sidecar]
#
# 步骤与 macOS 版一致：随包 ffmpeg（缺失或 --rebuild-ffmpeg 时从源码编译）→ 冻结后端 →
# 冻结后端冒烟 → 字体 → electron-builder。Linux 无签名步骤。需要 gcc/g++、make、pkg-config、
# nasm、uv、node 22+（见 scripts/build-desktop-ffmpeg.sh 的平台说明）。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP="${ROOT}/desktop"
BUILD="${DESKTOP}/build"
REBUILD_FFMPEG=0
SKIP_SIDECAR=0
for arg in "$@"; do
  case "$arg" in
    --rebuild-ffmpeg) REBUILD_FFMPEG=1 ;;
    --skip-sidecar) SKIP_SIDECAR=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "本脚本只在 Linux 上运行；macOS 用 scripts/build-desktop-mac.sh，Windows 用 scripts/build-desktop-win.ps1" >&2
  exit 1
fi

VERSION="$(sed -nE 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "${ROOT}/pyproject.toml" | head -1)"
PACKAGE_VERSION="$(node -p "require('${DESKTOP}/package.json').version")"
if [[ "${VERSION}" != "${PACKAGE_VERSION}" ]]; then
  echo "desktop/package.json 版本 ${PACKAGE_VERSION} 与 pyproject.toml ${VERSION} 不一致" >&2
  exit 1
fi

# Noto Sans CJK SC（SIL OFL 1.1）：烧录字幕的随包字体（与 macOS 版同一份）。
FONT_TAG="Sans2.004"
FONT_SHA256="2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
LICENSE_SHA256="6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
fetch() {  # 地址 目标 sha256
  if [[ ! -f "$2" ]] || [[ "$(sha256sum "$2" | cut -d' ' -f1)" != "$3" ]]; then
    curl -fsSL --retry 5 --retry-all-errors -o "$2.partial" "$1" && mv "$2.partial" "$2"
  fi
  [[ "$(sha256sum "$2" | cut -d' ' -f1)" == "$3" ]] || { echo "sha256 不符：$2" >&2; exit 1; }
}
mkdir -p "${BUILD}/fonts"
fetch "https://raw.githubusercontent.com/notofonts/noto-cjk/${FONT_TAG}/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf" \
  "${BUILD}/fonts/NotoSansCJKsc-Regular.otf" "${FONT_SHA256}"
fetch "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/LICENSE" \
  "${BUILD}/fonts/LICENSE-NotoSansCJK.txt" "${LICENSE_SHA256}"

if [[ "${REBUILD_FFMPEG}" == 1 || ! -x "${BUILD}/ffmpeg/bin/ffmpeg" ]]; then
  "${ROOT}/scripts/build-desktop-ffmpeg.sh"
fi
if [[ "${SKIP_SIDECAR}" == 0 || ! -x "${BUILD}/backend/mindex" ]]; then
  "${ROOT}/scripts/build-desktop-sidecar.sh"
fi
# 冻结后端 + 随包 ffmpeg + 随包字体，在隔离 PATH 下冒烟（含中文字幕烧录）。
uv run --project "${ROOT}" python "${ROOT}/scripts/check-desktop-sidecar.py" --isolated-path \
  --ffmpeg-dir "${BUILD}/ffmpeg/bin" --font "${BUILD}/fonts/NotoSansCJKsc-Regular.otf"

cd "${DESKTOP}"
npm ci --no-audit --no-fund
rm -rf dist
npx electron-builder --linux --x64 --publish never

tar -cf "${DESKTOP}/dist/mindex-desktop-ffmpeg-sources-${VERSION}.tar" -C "${BUILD}/ffmpeg" sources
echo "安装包："
ls -lh "${DESKTOP}/dist"/*.AppImage "${DESKTOP}/dist"/*.deb "${DESKTOP}/dist"/*.tar
