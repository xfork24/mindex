"""真实两 API/两 worker、不同本地目录的上传→持久执行→共享媒体链路。

这是一台主机上的四进程工程验收，不代表双机故障、模型质量或吞吐门槛已通过。
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from uuid import uuid4

import httpx
import pytest
import yaml

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_s3, pytest.mark.requires_chroma_server]


def free_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_two_apis_two_workers_share_real_source_tasks(tmp_path, video_file):
    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN")
    endpoint = os.environ.get("MINDEX_TEST_S3_ENDPOINT")
    chroma = os.environ.get("MINDEX_TEST_CHROMA_SERVER")
    if not all((dsn, endpoint, chroma)):
        pytest.skip("需要隔离的 PostgreSQL、MinIO 和 Chroma 服务")
    import boto3
    import psycopg

    from shared.db.migrate_pg import migrate_pg

    access = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
    secret = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
    )
    bucket = f"mindex-process-{uuid4().hex[:12]}"
    client.create_bucket(Bucket=bucket)
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    with psycopg.connect(dsn) as db:
        migrate_pg(db)
    env = {key: value for key, value in os.environ.items() if not key.startswith("MINDEX_")}
    env.update(MINDEX_DATABASE_DSN=dsn, MINDEX_S3_ACCESS_KEY=access, MINDEX_S3_SECRET_KEY=secret)
    env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    urls, configs, logs = [], [], []
    try:
        with ExitStack() as resources:

            def launch(config, role, *args):
                log = resources.enter_context((tmp_path / f"{config.stem}-{role}.log").open("w+"))
                logs.append(log)
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "from mindex.cli import app; app()",
                        "--config",
                        str(config),
                        role,
                        *args,
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                resources.callback(stop, process)
                return process

            gateway_port = free_port()
            for index in range(5):
                node = tmp_path / f"node-{index}"
                config = tmp_path / f"node-{index}.yaml"
                config.write_text(
                    yaml.safe_dump(
                        {
                            "data_dir": str(node),
                            "runtime_overrides": False,
                            "gpu": {"cpu_queues": ["source", "index"]},
                            "agent": {"gateway_url": f"http://127.0.0.1:{gateway_port}"},
                            "server": {"form": "cluster", "role": "agent-gateway" if index == 4 else "api"},
                            "database": {"backend": "postgres"},
                            "queue": {"backend": "postgres"},
                            "locks": {"backend": "postgres"},
                            "vector": {"backend": "chroma_server", "chroma_server": {"url": chroma}},
                            "storage": {
                                "backend": "s3",
                                "s3": {
                                    "endpoint_url": endpoint,
                                    "bucket": bucket,
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
                configs.append(config)
                if index == 4:
                    launch(config, "serve", "--port", str(gateway_port))
                if index < 2:
                    port = free_port()
                    launch(config, "serve", "--port", str(port))
                    urls.append(f"http://127.0.0.1:{port}")
            with httpx.Client(timeout=30, trust_env=False) as http:
                deadline = time.monotonic() + 30
                for url in urls:
                    while True:
                        try:
                            ready = http.get(url + "/api/ready")
                            if ready.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        assert time.monotonic() < deadline, "API 未在 30 秒内就绪"
                        time.sleep(0.1)
                tasks, sources, payloads = [], [], []
                for index in range(2):
                    payload = video_file.read_bytes() + bytes([index])
                    payloads.append(payload)
                    uploaded = http.post(
                        urls[index] + "/api/admin/sources/upload",
                        files={"file": (f"input-{index}.mp4", payload, "video/mp4")},
                    )
                    assert uploaded.status_code == 201, uploaded.text
                    source_id = uploaded.json()["source_id"]
                    sources.append(source_id)
                    task = http.post(
                        urls[index] + f"/api/admin/sources/{source_id}/shots", json={"publish": False}
                    )
                    assert task.status_code == 202, task.text
                    tasks.append(task.json()["id"])
                workers = [
                    launch(config, "worker", "--queues", "source", "--once") for config in configs[2:4]
                ]
                for worker in workers:
                    worker.wait(timeout=60)
                    assert worker.returncode == 0
                for index, task_id in enumerate(tasks):
                    # 从另一 API 读取状态和原片，该 API 没有处理 worker 的本地目录。
                    url = urls[1 - index]
                    task = http.get(url + f"/api/admin/source-tasks/{task_id}")
                    assert task.status_code == 200
                    assert task.json()["status"] == "complete", task.text
                    media = http.get(
                        url + f"/api/admin/sources/{sources[index]}/media", follow_redirects=True
                    )
                    assert media.status_code == 200, media.text[:500]
                    assert media.headers["content-type"] == "video/mp4" and len(media.content) > 0
                    # 原片预览按现有契约由镜头合成；物理镜头与权威对象逐字节核对。
                    with psycopg.connect(dsn) as db:
                        shot, digest = db.execute(
                            "SELECT s.id,f.sha256 FROM segments s JOIN shared_files f ON f.path=s.path"
                            " WHERE s.source_id=%s AND s.kind='shot' ORDER BY s.start LIMIT 1",
                            (sources[index],),
                        ).fetchone()
                    clip = http.get(url + f"/api/admin/segments/{shot}/media")
                    assert clip.status_code == 200, clip.text[:500]
                    assert hashlib.sha256(clip.content).hexdigest() == digest
            with psycopg.connect(dsn) as db:
                rows = db.execute(
                    "SELECT status,owner,attempt_id,fencing_token FROM task_queue ORDER BY id"
                ).fetchall()
                assert len(rows) == 2 and all(row[0] == "succeeded" for row in rows)
                assert len({row[1] for row in rows}) == 2
                assert all(row[2] and row[3] > 0 for row in rows)
                assert db.execute("SELECT count(*) FROM shared_files").fetchone()[0] > 0
            for index in range(5):
                assert not (tmp_path / f"node-{index}/metadata.sqlite3").exists()
    except BaseException:
        for log in logs:
            if not log.closed:
                log.flush()
                log.seek(0)
                print(log.read()[-4000:])
        raise
    finally:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            if page.get("Contents"):
                client.delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": item["Key"]} for item in page["Contents"]]}
                )
        client.delete_bucket(Bucket=bucket)
