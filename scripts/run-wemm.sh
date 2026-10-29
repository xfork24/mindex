#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LLAMA_SERVER="${MINDEX_LLAMA_SERVER:-${PROJECT_DIR}/bin/llama-server}"
CTX_SIZE="${MINDEX_WEMM_CTX_SIZE:-32768}"
# 权重实际存放目录（解析 models 软链接，便于定位实际文件）。
MODEL_DIR="$(cd -- "${MINDEX_WEMM_MODEL_DIR:-${PROJECT_DIR}/models/WeMM-Embedding}" && pwd -P)"
# 直接引用子目录模型文件。本地 schema 2 清单按当前路径重新核验 SHA256；
# 仅路径变化且内容与其他部署参数未变时，复用原清单和 smoke 记录，无需重建索引。
cd -- "$PROJECT_DIR"
exec "$LLAMA_SERVER" \
  -m "${MODEL_DIR}/WeMM-9B-Q8_0.gguf" \
  --mmproj "${MODEL_DIR}/mmproj-WeMM-9B-BF16.gguf" \
  --host 127.0.0.1 --port "${MINDEX_WEMM_PORT:-8888}" --alias "${MINDEX_WEMM_ALIAS:-wemm-macos}" \
  -ngl 99 --embeddings --pooling last --video-fps 4 \
  --ctx-size "$CTX_SIZE" --parallel 1 --cache-type-k f16 --cache-type-v f16 --cache-ram 0 \
  --log-verbosity "${MINDEX_WEMM_LOG_VERBOSITY:-4}"
