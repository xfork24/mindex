#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LLAMA_SERVER="${MINDEX_LLAMA_SERVER:-${PROJECT_DIR}/../llama.cpp/build/bin/llama-server}"
CTX_SIZE="${MINDEX_WEMM_CTX_SIZE:-32768}"
# 使用权重目录的物理路径，避免 clone 中的 models 软链接改变部署观测身份。
MODEL_DIR="$(cd -- "${MINDEX_WEMM_MODEL_DIR:-${PROJECT_DIR}/models}" && pwd -P)"
cd -- "$PROJECT_DIR"
exec "$LLAMA_SERVER" \
  -m "${MODEL_DIR}/WeMM-9B-Q8_0.gguf" \
  --mmproj "${MODEL_DIR}/mmproj-WeMM-9B-BF16.gguf" \
  --host 127.0.0.1 --port "${MINDEX_WEMM_PORT:-8888}" --alias "${MINDEX_WEMM_ALIAS:-wemm-macos}" \
  -ngl 99 --embeddings --pooling last --video-fps 4 \
  --ctx-size "$CTX_SIZE" --parallel 1 --cache-type-k f16 --cache-type-v f16 --cache-ram 0 \
  --log-verbosity "${MINDEX_WEMM_LOG_VERBOSITY:-4}"
