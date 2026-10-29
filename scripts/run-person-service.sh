#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_DIR"
# 模型包默认由 setup-person-service.sh 安装在仓库 models/insightface；显式设置时以环境变量为准。
export MINDEX_PERSON_MODEL_ROOT="${MINDEX_PERSON_MODEL_ROOT:-${PROJECT_DIR}/models/insightface}"
exec .venv-person/bin/python -m mindex.persons.person_server \
  --config "${MINDEX_CONFIG:-config.local.yaml}" \
  --host "${MINDEX_PERSON_HOST:-127.0.0.1}" --port "${MINDEX_PERSON_PORT:-4050}"
