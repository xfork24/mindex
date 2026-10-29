#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "${PROJECT_DIR}/scripts/lib/backend.sh"
mindex_parse_backend "${MINDEX_TTS_BACKEND:-}" "$@"
mindex_tts_backend_args
TTS_SERVER="${MINDEX_TTS_SERVER:-${PROJECT_DIR}/bin/tts-server}"
MODEL_DIR="${MINDEX_TTS_MODEL_DIR:-${PROJECT_DIR}/models/Qwen3-TTS}"
cd -- "$PROJECT_DIR"
exec "$TTS_SERVER" \
  --model "${MODEL_DIR}/qwen-talker-1.7b-base-Q8_0.gguf" \
  --codec "${MODEL_DIR}/qwen-tokenizer-12hz-Q8_0.gguf" \
  --alias "${MINDEX_TTS_ALIAS:-qwen3-tts-voiceclone}" \
  --host 127.0.0.1 --port "${MINDEX_TTS_PORT:-8890}" --lang auto \
  ${MINDEX_BACKEND_ARGS[@]+"${MINDEX_BACKEND_ARGS[@]}"}
