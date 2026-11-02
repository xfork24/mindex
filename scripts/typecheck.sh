#!/usr/bin/env bash
# mypy 基线入口。本仓库的包布局是 package-dir 映射（mindex = backend/src），editable 安装
# 用自定义 finder，mypy 解析不到；先建 .mypy-shim/mindex -> backend/src 的符号链接再检查。
# 检查范围与豁免清单见 pyproject.toml 的 [tool.mypy] 与 [[tool.mypy.overrides]]。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .mypy-shim
ln -sfn ../backend/src .mypy-shim/mindex
exec uv run --extra dev mypy "$@"
