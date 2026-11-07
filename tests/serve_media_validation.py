"""使用独立数据目录验收真实上传及处理，不替换模型实现。"""

import argparse
import logging
from pathlib import Path

import mindex  # noqa: F401 — 安装根包自举，后续使用包内短导入
import uvicorn

from api import create_app
from shared.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--local-detector", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    directory = args.data_dir.resolve()
    if directory == config.data_dir.resolve():
        parser.error("验收必须使用与应用不同的数据目录")
    config = config.model_copy(deep=True, update={"data_dir": directory, "runtime_overrides": False})
    if args.local_detector:
        config.detector.backend = "adaptive"
    logging.basicConfig(level=logging.INFO)
    print(f"独立验收数据目录：{directory}；分镜后端：{config.detector.backend}", flush=True)
    uvicorn.run(create_app(config), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
