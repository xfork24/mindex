#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_DIR"
exec .venv-person/bin/python -m mindex.persons.person_server \
  --config "${MINDEX_CONFIG:-config.local.yaml}" \
  --host "${MINDEX_PERSON_HOST:-127.0.0.1}" --port "${MINDEX_PERSON_PORT:-4050}"
