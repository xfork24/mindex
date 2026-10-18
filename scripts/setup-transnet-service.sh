#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$PROJECT_DIR"
for command in uv ffmpeg ffprobe; do
  if ! command -v "$command" >/dev/null; then
    echo "缺少 $command，请先安装后再运行。" >&2
    exit 1
  fi
done
if [[ ! -x .venv-detector/bin/python ]]; then
  uv venv .venv-detector --python 3.11
fi
uv pip install --python .venv-detector/bin/python -r deploy/detector-local.txt
# 仅安装服务所需依赖，不向此环境引入 Chroma、ASR 或人物依赖。
uv pip install --python .venv-detector/bin/python --no-deps -e .
.venv-detector/bin/python scripts/setup_transnet_weights.py
export MINDEX_TRANSNET_DIR="${MINDEX_TRANSNET_DIR:-${PROJECT_DIR}/models/TransNetV2/inference}"
export MINDEX_TRANSNET_DEVICE=cpu
export TF_NUM_INTRAOP_THREADS="${TF_NUM_INTRAOP_THREADS:-4}"
export TF_NUM_INTEROP_THREADS="${TF_NUM_INTEROP_THREADS:-2}"
export TF_CPP_MIN_LOG_LEVEL=2
.venv-detector/bin/python - <<'PY'
import numpy as np
from mindex.model_server import TransNetEngine
engine = TransNetEngine()
single, _ = engine.model.predict_raw(np.zeros((1, 100, 27, 48, 3), dtype=np.uint8))
if tuple(single.shape) != (1, 100, 1) or not np.isfinite(np.asarray(single)).all():
    raise RuntimeError("TransNetV2 权重推理验证失败")
print("TransNetV2 安装与推理验证通过：", engine.metadata)
print("启动服务：./scripts/run-transnet-service.sh")
PY
