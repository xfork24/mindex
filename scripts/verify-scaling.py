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
    "test_scaling_boundaries.py",
    "test_scaling_foundation.py",
    "test_scaling_correctness.py",
    "test_scaling_remaining_verification.py",
    "test_pg_backend.py",
    "test_pg_queue.py",
    "test_pg_locks.py",
    "test_pg_export.py",
    "test_s3_store.py",
    "test_s3_purge.py",
    "test_chroma_server.py",
    "test_staging.py",
    "test_migration_crash_recovery.py",
    "test_migration_stages.py",
    "test_cluster_process_flow.py",
    "test_doctor.py",
    "test_readiness_budget.py",
    "test_deployment_config.py",
    "test_pg_chroma_e2e.py",
    "test_migration_cli.py",
    "test_worker.py",
    "test_persons_pg.py",
    "test_media_delivery.py",
    "test_capacity_tool.py",
    "test_query_budget.py",
    "test_s3_multipart.py",
    "test_backend_identity.py",
    "test_backend_lifecycle.py",
    "test_scaling_completion.py",
    "test_object_coordination.py",
    "test_maintenance_and_gateway.py",
    "test_sql_characteristics.py",
    "test_scaling_execution.py",
    "test_scaling_delivery_core.py",
    "test_scaling_fault_matrix.py",
]


CPU_TESTS = ["test_scaling_cpu_acceptance.py"]

# faults 专项：本脚本自建两 API + 单网关进程，逐个停止/恢复 PG、MinIO、Chroma，
# 核对 live/ready 的区分与恢复；不与其他验收共用服务。
FAULT_TESTS = ["test_readiness_budget.py", "test_deployment_config.py"]


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
            time.sleep(0.25)


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


