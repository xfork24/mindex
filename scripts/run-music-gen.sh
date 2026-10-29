#!/usr/bin/env bash
# 本地 MiniMax Music 3（audio.cpp）音乐生成服务；配置见 deploy/audiocpp-music3.json。
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "${PROJECT_DIR}/scripts/lib/backend.sh"
mindex_parse_backend "${MINDEX_MUSIC3_BACKEND:-metal}" "$@"
SERVER="${MINDEX_AUDIOCPP_SERVER:-${PROJECT_DIR}/bin/audiocpp_server}"
cd -- "$PROJECT_DIR"
exec "$SERVER" \
  --config "${MINDEX_MUSIC3_CONFIG:-${PROJECT_DIR}/deploy/audiocpp-music3.json}" \
  --host 127.0.0.1 --port "${MINDEX_MUSIC3_PORT:-8891}" \
  --backend "$MINDEX_RUN_BACKEND" \
  --log
