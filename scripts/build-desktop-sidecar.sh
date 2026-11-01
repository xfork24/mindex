#!/usr/bin/env bash
# 构建桌面版冻结后端（PyInstaller onedir）：desktop/build/backend/mindex。
#
# 在独立虚拟环境中按 uv.lock 安装依赖（主依赖 + animation、mcp、dsh extra；不含 dev、person），以普通 wheel 安装 mindex，
# 再用锁定版本的 PyInstaller 打包。不改动仓库的 .venv。支持 macOS（arm64）与 Linux（x64）；
# Windows 用 scripts/build-desktop-sidecar.ps1（同一 mindex.spec）。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"  # 无论从哪个目录调用，后续步骤一律以项目根为工作目录
BUILD="${ROOT}/desktop/build"
VENV="${BUILD}/sidecar-venv"
VENV_BIN="${VENV}/bin"
PYTHON_VERSION="${MINDEX_SIDECAR_PYTHON:-3.13}"
PYINSTALLER_VERSION="6.22.3"

if [[ "$(uname -s)" == "Darwin" ]]; then
  if [[ "$(uname -m)" != "arm64" ]]; then
    echo "macOS 只支持 Apple Silicon（arm64）构建" >&2
    exit 1
  fi
elif [[ "$(uname -s)" != "Linux" ]]; then
  echo "本脚本支持 macOS 与 Linux；Windows 请用 scripts/build-desktop-sidecar.ps1" >&2
  exit 1
fi

mkdir -p "${BUILD}"
rm -rf "${VENV}" "${BUILD}/backend" "${BUILD}/pyinstaller-work"
uv venv --quiet --python "${PYTHON_VERSION}" "${VENV}"
uv export --quiet --project "${ROOT}" --frozen --no-dev --extra animation --extra mcp --extra dsh \
  --no-emit-project --no-hashes --output-file "${BUILD}/sidecar-requirements.txt"
uv pip install --quiet --python "${VENV_BIN}/python" -r "${BUILD}/sidecar-requirements.txt"
# 非 editable 安装：package-data（前端、模板）进入 site-packages，PyInstaller 从这里收集。
uv pip install --quiet --python "${VENV_BIN}/python" --no-deps --reinstall "${ROOT}"
uv pip install --quiet --python "${VENV_BIN}/python" "pyinstaller==${PYINSTALLER_VERSION}"

cd "${ROOT}/desktop/sidecar"
"${VENV_BIN}/pyinstaller" --noconfirm --clean --log-level WARN \
  --distpath "${BUILD}" --workpath "${BUILD}/pyinstaller-work" mindex.spec
rm -rf "${BUILD}/pyinstaller-work"
echo "冻结后端：${BUILD}/backend/mindex（$(du -sh "${BUILD}/backend" | cut -f1)）"
