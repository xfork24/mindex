#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_DIR"
uv venv .venv-person --python 3.12
uv pip install --python .venv-person/bin/python numpy cython setuptools wheel
uv pip install --python .venv-person/bin/python --no-build-isolation -r deploy/person.txt
uv pip install --python .venv-person/bin/python -e .
# 下载/校验官方 cheetah_l V2 模型包（幂等；首次约数百 MB，来自 GitHub release，需要代理时请先设置 https_proxy）。
export MINDEX_PERSON_MODEL_ROOT="${MINDEX_PERSON_MODEL_ROOT:-${PROJECT_DIR}/models/insightface}"
.venv-person/bin/python scripts/download_insightface_weights.py
# 引擎级验证：manifest、逐文件 SHA-256、四类任务与一次真实推理，与服务启动同一路径。
MINDEX_PERSON_PROVIDERS="${MINDEX_PERSON_PROVIDERS:-CPUExecutionProvider}" .venv-person/bin/python - <<'PY'
import tempfile
from pathlib import Path

import numpy as np

from mindex.shared.config import Config
from mindex.persons.person_server import PersonEngine

config = Config(data_dir=Path(tempfile.mkdtemp(prefix="mindex-person-setup-")))
engine = PersonEngine(config)
assert isinstance(engine.observations(np.zeros((480, 640, 3), dtype=np.uint8)), list)
print("PersonEngine 验证通过：", engine.space["model"], engine.space["manifest_sha256"][:12])
print("启动服务：./scripts/run-person-service.sh")
PY
