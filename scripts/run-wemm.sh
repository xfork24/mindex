#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LLAMA_SERVER="${MINDEX_LLAMA_SERVER:-${PROJECT_DIR}/bin/llama-server}"
CTX_SIZE="${MINDEX_WEMM_CTX_SIZE:-32768}"
# 权重实际存放目录（使用物理路径，避免 clone 中的 models 软链接改变部署观测身份）。
MODEL_DIR="$(cd -- "${MINDEX_WEMM_MODEL_DIR:-${PROJECT_DIR}/models/WeMM-Embedding}" && pwd -P)"
# 部署清单 data/deployments/wemm-macos-v2-32768-alias.json 固定了 /props.model_path（部署观测身份的一部分）为
# models/ 顶层的旧路径，而且已有的兼容验证记录按该部署身份保存。因此必须以顶层同名软链接启动 llama-server；
# 要把清单改为子目录路径，需要重新 pin 并重新通过六片段 smoke（见 docs/macos-model-migration.md）。
LINK_DIR="$(cd -- "${PROJECT_DIR}/models" && pwd -P)"
ln -sfn "${MODEL_DIR}/WeMM-9B-Q8_0.gguf" "${LINK_DIR}/WeMM-9B-Q8_0.gguf"
ln -sfn "${MODEL_DIR}/mmproj-WeMM-9B-BF16.gguf" "${LINK_DIR}/mmproj-WeMM-9B-BF16.gguf"
cd -- "$PROJECT_DIR"
exec "$LLAMA_SERVER" \
  -m "${LINK_DIR}/WeMM-9B-Q8_0.gguf" \
  --mmproj "${LINK_DIR}/mmproj-WeMM-9B-BF16.gguf" \
  --host 127.0.0.1 --port "${MINDEX_WEMM_PORT:-8888}" --alias "${MINDEX_WEMM_ALIAS:-wemm-macos}" \
  -ngl 99 --embeddings --pooling last --video-fps 4 \
  --ctx-size "$CTX_SIZE" --parallel 1 --cache-type-k f16 --cache-type-v f16 --cache-ram 0 \
  --log-verbosity "${MINDEX_WEMM_LOG_VERBOSITY:-4}"
