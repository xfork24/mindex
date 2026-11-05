#!/usr/bin/env python3
"""独立创建后端执行真实 CPU source 吞吐闸门；不要与其他容量负载同时运行。"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    spec = importlib.util.spec_from_file_location("scaling_verifier", ROOT / "scripts/verify-scaling.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    started = time.time()
    result = verifier.main(suite="cpu")
    # main 的 --output 显式指定时，将吞吐原始数据与后端环境证据放在同一目录。
    if "--output" in sys.argv:
        output = Path(sys.argv[sys.argv.index("--output")+1])
        measured = ROOT / "artifacts/scaling-cpu-throughput-result.json"
        if measured.is_file() and measured.stat().st_mtime >= started:
            shutil.copy2(measured, output / "cpu-result.json")
            details = ROOT / "artifacts/scaling-cpu-throughput-details"
            if details.is_dir():
                shutil.copytree(details, output / "cpu-details", dirs_exist_ok=True)
            report_path = output / "report.json"
            report = json.loads(report_path.read_text())
            report["scope"] = "单机真实 CPU source 吞吐，固定每 worker 媒体线程预算；不替代双机或检索容量"
            report["cpu_result"] = json.loads(measured.read_text())
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
