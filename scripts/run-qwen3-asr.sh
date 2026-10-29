#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "${PROJECT_DIR}/scripts/lib/backend.sh"
mindex_parse_backend "${MINDEX_ASR_BACKEND:-}" "$@"
mindex_llama_backend_args
LLAMA_SERVER="${MINDEX_LLAMA_SERVER:-${PROJECT_DIR}/bin/llama-server}"
cd -- "$PROJECT_DIR"
exec "$LLAMA_SERVER" \
  -m "${PROJECT_DIR}/models/Qwen-ASR/Qwen3-ASR-1.7B-Q8_0.gguf" \
  --mmproj "${PROJECT_DIR}/models/Qwen-ASR/mmproj-Qwen3-ASR-1.7B-Q8_0.gguf" \
  --host 127.0.0.1 --port "${MINDEX_ASR_PORT:-8889}" --alias "${MINDEX_ASR_ALIAS:-qwen3-asr-macos}" \
  "${MINDEX_BACKEND_ARGS[@]}" --log-verbosity "${MINDEX_ASR_LOG_VERBOSITY:-4}"
