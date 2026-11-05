"""独立 CPU 吞吐闸门：真实 source 管线；不以 sleep 或模型替身模拟吞吐。

由 scripts/verify-scaling-cpu.py 在没有其他验收负载时执行；默认离线测试门控跳过。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from test_scaling_correctness import pg_config as pg_config
from test_scaling_correctness import s3_config as s3_config

from mindex.admin.source_tasks import SourceTasks, WorkOptions
from mindex.processing.pipeline import Pipeline
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.shared.media import command
from mindex.shared.storage import Store

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_s3]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_real_cpu_source_throughput_one_and_two_workers(s3_config, tmp_path):
    cfg = s3_config
    cfg.runtime_overrides = False
    cfg.embedding.dimensions = 8
    cfg.embedding.base_url = "http://127.0.0.1:9"  # 不允许意外调用模型；publish=False。
    cfg.gpu.capacity = 0
    cfg.local_services.enabled = False
    video = tmp_path / "cpu.mp4"
    command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=480x270:rate=12:duration=30", "-c:v", "libx264", "-threads", "1", str(video)])
    original = video.read_bytes()
    store = Store.open(cfg)
    pipeline = Pipeline(cfg)
    ids = []
    for index in range(19):
        path = tmp_path / f"source-{index}.mp4"
        path.write_bytes(original + bytes([index]))
        ids.append(pipeline.register(path)["source_id"])
    # 每个 worker 的图像处理/编码线程预算一致；两 worker 对应两份 CPU 执行能力。
    # 包装器仅设置真实 ffmpeg 的解码/编码线程数，不替代任何媒体工作。
    ffmpeg = shutil.which("ffmpeg")
    wrapper = tmp_path / "ffmpeg-one-thread"
    wrapper.write_text(f"#!{sys.executable}\nimport os,sys\na=sys.argv[1:]\n"
                       f"os.execv({ffmpeg!r},[{ffmpeg!r},'-threads','1',*a[:-1],'-threads','1',a[-1]])\n")
    wrapper.chmod(0o700)
    env = {**os.environ, "MINDEX_FFMPEG": str(wrapper), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    runtime = RuntimeConfig(cfg, store)
    runner = SourceTasks(runtime, store)
    reports = []
    report = {"status": "running", "platform": platform.platform(), "physical_cpus": os.cpu_count(),
              "dataset": {"seconds": 30, "width": 480, "height": 270, "fps": 12,
                          "source_sha256": hashlib.sha256(original).hexdigest(), "tasks_per_phase": 8},
              "worker_budget": "每进程 OpenCV 算法/BLAS 各 1 线程、ffmpeg 子进程解码与编码各 1 线程；无 GPU，无模型调用",
              "phases": reports, "scope": "单机 CPU 横向并行；不代替双机、检索容量或真实模型验收"}
    output = Path("artifacts/scaling-cpu-throughput-result.json")
    output.parent.mkdir(exist_ok=True)
    try:
        offset = 0
        for count in (1, 2):
            processes, logs, paths = [], [], []
            try:
                for index in range(count):
                    node = cfg.model_copy(deep=True, update={"data_dir": tmp_path / f"worker-{count}-{index}"})
                    data = node.model_dump(mode="json")
                    for key in ("database", "queue", "locks", "vector", "storage", "server", "gpu", "local_services"):
                        data[key] = getattr(node, key).model_dump(mode="json")
                    config = tmp_path / f"worker-{count}-{index}.yaml"
                    config.write_text(yaml.safe_dump(data, allow_unicode=True))
                    log_path = tmp_path / f"worker-{count}-{index}.log"
                    log = log_path.open("w+")
                    logs.append(log)
                    paths.append(log_path)
                    process = subprocess.Popen([sys.executable, "-c",
                        "import cv2;cv2.setNumThreads(1);from mindex.cli import app;app()",
                        "--config", str(config), "worker", "--queues", "source", "--idle-exit", "60"],
                        stdout=log, stderr=subprocess.STDOUT, env=env)
                    processes.append(process)
                deadline = time.monotonic() + 60
                while not all("worker 启动" in path.read_text() for path in paths):
                    assert all(p.poll() is None for p in processes), "worker 启动失败"
                    assert time.monotonic() < deadline, "worker 启动超时"
                    time.sleep(.1)
                def finish(tasks, processes):
                    deadline = time.monotonic() + 240
                    while True:
                        rows = [store.one("SELECT status,error FROM source_tasks WHERE id=?", (task,)) for task in tasks]
                        assert all(row["status"] not in {"failed", "cancelled", "interrupted"} for row in rows), rows
                        if all(row["status"] == "complete" for row in rows):
                            return
                        assert all(p.poll() is None for p in processes), "worker 意外退出"
                        assert time.monotonic() < deadline, "CPU 处理超时"
                        time.sleep(.05)
                warm = [runner.start(ids[offset+i], "shots", WorkOptions(publish=False))["id"] for i in range(count)]
                offset += count
                finish(warm, processes)
                measured_sources = ids[offset:offset+8]
                offset += 8
                started = time.monotonic()
                tasks = [runner.start(sid, "shots", WorkOptions(publish=False))["id"] for sid in measured_sources]
                finish(tasks, processes)
                elapsed = time.monotonic() - started
                owners = {store.one("SELECT owner FROM task_queue WHERE ledger_id=?", (task,))["owner"] for task in tasks}
                segments = sum(store.one("SELECT count(*) AS n FROM segments WHERE source_id=?", (sid,))["n"]
                               for sid in measured_sources)
                assert segments > 0 and len(owners) == count
                verified = 0
                for sid in measured_sources:
                    objects = store.rows("SELECT f.object_key,f.sha256,f.size FROM shared_files f "
                        "JOIN segments s ON s.path=f.path WHERE s.source_id=? AND s.kind='shot'", (sid,))
                    assert objects
                    for item in objects:
                        actual = store.media.stat(item["object_key"])
                        assert actual.sha256 == item["sha256"] and actual.size == item["size"] > 0
                        verified += 1
                reports.append({"workers": count, "wall_seconds": elapsed, "tasks": len(tasks),
                                "tasks_per_second": len(tasks)/elapsed, "segments": segments, "verified_objects": verified, "owners": sorted(owners)})
            finally:
                for process in processes:
                    stop(process)
                for log in logs:
                    log.close()
        ratio = reports[1]["tasks_per_second"] / reports[0]["tasks_per_second"]
        report.update(speedup=ratio, threshold=1.5, status="passed" if ratio >= 1.5 else "failed")
        assert ratio >= 1.5, report
    finally:
        if report["status"] == "running":
            report["status"] = "failed"
        details = Path("artifacts/scaling-cpu-throughput-details")
        details.mkdir(exist_ok=True)
        for path in tmp_path.glob("worker-*"):
            if path.is_file() and path.suffix in {".yaml", ".log"}:
                shutil.copy2(path, details / path.name)
        shutil.copy2(wrapper, details / wrapper.name)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
