#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LLAMA_SERVER="${MINDEX_LLAMA_SERVER:-${PROJECT_DIR}/../llama.cpp/build/bin/llama-server}"
CTX_SIZE="${MINDEX_WEMM_CTX_SIZE:-32768}"
cd -- "$PROJECT_DIR"
exec "$LLAMA_SERVER" \
  -m "${PROJECT_DIR}/models/WeMM-9B-Q8_0.gguf" \
  --mmproj "${PROJECT_DIR}/models/mmproj-WeMM-9B-BF16.gguf" \
  --host 127.0.0.1 --port "${MINDEX_WEMM_PORT:-8888}" --alias "${MINDEX_WEMM_ALIAS:-wemm-macos}" \
  -ngl 99 --embeddings --pooling last --video-fps 4 \
  --ctx-size "$CTX_SIZE" --parallel 1 --cache-type-k f16 --cache-type-v f16 --cache-ram 0 \
  --log-verbosity "${MINDEX_WEMM_LOG_VERBOSITY:-4}"
