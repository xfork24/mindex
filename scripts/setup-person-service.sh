#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_DIR"
uv venv .venv-person --python 3.11
uv pip install --python .venv-person/bin/python numpy cython setuptools wheel
uv pip install --python .venv-person/bin/python --no-build-isolation -r deploy/person.txt
uv pip install --python .venv-person/bin/python -e .