def dependency_fault_drills(
    *,
    root,
    output,
    env,
    dsn,
    endpoint,
    chroma,
    password,
    run_id,
    minio,
    minio_argv,
    minio_env,
    containers,
    report,
):
    """两 API + 单网关真实进程：逐个停止/恢复 PG、MinIO、Chroma 与网关，核对 live/ready。

    liveness 只反映进程存活；readiness 在有界期限内区分具体依赖故障，恢复后自动转绿。
    演练细节随步写入 report["drills"]；任一步失败即中止并保留已记录证据。
    """
    import httpx
    import psycopg
    import yaml

    from shared.db.migrate_pg import migrate_pg

    token = uuid4().hex
    node_env = {
        **env,
        "MINDEX_DATABASE_DSN": dsn,
        "MINDEX_S3_ACCESS_KEY": "mindex",
        "MINDEX_S3_SECRET_KEY": password,
        "MINDEX_WEB_TOKEN": token,
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
    }
    gateway_port = port()
    nodes: list[tuple[str, subprocess.Popen, str]] = []  # 名称、进程、基址

    def node_config(name, role, node_port):
        config = root / f"fault-{name}.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "data_dir": str(root / f"fault-{name}"),
                    "runtime_overrides": False,
                    "gpu": {"cpu_queues": ["source", "index"]},
                    "agent": {"gateway_url": f"http://127.0.0.1:{gateway_port}"},
                    "server": {"form": "cluster", "role": role},
                    "database": {"backend": "postgres"},
                    "queue": {"backend": "postgres"},
                    "locks": {"backend": "postgres"},
                    "vector": {"backend": "chroma_server", "chroma_server": {"url": chroma}},
                    "storage": {
                        "backend": "s3",
                        "s3": {
                            "endpoint_url": endpoint,
                            "bucket": f"mindex-fault-{run_id}",
                            "prefix": "library/",
                            "min_free_gb": 0,
                            "staging_quota_gb": 1,
                        },
                    },
                    "topics": {"scheduler": {"enabled": False}},
                    "local_services": {"enabled": False},
                    "embedding": {"base_url": "http://127.0.0.1:9", "dimensions": 8},
                }
            ),
            encoding="utf-8",
        )
        return config

    def launch(name, role, node_port):
        log = (output / f"node-{name}.log").open("w")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from mindex.cli import app; app()",
                "--config",
                str(node_config(name, role, node_port)),
                "serve",
                "--port",
                str(node_port),
            ],
            env=node_env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        nodes.append((name, process, f"http://127.0.0.1:{node_port}"))
        return process

    def launch_bucket():
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id="mindex",
            aws_secret_access_key=password,
            region_name="us-east-1",
        )
        client.create_bucket(Bucket=f"mindex-fault-{run_id}")

    def probe(base):
        headers = {"Authorization": f"Bearer {token}"}
        live = httpx.get(base + "/api/live", headers=headers, timeout=30, trust_env=False)
        ready = httpx.get(base + "/api/ready", headers=headers, timeout=60, trust_env=False)
        checks = {item["check"]: item["ok"] for item in ready.json().get("checks", [])}
        return {"live": live.status_code, "ready": ready.status_code, "checks": checks}

    def record(step, action, expect):
        started = time.monotonic()
        action()
        apis = [probe(base) for _, _, base in nodes if base and base != f"http://127.0.0.1:{gateway_port}"]
        if len(apis) != 2:
            raise AssertionError(f"{step}: 必须始终探测两个业务 API，实际 {len(apis)}")
        problems = []
        for api in apis:
            if api["live"] != 200:
                problems.append(f"{step}: liveness 应保持进程存活语义，实际 {api}")
            if api["ready"] != (200 if not expect else 503):
                problems.append(f"{step}: readiness 状态 {api}")
            problems += [
                f"{step}: 依赖 {name} 应为 {ok}，实际 {api}"
                for name, ok in expect.items()
                if api["checks"].get(name) is not ok
            ]
        if problems:
            # 失败时保留容器与服务状态，避免只看到断言而无法定位。
            api_names = [
                name for name, _, base in nodes if base and base != f"http://127.0.0.1:{gateway_port}"
            ]
            report.setdefault("drills", {}).setdefault("failures", []).append(
                {
                    "step": step,
                    "problems": problems,
                    "containers": {
                        name: command(["docker", "inspect", "-f", "{{.State.Status}}", name])
                        for name in (pg_container, chroma_container)
                    },
                    "api_logs": {
                        name: (output / f"node-{name}.log").read_text(encoding="utf-8", errors="replace")[
                            -2000:
                        ]
                        for name in api_names
                        if (output / f"node-{name}.log").is_file()
                    },
                }
            )
            raise AssertionError("; ".join(problems))
        report.setdefault("drills", {}).setdefault("steps", []).append(
            {"step": step, "apis": apis, "seconds": round(time.monotonic() - started, 3)}
        )

    def wait_recovered(label):
        started = time.monotonic()
        consecutive = {"count": 0}

        def ready_all():
            # 连续三次全绿才算恢复：只成功一次可能仍复用到残留的失效连接。
            healthy = all(
                probe(base)["ready"] == 200
                for _, _, base in nodes
                if base and base != f"http://127.0.0.1:{gateway_port}"
            )
            consecutive["count"] = consecutive["count"] + 1 if healthy else 0
            if consecutive["count"] < 3:
                # wait_for 以异常表示未就绪；返回 False 会被当作成功。
                raise RuntimeError(f"尚未恢复（连续 {consecutive['count']}/3）")
            return True

        wait_for(ready_all, label)
        report["drills"]["steps"].append(
            {"step": f"recover.{label}", "seconds": round(time.monotonic() - started, 3)}
        )

    report["drills"] = {"ok": True, "steps": []}
    pg_container = f"mindex-acceptance-{run_id}-pg"
    chroma_container = f"mindex-acceptance-{run_id}-chroma"
    with psycopg.connect(dsn) as db:
        migrate_pg(db)
    launch_bucket()
    try:
        launch("api-1", "api", port())
        wait_for(lambda: probe(nodes[0][2])["ready"] == 200, "API-1")
        launch("api-2", "api", port())
        wait_for(lambda: probe(nodes[1][2])["ready"] == 200, "API-2")
        launch("gateway", "agent-gateway", gateway_port)
        wait_for(
            lambda: (
                httpx.get(
                    f"http://127.0.0.1:{gateway_port}/api/live",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=5,
                    trust_env=False,
                ).status_code
                == 200
            ),
            "网关",
        )
        record("baseline", lambda: None, {})  # 基线：全部依赖健康。

        # PostgreSQL 停止 → 数据库检查失败、其余依赖仍判定可用；恢复后自动转绿。
        record(
            "postgres.down",
            lambda: command(["docker", "stop", "-t", "2", pg_container]),
            {
                "database.query": False,
                "database.control": False,
                "storage.bucket": True,
                "vectors.server": True,
            },
        )
        command(["docker", "start", pg_container])
        wait_recovered("postgres")

        # MinIO 停止 → 仅存储检查失败。
        record(
            "minio.down",
            lambda: stop_process(minio),
            {
                "database.query": True,
                "database.control": True,
                "storage.bucket": False,
                "vectors.server": True,
            },
        )
        with (output / "minio-restart.log").open("w") as log:
            minio_restart = subprocess.Popen(minio_argv, env=minio_env, stdout=log, stderr=subprocess.STDOUT)
            nodes.append(("minio-restart", minio_restart, ""))  # 随节点一起清理
        wait_for(lambda: http_ready(f"{endpoint}/minio/health/live"), "MinIO 重启")
        wait_recovered("minio")

        # Chroma 停止 → 仅向量检查失败。
        record(
            "chroma.down",
            lambda: command(["docker", "stop", "-t", "2", chroma_container]),
            {
                "database.query": True,
                "database.control": True,
                "storage.bucket": True,
                "vectors.server": False,
            },
        )
        command(["docker", "start", chroma_container])
        wait_for(lambda: http_ready(f"{chroma}/api/v2/heartbeat"), "Chroma 重启")
        wait_recovered("chroma")

        # 网关 SIGKILL → 助手明确不可用，业务 API 与 liveness 不受影响；重启后恢复。
        started = time.monotonic()
        gateway_name, gateway_process, gateway_base = next(
            item for item in nodes if item[2] == f"http://127.0.0.1:{gateway_port}"
        )
        gateway_process.kill()
        gateway_process.wait(timeout=10)
        api_base = next(item[2] for item in nodes if item[0] == "api-1")
        headers = {"Authorization": f"Bearer {token}"}
        status = httpx.get(api_base + "/api/agent/status", headers=headers, timeout=30, trust_env=False)
        assert status.status_code == 503, f"网关停机时助手应明确不可用：{status.status_code}"
        assert (
            httpx.get(api_base + "/api/live", headers=headers, timeout=10, trust_env=False).status_code == 200
        )
        launch(gateway_name, "agent-gateway", gateway_port)
        wait_for(
            lambda: (
                httpx.get(
                    gateway_base + "/api/agent/status", headers=headers, timeout=5, trust_env=False
                ).status_code
                == 200
            ),
            "网关重启",
        )
        report["drills"]["steps"].append(
            {"step": "gateway.kill-restart", "seconds": round(time.monotonic() - started, 3)}
        )
    except BaseException as exc:
        report["drills"]["ok"] = False
        report["drills"]["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for _, process, _ in nodes:
            stop_process(process)
    return report["drills"]


def main(*, suite="integration"):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        choices=["integration", "cpu", "faults"],
        default=None,
        help="验收专项；缺省 integration。faults 为依赖断连演练",
    )
    parser.add_argument(
        "--minio-binary", default=shutil.which("minio"), help="MinIO 可执行文件（记录其摘要）"
    )
    parser.add_argument("--output", type=Path, help="空证据目录；默认 artifacts/scaling/<时间与随机 ID>")
    args = parser.parse_args()
    if args.suite:
        suite = args.suite
    if suite not in {"integration", "cpu", "faults"}:
        raise ValueError("未知验收专项")
    test_files = {"integration": TESTS, "cpu": CPU_TESTS, "faults": FAULT_TESTS}[suite]
    if not args.minio_binary or not shutil.which("docker"):
        parser.error("需要 Docker 与 MinIO 可执行文件；环境不足不能标记验收通过")
    markers = (
        "requires_postgres",
        "requires_s3",
        "requires_chroma_server",
        "MINDEX_TEST_POSTGRES_DSN",
        "MINDEX_TEST_S3_ENDPOINT",
        "MINDEX_TEST_CHROMA_SERVER",
    )
    marked_files = {
        path.name
        for path in (ROOT / "tests").glob("test_*.py")
        if any(marker in path.read_text(encoding="utf-8") for marker in markers)
    }
    missing = marked_files - set(TESTS) - set(CPU_TESTS) - set(FAULT_TESTS)
    if missing:
        parser.error(f"验收入口漏掉外部后端测试文件：{sorted(missing)}")
    import psycopg

    run_id = uuid4().hex[:12]
    output = (
        args.output or ROOT / "artifacts/scaling" / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{run_id}"
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    containers = []
    volumes = []
    minio = None
    report = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "code_version": command(["git", "rev-parse", "HEAD"]),
        "source_sha256": source_identity(),
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "images": {"postgres": PG_IMAGE, "chroma": CHROMA_IMAGE},
        "postgres_storage": "Docker 命名卷（可 stop/start 重启演练；仅组件验收，不用于容量/灾备结论）",
        "minio_sha256": hashlib.sha256(Path(args.minio_binary).read_bytes()).hexdigest(),
        "minio_version": command([args.minio_binary, "--version"]),
        "scope": "单机隔离组件与集成验收；双机、容量、模型和安装包待验收",
        "status": "running",
    }
    result_code = 1
    try:
        with ExitStack() as resources:
            root = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="mindex-scaling-")))
            password = uuid4().hex
            for label, image, container_port, extra in (
                (
                    "pg",
                    PG_IMAGE,
                    5432,
                    [
                        "-e",
                        "POSTGRES_USER=mindex",
                        "-e",
                        f"POSTGRES_PASSWORD={password}",
                        "-e",
                        "POSTGRES_DB=mindex_test",
                    ],
                ),
                ("chroma", CHROMA_IMAGE, 8000, []),
            ):
                name = f"mindex-acceptance-{run_id}-{label}"
                containers.append(name)
                # 固定空闲宿主端口：docker stop/start 会重新分配随机端口，faults 专项必须稳定重连。
                host_port = port()
                # PG 用命名卷：--tmpfs 在容器 stop/start 后重新 initdb，迁移过的库会变成空库。
                # 不用 --rm：faults 专项需要 docker stop/start 往复；统一在 finally 清理。
                volume = f"mindex-acceptance-{run_id}-{label}-data"
                if label == "pg":
                    volumes.append(volume)
                command(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        name,
                        "-p",
                        f"127.0.0.1:{host_port}:{container_port}",
                        *(["-v", f"{volume}:/var/lib/postgresql/data"] if label == "pg" else []),
                        *extra,
                        image,
                    ]
                )
                bound = command(["docker", "port", name, str(container_port)]).splitlines()[0]
                report.setdefault("ports", {})[label] = int(bound.rsplit(":", 1)[1])
                assert report["ports"][label] == host_port, "容器端口与预留端口不一致"
            endpoint = f"http://127.0.0.1:{port()}"
            env = {
                key: value for key, value in os.environ.items() if not key.startswith(("MINDEX_", "MINIO_"))
            }
            minio_argv = [
                args.minio_binary,
                "server",
                str(root / "objects"),
                "--address",
                endpoint.removeprefix("http://"),
                "--console-address",
                f"127.0.0.1:{port()}",
                "--quiet",
            ]
            minio_env = {**env, "MINIO_ROOT_USER": "mindex", "MINIO_ROOT_PASSWORD": password}
            with (output / "minio.log").open("w") as log:
                minio = subprocess.Popen(minio_argv, env=minio_env, stdout=log, stderr=subprocess.STDOUT)
                resources.callback(stop_process, minio)
                dsn = f"postgresql://mindex:{password}@127.0.0.1:{report['ports']['pg']}/mindex_test"

                def pg_ready():
                    with psycopg.connect(dsn, connect_timeout=1):
                        pass

                wait_for(pg_ready, "PostgreSQL")
                wait_for(lambda: http_ready(f"{endpoint}/minio/health/live"), "MinIO")
                chroma = f"http://127.0.0.1:{report['ports']['chroma']}"
                wait_for(lambda: http_ready(f"{chroma}/api/v2/heartbeat"), "Chroma")
                env.update(
                    {
                        "MINDEX_TEST_POSTGRES_DSN": dsn,
                        "MINDEX_TEST_S3_ENDPOINT": endpoint,
                        "MINDEX_TEST_EVIDENCE_DIR": str(output),
                        "MINDEX_TEST_S3_KEY": "mindex",
                        "MINDEX_TEST_S3_SECRET": password,
                        "MINDEX_TEST_S3_SECRET_KEY": password,
                        "MINDEX_TEST_CHROMA_SERVER": chroma,
                    }
                )
                report["tests"] = test_files
                report["suite"] = suite
                if suite == "faults":
                    # 先演练依赖断连与恢复，再执行就绪/部署组件测试。
                    report["drills"] = dependency_fault_drills(
                        root=root,
                        output=output,
                        env=env,
                        dsn=dsn,
                        endpoint=endpoint,
                        chroma=chroma,
                        password=password,
                        run_id=run_id,
                        minio=minio,
                        minio_argv=minio_argv,
                        minio_env=minio_env,
                        containers=containers,
                        report=report,
                    )
                # 600 秒：长专项（CPU 吞吐）里频繁 dump 会与 faulthandler 取消操作争锁而挂起，
                # 阈值只用于发现真正卡死，不应被正常长测试反复触发。
                test_command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--tb=short",
                    "-o",
                    "faulthandler_timeout=600",
                    "-W",
                    "error::pytest.PytestUnhandledThreadExceptionWarning",
                    "-W",
                    "error::pytest.PytestUnraisableExceptionWarning",
                    f"--junitxml={output / 'junit.xml'}",
                    *(f"tests/{name}" for name in test_files),
                ]
                print(f"独立验收环境已就绪；执行 {len(test_files)} 个测试文件；证据：{output}", flush=True)
                with (output / "pytest.log").open("w") as test_log:
                    result = subprocess.run(
                        test_command,
                        cwd=ROOT,
                        env=env,
                        stdout=test_log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
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
                if report.get("drills", {}).get("ok") is False:
                    result_code = 1
                report["status"] = "passed" if result_code == 0 else "failed"
                print(
                    json.dumps(
                        {
                            "status": report["status"],
                            "drills": report.get("drills", {}),
                            **report.get("counts", {}),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
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
        for volume in volumes:
            subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)
        report["finished_at"] = datetime.now(UTC).isoformat()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result_code


if __name__ == "__main__":
    raise SystemExit(main(suite=os.environ.get("MINDEX_VERIFY_SUITE", "integration")))
