#!/usr/bin/env python3
"""创建一次性 PG/MinIO/Chroma 环境执行集群组件验收；任何 skip 都判失败。

仅删除本脚本创建的容器与临时目录，不读取用户配置或使用已有服务。
该入口不替代双机、容量、真实模型及三平台安装包验收。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PG_IMAGE = "postgres@sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab"
CHROMA_IMAGE = "chromadb/chroma@sha256:1e0b73a187a28757c572acba508c46f48c9e8b0acaf5c20e6d95cdedce1acdf6"
TESTS = [
    "test_scaling_boundaries.py", "test_scaling_foundation.py", "test_scaling_correctness.py",
    "test_scaling_remaining_verification.py", "test_pg_backend.py", "test_pg_queue.py",
    "test_pg_locks.py", "test_pg_export.py", "test_s3_store.py", "test_s3_purge.py",
    "test_chroma_server.py", "test_staging.py",
    "test_migration_crash_recovery.py", "test_migration_stages.py",
    "test_cluster_process_flow.py",
    "test_doctor.py", "test_readiness_budget.py", "test_deployment_config.py", "test_pg_chroma_e2e.py", "test_migration_cli.py", "test_worker.py",
    "test_persons_pg.py", "test_media_delivery.py",
    "test_capacity_tool.py", "test_query_budget.py", "test_s3_multipart.py", "test_backend_identity.py", "test_backend_lifecycle.py", "test_scaling_completion.py", "test_object_coordination.py",
    "test_maintenance_and_gateway.py", "test_sql_characteristics.py",
    "test_scaling_execution.py", "test_scaling_delivery_core.py",
]


CPU_TESTS = ["test_scaling_cpu_acceptance.py"]


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def command(args):
    return subprocess.check_output(args, text=True, cwd=ROOT, stderr=subprocess.STDOUT).strip()


def port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def wait_for(action, label):
    deadline = time.monotonic() + 60
    while True:
        try:
            action()
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{label} 在 60 秒内未就绪") from exc
            time.sleep(.25)


def http_ready(url):
    with urllib.request.urlopen(url, timeout=1) as response:
        if response.status != 200:
            raise RuntimeError("后端未就绪")


def source_identity():
    hashed = hashlib.sha256()
    for directory in ("backend/src", "frontend/src", "desktop", "tests", "scripts"):
        for path in sorted((ROOT / directory).rglob("*")):
            relative = path.relative_to(ROOT)
            if any(part in {"node_modules", "build", "dist", "__pycache__"} for part in relative.parts):
                continue
            if path.is_file() and path.suffix in {".py", ".js", ".json", ".cjs", ".yaml", ".html"}:
                hashed.update(str(relative).encode() + b"\0" + path.read_bytes())
    for name in ("pyproject.toml", "uv.lock"):
        hashed.update((ROOT / name).read_bytes())
    return hashed.hexdigest()


def main(*, suite="integration"):
    if suite not in {"integration", "cpu"}:
        raise ValueError("未知验收专项")
    test_files = TESTS if suite == "integration" else CPU_TESTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minio-binary", default=shutil.which("minio"), help="MinIO 可执行文件（记录其摘要）")
    parser.add_argument("--output", type=Path, help="空证据目录；默认 artifacts/scaling/<时间与随机 ID>")
    args = parser.parse_args()
    if not args.minio_binary or not shutil.which("docker"):
        parser.error("需要 Docker 与 MinIO 可执行文件；环境不足不能标记验收通过")
    markers = ("requires_postgres", "requires_s3", "requires_chroma_server",
               "MINDEX_TEST_POSTGRES_DSN", "MINDEX_TEST_S3_ENDPOINT", "MINDEX_TEST_CHROMA_SERVER")
    marked_files = {path.name for path in (ROOT / "tests").glob("test_*.py")
                    if any(marker in path.read_text(encoding="utf-8") for marker in markers)}
    missing = marked_files - set(TESTS) - set(CPU_TESTS)
    if missing:
        parser.error(f"验收入口漏掉外部后端测试文件：{sorted(missing)}")
    import psycopg

    run_id = uuid4().hex[:12]
    output = (args.output or ROOT / "artifacts/scaling" /
              f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{run_id}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    containers = []
    minio = None
    report = {"run_id": run_id, "started_at": datetime.now(UTC).isoformat(),
              "code_version": command(["git", "rev-parse", "HEAD"]), "source_sha256": source_identity(),
              "python": sys.version, "platform": platform.platform(), "cpu_count": os.cpu_count(),
              "images": {"postgres": PG_IMAGE, "chroma": CHROMA_IMAGE},
              "postgres_storage": "tmpfs 768 MiB（仅组件验收，不用于容量/灾备结论）",
              "minio_sha256": hashlib.sha256(Path(args.minio_binary).read_bytes()).hexdigest(),
              "minio_version": command([args.minio_binary, "--version"]),
              "scope": "单机隔离组件与集成验收；双机、容量、模型和安装包待验收", "status": "running"}
    result_code = 1
    try:
        with ExitStack() as resources:
            root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="mindex-scaling-")))
            password = uuid4().hex
            for label, image, container_port, extra in (
                ("pg", PG_IMAGE, 5432, ["-e", "POSTGRES_USER=mindex", "-e", f"POSTGRES_PASSWORD={password}",
                                      "-e", "POSTGRES_DB=mindex_test"]),
                ("chroma", CHROMA_IMAGE, 8000, []),
            ):
                name = f"mindex-acceptance-{run_id}-{label}"
                containers.append(name)
                command(["docker", "run", "--rm", "-d", "--name", name, "-p",
                         f"127.0.0.1::{container_port}",
                         *(["--tmpfs", "/var/lib/postgresql/data:rw,size=768m"] if label == "pg" else []),
                         *extra, image])
                bound = command(["docker", "port", name, str(container_port)]).splitlines()[0]
                report.setdefault("ports", {})[label] = int(bound.rsplit(":", 1)[1])
            endpoint = f"http://127.0.0.1:{port()}"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("MINDEX_", "MINIO_"))}
            with (output / "minio.log").open("w") as log:
                minio = subprocess.Popen([args.minio_binary, "server", str(root / "objects"),
                                          "--address", endpoint.removeprefix("http://"),
                                          "--console-address", f"127.0.0.1:{port()}", "--quiet"],
                                         env={**env, "MINIO_ROOT_USER": "mindex", "MINIO_ROOT_PASSWORD": password},
                                         stdout=log, stderr=subprocess.STDOUT)
                resources.callback(stop_process, minio)
                dsn = f"postgresql://mindex:{password}@127.0.0.1:{report['ports']['pg']}/mindex_test"
                def pg_ready():
                    with psycopg.connect(dsn, connect_timeout=1):
                        pass
                wait_for(pg_ready, "PostgreSQL")
                wait_for(lambda: http_ready(f"{endpoint}/minio/health/live"), "MinIO")
                chroma = f"http://127.0.0.1:{report['ports']['chroma']}"
                wait_for(lambda: http_ready(f"{chroma}/api/v2/heartbeat"), "Chroma")
                env.update({"MINDEX_TEST_POSTGRES_DSN": dsn, "MINDEX_TEST_S3_ENDPOINT": endpoint,
                            "MINDEX_TEST_EVIDENCE_DIR": str(output),
                            "MINDEX_TEST_S3_KEY": "mindex", "MINDEX_TEST_S3_SECRET": password,
                            "MINDEX_TEST_S3_SECRET_KEY": password, "MINDEX_TEST_CHROMA_SERVER": chroma})
                test_command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "-o", "faulthandler_timeout=60",
                                "-W", "error::pytest.PytestUnhandledThreadExceptionWarning",
                                "-W", "error::pytest.PytestUnraisableExceptionWarning",
                                f"--junitxml={output / 'junit.xml'}", *(f"tests/{name}" for name in test_files)]
                report["tests"] = test_files
                report["suite"] = suite
                print(f"独立验收环境已就绪；执行 {len(test_files)} 个测试文件；证据：{output}", flush=True)
                with (output / "pytest.log").open("w") as test_log:
                    result = subprocess.run(test_command, cwd=ROOT, env=env, stdout=test_log,
                                            stderr=subprocess.STDOUT, check=False)
                result_code = result.returncode
                if result_code < 0 or result_code == 2:
                    report["status"] = "interrupted"
                    report["returncode"] = result_code
                    return result_code if result_code > 0 else 1
                suites = ET.parse(output / "junit.xml").getroot().iter("testsuite")
                report["counts"] = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
                for suite in suites:
                    for key in report["counts"]:
                        report["counts"][key] += int(suite.get(key, "0"))
                report["source_unchanged"] = source_identity() == report["source_sha256"]
                if not report["source_unchanged"]:
                    result_code = 1
                if report["counts"]["skipped"]:
                    result_code = 1
                report["status"] = "passed" if result_code == 0 else "failed"
                print(json.dumps({"status": report["status"], **report["counts"]}, ensure_ascii=False), flush=True)
    except Exception as exc:
        report.update(status="environment_or_execution_failed", error=type(exc).__name__)
        raise
    finally:
        if minio is not None:
            minio.terminate()
            try:
                minio.wait(timeout=5)
            except subprocess.TimeoutExpired:
                minio.kill()
                minio.wait()
        for name in reversed(containers):
            subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, check=False)
        report["finished_at"] = datetime.now(UTC).isoformat()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
