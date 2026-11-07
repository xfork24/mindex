#!/usr/bin/env bash
# mypy 基线入口。包内使用短导入，pyproject.toml 的 mypy_path 直接指向 backend/src。
# 检查范围与豁免清单见 pyproject.toml 的 [tool.mypy] 与 [[tool.mypy.overrides]]。
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --frozen --extra dev mypy "$@"
