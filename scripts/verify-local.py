#!/usr/bin/env python3
"""对冻结源码执行完整离线回归；按历史 JUnit 耗时分组，保留选择、日志与结果。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9), help="独立测试进程数（1–8）")
    parser.add_argument("--output", type=Path, help="新建证据目录")
    parser.add_argument("--durations", type=Path, default=ROOT / "artifacts/scaling-local-frozen",
                        help="历史 JUnit 目录，仅用于分组，不改变测试选择")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("scaling_verifier", ROOT / "scripts/verify-scaling.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    output = (args.output or ROOT / "artifacts/local" /
              f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:12]}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("MINDEX_") or key == "MINDEX_PLAYWRIGHT_MODULE"}
    collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"], cwd=ROOT,
                               env=env, text=True, capture_output=True, check=False)
    (output / "collection.log").write_text(collected.stdout + collected.stderr, encoding="utf-8")
    if collected.returncode:
        raise RuntimeError(f"测试收集失败，见 {output / 'collection.log'}")
    nodes = [line for line in collected.stdout.splitlines() if line.startswith("tests/") and "::" in line]
    if not nodes or len(nodes) != len(set(nodes)):
        raise RuntimeError("测试收集为空或存在重复节点，拒绝执行不完整回归")
    durations = {}
    for file in args.durations.glob("*.xml"):
        for case in ET.parse(file).getroot().iter("testcase"):
            durations[(case.get("classname"), case.get("name"))] = float(case.get("time", "1"))

    def weight(node):
        file, *names = node.split("::")
        classname = ".".join([file.removesuffix(".py").replace("/", "."), *names[:-1]])
        return max(.05, durations.get((classname, names[-1]), 1))

    groups = [[] for _ in range(args.workers)]
    weights = [0.0] * args.workers
    for node in sorted(nodes, key=lambda item: (-weight(item), item)):
        index = weights.index(min(weights))
        groups[index].append(node)
        weights[index] += weight(node)
    # 每组恢复收集顺序，保留模块内部既有执行次序；每个节点恰好执行一次。
    positions = {node: index for index, node in enumerate(nodes)}
    groups = [sorted(group, key=positions.__getitem__) for group in groups]
    fingerprint = verifier.source_identity()
    report = {"started_at": datetime.now(UTC).isoformat(), "source_sha256": fingerprint,
              "nodes": len(nodes), "workers": args.workers, "groups": groups,
              "estimated_seconds": weights, "status": "running"}
    (output / "selection.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整离线回归：{len(nodes)} 项，{args.workers} 个独立进程；证据：{output}", flush=True)
    processes, logs, codes = {}, [], {}
    try:
        for index, group in enumerate(groups):
            log = (output / f"shard-{index}.log").open("w")
            logs.append(log)
            processes[index] = subprocess.Popen(
                [sys.executable, "-m", "pytest", "-q", "--tb=short", f"--junitxml={output / f'shard-{index}.xml'}",
                 *group], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        while processes:
            for index, process in list(processes.items()):
                if process.poll() is not None:
                    codes[index] = process.returncode
                    del processes[index]
                    print(f"分组 {index} 退出码 {process.returncode}", flush=True)
            if processes:
                time.sleep(1)
        counts = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
        for file in output.glob("shard-*.xml"):
            for suite in ET.parse(file).getroot().iter("testsuite"):
                for key in counts:
                    counts[key] += int(suite.get(key, "0"))
        unchanged = verifier.source_identity() == fingerprint
        passed = not any(codes.values()) and counts["tests"] == len(nodes) and unchanged
        report.update(status="passed" if passed else "failed", counts=counts, exit_codes=codes,
                      source_unchanged=unchanged, finished_at=datetime.now(UTC).isoformat())
        print(json.dumps({"status": report["status"], "source_unchanged": unchanged, **counts}), flush=True)
        return 0 if passed else 1
    finally:
        for process in processes.values():
            verifier.stop_process(process)
        for log in logs:
            log.close()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
