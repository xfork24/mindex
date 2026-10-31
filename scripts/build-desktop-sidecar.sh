#!/usr/bin/env bash
# 构建桌面版冻结后端（PyInstaller onedir，arm64）：desktop/build/backend/mindex。
#
# 在独立虚拟环境中按 uv.lock 安装依赖（主依赖 + animation、mcp extra；不含 dev、person），以普通 wheel 安装 mindex，
# 再用锁定版本的 PyInstaller 打包。不改动仓库的 .venv。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"  # 无论从哪个目录调用，后续步骤一律以项目根为工作目录
BUILD="${ROOT}/desktop/build"
VENV="${BUILD}/sidecar-venv"
PYTHON_VERSION="${MINDEX_SIDECAR_PYTHON:-3.13}"
PYINSTALLER_VERSION="6.22.3"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "只支持在 Apple Silicon macOS 上构建" >&2
  exit 1
fi

mkdir -p "${BUILD}"
rm -rf "${VENV}" "${BUILD}/backend" "${BUILD}/pyinstaller-work"
uv venv --quiet --python "${PYTHON_VERSION}" "${VENV}"
uv export --quiet --project "${ROOT}" --frozen --no-dev --extra animation --extra mcp \
  --no-emit-project --no-hashes --output-file "${BUILD}/sidecar-requirements.txt"
uv pip install --quiet --python "${VENV}/bin/python" -r "${BUILD}/sidecar-requirements.txt"
# 非 editable 安装：package-data（前端、模板）进入 site-packages，PyInstaller 从这里收集。
uv pip install --quiet --python "${VENV}/bin/python" --no-deps --reinstall "${ROOT}"
uv pip install --quiet --python "${VENV}/bin/python" "pyinstaller==${PYINSTALLER_VERSION}"

cd "${ROOT}/desktop/sidecar"
"${VENV}/bin/pyinstaller" --noconfirm --clean --log-level WARN \
  --distpath "${BUILD}" --workpath "${BUILD}/pyinstaller-work" mindex.spec
rm -rf "${BUILD}/pyinstaller-work"
echo "冻结后端：${BUILD}/backend/mindex（$(du -sh "${BUILD}/backend" | cut -f1)）"
