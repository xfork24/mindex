#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "${PROJECT_DIR}/scripts/lib/backend.sh"
MINDEX_RUN_DEFAULT_LABEL="${MINDEX_TRANSNET_DEVICE:-cpu}"
mindex_parse_backend "${MINDEX_TRANSNET_BACKEND:-}" "$@"
cd -- "$PROJECT_DIR"
if [[ ! -x .venv-detector/bin/python ]]; then
  echo "尚未安装 TransNetV2，请先执行 ./scripts/setup-transnet-service.sh" >&2
  exit 1
fi
export MINDEX_TRANSNET_DIR="${MINDEX_TRANSNET_DIR:-${PROJECT_DIR}/models/TransNetV2/inference}"
export MINDEX_TRANSNET_DEVICE="${MINDEX_RUN_BACKEND:-${MINDEX_TRANSNET_DEVICE:-cpu}}"
export TF_NUM_INTRAOP_THREADS="${TF_NUM_INTRAOP_THREADS:-4}"
export TF_NUM_INTEROP_THREADS="${TF_NUM_INTEROP_THREADS:-2}"
export TF_CPP_MIN_LOG_LEVEL="${TF_CPP_MIN_LOG_LEVEL:-2}"
exec .venv-detector/bin/python -m mindex.models.model_server \
  --role detector --config "${MINDEX_CONFIG:-config.local.yaml}" \
  --host "${MINDEX_TRANSNET_HOST:-127.0.0.1}" --port "${MINDEX_TRANSNET_PORT:-18203}"
